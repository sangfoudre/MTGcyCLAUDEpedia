#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mtgc-web — MTGcyCLAUDEpedia
Génère un site statique à partir d'un data-dir :
  - une page d'accueil listant les extensions présentes (nom, code, date,
    nombre de cartes, icône keyrune) ;
  - une page par extension, icône en tête + mêmes infos + grille de cartes ;
  - une page par carte (oracle_id), regroupant TOUTES ses impressions dans
    tous les sets présents, avec le texte oracle et le dernier ruling ;
  - un viewer plein écran au clic (zoom, rotation, précédent/suivant, retour).

Lit uniquement ce qui est dans le data-dir :
  - sets/<CODE>/<code>-<num>[-a|-b].<ext>   les images (schéma 1.3.0)
  - metadata/*cards*.jsonl.gz               le bulk cartes (noms, dates…)
  - metadata/*rulings*.jsonl.gz             le bulk rulings (optionnel)

Les icônes de set et les symboles de mana viennent des fontes d'Andrew Gioia
(keyrune, mana-font) servies par CDN jsdelivr : plus léger que d'embarquer un
millier de SVG. Le reste du site est autonome.

Sans dépendance Python, sans accès réseau à la génération.

Exemples
--------
    ./mtgc-web.py --data-dir ~/mtg
    ./mtgc-web.py --data-dir ~/mtg --out ~/mtg/site --open
    ./mtgc-web.py --data-dir ~/mtg --no-card-pages   # plus rapide
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import shutil
import sys
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

VERSION = "0.2.0"

# --------------------------------------------------------------- fontes
# Fontes d'Andrew Gioia embarquées LOCALEMENT dans le site (assets/), pour un
# fonctionnement 100 % hors-ligne. Téléchargées une fois puis mises en cache
# dans le data-dir ; le CSS est réécrit pour pointer sur le .woff2 local.
FONT_SOURCES = {
    "keyrune.woff2":
        "https://cdn.jsdelivr.net/npm/keyrune@latest/fonts/keyrune.woff2",
    "keyrune.css":
        "https://cdn.jsdelivr.net/npm/keyrune@latest/css/keyrune.min.css",
    "mana.woff2":
        "https://cdn.jsdelivr.net/npm/mana-font@latest/fonts/mana.woff2",
    "mana.css":
        "https://cdn.jsdelivr.net/npm/mana-font@latest/css/mana.min.css",
}

# ------------------------------------------------------------------- thème
THEME = {
    "bg": "#14100a", "panel": "#1c160d", "panel_alt": "#181209",
    "border": "#2a2013", "border_hi": "#3a2e1a", "gold": "#c9a227",
    "gold_dim": "#8a7a54", "ink": "#e8dcc0", "ink_dim": "#a89968",
    "ink_faint": "#6f6142",
}
RARITY_COLOR = {
    "common": "#c8c8c8", "uncommon": "#a9b8c6", "rare": "#d5aa5a",
    "mythic": "#e07b28", "special": "#b57edc", "bonus": "#b57edc",
}
DFC_LAYOUTS = {"transform", "modal_dfc", "reversible_card",
               "double_faced_token", "art_series"}

# ------------------------------------------------------------- utilitaires

def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def natural_key(cn: str):
    m = re.match(r"^(\d+)(.*)$", cn or "")
    return (0, int(m.group(1)), m.group(2)) if m else (1, 0, cn or "")


_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_cn(part: str) -> str:
    """Copie fidèle de sanitize() du téléchargeur, pour retrouver ses fichiers."""
    if not part:
        return "_"
    part = part.replace("★", "-star").replace("†", "-dagger")
    part = unicodedata.normalize("NFKD", part)
    part = part.encode("ascii", "ignore").decode("ascii")
    return _SAFE_RE.sub("-", part).strip("-._")


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


# --------------------------------------------------------------- bulk I/O

def find_bulk(meta_dir: Path, kind: str) -> Path | None:
    """Trouve un bulk par mot-clé ('cards' ou 'rulings')."""
    for pat in (f"*{kind}*.jsonl.gz", f"*{kind}*.json"):
        hits = sorted(meta_dir.glob(pat))
        if hits:
            return hits[0]
    if kind == "cards":
        hits = [p for p in sorted(meta_dir.glob("*.jsonl.gz"))
                if "ruling" not in p.name.lower()]
        return hits[0] if hits else None
    return None


def iter_bulk(path: Path):
    op = gzip.open if path.suffix == ".gz" else open
    with op(path, "rt", encoding="utf-8") as f:
        head = f.read(1)
        f.seek(0)
        if head == "[":
            for c in json.load(f):
                yield c
            return
        for line in f:
            line = line.strip().rstrip(",")
            if line and line not in ("[", "]"):
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


# ------------------------------------------------------------ nommage image

def image_names(code: str, cn: str, layout: str) -> dict[str, list[str]]:
    """Noms candidats pour une carte, schéma <code>-<num>[-a|-b].<ext>."""
    bases = list(dict.fromkeys([cn, sanitize_cn(cn)]))
    front, back = [], []
    for b in bases:
        stem = f"{code}-{b}"
        if layout in DFC_LAYOUTS:
            front += [f"{stem}-a.jpg", f"{stem}-a.png"]
            back += [f"{stem}-b.jpg", f"{stem}-b.png"]
        front += [f"{stem}.jpg", f"{stem}.png"]
    return {"front": front, "back": back}


# ------------------------------------------------------------- collecte

def scan_disk(images_dir: Path) -> dict[str, set[str]]:
    present: dict[str, set[str]] = {}
    if not images_dir.is_dir():
        return present
    for set_dir in sorted(images_dir.iterdir()):
        if not set_dir.is_dir():
            continue
        files = {p.name for p in set_dir.iterdir()
                 if p.suffix in (".jpg", ".png") and p.is_file()}
        if files:
            present[set_dir.name.lower()] = files
    return present


def load_rulings(meta_dir: Path) -> dict[str, list[dict]]:
    """oracle_id -> rulings triés du plus récent au plus ancien."""
    path = find_bulk(meta_dir, "rulings")
    if not path:
        return {}
    out: dict[str, list[dict]] = defaultdict(list)
    for r in iter_bulk(path):
        oid = r.get("oracle_id")
        if oid and r.get("comment"):
            out[oid].append({"date": r.get("published_at") or "",
                             "text": r["comment"]})
    for oid in out:
        out[oid].sort(key=lambda x: x["date"], reverse=True)
    return out


def build_model(data_dir: Path, want_rulings: bool = True):
    """Croise disque et bulk. Retourne (sets, cards_by_set, by_oracle, rulings)."""
    images_dir = data_dir / "sets"
    meta_dir = data_dir / "metadata"
    present = scan_disk(images_dir)
    if not present:
        raise SystemExit(f"aucune image trouvée dans {images_dir}")

    cards_bulk = find_bulk(meta_dir, "cards")
    rulings = load_rulings(meta_dir) if want_rulings else {}

    set_meta: dict[str, dict] = {}
    cards_by_set: dict[str, list] = {code: [] for code in present}
    by_oracle: dict[str, list] = defaultdict(list)
    claimed: dict[str, set[str]] = {code: set() for code in present}

    if cards_bulk:
        for c in iter_bulk(cards_bulk):
            code = (c.get("set") or "").lower()
            if code not in present:
                continue
            set_meta.setdefault(code, {
                "name": c.get("set_name") or code.upper(),
                "released_at": c.get("released_at") or "",
                "set_type": c.get("set_type") or "",
            })
            cn = c.get("collector_number") or ""
            names = image_names(code, cn, c.get("layout") or "")
            img = next((v for v in names["front"] if v in present[code]), None)
            back = next((v for v in names["back"] if v in present[code]), None)
            if not img:
                continue
            claimed[code].add(img)
            if back:
                claimed[code].add(back)
            oid = c.get("oracle_id") or f"noid-{code}-{cn}"
            entry = {
                "oid": oid, "cn": cn, "name": c.get("name") or "?",
                "rarity": c.get("rarity") or "", "artist": c.get("artist") or "",
                "type": c.get("type_line") or "", "mana": c.get("mana_cost") or "",
                "cmc": c.get("cmc"), "colors": "".join(c.get("colors") or []),
                "oracle_text": c.get("oracle_text") or "",
                "set_code": code, "img": img, "back": back,
                "released_at": c.get("released_at") or "",
            }
            cards_by_set[code].append(entry)
            by_oracle[oid].append(entry)

    # Filet anti-perte : toute image sur disque qu'aucune carte n'a réclamée.
    for code, files in present.items():
        set_meta.setdefault(code, {"name": code.upper(), "released_at": "",
                                   "set_type": ""})
        for fn in sorted(files - claimed[code],
                         key=lambda f: natural_key(Path(f).stem)):
            stem = Path(fn).stem
            cn = (stem[len(code) + 1:] if stem.lower().startswith(code + "-")
                  else stem)
            oid = f"noid-{code}-{cn}"
            entry = {"oid": oid, "cn": cn, "name": cn, "rarity": "",
                     "artist": "", "type": "", "mana": "", "cmc": None,
                     "colors": "", "oracle_text": "", "set_code": code,
                     "img": fn, "back": None, "released_at": ""}
            cards_by_set[code].append(entry)
            by_oracle[oid].append(entry)

    for code in cards_by_set:
        cards_by_set[code].sort(key=lambda x: natural_key(x["cn"]))
    for oid in by_oracle:
        by_oracle[oid].sort(
            key=lambda x: (x["released_at"] or "9999", x["set_code"]))

    sets = []
    for code, m in set_meta.items():
        sets.append({"code": code, "name": m["name"],
                     "released_at": m["released_at"], "set_type": m["set_type"],
                     "count": len(cards_by_set[code])})
    sets.sort(key=lambda s: (s["released_at"] or "9999", s["code"]))
    return sets, cards_by_set, by_oracle, rulings


# ------------------------------------------------------------- rendu : CSS

def prepare_fonts(out: Path, cache_dir: Path, offline: bool) -> bool:
    """Embarque keyrune + mana dans <out>/assets, en local.

    Télécharge les .woff2 et les CSS (une seule fois, mis en cache dans le
    data-dir), réécrit les `url(...)` des CSS pour ne garder que le .woff2
    local, et retire le bloc mplantin (police de texte optionnelle absente
    en woff2). Retourne True si les fontes sont disponibles.

    En cas d'absence de réseau et de cache, on ne bloque pas la génération :
    le site se rabat sur les CDN (voir head()), avec un avertissement.
    """
    assets = out / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    def get(name: str) -> bytes | None:
        cached = cache_dir / name
        if cached.exists() and cached.stat().st_size > 0:
            return cached.read_bytes()
        if offline:
            return None
        try:
            req = urllib.request.Request(
                FONT_SOURCES[name],
                headers={"User-Agent": "MTGcyCLAUDEpedia"})
            data = urllib.request.urlopen(req, timeout=30).read()
            cached.write_bytes(data)
            return data
        except (urllib.error.URLError, TimeoutError, OSError):
            return None

    ok = True
    for woff in ("keyrune.woff2", "mana.woff2"):
        data = get(woff)
        if data:
            (assets / woff).write_bytes(data)
        else:
            ok = False

    for css_name, woff, family in (("keyrune.css", "keyrune.woff2", "keyrune"),
                                   ("mana.css", "mana.woff2", "mana")):
        raw = get(css_name)
        if raw is None:
            ok = False
            continue
        text = raw.decode("utf-8", "replace")
        # ne conserver que le @font-face de la fonte d'icônes (écarte mplantin,
        # absent en woff2)
        text = _strip_extra_fontface(text, family)
        # Réécrire le bloc @font-face pour ne pointer QUE sur le woff2 local.
        # Un @font-face keyrune contient deux déclarations src: (eot seul, puis
        # multi-format) ; les laisser produirait des requêtes mortes vers des
        # .eot/.ttf/.svg absents. On remplace tout le bloc d'un coup.
        text = re.sub(
            r"@font-face\s*\{[^}]*\}",
            "@font-face{font-family:'" + family + "';"
            "src:url('" + woff + "') format('woff2');"
            "font-weight:normal;font-style:normal;font-display:block}",
            text, count=1)
        (assets / css_name).write_text(text, encoding="utf-8")
    return ok


def _strip_extra_fontface(css_text: str, keep_family: str) -> str:
    """Ne garde que le @font-face dont la font-family contient keep_family."""
    blocks = list(re.finditer(r"@font-face\s*\{[^}]*\}", css_text))
    for b in blocks:
        if keep_family.lower() not in b.group(0).lower():
            css_text = css_text.replace(b.group(0), "", 1)
    return css_text


def css() -> str:
    t = THEME
    return f""":root{{--bg:{t['bg']};--panel:{t['panel']};--panel2:{t['panel_alt']};
--bd:{t['border']};--bdhi:{t['border_hi']};--gold:{t['gold']};
--golddim:{t['gold_dim']};--ink:{t['ink']};--inkdim:{t['ink_dim']};
--inkfaint:{t['ink_faint']}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
line-height:1.5}}
a{{color:inherit;text-decoration:none}}
.wrap{{max-width:1180px;margin:0 auto;padding:28px 22px 60px}}
.top{{display:flex;align-items:center;gap:16px;border-bottom:1px solid var(--bd);
padding-bottom:18px;margin-bottom:26px}}
.clock{{width:38px;height:38px;border-radius:50%;border:1.5px solid var(--gold);
display:flex;align-items:center;justify-content:center;color:var(--gold);
flex-shrink:0}}
.brand{{font-family:Georgia,"Times New Roman",serif;font-size:24px;
color:var(--ink);letter-spacing:-.01em}}
.brand a:hover{{color:var(--gold)}}
.sub{{margin-left:auto;text-align:right;font-size:12px;color:var(--golddim)}}
.crumb{{font-size:13px;color:var(--golddim);margin-bottom:20px}}
.crumb a:hover{{color:var(--gold)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));
gap:16px}}
.setcard{{background:var(--panel);border:1px solid var(--bd);border-radius:12px;
overflow:hidden;transition:border-color .15s,transform .15s}}
.setcard:hover{{border-color:var(--gold);transform:translateY(-2px)}}
.setcard .bar{{height:3px;background:var(--gold)}}
.setcard .body{{padding:16px 18px}}
.setcard .head{{display:flex;align-items:center;gap:12px;margin-bottom:13px}}
.icon{{width:40px;height:40px;border-radius:8px;background:var(--bg);
border:1px solid var(--bd);display:flex;align-items:center;
justify-content:center;flex-shrink:0;color:var(--gold);font-size:24px}}
.setname{{font-size:15px;font-weight:500;color:var(--ink);line-height:1.25}}
.setcode{{font-size:11px;color:var(--golddim);font-family:ui-monospace,
"SF Mono",Menlo,monospace;letter-spacing:.05em;text-transform:uppercase}}
.setfoot{{display:flex;justify-content:space-between;align-items:center;
font-size:12px;border-top:1px solid var(--bd);padding-top:11px}}
.setfoot .date{{color:var(--inkdim)}}
.setfoot .cnt{{color:var(--gold);font-weight:500;font-size:15px}}
.sethdr{{display:flex;align-items:center;gap:20px;margin-bottom:8px}}
.sethdr .bigicon{{width:72px;height:72px;border-radius:12px;background:var(--panel);
border:1px solid var(--bd);display:flex;align-items:center;
justify-content:center;flex-shrink:0;color:var(--gold);font-size:46px}}
.sethdr h1{{font-family:Georgia,serif;font-size:28px;margin:0 0 4px;
color:var(--ink);font-weight:500}}
.metaline{{font-size:13px;color:var(--inkdim);display:flex;gap:16px;
flex-wrap:wrap}}
.metaline .k{{color:var(--inkfaint)}}
.metaline b{{color:var(--gold);font-weight:500}}
.tools{{display:flex;gap:10px;align-items:center;margin:22px 0 18px;
flex-wrap:wrap}}
.tools input,.tools select{{background:var(--panel);border:1px solid var(--bd);
color:var(--ink);border-radius:8px;padding:8px 12px;font-size:13px;
font-family:inherit}}
.tools input{{flex:1;min-width:180px}}
.tools input::placeholder{{color:var(--inkfaint)}}
.tools input:focus,.tools select:focus{{outline:none;border-color:var(--gold)}}
.count{{font-size:12px;color:var(--golddim);margin-left:auto}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));
gap:16px}}
.card{{position:relative}}
.card img{{width:100%;border-radius:11px;display:block;
background:var(--panel);border:1px solid var(--bd);aspect-ratio:488/680;
object-fit:cover;cursor:pointer}}
.card img:hover{{border-color:var(--gold)}}
.card .cap{{margin-top:6px;font-size:12px;color:var(--inkdim);
display:flex;justify-content:space-between;gap:6px}}
.card .cap .nm{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.card .cap .cn{{color:var(--inkfaint);font-family:ui-monospace,monospace;
flex-shrink:0}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:50%;
margin-right:5px;vertical-align:1px}}
.empty{{color:var(--inkfaint);text-align:center;padding:60px 0;font-size:14px}}
.foot{{margin-top:50px;padding-top:18px;border-top:1px solid var(--bd);
font-size:11px;color:var(--inkfaint);text-align:center}}
.cardtop{{display:grid;grid-template-columns:340px 1fr;gap:32px;
align-items:start}}
@media(max-width:720px){{.cardtop{{grid-template-columns:1fr}}}}
.cardtop img.hero{{width:100%;border-radius:16px;border:1px solid var(--bd);
cursor:pointer}}
.cardinfo h1{{font-family:Georgia,serif;font-size:30px;margin:0 0 4px;
color:var(--ink);font-weight:500}}
.cardinfo .tl{{font-size:14px;color:var(--inkdim);margin-bottom:14px}}
.cardinfo .oracle{{background:var(--panel);border:1px solid var(--bd);
border-radius:12px;padding:16px 18px;font-size:14px;line-height:1.7;
white-space:pre-wrap;color:var(--ink)}}
.mana{{margin-left:8px}}
.ms-cost{{font-size:15px}}
.ruling{{margin-top:16px;background:var(--panel2);border:1px solid var(--bd);
border-left:3px solid var(--gold);border-radius:0 8px 8px 0;padding:12px 16px;
font-size:13px;color:var(--inkdim)}}
.ruling .rdate{{color:var(--golddim);font-family:ui-monospace,monospace;
font-size:11px;margin-bottom:4px}}
.prints{{margin-top:34px}}
.prints h2{{font-size:16px;font-weight:500;color:var(--ink);
border-bottom:1px solid var(--bd);padding-bottom:8px;margin-bottom:16px}}
.printrow{{display:flex;gap:14px;flex-wrap:wrap}}
.print{{width:150px}}
.print img{{width:100%;border-radius:9px;border:1px solid var(--bd);
cursor:pointer}}
.print img:hover{{border-color:var(--gold)}}
.print .pl{{font-size:11px;color:var(--inkdim);margin-top:5px;
display:flex;align-items:center;gap:6px}}
.print .pl .ss{{font-size:15px;color:var(--gold)}}
#vw{{position:fixed;inset:0;background:rgba(8,6,3,.95);z-index:999;
display:none;flex-direction:column}}
#vw.on{{display:flex}}
#vwstage{{flex:1;display:flex;align-items:center;justify-content:center;
overflow:hidden}}
#vwimg{{max-width:92vw;max-height:82vh;border-radius:12px;
transition:transform .12s;box-shadow:0 8px 40px rgba(0,0,0,.6)}}
#vwbar{{display:flex;align-items:center;justify-content:center;gap:10px;
padding:14px;background:rgba(20,16,10,.8)}}
#vwbar button{{background:var(--panel);border:1px solid var(--bd);
color:var(--ink);width:44px;height:44px;border-radius:10px;font-size:18px;
cursor:pointer;display:flex;align-items:center;justify-content:center}}
#vwbar button:hover{{border-color:var(--gold);color:var(--gold)}}
#vwcap{{position:absolute;top:16px;left:0;right:0;text-align:center;
color:var(--inkdim);font-size:13px;pointer-events:none}}
#vwx{{position:absolute;top:14px;right:18px;background:none;border:none;
color:var(--inkdim);font-size:26px;cursor:pointer}}
#vwx:hover{{color:var(--gold)}}"""


# ---------------------------------------------------------- favicon SVG

def favicon_svg() -> str:
    """Favicon SVG inline : lotus doré sur fond sombre, identité du site.

    La variation par set demandée passerait par le glyphe keyrune, mais une
    webfont ne se charge pas dans un data: SVG de favicon (contexte isolé),
    donc on garde une identité constante. Variation par set = limite connue,
    voir CHANGELOG.
    """
    return ("data:image/svg+xml,"
            "%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2032%2032'%3E"
            "%3Crect%20width='32'%20height='32'%20rx='6'%20fill='%2314100a'/%3E"
            "%3Cpath%20d='M16%206c2%204%206%205%208%205-1%204-4%207-8%209-4-2-7-5-8-9%202%200%206-1%208-5z'"
            "%20fill='none'%20stroke='%23c9a227'%20stroke-width='1.6'/%3E"
            "%3Ccircle%20cx='16'%20cy='16'%20r='2.4'%20fill='%23c9a227'/%3E%3C/svg%3E")


CLOCK_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
             'stroke-width="1.6" width="20" height="20"><circle cx="12" cy="12" '
             'r="9"/><path d="M12 7v5l3 2"/></svg>')


def head(title: str, favicon: str) -> str:
    return (f"<!doctype html><html lang=fr><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title>"
            f"<link rel='icon' type='image/svg+xml' href=\"{favicon}\">"
            f"<link rel=stylesheet href='assets/keyrune.css'>"
            f"<link rel=stylesheet href='assets/mana.css'>"
            f"<link rel=stylesheet href='style.css'></head><body>")


def set_icon(code: str, cls: str = "icon") -> str:
    return f'<i class="ss ss-{esc(code)} {cls}"></i>'


def mana_html(cost: str) -> str:
    if not cost:
        return ""
    out = []
    for sym in re.findall(r"\{([^}]+)\}", cost):
        key = sym.lower().replace("/", "")
        out.append(f'<i class="ms ms-{key} ms-cost"></i>')
    return f'<span class=mana>{"".join(out)}</span>'


# ---------------------------------------------------------- pages

def render_index(sets, favicon: str) -> str:
    total = sum(s["count"] for s in sets)
    o = [head("MTGcyCLAUDEpedia", favicon), "<div class=wrap>"]
    o.append(f"<div class=top><div class=clock>{CLOCK_SVG}</div>"
             f"<div class=brand>MTGcyCLAUDEpedia</div>"
             f"<div class=sub>{len(sets)} extension"
             f"{'s' if len(sets) > 1 else ''}<br>{total} cartes</div></div>")
    o.append("<div class=grid>")
    for s in sets:
        o.append(
            f"<a class=setcard href='set-{slug(s['code'])}.html'>"
            f"<div class=bar></div><div class=body>"
            f"<div class=head>{set_icon(s['code'])}"
            f"<div><div class=setname>{esc(s['name'])}</div>"
            f"<div class=setcode>{esc(s['code'])}</div></div></div>"
            f"<div class=setfoot><span class=date>{s['released_at'] or '—'}</span>"
            f"<span class=cnt>{s['count']}</span></div></div></a>")
    o.append("</div>")
    o.append("<div class=foot>MTGcyCLAUDEpedia · généré localement · "
             "icônes keyrune + mana © Andrew Gioia (SIL OFL) · "
             "cartes © Wizards of the Coast via Scryfall</div>")
    o.append("</div></body></html>")
    return "".join(o)


def render_set(s, cards, favicon: str, card_pages: bool) -> str:
    o = [head(f"{s['name']} — MTGcyCLAUDEpedia", favicon), "<div class=wrap>"]
    o.append(f"<div class=top><div class=clock>{CLOCK_SVG}</div>"
             f"<div class=brand><a href='index.html'>MTGcyCLAUDEpedia</a>"
             f"</div></div>")
    o.append("<div class=crumb><a href='index.html'>Extensions</a> "
             f"&rsaquo; {esc(s['name'])}</div>")
    stype = (s["set_type"] or "—").replace("_", " ")
    o.append(
        f"<div class=sethdr>{set_icon(s['code'], 'bigicon')}<div>"
        f"<h1>{esc(s['name'])}</h1><div class=metaline>"
        f"<span><span class=k>Code</span> <b>{esc(s['code'].upper())}</b></span>"
        f"<span><span class=k>Sortie</span> {s['released_at'] or '—'}</span>"
        f"<span><span class=k>Type</span> {esc(stype)}</span>"
        f"<span><span class=k>Cartes</span> <b>{s['count']}</b></span>"
        f"</div></div></div>")
    o.append(
        "<div class=tools>"
        "<input id=q placeholder='Filtrer par nom, type, illustrateur…' "
        "oninput=flt()>"
        "<select id=rar onchange=flt()><option value=''>Toutes raretés</option>"
        "<option value=common>Commune</option>"
        "<option value=uncommon>Peu commune</option>"
        "<option value=rare>Rare</option>"
        "<option value=mythic>Mythique</option></select>"
        "<select id=sort onchange=flt()>"
        "<option value=cn>N° de collection</option>"
        "<option value=name>Nom (A→Z)</option>"
        "<option value=cmc>Coût converti</option></select>"
        "<span class=count id=count></span></div>")
    o.append("<div class=cards id=cards>")
    viewer_list = []
    for i, c in enumerate(cards):
        col = RARITY_COLOR.get(c["rarity"], THEME["ink_faint"])
        img = f"sets/{c['set_code']}/{c['img']}"
        cmc = ("" if c["cmc"] is None
               else str(int(c["cmc"]) if c["cmc"] == int(c["cmc"]) else c["cmc"]))
        has_page = card_pages and not c["oid"].startswith("noid-")
        viewer_list.append({"src": img, "name": c["name"], "cn": c["cn"]})
        if has_page:
            link = (f"<a href='card-{slug(c['oid'])}.html'>"
                    f"<img loading=lazy src='{img}' alt=\"{esc(c['name'])}\"></a>")
        else:
            link = (f"<img loading=lazy src='{img}' alt=\"{esc(c['name'])}\" "
                    f"onclick='vwOpen({i})'>")
        o.append(
            f"<div class=card data-n=\"{esc(c['name'].lower())}\" "
            f"data-t=\"{esc(c['type'].lower())}\" "
            f"data-a=\"{esc(c['artist'].lower())}\" data-r=\"{c['rarity']}\" "
            f"data-cmc=\"{cmc or 0}\" data-cn=\"{esc(c['cn'])}\" "
            f"data-name=\"{esc(c['name'])}\">{link}"
            f"<div class=cap><span class=nm>"
            f"<span class=dot style='background:{col}'></span>"
            f"{esc(c['name'])}</span>"
            f"<span class=cn>{esc(c['cn'])}</span></div></div>")
    o.append("</div>")
    o.append("<div class=empty id=empty style=display:none>"
             "Aucune carte ne correspond au filtre.</div>")
    o.append(f"<div class=foot>{s['count']} cartes · MTGcyCLAUDEpedia</div>")
    o.append("</div>")
    o.append(viewer_html())
    o.append(f"<script>const VIEW={json.dumps(viewer_list, ensure_ascii=False)};"
             f"</script>")
    o.append(SET_JS)
    o.append(VIEWER_JS)
    o.append("</body></html>")
    return "".join(o)


def render_card(group, rulings, favicon: str) -> str:
    first = group[0]
    o = [head(f"{first['name']} — MTGcyCLAUDEpedia", favicon), "<div class=wrap>"]
    o.append(f"<div class=top><div class=clock>{CLOCK_SVG}</div>"
             f"<div class=brand><a href='index.html'>MTGcyCLAUDEpedia</a>"
             f"</div></div>")
    o.append(f"<div class=crumb><a href='index.html'>Extensions</a> &rsaquo; "
             f"<a href='set-{slug(first['set_code'])}.html'>"
             f"{esc(first['set_code'].upper())}</a> &rsaquo; "
             f"{esc(first['name'])}</div>")
    hero = f"sets/{first['set_code']}/{first['img']}"
    o.append("<div class=cardtop>")
    o.append(f"<img class=hero src='{hero}' alt=\"{esc(first['name'])}\" "
             f"onclick='vwOpen(0)'>")
    o.append("<div class=cardinfo>")
    o.append(f"<h1>{esc(first['name'])}{mana_html(first['mana'])}</h1>")
    o.append(f"<div class=tl>{esc(first['type'])}</div>")
    if first["oracle_text"]:
        o.append(f"<div class=oracle>{esc(first['oracle_text'])}</div>")
    rr = rulings.get(first["oid"])
    if rr:
        r = rr[0]
        o.append(f"<div class=ruling><div class=rdate>Dernier ruling — "
                 f"{esc(r['date'])}</div>{esc(r['text'])}</div>")
    o.append("</div></div>")

    o.append(f"<div class=prints><h2>Toutes les impressions "
             f"({len(group)})</h2><div class=printrow>")
    viewer_list = []
    for i, c in enumerate(group):
        img = f"sets/{c['set_code']}/{c['img']}"
        viewer_list.append({"src": img, "name": c["name"],
                            "cn": f"{c['set_code'].upper()} {c['cn']}"})
        o.append(
            f"<div class=print><img loading=lazy src='{img}' "
            f"alt=\"{esc(c['set_code'])} {esc(c['cn'])}\" "
            f"onclick='vwOpen({i})'>"
            f"<div class=pl>{set_icon(c['set_code'], '')}"
            f"<a href='set-{slug(c['set_code'])}.html'>"
            f"{esc(c['set_code'].upper())}</a> · {esc(c['cn'])}</div></div>")
    o.append("</div></div>")
    o.append(f"<div class=foot>{first['name']} · {len(group)} impression"
             f"{'s' if len(group) > 1 else ''} · MTGcyCLAUDEpedia</div>")
    o.append("</div>")
    o.append(viewer_html())
    o.append(f"<script>const VIEW={json.dumps(viewer_list, ensure_ascii=False)};"
             f"</script>")
    o.append(VIEWER_JS)
    o.append("</body></html>")
    return "".join(o)


def viewer_html() -> str:
    return ("<div id=vw><button id=vwx onclick=vwClose()>&times;</button>"
            "<div id=vwcap></div><div id=vwstage>"
            "<img id=vwimg src='' alt=''></div>"
            "<div id=vwbar>"
            "<button onclick=vwPrev() title='Précédent (←)'>&lsaquo;</button>"
            "<button onclick=vwZoom(-1) title='Zoom -'>&minus;</button>"
            "<button onclick=vwZoom(1) title='Zoom +'>+</button>"
            "<button onclick=vwRot() title='Rotation (r)'>&#8635;</button>"
            "<button onclick=vwReset() title='Réinitialiser'>&#9633;</button>"
            "<button onclick=vwNext() title='Suivant (→)'>&rsaquo;</button>"
            "</div></div>")


SET_JS = """<script>
const cards=[...document.querySelectorAll('.card')];
const cont=document.getElementById('cards');
const cntEl=document.getElementById('count');
const emptyEl=document.getElementById('empty');
function flt(){
  const q=document.getElementById('q').value.toLowerCase().trim();
  const r=document.getElementById('rar').value;
  const sort=document.getElementById('sort').value;
  let vis=0;
  for(const c of cards){
    const okq=!q||c.dataset.n.includes(q)||c.dataset.t.includes(q)||c.dataset.a.includes(q);
    const okr=!r||c.dataset.r===r;
    const show=okq&&okr;c.style.display=show?'':'none';if(show)vis++;
  }
  const shown=cards.filter(c=>c.style.display!=='none');
  shown.sort((a,b)=>{
    if(sort==='name')return a.dataset.name.localeCompare(b.dataset.name);
    if(sort==='cmc')return (+a.dataset.cmc)-(+b.dataset.cmc)||natcn(a,b);
    return natcn(a,b);
  });
  for(const c of shown)cont.appendChild(c);
  cntEl.textContent=vis+' / '+cards.length;
  emptyEl.style.display=vis?'none':'';
}
function natcn(a,b){
  const pa=a.dataset.cn.match(/^(\\d+)(.*)$/),pb=b.dataset.cn.match(/^(\\d+)(.*)$/);
  if(pa&&pb){const d=(+pa[1])-(+pb[1]);return d||pa[2].localeCompare(pb[2]);}
  return a.dataset.cn.localeCompare(b.dataset.cn);
}
flt();
</script>"""


VIEWER_JS = """<script>
let vi=0,vz=1,vr=0;
const vw=document.getElementById('vw'),vimg=document.getElementById('vwimg'),
      vcap=document.getElementById('vwcap');
function vwApply(){vimg.style.transform=`scale(${vz}) rotate(${vr}deg)`;}
function vwShow(){const v=VIEW[vi];vimg.src=v.src;vimg.alt=v.name;
  vcap.textContent=v.name+'  ·  '+v.cn+'   ('+(vi+1)+'/'+VIEW.length+')';
  vz=1;vr=0;vwApply();}
function vwOpen(i){vi=i;vw.classList.add('on');vwShow();return false;}
function vwClose(){vw.classList.remove('on');}
function vwNext(){vi=(vi+1)%VIEW.length;vwShow();}
function vwPrev(){vi=(vi-1+VIEW.length)%VIEW.length;vwShow();}
function vwZoom(d){vz=Math.max(.3,Math.min(6,vz+d*.25));vwApply();}
function vwRot(){vr=(vr+90)%360;vwApply();}
function vwReset(){vz=1;vr=0;vwApply();}
document.addEventListener('keydown',e=>{
  if(!vw.classList.contains('on'))return;
  if(e.key==='Escape')vwClose();
  else if(e.key==='ArrowRight')vwNext();
  else if(e.key==='ArrowLeft')vwPrev();
  else if(e.key==='r'||e.key==='R')vwRot();
  else if(e.key==='+'||e.key==='=')vwZoom(1);
  else if(e.key==='-')vwZoom(-1);
});
vw.addEventListener('click',e=>{if(e.target===vw||e.target.id==='vwstage')vwClose();});
</script>"""


# ------------------------------------------------------------------ main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="mtgc-web",
        description="Génère un site statique depuis un data-dir MTGcyCLAUDEpedia.")
    ap.add_argument("--data-dir", default="~/mtg", type=Path)
    ap.add_argument("--out", type=Path, default=None,
                    help="répertoire de sortie (défaut : <data-dir>/site)")
    ap.add_argument("--no-card-pages", action="store_true",
                    help="ne pas générer une page par carte (plus rapide)")
    ap.add_argument("--no-rulings", action="store_true",
                    help="ignorer le bulk rulings même s'il est présent")
    ap.add_argument("--offline", action="store_true",
                    help="ne pas tenter de télécharger les fontes ; "
                         "utiliser le cache local s'il existe")
    ap.add_argument("--open", action="store_true",
                    help="ouvrir la page d'accueil dans le navigateur")
    ap.add_argument("--version", action="version", version=f"mtgc-web {VERSION}")
    a = ap.parse_args(argv)

    data_dir = a.data_dir.expanduser().resolve()
    out = (a.out or data_dir / "site").expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    card_pages = not a.no_card_pages

    print(f"mtgc-web {VERSION} — lecture de {data_dir}", file=sys.stderr)
    sets, cards_by_set, by_oracle, rulings = build_model(
        data_dir, want_rulings=not a.no_rulings)
    total = sum(s["count"] for s in sets)
    print(f"  {len(sets)} extension(s), {total} carte(s), "
          f"{len(by_oracle)} carte(s) unique(s)"
          + (f", {len(rulings)} oracle_id avec rulings" if rulings else ""),
          file=sys.stderr)

    fonts_ok = prepare_fonts(out, data_dir / "metadata" / "fonts", a.offline)
    if fonts_ok:
        print("  fontes keyrune + mana embarquées (hors-ligne)", file=sys.stderr)
    else:
        print("  fontes indisponibles (ni cache ni réseau) : les icônes "
              "n'apparaîtront pas tant que assets/ n'est pas peuplé",
              file=sys.stderr)

    fav = favicon_svg()
    (out / "style.css").write_text(css(), encoding="utf-8")
    (out / "index.html").write_text(render_index(sets, fav), encoding="utf-8")
    for s in sets:
        (out / f"set-{slug(s['code'])}.html").write_text(
            render_set(s, cards_by_set[s["code"]], fav, card_pages),
            encoding="utf-8")

    n_card = 0
    if card_pages:
        for oid, group in by_oracle.items():
            if oid.startswith("noid-"):
                continue
            (out / f"card-{slug(oid)}.html").write_text(
                render_card(group, rulings, fav), encoding="utf-8")
            n_card += 1
        print(f"  {n_card} page(s) de carte", file=sys.stderr)

    link = out / "sets"
    if not link.exists():
        try:
            os.symlink(data_dir / "sets", link)
        except OSError:
            shutil.copytree(data_dir / "sets", link)

    print(f"  site écrit dans {out}", file=sys.stderr)
    print(f"  ouvrir {out / 'index.html'}", file=sys.stderr)
    if a.open:
        import webbrowser
        webbrowser.open((out / "index.html").as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
