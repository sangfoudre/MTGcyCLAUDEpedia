#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mtgc-web — MTGcyclopedia
Génère un site statique à partir d'un data-dir : une page d'accueil listant
les extensions présentes, puis une page par extension avec toutes ses cartes.

Ne dépend que de ce qui est réellement dans le data-dir :
  - sets/<CODE>/*.jpg|png   les images téléchargées
  - metadata/*.jsonl.gz     le bulk Scryfall (noms, dates, raretés…)
  - icons/<CODE>.svg        les icônes d'extension (optionnelles)

Aucune dépendance, aucun accès réseau. Bibliothèque standard seule.

Le rendu est un site 100 % statique : une carte = une balise <img> vers le
fichier local. Filtre et tri se font côté client en JavaScript, sans serveur.

Exemples
--------
    ./mtgc-web.py --data-dir ~/mtg
    ./mtgc-web.py --data-dir ~/mtg --out ~/mtg/site --open
"""

from __future__ import annotations

import argparse
import glob
import gzip
import html
import json
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path

VERSION = "0.1.0"

# ------------------------------------------------------------------ thème
# Style « sombre à accents dorés / parchemin » — cohérent avec l'emblème de
# l'horloge. Toutes les couleurs sont ici, pour un rethème facile.
THEME = {
    "bg":        "#14100a",
    "panel":     "#1c160d",
    "panel_alt": "#181209",
    "border":    "#2a2013",
    "border_hi": "#3a2e1a",
    "gold":      "#c9a227",
    "gold_dim":  "#8a7a54",
    "ink":       "#e8dcc0",
    "ink_dim":   "#a89968",
    "ink_faint": "#6f6142",
}

RARITY_COLOR = {
    "common":   "#c8c8c8",
    "uncommon": "#a9b8c6",
    "rare":     "#d5aa5a",
    "mythic":   "#e07b28",
    "special":  "#b57edc",
    "bonus":    "#b57edc",
}

# ------------------------------------------------------------- utilitaires

def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def natural_key(cn: str):
    """Tri naturel des collector numbers : 2 < 10, 100a avant 100b."""
    m = re.match(r"^(\d+)(.*)$", cn or "")
    if m:
        return (0, int(m.group(1)), m.group(2))
    return (1, 0, cn or "")


def find_bulk(meta_dir: Path) -> Path | None:
    cands = sorted(meta_dir.glob("*.jsonl.gz")) + sorted(meta_dir.glob("*.json"))
    return cands[0] if cands else None


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


# ------------------------------------------------------------- collecte

def scan_disk(images_dir: Path) -> dict[str, set[str]]:
    """Quelles images sont réellement présentes, par code d'extension.

    Le site ne montre QUE ce qui est sur le disque : la source de vérité est
    le système de fichiers, le bulk ne sert qu'à enrichir.
    """
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


# Doit refléter la sanitisation du téléchargeur (mtgc-images.py) : les
# collector numbers exotiques y sont translittérés (★->star, †->dagger…).
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_cn(part: str) -> str:
    """Copie fidèle de sanitize() du téléchargeur, pour retrouver ses fichiers.

    Toute divergence ferait manquer des images : les deux fonctions DOIVENT
    produire la même chaîne. Voir mtgc-images.py:sanitize (test dédié).
    """
    if not part:
        return "_"
    part = part.replace("★", "-star").replace("†", "-dagger")
    part = unicodedata.normalize("NFKD", part)
    part = part.encode("ascii", "ignore").decode("ascii")
    return _SAFE_RE.sub("-", part).strip("-._")


def stem_variants(cn: str) -> list[str]:
    """Noms de fichiers possibles pour un collector number et ses faces.

    On essaie le numéro brut ET sa forme translittérée, pour retomber sur
    les fichiers que le téléchargeur a renommés (ex. 232† -> 232-dagger).
    """
    out = []
    for base in dict.fromkeys([cn, sanitize_cn(cn)]):
        out += [f"{base}.jpg", f"{base}.png", f"{base}-a.jpg",
                f"{base}-a.png", f"{base}-b.jpg", f"{base}-b.png"]
    return out


def build_model(data_dir: Path):
    """Croise le disque et le bulk. Retourne (liste de sets, cartes par set)."""
    images_dir = data_dir / "sets"
    meta_dir = data_dir / "metadata"
    present = scan_disk(images_dir)
    if not present:
        raise SystemExit(f"aucune image trouvée dans {images_dir}")

    bulk = find_bulk(meta_dir)
    meta: dict[str, dict] = {}          # card_id ou set -> infos
    set_meta: dict[str, dict] = {}
    cards_by_set: dict[str, list] = {code: [] for code in present}

    # Index des cartes du bulk qui appartiennent à un set présent, par (set, cn).
    if bulk:
        for c in iter_bulk(bulk):
            code = (c.get("set") or "").lower()
            if code not in present:
                continue
            if code not in set_meta:
                set_meta[code] = {
                    "name": c.get("set_name") or code.upper(),
                    "released_at": c.get("released_at") or "",
                    "set_type": c.get("set_type") or "",
                }
            cn = c.get("collector_number") or ""
            # quelle image sur disque correspond à cette carte ?
            img = next((v for v in stem_variants(cn) if v in present[code]), None)
            back = None
            if c.get("layout") in ("transform", "modal_dfc", "reversible_card",
                                    "double_faced_token", "art_series"):
                a = next((v for v in (f"{cn}-a.jpg", f"{cn}-a.png")
                          if v in present[code]), None)
                b = next((v for v in (f"{cn}-b.jpg", f"{cn}-b.png")
                          if v in present[code]), None)
                img, back = a or img, b
            if not img:
                continue
            cards_by_set[code].append({
                "cn": cn, "name": c.get("name") or "?",
                "rarity": c.get("rarity") or "",
                "artist": c.get("artist") or "",
                "type": c.get("type_line") or "",
                "mana": c.get("mana_cost") or "",
                "cmc": c.get("cmc"),
                "colors": "".join(c.get("colors") or []),
                "img": img, "back": back,
            })

    # Filet anti-perte : toute image présente sur disque mais qu'aucune carte
    # du bulk n'a réclamée est ajoutée telle quelle. Un site qui montre
    # 1052 cartes quand le disque en a 1057 est un bug silencieux.
    for code, files in present.items():
        if code not in set_meta:
            set_meta[code] = {"name": code.upper(), "released_at": "",
                              "set_type": ""}
        claimed = {c["img"] for c in cards_by_set[code]}
        claimed |= {c["back"] for c in cards_by_set[code] if c["back"]}
        for fn in sorted(files - claimed, key=lambda f: natural_key(Path(f).stem)):
            cards_by_set[code].append({
                "cn": Path(fn).stem, "name": Path(fn).stem, "rarity": "",
                "artist": "", "type": "", "mana": "", "cmc": None,
                "colors": "", "img": fn, "back": None})

    for code in cards_by_set:
        cards_by_set[code].sort(key=lambda x: natural_key(x["cn"]))

    sets = []
    for code, m in set_meta.items():
        sets.append({
            "code": code, "name": m["name"],
            "released_at": m["released_at"], "set_type": m["set_type"],
            "count": len(cards_by_set[code]),
            "has_icon": (data_dir / "icons" / f"{code}.svg").is_file(),
        })
    sets.sort(key=lambda s: (s["released_at"] or "9999", s["code"]))
    return sets, cards_by_set


# ------------------------------------------------------------- rendu HTML

def css() -> str:
    t = THEME
    return f"""
:root{{--bg:{t['bg']};--panel:{t['panel']};--panel2:{t['panel_alt']};
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
font-size:20px;flex-shrink:0}}
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
justify-content:center;flex-shrink:0}}
.icon svg{{width:26px;height:26px;fill:var(--gold)}}
.icon .fallback{{color:var(--gold);font-size:19px;font-weight:600}}
.setname{{font-size:15px;font-weight:500;color:var(--ink);
line-height:1.25}}
.setcode{{font-size:11px;color:var(--golddim);font-family:ui-monospace,
"SF Mono",Menlo,monospace;letter-spacing:.05em;text-transform:uppercase}}
.setfoot{{display:flex;justify-content:space-between;align-items:center;
font-size:12px;border-top:1px solid var(--bd);padding-top:11px}}
.setfoot .date{{color:var(--inkdim)}}
.setfoot .cnt{{color:var(--gold);font-weight:500;font-size:15px}}
.sethdr{{display:flex;align-items:center;gap:20px;margin-bottom:8px}}
.sethdr .bigicon{{width:72px;height:72px;border-radius:12px;background:var(--panel);
border:1px solid var(--bd);display:flex;align-items:center;
justify-content:center;flex-shrink:0}}
.sethdr .bigicon svg{{width:48px;height:48px;fill:var(--gold)}}
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
object-fit:cover}}
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
"""


CLOCK_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
             'stroke-width="1.6" width="20" height="20"><circle cx="12" cy="12" '
             'r="9"/><path d="M12 7v5l3 2"/></svg>')


def icon_html(data_dir: Path, code: str, big: bool = False) -> str:
    p = data_dir / "icons" / f"{code}.svg"
    if p.is_file():
        svg = p.read_text(encoding="utf-8")
        svg = re.sub(r'\sfill="[^"]*"', "", svg)   # retire TOUS les fill, le CSS colore
        return svg
    return f'<span class="fallback">{html.escape(code[:2].upper())}</span>'


def page_head(title: str, rel: str) -> str:
    return (f"<!doctype html><html lang=fr><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title>"
            f"<link rel=stylesheet href='{rel}style.css'></head><body>")


def render_index(sets, data_dir: Path) -> str:
    total = sum(s["count"] for s in sets)
    out = [page_head("MTGcyclopedia", "")]
    out.append("<div class=wrap>")
    out.append(
        f"<div class=top><div class=clock>{CLOCK_SVG}</div>"
        f"<div class=brand>MTGcyclopedia</div>"
        f"<div class=sub>{len(sets)} extension{'s' if len(sets)>1 else ''}<br>"
        f"{total:,} cartes</div></div>".replace(",", "\u202f"))
    out.append("<div class=grid>")
    for s in sets:
        ic = icon_html(data_dir, s["code"])
        date = s["released_at"] or "—"
        out.append(
            f"<a class=setcard href='set-{slug(s['code'])}.html'>"
            f"<div class=bar></div><div class=body>"
            f"<div class=head><div class=icon>{ic}</div>"
            f"<div><div class=setname>{html.escape(s['name'])}</div>"
            f"<div class=setcode>{html.escape(s['code'])}</div></div></div>"
            f"<div class=setfoot><span class=date>{date}</span>"
            f"<span class=cnt>{s['count']}</span></div></div></a>")
    out.append("</div>")
    out.append(f"<div class=foot>MTGcyclopedia · généré localement · "
               f"données et images © Wizards of the Coast via Scryfall</div>")
    out.append("</div></body></html>")
    return "".join(out)


def render_set(s, cards, data_dir: Path) -> str:
    ic = icon_html(data_dir, s["code"], big=True)
    out = [page_head(f"{s['name']} — MTGcyclopedia", "")]
    out.append("<div class=wrap>")
    out.append(
        f"<div class=top><div class=clock>{CLOCK_SVG}</div>"
        f"<div class=brand><a href='index.html'>MTGcyclopedia</a></div></div>")
    out.append("<div class=crumb><a href='index.html'>Extensions</a> "
               f"&rsaquo; {html.escape(s['name'])}</div>")
    date = s["released_at"] or "—"
    stype = s["set_type"].replace("_", " ") or "—"
    out.append(
        f"<div class=sethdr><div class=bigicon>{ic}</div><div>"
        f"<h1>{html.escape(s['name'])}</h1>"
        f"<div class=metaline>"
        f"<span><span class=k>Code</span> <b>{html.escape(s['code'].upper())}</b></span>"
        f"<span><span class=k>Sortie</span> {date}</span>"
        f"<span><span class=k>Type</span> {html.escape(stype)}</span>"
        f"<span><span class=k>Cartes</span> <b>{s['count']}</b></span>"
        f"</div></div></div>")

    out.append(
        "<div class=tools>"
        "<input id=q placeholder='Filtrer par nom, type, illustrateur…' "
        "oninput=flt()>"
        "<select id=rar onchange=flt()>"
        "<option value=''>Toutes raretés</option>"
        "<option value=common>Commune</option>"
        "<option value=uncommon>Peu commune</option>"
        "<option value=rare>Rare</option>"
        "<option value=mythic>Mythique</option></select>"
        "<select id=sort onchange=flt()>"
        "<option value=cn>N° de collection</option>"
        "<option value=name>Nom (A→Z)</option>"
        "<option value=cmc>Coût converti</option></select>"
        "<span class=count id=count></span></div>")

    out.append("<div class=cards id=cards>")
    for c in cards:
        col = RARITY_COLOR.get(c["rarity"], THEME["ink_faint"])
        alt = html.escape(f"{c['name']} ({s['code'].upper()} {c['cn']})", quote=True)
        img = f"sets/{s['code']}/{c['img']}"
        cmc = "" if c["cmc"] is None else str(int(c["cmc"]) if c["cmc"] == int(c["cmc"]) else c["cmc"])
        data = (f"data-n=\"{html.escape(c['name'].lower(),quote=True)}\" "
                f"data-t=\"{html.escape(c['type'].lower(),quote=True)}\" "
                f"data-a=\"{html.escape(c['artist'].lower(),quote=True)}\" "
                f"data-r=\"{c['rarity']}\" data-cmc=\"{cmc or 0}\" "
                f"data-cn=\"{html.escape(c['cn'],quote=True)}\" "
                f"data-name=\"{html.escape(c['name'],quote=True)}\"")
        out.append(
            f"<div class=card {data}>"
            f"<a href='{img}' target=_blank>"
            f"<img loading=lazy src='{img}' alt=\"{alt}\"></a>"
            f"<div class=cap><span class=nm>"
            f"<span class=dot style='background:{col}'></span>"
            f"{html.escape(c['name'])}</span>"
            f"<span class=cn>{html.escape(c['cn'])}</span></div></div>")
    out.append("</div>")
    out.append("<div class=empty id=empty style=display:none>"
               "Aucune carte ne correspond au filtre.</div>")
    out.append(f"<div class=foot>{s['count']} cartes · MTGcyclopedia</div>")
    out.append("</div>")
    out.append(SET_JS)
    out.append("</body></html>")
    return "".join(out)


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
    const show=okq&&okr;
    c.style.display=show?'':'none';
    if(show)vis++;
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


# ------------------------------------------------------------------ main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="mtgc-web",
        description="Génère un site statique à partir d'un data-dir "
                    "MTGcyclopedia.")
    ap.add_argument("--data-dir", default="~/mtg", type=Path,
                    help="racine des données (défaut : ~/mtg)")
    ap.add_argument("--out", type=Path, default=None,
                    help="répertoire de sortie (défaut : <data-dir>/site)")
    ap.add_argument("--open", action="store_true",
                    help="ouvrir la page d'accueil dans le navigateur")
    ap.add_argument("--version", action="version",
                    version=f"mtgc-web {VERSION}")
    a = ap.parse_args(argv)

    data_dir = a.data_dir.expanduser().resolve()
    out = (a.out or data_dir / "site").expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    print(f"mtgc-web {VERSION} — lecture de {data_dir}", file=sys.stderr)
    sets, cards_by_set = build_model(data_dir)
    print(f"  {len(sets)} extension(s), "
          f"{sum(s['count'] for s in sets)} carte(s)", file=sys.stderr)

    (out / "style.css").write_text(css(), encoding="utf-8")
    (out / "index.html").write_text(render_index(sets, data_dir),
                                    encoding="utf-8")
    for s in sets:
        page = render_set(s, cards_by_set[s["code"]], data_dir)
        (out / f"set-{slug(s['code'])}.html").write_text(page, encoding="utf-8")

    # Les images : lien symbolique vers sets/ si possible (pas de copie de
    # plusieurs Go), copie en dernier recours.
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
