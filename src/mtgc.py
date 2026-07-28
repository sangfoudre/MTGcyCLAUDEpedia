#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mtgc — MTGcyCLAUDEpedia : outil unique.

Une seule commande fait tout : télécharge les images (un dossier par set,
schéma <code>-<numéro>), les rulings, les fontes, puis génère le site.

    mtgc sync                                  # tout, dans ~/mtg (defaut)
    mtgc sync --data-dir ~/mtg                 # emplacement explicite
    mtgc sync --data-dir ~/mtg --quality png
    mtgc sync --data-dir ~/mtg --no-web        # images seulement
    mtgc sync --data-dir ~/mtg --no-images     # (re)générer le site seul
    mtgc sync --data-dir ~/mtg --sets isd,dka  # se limiter à des extensions

Étapes activables/désactivables : --no-images, --no-rulings, --no-fonts,
--no-web, --no-card-pages. Par défaut tout est activé.

Sous-commandes détaillées disponibles : images, web, verify, sizes.

Sans dépendance : bibliothèque standard seule.
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict
import concurrent.futures as cf
from pathlib import Path

VERSION = "2.5.0"
UA = "MTGcyCLAUDEpedia/2.0"
API = "https://api.scryfall.com"
API_DELAY = 0.1
HEADERS = {"User-Agent": UA, "Accept": "application/json;q=0.9,*/*;q=0.8"}
IMAGE_STATUS_RANK = {"missing": 0, "placeholder": 1, "lowres": 2,
                     "highres_scan": 3}

# fontes Gioia (embarquées en local à la génération)
FONT_SOURCES = {
    "keyrune.woff2": "https://cdn.jsdelivr.net/npm/keyrune@latest/fonts/keyrune.woff2",
    "keyrune.css": "https://cdn.jsdelivr.net/npm/keyrune@latest/css/keyrune.min.css",
    "mana.woff2": "https://cdn.jsdelivr.net/npm/mana-font@latest/fonts/mana.woff2",
    "mana.css": "https://cdn.jsdelivr.net/npm/mana-font@latest/css/mana.min.css",
}

# ---- skin : plus clair, plus lisible (demande utilisateur) ----
THEME = {
    "bg": "#1a1712", "panel": "#242019", "panel_alt": "#2b2620",
    "border": "#3a332a", "border_hi": "#4a4234", "gold": "#e0b84e",
    "gold_dim": "#a89563", "ink": "#f2ecdd", "ink_dim": "#c4bba6",
    "ink_faint": "#8a8069",
}
RARITY_COLOR = {
    "common": "#d8d8d8", "uncommon": "#b8c6d4", "rare": "#e0bd5a",
    "mythic": "#e8842e", "special": "#c088e0", "bonus": "#c088e0",
}
DFC_LAYOUTS = {"transform", "modal_dfc", "reversible_card",
               "double_faced_token", "art_series"}
QUALITY_DIMS = {"small": "146x204", "normal": "488x680", "large": "672x936",
                "png": "745x1040", "art_crop": "variable", "border_crop": "480x680"}
QUALITIES = ["png", "large", "normal", "small", "border_crop", "art_crop"]
BULK_TYPES = ["default_cards", "all_cards", "unique_artwork", "oracle_cards"]


# ==================================================================
# TÉLÉCHARGEMENT (ex-mtgc-images)
# ==================================================================

class Log:
    C = {"info": "\033[34m", "ok": "\033[32m", "warn": "\033[33m",
         "err": "\033[1m\033[31m", "end": "\033[0m"}

    def __init__(self, verbose: bool = False, color: bool | None = None):
        self.verbose = verbose
        self.color = sys.stderr.isatty() if color is None else color
        self._lock = threading.Lock()

    def _w(self, kind: str, msg: str) -> None:
        with self._lock:
            if self.color:
                print(f"{self.C[kind]}{msg}{self.C['end']}", file=sys.stderr)
            else:
                print(msg, file=sys.stderr)

    def info(self, m): self._w("info", m)
    def ok(self, m): self._w("ok", m)
    def warn(self, m): self._w("warn", f"attention : {m}")
    def err(self, m): self._w("err", f"erreur : {m}")
    def debug(self, m):
        if self.verbose:
            self._w("info", f"  {m}")


log = Log()


# ------------------------------------------------------------- utilitaires

def human(n: float) -> str:
    for unit in ("o", "Kio", "Mio", "Gio", "Tio"):
        if abs(n) < 1024:
            return f"{n:,.1f} {unit}".replace(",", " ")
        n /= 1024
    return f"{n:.1f} Pio"


def human_time(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize(part: str) -> str:
    """Rend un fragment de nom de fichier sûr sur un système de fichiers Linux.

    Les collector numbers réels contiennent ★ (etched/prerelease), † (variantes)
    et parfois des espaces ou des barres obliques. On translittère d'abord en
    ASCII pour garder quelque chose de lisible, puis on remplace le reste.
    """
    if not part:
        return "_"
    part = part.replace("★", "-star").replace("†", "-dagger")
    part = unicodedata.normalize("NFKD", part)
    part = part.encode("ascii", "ignore").decode("ascii")
    part = _SAFE_RE.sub("-", part).strip("-._")
    return part or "_"


# ------------------------------------------------------------------ réseau

_last_api_call = 0.0
_api_lock = threading.Lock()


def api_get(url: str, retries: int = 4) -> dict:
    """Appel à api.scryfall.com, limité à 10 req/s, avec reprise sur 429."""
    global _last_api_call
    for attempt in range(retries):
        with _api_lock:
            wait = API_DELAY - (time.monotonic() - _last_api_call)
            if wait > 0:
                time.sleep(wait)
            _last_api_call = time.monotonic()
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # la documentation annonce une mise à l'écart de 30 s
                log.warn(f"429 reçu, pause 32 s (tentative {attempt + 1})")
                time.sleep(32)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise
            log.warn(f"{type(e).__name__}, nouvelle tentative dans 3 s")
            time.sleep(3)
    raise RuntimeError(f"échec après {retries} tentatives : {url}")


def download_file(url: str, dest: Path, timeout: int = 120) -> int:
    """Télécharge vers un fichier temporaire puis renomme (écriture atomique).

    Une interruption ne laisse donc jamais un fichier tronqué en place, ce qui
    rend la reprise fiable.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r, tmp.open("wb") as f:
        shutil.copyfileobj(r, f, 256 * 1024)
    size = tmp.stat().st_size
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise OSError("fichier vide")
    tmp.replace(dest)
    return size


# -------------------------------------------------------------- bulk data

def resolve_bulk(bulk_type: str) -> dict:
    """Retrouve le descripteur du bulk voulu.

    Les URL de téléchargement portent un horodatage et changent chaque jour :
    il faut impérativement les redemander à l'API plutôt que de les figer.
    """
    data = api_get(f"{API}/bulk-data")
    for b in data["data"]:
        if b["type"] == bulk_type:
            return b
    dispo = ", ".join(sorted(b["type"] for b in data["data"]))
    raise SystemExit(f"bulk '{bulk_type}' introuvable. Disponibles : {dispo}")


def fetch_bulk(bulk_type: str, meta_dir: Path, force: bool = False) -> Path:
    """Télécharge le bulk si le fichier local est absent ou périmé."""
    desc = resolve_bulk(bulk_type)
    updated = desc["updated_at"]
    meta_dir.mkdir(parents=True, exist_ok=True)
    stamp_file = meta_dir / f"{bulk_type}.stamp"
    local = meta_dir / f"{bulk_type}.jsonl.gz"

    if (not force and local.exists() and stamp_file.exists()
            and stamp_file.read_text().strip() == updated):
        log.info(f"bulk '{bulk_type}' à jour ({human(local.stat().st_size)})")
        return local

    # jsonl_download_uri est le format en flux ; download_uri (tableau JSON
    # monolithique) existe toujours mais oblige à tout charger en mémoire.
    url = desc.get("jsonl_download_uri") or desc["download_uri"]
    log.info(f"téléchargement du bulk '{bulk_type}' "
             f"({human(desc.get('size', 0))} annoncés, maj {updated[:19]})")
    t0 = time.time()
    size = download_file(url, local)
    stamp_file.write_text(updated)
    log.ok(f"bulk récupéré : {human(size)} en {human_time(time.time() - t0)}")
    return local


def iter_cards(path: Path):
    """Lit le bulk en flux. Accepte le JSONL gzippé et le JSON monolithique."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        head = f.read(1)
        f.seek(0)
        if head == "[":                      # ancien format : tableau JSON
            for c in json.load(f):
                yield c
            return
        for line in f:                       # JSON Lines
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# ------------------------------------------------------------ planification

class Job:
    """Une image à obtenir.

    Si `link_to` est renseigné, aucun téléchargement n'a lieu : on crée un
    lien symbolique relatif vers le fichier réel, déposé dans le set où
    l'illustration paraît pour la première fois.
    """
    __slots__ = ("url", "path", "card_id", "face", "fmt", "illustration",
                 "updated", "set_code", "link_to")

    def __init__(self, url, path, card_id, face, fmt, illustration, updated,
                 set_code, link_to=None):
        self.link_to = link_to
        self.url = url
        self.path = path
        self.card_id = card_id
        self.face = face
        self.fmt = fmt
        self.illustration = illustration
        self.updated = updated
        self.set_code = set_code


def filename_for(card: dict, face_idx: int, multi: bool, fmt: str) -> str:
    """Nom de fichier d'une image. Source unique de vérité.

    Schéma : ``<code-set>-<numéro officiel>[-a|-b].<ext>``, ex.
    ``lea-186.jpg``. Le préfixe du set rend le nom globalement unique même
    sorti de son dossier — indispensable aux pages de carte qui agrègent
    plusieurs impressions issues de sets différents dans une même vue.

    Utilisé aussi bien pour poser le fichier réel que pour viser un lien :
    les deux DOIVENT produire exactement la même chaîne, faute de quoi les
    liens pointeraient dans le vide.
    """
    code = sanitize((card.get("set") or "xxx").lower())
    cn = sanitize(card.get("collector_number") or card["id"][:8])
    ext = ".png" if fmt == "png" else ".jpg"
    suffix = ""
    if multi:
        suffix = "-" + ("abcdefgh"[face_idx] if face_idx < 8 else f"f{face_idx}")
    return f"{code}-{cn}{suffix}{ext}"


def pick_quality(uris: dict, wanted: list[str]) -> tuple[str, str] | None:
    """Premier format disponible dans l'ordre de préférence — et on s'arrête.

    C'est précisément le point où l'ancien script se trompait : il parcourait
    la liste sans interrompre la boucle, et retenait donc systématiquement la
    dernière qualité examinée.
    """
    for q in wanted:
        if uris.get(q):
            return q, uris[q]
    return None


def image_targets(card: dict) -> list[tuple[int, dict, str | None]]:
    """Retourne [(index_face, image_uris, illustration_id)] pour une carte.

    Les cinq layouts double-face n'ont pas d'image_uris à la racine : leurs
    images sont portées par chaque face. Les autres layouts munis de
    card_faces (split, adventure, flip, prepare) sont une carte physique
    unique : une seule image.
    """
    if card.get("image_uris"):
        return [(0, card["image_uris"], card.get("illustration_id"))]
    out = []
    if card.get("layout") in DFC_LAYOUTS or not card.get("image_uris"):
        for i, face in enumerate(card.get("card_faces") or []):
            if face.get("image_uris"):
                out.append((i, face["image_uris"], face.get("illustration_id")))
    return out


def plan(cards_factory, cfg) -> tuple[list[Job], dict]:
    """Construit la liste des téléchargements et les statistiques associées.

    `cards_factory` doit être un appelable rendant un itérateur NEUF à chaque
    appel : la sélection du meilleur représentant par illustration impose deux
    passes sur le bulk, et un générateur simple serait épuisé dès la première.
    Relire le .gz coûte quelques dizaines de secondes mais évite de charger
    ~120 000 objets JSON en mémoire.
    """
    def in_scope(c: dict) -> bool:
        """Filtre partagé par les deux passes.

        Indispensable : si la passe 1 élit un représentant hors du périmètre
        demandé (--set / --lang), la passe 2 ne le rencontrera jamais et
        l'illustration serait perdue sans un mot.
        """
        if cfg.sets and (c.get("set") or "").lower() not in cfg.sets:
            return False
        if cfg.langs and (c.get("lang") or "").lower() not in cfg.langs:
            return False
        return True

    jobs: list[Job] = []
    seen_art: dict[str, str] = {}     # illustration_id -> meilleur card_id
    best_rank: dict[str, int] = {}
    stats = {"cartes": 0, "ignorées_langue": 0, "sans_image": 0,
             "doublons_art": 0, "faces": 0, "liens": 0,
             "liens_impossibles": 0}

    # Passe 1 : sélection du meilleur représentant par illustration.
    # On retient le meilleur scan disponible, puis l'anglais, puis la carte
    # la plus ancienne — de sorte qu'une illustration ne soit jamais
    # représentée par un placeholder si un highres_scan existe.
    # En mode `link`, le représentant n'est plus le meilleur scan mais la
    # PREMIÈRE parution chronologique : c'est là que l'octet réel se pose,
    # toutes les parutions ultérieures y renvoient par lien symbolique.
    # Chaque set possède ainsi une arborescence complète sans duplication.
    if cfg.unique in ("art", "link"):
        for c in cards_factory():
            if not in_scope(c):
                continue
            # Parcourir les MÊMES unités que la passe 2 : une carte
            # recto-verso porte un illustration_id par face, jamais à la
            # racine. Ne regarder que la racine ferait passer chaque face
            # pour un doublon, et supprimerait toutes les transform/mdfc.
            for face_idx, _uris, iid in image_targets(c):
                if not iid:
                    continue
                if cfg.unique == "link":
                    # tri croissant : date de sortie, puis set, puis numéro,
                    # pour un résultat déterministe entre exécutions
                    key = (c.get("released_at") or "9999-99-99",
                           c.get("set") or "zzz",
                           str(c.get("collector_number") or "").zfill(6),
                           c["id"])
                    if iid not in best_rank or key < best_rank[iid]:
                        best_rank[iid] = key
                        seen_art[iid] = (c["id"], face_idx)
                else:
                    score = (IMAGE_STATUS_RANK.get(c.get("image_status"), 0) * 100
                             + (10 if c.get("lang") == "en" else 0)
                             + (1 if c.get("highres_image") else 0))
                    if iid not in best_rank or score > best_rank[iid]:
                        best_rank[iid] = score
                        seen_art[iid] = (c["id"], face_idx)

    # Chemin du fichier réel de chaque illustration, indispensable pour
    # viser correctement les liens. Rempli au fil de la passe 2 : les
    # représentants sont rencontrés dans l'ordre du bulk, qui n'est pas
    # chronologique, d'où une pré-passe dédiée.
    repr_path: dict[str, Path] = {}
    if cfg.unique == "link":
        for c in cards_factory():
            if not in_scope(c):
                continue
            for face_idx, uris, iid in image_targets(c):
                if iid and seen_art.get(iid) == (c["id"], face_idx):
                    got = pick_quality(uris, qualities_for(cfg, set_code))
                    if not got:
                        continue
                    fmt, _url = got
                    repr_path[iid] = (cfg.images_dir
                                      / (c.get("set") or "unknown").lower()
                                      / filename_for(c, face_idx, len(
                                          image_targets(c)) > 1, fmt))

    for c in cards_factory():
        stats["cartes"] += 1
        set_code = (c.get("set") or "unknown").lower()

        if cfg.sets and set_code not in cfg.sets:
            continue

        # Filtrage linguistique : `default_cards` contient déjà l'anglais quand
        # il existe, et la langue d'origine sinon. On ne filtre donc que si
        # l'utilisateur l'a explicitement demandé, pour ne perdre aucune carte
        # exclusive à une langue (promos japonaises, exclusivités régionales).
        if cfg.langs and (c.get("lang") or "").lower() not in cfg.langs:
            stats["ignorées_langue"] += 1
            continue

        targets = image_targets(c)
        if not targets:
            stats["sans_image"] += 1
            log.debug(f"aucune image : {set_code}/{c.get('collector_number')} "
                      f"{c.get('name')} [{c.get('layout')}]")
            continue

        multi = len(targets) > 1

        for face_idx, uris, iid in targets:
            is_repr = (not iid) or seen_art.get(iid) == (c["id"], face_idx)

            # mode `art` : on jette purement et simplement les doublons.
            if cfg.unique == "art" and iid and not is_repr:
                stats["doublons_art"] += 1
                continue

            # mode `link` : le doublon devient un lien symbolique vers le
            # fichier réel, déposé dans le set de première parution.
            link_target = None
            if cfg.unique == "link" and iid and not is_repr:
                ref = repr_path.get(iid)
                if ref is None:
                    # Le représentant est hors périmètre (--set / --lang) :
                    # sans fichier réel à viser, on télécharge pour de bon
                    # plutôt que de créer un lien mort.
                    stats["liens_impossibles"] += 1
                else:
                    link_target = ref
                    stats["liens"] += 1

            got = pick_quality(uris, qualities_for(cfg, (c.get('set') or '').lower()))
            if not got:
                stats["sans_image"] += 1
                continue
            fmt, url = got

            path = cfg.images_dir / set_code / filename_for(c, face_idx,
                                                            multi, fmt)
            if multi:
                stats["faces"] += 1
            jobs.append(Job(url, path, c["id"], face_idx, fmt, iid,
                            c.get("image_updated_at") or c.get("released_at"),
                            set_code, link_to=link_target))
    return jobs, stats


# --------------------------------------------------------------- manifeste

MANIFEST_SQL = """
CREATE TABLE IF NOT EXISTS downloads (
    path         TEXT PRIMARY KEY,
    card_id      TEXT NOT NULL,
    face         INTEGER NOT NULL DEFAULT 0,
    set_code     TEXT,
    fmt          TEXT,
    illustration TEXT,
    bytes        INTEGER,
    src_updated  TEXT,
    fetched_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_dl_set  ON downloads(set_code);
CREATE INDEX IF NOT EXISTS idx_dl_card ON downloads(card_id);
"""


def open_manifest(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(MANIFEST_SQL)
    conn.commit()
    return conn


def already_done(conn: sqlite3.Connection) -> dict[str, tuple[int, str, str]]:
    """État connu de chaque fichier : taille, date source, ET qualité.

    La qualité fait partie de l'identité : sans elle, redemander `normal`
    après un passage en `large` ne changeait rien et l'utilisateur
    conservait silencieusement l'ancienne qualité.
    """
    return {r[0]: (r[1], r[2], r[3]) for r in
            conn.execute("SELECT path, bytes, src_updated, fmt FROM downloads")}


def adopt_orphans(conn: sqlite3.Connection, images_dir: Path) -> int:
    """Recense les images présentes sur disque mais absentes du manifeste.

    Sans cela, un manifeste perdu ou corrompu relancerait 120 000
    téléchargements. On les adopte avec fmt='?' : elles seront vérifiées,
    pas retéléchargées à l'aveugle.
    """
    known = {r[0] for r in conn.execute("SELECT path FROM downloads")}
    rows = []
    for f in images_dir.rglob("*"):
        if f.is_file() and not f.name.endswith(".part") and str(f) not in known:
            rows.append((str(f), "", 0, f.parent.name, "?", "", f.stat().st_size,
                         "", time.strftime("%Y-%m-%dT%H:%M:%S")))
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO downloads VALUES(?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
    return len(rows)


def purge_siblings(dest: Path) -> int:
    """Supprime les autres extensions du même numéro de collection.

    Un changement de qualité ne doit pas laisser cohabiter `280.jpg` et
    `280.png` : un seul fichier par carte, sinon les outils en aval ne
    savent plus lequel utiliser.
    """
    n = 0
    for other in dest.parent.glob(dest.stem + ".*"):
        if other != dest and other.suffix in (".jpg", ".png"):
            other.unlink(missing_ok=True)
            n += 1
    return n


def estimate_sizes(jobs, sample_n: int = 30) -> dict[str, float]:
    """Taille moyenne réelle de chaque qualité, par requêtes HEAD.

    Échantillonner plutôt que figer des constantes : les scans de Scryfall
    évoluent, et les vieux sets pèsent nettement plus que les récents. Un
    tirage aléatoire sur l'ensemble planifié donne un ordre de grandeur
    honnête pour quelques secondes de réseau.
    """
    import random as _r
    if not jobs:
        return {}
    _r.seed(1)
    pool = _r.sample(jobs, min(sample_n, len(jobs)))
    out: dict[str, list[int]] = {}
    for q in QUALITIES:
        sizes = []
        for j in pool:
            url = re.sub(r"/(%s)/" % "|".join(QUALITIES),
                         f"/{q}/", j.url)
            url = url.replace(".jpg", ".png") if q == "png" else \
                  url.replace(".png", ".jpg")
            try:
                req = urllib.request.Request(url, headers=HEADERS, method="HEAD")
                with urllib.request.urlopen(req, timeout=15) as r:
                    n = int(r.headers.get("Content-Length") or 0)
                    if n:
                        sizes.append(n)
            except Exception:                              # noqa: BLE001
                continue
        if sizes:
            out[q] = sum(sizes) / len(sizes)
    return out


def print_size_table(jobs, cfg) -> None:
    """Tableau comparatif de toutes les qualités pour le lot planifié."""
    n = len(jobs)
    log.info(f"sondage des tailles réelles sur un échantillon "
             f"(HEAD, quelques secondes)…")
    avg = estimate_sizes(jobs)
    if not avg:
        log.warn("sondage impossible (réseau) — pas d'estimation de volume")
        return
    print(f"\n  Volume estimé pour {n:,} image(s)".replace(",", " "),
          file=sys.stderr)
    print(f"  {'qualité':<13} {'dimensions':<12} {'moy/image':>10} {'TOTAL':>11}",
          file=sys.stderr)
    print("  " + "-" * 49, file=sys.stderr)
    for q in QUALITIES:
        if q not in avg:
            continue
        mark = " <-" if q == cfg.quality else ""
        print(f"  {q:<13} {QUALITY_DIMS.get(q, '?'):<12} "
              f"{avg[q]/1024:>8.0f} Kio {human(avg[q]*n):>11}{mark}",
              file=sys.stderr)
    if cfg.quality_for:
        print("\n  Surcharges par set :", file=sys.stderr)
        for code, fmt in sorted(cfg.quality_for.items()):
            k = sum(1 for j in jobs if j.set_code == code)
            print(f"    {code:<8} -> {fmt:<11} {k:>6} image(s) "
                  f"~{human(avg.get(fmt, 0)*k)}", file=sys.stderr)


def verify_files(conn, cfg) -> dict:
    """Contrôle l'état réel du disque : taille, en-tête, fichiers manquants."""
    rows = list(conn.execute("SELECT path, bytes, fmt FROM downloads"))
    missing = truncated = corrupt = ok = 0
    bad: list[str] = []
    for path, size, fmt in rows:
        f = Path(path)
        if not f.exists():
            missing += 1
            bad.append(path)
            continue
        real = f.stat().st_size
        if size and real != size:
            truncated += 1
            bad.append(path)
            continue
        head = f.open("rb").read(8)
        is_png = head[:4] == b"\x89PNG"
        is_jpg = head[:2] == b"\xff\xd8"
        if not (is_png or is_jpg):
            corrupt += 1
            bad.append(path)
            continue
        ok += 1
    orphans = sum(1 for f in cfg.images_dir.rglob("*.part"))
    log.info(f"vérification : {ok} sain(s), {missing} manquant(s), "
             f"{truncated} de taille incohérente, {corrupt} illisible(s), "
             f"{orphans} fragment(s) .part")
    if bad:
        report = cfg.data_dir / "verify_failed.txt"
        report.write_text("\n".join(bad), encoding="utf-8")
        log.warn(f"{len(bad)} fichier(s) à reprendre — liste : {report}")
        log.info("relancer la commande de téléchargement les récupérera")
    return {"ok": ok, "bad": len(bad)}


# ------------------------------------------------------------ téléchargement

def make_symlink(link: Path, target: Path) -> bool:
    """Crée un lien symbolique RELATIF vers `target`.

    Relatif et non absolu : l'arborescence reste déplaçable d'un disque à
    l'autre sans casser les milliers de liens.
    """
    link.parent.mkdir(parents=True, exist_ok=True)
    rel = os.path.relpath(target, link.parent)
    if link.is_symlink():
        if os.readlink(link) == rel:
            return False                     # déjà correct
        link.unlink()
    elif link.exists():
        return False                         # fichier réel déjà en place
    link.symlink_to(rel)
    return True


def run_downloads(jobs: list[Job], cfg, conn: sqlite3.Connection) -> dict:
    done = already_done(conn)
    todo: list[Job] = []
    skipped = 0
    upgraded = 0

    # Les liens sont traités APRÈS les téléchargements : leur cible doit
    # exister, sinon on sème des liens morts.
    links = [j for j in jobs if j.link_to is not None]
    jobs = [j for j in jobs if j.link_to is None]

    for j in jobs:
        key = str(j.path)
        rec = done.get(key)
        if rec and j.path.exists() and j.path.stat().st_size == rec[0]:
            same_fmt = rec[2] in (j.fmt, "?")   # '?' = fichier adopté
            fresher = j.updated and rec[1] and j.updated > rec[1]
            if same_fmt and not fresher:
                skipped += 1
                continue
            if not same_fmt:
                upgraded += 1
        todo.append(j)

    total = len(todo)
    if upgraded:
        log.info(f"{upgraded} image(s) à remplacer (qualité différente)")
    log.info(f"{total} image(s) à télécharger, {skipped} déjà à jour"
             + (f", {len(links)} lien(s) à poser" if links else ""))
    if cfg.dry_run or (not total and not links):
        return {"téléchargées": 0, "ignorées": skipped, "échecs": 0,
                "octets": 0, "liens": 0}

    counters = {"n": 0, "bytes": 0, "fail": 0}
    lock = threading.Lock()
    t0 = time.time()
    pending: list[tuple] = []

    # Le CDN renvoie des 503 sous forte concurrence. Sur 120 000 images, un
    # taux d'échec de 2 % laisserait des milliers de cartes manquantes : la
    # patience ici vaut mieux que la vitesse.
    RETRYABLE = {429, 500, 502, 503, 504, 408}

    def work(j: Job):
        last = None
        for attempt in range(6):
            try:
                size = download_file(j.url, j.path)
                purge_siblings(j.path)
                return j, size, None
            except urllib.error.HTTPError as e:
                last = e
                if e.code not in RETRYABLE:
                    return j, 0, e            # 404 : inutile d'insister
                wait = float(e.headers.get("Retry-After") or 0) or (2 ** attempt)
            except Exception as e:                        # noqa: BLE001
                last = e
                wait = 2 ** attempt
            if attempt == 5:
                break
            # jitter : évite que les 8 threads ne repartent en même temps et
            # ne reproduisent le pic qui a déclenché le 503
            time.sleep(min(wait, 30) + random.uniform(0, 0.75))
        return j, 0, last or RuntimeError("inatteignable")

    # Les images sont servies par *.scryfall.io, qui n'impose pas de limite de
    # débit : la concurrence est donc légitime ici, contrairement aux appels
    # à api.scryfall.com.
    with cf.ThreadPoolExecutor(max_workers=cfg.jobs) as pool:
        for j, size, err in pool.map(work, todo):
            with lock:
                counters["n"] += 1
                if err:
                    counters["fail"] += 1
                    # un échec isolé ne doit jamais interrompre la campagne
                    log.warn(f"{j.set_code}/{j.path.name} : {err}")
                else:
                    counters["bytes"] += size
                    pending.append((str(j.path), j.card_id, j.face, j.set_code,
                                    j.fmt, j.illustration, size, j.updated,
                                    time.strftime("%Y-%m-%dT%H:%M:%S")))
                n = counters["n"]
                if len(pending) >= 200:
                    conn.executemany(
                        "INSERT OR REPLACE INTO downloads VALUES(?,?,?,?,?,?,?,?,?)",
                        pending)
                    conn.commit()
                    pending.clear()
                if n % 50 == 0 or n == total:
                    el = time.time() - t0
                    rate = n / el if el else 0
                    eta = (total - n) / rate if rate else 0
                    pct = 100 * n / total
                    print(f"\r  {n}/{total} ({pct:5.1f}%)  "
                          f"{human(counters['bytes'])}  "
                          f"{rate:.1f} img/s  reste {human_time(eta)}   ",
                          end="", file=sys.stderr, flush=True)

    if pending:
        conn.executemany(
            "INSERT OR REPLACE INTO downloads VALUES(?,?,?,?,?,?,?,?,?)", pending)
        conn.commit()
    print(file=sys.stderr)

    # Liens symboliques, une fois les cibles réellement présentes.
    n_links = n_dead = 0
    for j in links:
        if not j.link_to.exists():
            n_dead += 1
            log.debug(f"cible absente, lien ignoré : {j.path.name} "
                      f"-> {j.link_to}")
            continue
        try:
            if make_symlink(j.path, j.link_to):
                n_links += 1
        except OSError as e:
            n_dead += 1
            log.warn(f"lien {j.path} : {e}")
    if links:
        log.ok(f"{n_links} lien(s) créé(s)"
               + (f", {n_dead} ignoré(s) faute de cible" if n_dead else ""))

    return {"téléchargées": counters["n"] - counters["fail"],
            "ignorées": skipped, "échecs": counters["fail"],
            "octets": counters["bytes"], "liens": n_links}


# --------------------------------------------------------------- icônes set

def download_set_icons(cfg, conn) -> int:
    """Récupère les icônes SVG des sets (utiles aux en-têtes du catalogue)."""
    data = api_get(f"{API}/sets")
    n = 0
    for s in data["data"]:
        if cfg.sets and s["code"].lower() not in cfg.sets:
            continue
        uri = s.get("icon_svg_uri")
        if not uri:
            continue
        dest = cfg.icons_dir / s["code"].lower() / f"{s['code'].lower()}.svg"
        if dest.exists():
            continue
        try:
            download_file(uri, dest)
            n += 1
        except Exception as e:                            # noqa: BLE001
            log.warn(f"icône {s['code']} : {e}")
    return n


# --------------------------------------------------------------------- CLI

class Cfg:
    pass


def qualities_for(cfg, set_code: str) -> list[str]:
    """Chaîne de qualités à essayer pour ce set, la voulue en tête.

    Permet `--quality large --quality-for png:mh3,blb` : tout en large,
    sauf ces sets-là en png.
    """
    want = cfg.quality_for.get(set_code, cfg.quality)
    if cfg.strict:
        return [want]
    return [want] + [q for q in QUALITIES if q != want]


def build_cfg(a) -> Cfg:
    c = Cfg()
    c.data_dir = Path(a.data_dir).expanduser().resolve()
    c.images_dir = c.data_dir / "sets"
    c.icons_dir = c.data_dir / "icons"
    c.meta_dir = c.data_dir / "metadata"
    c.manifest = c.data_dir / "images.sqlite3"
    c.quality = a.quality
    c.strict = a.strict_quality
    # Surcharges par set : --quality-for png:mh3,blb
    c.quality_for: dict[str, str] = {}
    for spec in (a.quality_for or []):
        if ":" not in spec:
            raise SystemExit(f"--quality-for attend FORMAT:set1,set2 (reçu {spec!r})")
        fmt, codes = spec.split(":", 1)
        fmt = fmt.strip().lower()
        if fmt not in QUALITIES:
            raise SystemExit(f"qualité inconnue dans --quality-for : {fmt}")
        for code in codes.split(","):
            if code.strip():
                c.quality_for[code.strip().lower()] = fmt
    c.verify = getattr(a, "verify", False)
    c.jobs = a.jobs
    c.dry_run = a.dry_run
    c.unique = a.unique
    c.sets = {s.lower() for s in a.set} if a.set else None
    c.langs = {l.lower() for l in a.lang} if a.lang else None
    return c



# ==================================================================
# SITE WEB (ex-mtgc-web)
# ==================================================================

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
            # DFC : extraire les deux faces complètes (nom, type, mana, oracle,
            # image). Le champ `back` (nom de fichier) est conservé pour compat ;
            # `faces` porte les données riches, base de tous les affichages.
            faces = None
            layout = c.get("layout") or ""
            if layout in DFC_LAYOUTS and c.get("card_faces") and back:
                cf = c["card_faces"]
                faces = []
                for fi, fa in enumerate(cf[:2]):
                    faces.append({
                        "name": fa.get("name") or "?",
                        "type": fa.get("type_line") or "",
                        "mana": fa.get("mana_cost") or "",
                        "oracle": fa.get("oracle_text") or "",
                        "img": img if fi == 0 else back,
                    })
            entry = {
                "oid": oid, "cn": cn, "name": c.get("name") or "?",
                "rarity": c.get("rarity") or "", "artist": c.get("artist") or "",
                "type": c.get("type_line") or "", "mana": c.get("mana_cost") or "",
                "cmc": c.get("cmc"), "colors": "".join(c.get("colors") or []),
                "oracle_text": c.get("oracle_text") or "",
                "set_code": code, "img": img, "back": back, "faces": faces,
                "layout": layout,
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
                     "img": fn, "back": None, "faces": None, "layout": "",
                     "released_at": ""}
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
.setnavbar{{display:flex;align-items:center;justify-content:space-between;
gap:12px;margin-bottom:22px;padding:10px 14px;background:var(--panel);
border:1px solid var(--bd);border-radius:10px}}
.setnav{{font-family:ui-monospace,monospace;font-size:13px;color:var(--gold);
padding:4px 10px;border-radius:6px;white-space:nowrap}}
.setnav:hover{{background:var(--panel2);color:var(--ink)}}
.setnav.off{{color:var(--ink-faint);opacity:.35;pointer-events:none}}
.setnavmid{{font-size:11px;color:var(--inkfaint);letter-spacing:.04em}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));
gap:16px}}
.setcard{{background:var(--panel);border:1px solid var(--bd);border-radius:12px;
overflow:hidden;transition:border-color .15s,transform .15s}}
.setcard:hover{{border-color:var(--gold);transform:translateY(-2px)}}
.setcard .bar{{height:3px;background:var(--gold)}}
.setcard .body{{padding:16px 18px}}
.setcard .head{{display:flex;align-items:center;gap:12px;margin-bottom:13px}}
.icon{{width:56px;height:56px;border-radius:10px;background:var(--bg);
border:1px solid var(--bd);display:flex;align-items:center;
justify-content:center;flex-shrink:0;color:var(--gold);font-size:38px}}
.icon.svgicon,.bigicon.svgicon{{padding:8px;object-fit:contain;
filter:brightness(0) saturate(100%) invert(72%) sepia(38%) saturate(560%)
hue-rotate(2deg) brightness(92%) contrast(90%)}}
.fallback{{font-weight:700;font-size:18px;font-family:Georgia,serif}}
.setname{{font-size:15px;font-weight:500;color:var(--ink);line-height:1.25}}
.setcode{{font-size:11px;color:var(--golddim);font-family:ui-monospace,
"SF Mono",Menlo,monospace;letter-spacing:.05em;text-transform:uppercase}}
.setfoot{{display:flex;justify-content:space-between;align-items:center;
font-size:12px;border-top:1px solid var(--bd);padding-top:11px}}
.setfoot .date{{color:var(--inkdim)}}
.setfoot .cnt{{color:var(--gold);font-weight:500;font-size:15px}}
.sethdr{{display:flex;align-items:center;gap:20px;margin-bottom:8px}}
.sethdr .bigicon{{width:104px;height:104px;border-radius:14px;
background:var(--panel);border:1px solid var(--bd);display:flex;
align-items:center;justify-content:center;flex-shrink:0;color:var(--gold);
font-size:72px}}
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
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
gap:16px}}
.card{{position:relative}}
.card img{{width:100%;border-radius:11px;display:block;
background:var(--panel);border:1px solid var(--bd);aspect-ratio:488/680;
object-fit:cover;cursor:pointer}}
.card img:hover{{border-color:var(--gold)}}
.card{{position:relative}}
.flipbadge{{position:absolute;top:6px;right:6px;width:28px;height:28px;
border-radius:50%;background:rgba(20,16,10,.85);border:1px solid var(--gold);
color:var(--gold);font-size:15px;cursor:pointer;display:flex;
align-items:center;justify-content:center;padding:0;line-height:1;z-index:2}}
.flipbadge:hover{{background:var(--gold);color:var(--bg)}}
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
.herowrap{{position:relative;display:flex;flex-direction:column;gap:10px}}
#heroflip{{background:var(--panel);border:1px solid var(--gold);
color:var(--gold);border-radius:8px;padding:8px 14px;font-size:14px;
cursor:pointer;font-family:inherit}}
#heroflip:hover{{background:var(--gold);color:var(--bg)}}
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
.print .pl .ss,.print .pl .svgicon{{font-size:18px;width:20px;height:20px;
color:var(--gold)}}
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
#vwx:hover{{color:var(--gold)}}
/* ---- panneau de survol (points 1 & 3) ---- */
#hp{{position:fixed;inset:0;display:none;align-items:center;
justify-content:center;z-index:800;pointer-events:none;
background:rgba(10,8,4,.55)}}
#hp.on{{display:flex}}
#hpcard{{position:relative;display:flex;flex-direction:column;gap:18px;
max-width:720px;max-height:92vh;overflow-y:auto;background:var(--panel);
border:1px solid var(--gold);border-radius:16px;padding:22px;
box-shadow:0 12px 60px rgba(0,0,0,.7)}}
.hppos{{position:absolute;top:12px;right:16px;font-family:ui-monospace,monospace;
font-size:12px;color:var(--golddim);letter-spacing:.05em}}
.hpface{{display:flex;gap:18px}}
.hpimg{{width:450px;border-radius:12px;flex-shrink:0;align-self:flex-start}}
.hpinfo{{display:flex;flex-direction:column;min-width:0;max-width:220px}}
.hphead{{display:flex;align-items:baseline;gap:8px;margin-bottom:4px;
flex-wrap:wrap}}
.hpname{{font-family:Georgia,serif;font-size:19px;color:var(--ink);
line-height:1.2}}
.hpmana .ms-cost{{font-size:14px}}
.hptype{{font-size:12px;color:var(--inkdim);margin-bottom:10px}}
.hporacle{{font-size:13px;line-height:1.6;color:var(--ink);
white-space:pre-wrap}}
.hprule{{padding:10px 14px;background:var(--panel2);
border-left:3px solid var(--gold);border-radius:0 8px 8px 0;font-size:12px;
color:var(--inkdim)}}
.hprule .hprdate{{color:var(--golddim);font-family:ui-monospace,monospace;
font-size:11px;margin-bottom:4px}}
@media(max-width:800px){{.hpface{{flex-direction:column}}
.hpimg{{width:min(70vw,320px);align-self:center}}
.hpinfo{{max-width:none}}}}"""


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


# Codes couverts par la fonte keyrune (remplie à la génération). Les autres
# retombent sur le SVG Scryfall téléchargé dans assets/seticons/.
KEYRUNE_CODES: set[str] = set()
SVG_ICON_CODES: set[str] = set()


def set_icon(code: str, cls: str = "icon") -> str:
    code = code.lower()
    if code in KEYRUNE_CODES:
        return f'<i class="ss ss-{esc(code)} {cls}"></i>'
    if code in SVG_ICON_CODES:
        return (f'<img class="{cls} svgicon" '
                f'src="assets/seticons/{esc(code)}.svg" alt="{esc(code)}">')
    # dernier recours : initiales
    return f'<span class="{cls} fallback">{esc(code[:2].upper())}</span>'


def mana_html(cost: str) -> str:
    if not cost:
        return ""
    out = []
    for sym in re.findall(r"\{([^}]+)\}", cost):
        key = sym.lower().replace("/", "")
        out.append(f'<i class="ms ms-{key} ms-cost"></i>')
    return f'<span class=mana>{"".join(out)}</span>'


# ---------------------------------------------------------- pages

INDEX_JS = """<script>
const items=[...document.querySelectorAll('.setcard')];
const grid=document.getElementById('grid');
const cnt=document.getElementById('count');
const emptyEl=document.getElementById('empty');
const qEl=document.getElementById('q');
const sortEl=document.getElementById('sort');
// Mémoire de session : le tri et la recherche survivent à un aller-retour
// dans une extension. sessionStorage = le temps de l'onglet, pas au-delà.
try{
  const sv=sessionStorage.getItem('mtgc_sort');
  if(sv)sortEl.value=sv;
  const qv=sessionStorage.getItem('mtgc_q');
  if(qv)qEl.value=qv;
}catch(e){}
function flt(){
  const q=qEl.value.toLowerCase().trim();
  const sort=sortEl.value;
  try{
    sessionStorage.setItem('mtgc_sort',sort);
    sessionStorage.setItem('mtgc_q',qEl.value);
  }catch(e){}
  let vis=0;
  for(const it of items){
    const show=!q||it.dataset.name.includes(q)||it.dataset.code.includes(q);
    it.style.display=show?'':'none';if(show)vis++;
  }
  const shown=items.filter(it=>it.style.display!=='none');
  const [key,dir]=sort.split('-');
  shown.sort((a,b)=>{
    let d;
    if(key==='name')d=a.dataset.name.localeCompare(b.dataset.name);
    else if(key==='count')d=(+a.dataset.count)-(+b.dataset.count);
    else d=a.dataset.date.localeCompare(b.dataset.date);
    return dir==='desc'?-d:d;
  });
  for(const it of shown)grid.appendChild(it);
  cnt.textContent=vis+' / '+items.length;
  emptyEl.style.display=vis?'none':'';
}
flt();
</script>"""


def render_index(sets, favicon: str) -> str:
    total = sum(s["count"] for s in sets)
    o = [head("MTGcyCLAUDEpedia", favicon), "<div class=wrap>"]
    o.append(f"<div class=top><div class=clock>{CLOCK_SVG}</div>"
             f"<div class=brand>MTGcyCLAUDEpedia</div>"
             f"<div class=sub>{len(sets)} extension"
             f"{'s' if len(sets) > 1 else ''} · {total} cartes</div></div>")
    # barre de recherche + tri
    o.append(
        "<div class=tools>"
        "<input id=q placeholder='Rechercher une extension…' oninput=flt()>"
        "<select id=sort onchange=flt()>"
        "<option value=date-desc>Plus récentes d'abord</option>"
        "<option value=date-asc>Plus anciennes d'abord</option>"
        "<option value=name-asc>Nom A→Z</option>"
        "<option value=name-desc>Nom Z→A</option>"
        "<option value=count-desc>Plus de cartes</option>"
        "<option value=count-asc>Moins de cartes</option>"
        "</select><span class=count id=count></span></div>")
    o.append("<div class=grid id=grid>")
    for s in sets:
        o.append(
            f"<a class=setcard href='set-{slug(s['code'])}.html' "
            f"data-name=\"{esc(s['name'].lower())}\" "
            f"data-code=\"{esc(s['code'])}\" "
            f"data-date=\"{esc(s['released_at'] or '0000')}\" "
            f"data-count=\"{s['count']}\">"
            f"<div class=bar></div><div class=body>"
            f"<div class=head>{set_icon(s['code'])}"
            f"<div><div class=setname>{esc(s['name'])}</div>"
            f"<div class=setcode>{esc(s['code'])}</div></div></div>"
            f"<div class=setfoot><span class=date>{s['released_at'] or '—'}</span>"
            f"<span class=cnt>{s['count']}</span></div></div></a>")
    o.append("</div>")
    o.append("<div class=empty id=empty style=display:none>"
             "Aucune extension ne correspond.</div>")
    o.append("<div class=foot>MTGcyCLAUDEpedia · généré localement · "
             "icônes keyrune + mana © Andrew Gioia (SIL OFL) · "
             "icônes de repli et cartes © Wizards of the Coast via Scryfall</div>")
    o.append("</div>")
    o.append(INDEX_JS)
    o.append("</body></html>")
    return "".join(o)


def render_set(s, cards, favicon: str, card_pages: bool,
               prev=None, nxt=None, rulings=None) -> str:
    rulings = rulings or {}
    o = [head(f"{s['name']} — MTGcyCLAUDEpedia", favicon), "<div class=wrap>"]
    o.append(f"<div class=top><div class=clock>{CLOCK_SVG}</div>"
             f"<div class=brand><a href='index.html'>MTGcyCLAUDEpedia</a>"
             f"</div></div>")
    o.append("<div class=crumb><a href='index.html'>Extensions</a> "
             f"&rsaquo; {esc(s['name'])}</div>")
    # navigation chronologique entre extensions
    prev_html = (f"<a class=setnav href='set-{slug(prev['code'])}.html' "
                 f"title=\"{esc(prev['name'])}\">"
                 f"&lsaquo; {esc(prev['code'].upper())}</a>"
                 if prev else "<span class='setnav off'>&lsaquo;</span>")
    next_html = (f"<a class=setnav href='set-{slug(nxt['code'])}.html' "
                 f"title=\"{esc(nxt['name'])}\">"
                 f"{esc(nxt['code'].upper())} &rsaquo;</a>"
                 if nxt else "<span class='setnav off'>&rsaquo;</span>")
    o.append(f"<div class=setnavbar>{prev_html}"
             f"<span class=setnavmid>ordre chronologique</span>"
             f"{next_html}</div>")
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
    # Virtualisation : les cartes vivent en JSON, pas en 460 balises <img>.
    # Le DOM ne contient que les cartes visibles (voir SET_JS). Sur une page
    # de plusieurs centaines de cartes, c'est ce qui garde le défilement fluide.
    data = []
    for c in cards:
        img = f"sets/{c['set_code']}/{c['img']}"
        cmc = (0 if c["cmc"] is None
               else int(c["cmc"]) if c["cmc"] == int(c["cmc"]) else c["cmc"])
        has_page = card_pages and not c["oid"].startswith("noid-")
        # oracle + dernier ruling, embarqués pour le panneau de survol (point D).
        # Seule option compatible file:// hors-ligne : tout dans la page.
        rr = rulings.get(c["oid"]) or []
        last_ruling = rr[0] if rr else None
        # DFC : les deux faces (nom, type, oracle, image) pour le survol empilé
        # et le flip. Chaque face porte son image sous sets/<code>/.
        faces_js = None
        if c.get("faces"):
            faces_js = [{"name": f["name"], "type": f["type"],
                         "mana": f["mana"], "oracle": f["oracle"],
                         "src": f"sets/{c['set_code']}/{f['img']}"}
                        for f in c["faces"]]
        data.append({
            "src": img, "name": c["name"], "cn": c["cn"],
            "n": c["name"].lower(), "t": c["type"].lower(),
            "a": c["artist"].lower(), "r": c["rarity"],
            "cmc": cmc, "dot": RARITY_COLOR.get(c["rarity"], THEME["ink_faint"]),
            "href": (f"card-{slug(c['oid'])}.html#{c['set_code']}"
                     if has_page else ""),
            "type": c["type"], "mana": c.get("mana", ""),
            "oracle": c.get("oracle_text", ""),
            "rule": ({"d": last_ruling["date"], "t": last_ruling["text"]}
                     if last_ruling else None),
            "faces": faces_js,
        })
    o.append("<div class=cards id=cards></div>")
    o.append("<div class=empty id=empty style=display:none>"
             "Aucune carte ne correspond au filtre.</div>")
    o.append(f"<div class=foot>{s['count']} cartes · MTGcyCLAUDEpedia</div>")
    o.append("</div>")
    o.append(hover_panel_html())
    o.append(viewer_html())
    prev_url = f"set-{slug(prev['code'])}.html" if prev else ""
    next_url = f"set-{slug(nxt['code'])}.html" if nxt else ""
    o.append(f"<script>const VIEW={json.dumps(data, ensure_ascii=False)};"
             f"const SET_PREV={json.dumps(prev_url)};"
             f"const SET_NEXT={json.dumps(next_url)};</script>")
    o.append(SET_JS)
    o.append(VIEWER_JS)
    o.append(SET_NAV_JS)
    o.append(HOVER_JS)
    o.append("</body></html>")
    return "".join(o)


def render_card(group, rulings, favicon: str, origin_set=None,
                neighbors=None) -> str:
    # BUG corrigé : afficher en premier l'impression du set d'où l'on vient,
    # pas la plus ancienne. Si on arrive de la page 5ED, le hero est la 5ED.
    ordered = group
    if origin_set:
        here = [c for c in group if c["set_code"] == origin_set]
        rest = [c for c in group if c["set_code"] != origin_set]
        ordered = here + rest
    group = ordered
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
    o.append("<div class=herowrap>")
    o.append(f"<img class=hero id=hero src='{hero}' alt=\"{esc(first['name'])}\" "
             f"onclick='vwOpen(0)'>")
    if first.get("faces"):
        o.append("<button id=heroflip onclick='cardFlip()'>"
                 "&#8646; Retourner</button>")
    o.append("</div>")
    o.append("<div class=cardinfo id=cardinfo>")
    if first.get("faces"):
        # DFC : le JS remplit cardinfo selon la face courante. On pré-remplit
        # avec la face 0 pour l'affichage sans JS et le SEO.
        fa = first["faces"][0]
        o.append(f"<h1 id=cname>{esc(fa['name'])}"
                 f"<span id=cmana>{mana_html(fa['mana'])}</span></h1>")
        o.append(f"<div class=tl id=ctype>{esc(fa['type'])}</div>")
        o.append(f"<div class=oracle id=coracle>{esc(fa['oracle'])}</div>")
    else:
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
        # Point 2 (option simple) : le survol montre l'image + set + numéro,
        # sans oracle (déjà affiché en haut de la page). On réutilise hovIn :
        # 'type' porte "SET · numéro", pas d'oracle ni de rule.
        faces_js = None
        if c.get("faces"):
            faces_js = [{"name": f["name"], "type": f["type"], "mana": f["mana"],
                         "oracle": f["oracle"],
                         "src": f"sets/{c['set_code']}/{f['img']}"}
                        for f in c["faces"]]
        viewer_list.append({
            "src": img, "name": c["name"], "set": c["set_code"],
            "cn": f"{c['set_code'].upper()} {c['cn']}",
            "type": f"{c['set_code'].upper()} · {c['cn']}",
            "mana": "", "oracle": "", "rule": None, "faces": faces_js,
        })
        o.append(
            f"<div class=print><img loading=lazy src='{img}' "
            f"alt=\"{esc(c['set_code'])} {esc(c['cn'])}\" "
            f"onclick='vwOpen({i})' onmouseenter='hovIn({i})' "
            f"onmouseleave='hovOut()'>"
            f"<div class=pl>{set_icon(c['set_code'], '')}"
            f"<a href='set-{slug(c['set_code'])}.html'>"
            f"{esc(c['set_code'].upper())}</a> · {esc(c['cn'])}</div></div>")
    o.append("</div></div>")
    o.append(f"<div class=foot>{first['name']} · {len(group)} impression"
             f"{'s' if len(group) > 1 else ''} · MTGcyCLAUDEpedia</div>")
    o.append("</div>")
    o.append(hover_panel_html())
    o.append(viewer_html())
    hero_faces = None
    if first.get("faces"):
        hero_faces = [{"name": f["name"], "type": f["type"], "mana": f["mana"],
                       "oracle": f["oracle"],
                       "src": f"sets/{first['set_code']}/{f['img']}"}
                      for f in first["faces"]]
    o.append(f"<script>const VIEW={json.dumps(viewer_list, ensure_ascii=False)};"
             f"const NEIGHBORS={json.dumps(neighbors or {}, ensure_ascii=False)};"
             f"const HERO_FACES={json.dumps(hero_faces, ensure_ascii=False)};"
             f"</script>")
    o.append(CARD_HERO_JS)
    o.append(CARD_FLIP_JS)
    o.append(VIEWER_JS)
    o.append(CARD_NAV_JS)
    o.append(HOVER_JS)
    o.append("</body></html>")
    return "".join(o)


CARD_HERO_JS = """<script>
// Si on arrive avec #<set>, afficher l'impression de ce set en grand.
(function(){
  const h=location.hash.replace('#','').toLowerCase();
  if(!h||typeof VIEW==='undefined')return;
  const i=VIEW.findIndex(v=>(v.set||'').toLowerCase()===h);
  if(i>0){
    const hero=document.querySelector('img.hero');
    if(hero){hero.src=VIEW[i].src;hero.setAttribute('onclick','vwOpen('+i+')');}
  }
})();
</script>"""


CARD_FLIP_JS = """<script>
// Flip du hero sur une page de carte DFC. Bascule image + nom + type + oracle
// entre les deux faces. HERO_FACES est null si la carte n'est pas une DFC.
(function(){
  if(typeof HERO_FACES==='undefined'||!HERO_FACES)return;
  let f=0;
  function manaHTML(cost){
    if(!cost)return '';
    return (cost.match(/\\{([^}]+)\\}/g)||[]).map(s=>{
      const k=s.slice(1,-1).toLowerCase().replace(/\\//g,'');
      return '<i class="ms ms-'+k+' ms-cost"></i>';
    }).join('');
  }
  function escHtml(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML;}
  window.cardFlip=function(){
    f=f?0:1;
    const fa=HERO_FACES[f];
    const hero=document.getElementById('hero');
    if(hero)hero.src=fa.src;
    const nm=document.getElementById('cname');
    if(nm)nm.innerHTML=escHtml(fa.name)+'<span id=cmana>'+manaHTML(fa.mana)+'</span>';
    const ty=document.getElementById('ctype');if(ty)ty.textContent=fa.type||'';
    const or=document.getElementById('coracle');if(or)or.textContent=fa.oracle||'';
  };
})();
</script>"""


CARD_NAV_JS = """<script>
// Navigation clavier entre CARTES d'un meme set (point B). Sur une page de
// carte ouverte avec #<set>, gauche/droite sautent a la page de carte de la
// carte precedente/suivante DU MEME set, par numero de collection. La 1re
// carte du set n'a pas de gauche, la derniere pas de droite. Ignore si le
// viewer plein ecran est ouvert ou si le focus est dans un champ.
(function(){
  const h=location.hash.replace('#','').toLowerCase();
  if(!h||typeof NEIGHBORS==='undefined'||!NEIGHBORS[h])return;
  const nb=NEIGHBORS[h];
  document.addEventListener('keydown',function(e){
    const vw=document.getElementById('vw');
    if(vw&&vw.classList.contains('on'))return;
    const tag=(e.target.tagName||'').toLowerCase();
    if(tag==='input'||tag==='select'||tag==='textarea')return;
    if(e.key==='ArrowLeft'&&nb.prev){location.href=nb.prev;}
    else if(e.key==='ArrowRight'&&nb.next){location.href=nb.next;}
  });
})();
</script>"""


def hover_panel_html() -> str:
    """Panneau de survol (points 1 & D). Conteneur rempli par le JS : image
    en grand (450px), numérotation « n/total », texte oracle étroit. Pour les
    DFC, les deux faces sont empilées, chacune avec son propre oracle. Repli
    tactile : aucun survol, le clic ouvre le viewer."""
    return "<div id=hp><div id=hpcard></div></div>"


HOVER_JS = """<script>
// Panneau de survol (points 1 & 3). Après ~200 ms de survol d'une vignette,
// affiche au centre : l'image en grand (450px), la numérotation n/total, le
// texte oracle (colonne étroite), le dernier ruling. Pour une carte DFC, les
// deux faces sont empilées, chacune avec son nom/type/oracle propre.
(function(){
  const hp=document.getElementById('hp');
  const card=document.getElementById('hpcard');
  let timer=null;
  function esc(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML;}
  function manaHTML(cost){
    if(!cost)return '';
    return (cost.match(/\\{([^}]+)\\}/g)||[]).map(s=>{
      const k=s.slice(1,-1).toLowerCase().replace(/\\//g,'');
      return '<i class="ms ms-'+k+' ms-cost"></i>';
    }).join('');
  }
  // Un « côté » = une image + un bloc texte (nom, mana, type, oracle).
  function faceBlock(src,name,mana,type,oracle){
    return '<div class=hpface>'
      +'<img class=hpimg src="'+src+'" alt="'+esc(name)+'">'
      +'<div class=hpinfo><div class=hphead><span class=hpname>'+esc(name)
      +'</span><span class=hpmana>'+manaHTML(mana)+'</span></div>'
      +'<div class=hptype>'+esc(type||'')+'</div>'
      +(oracle?'<div class=hporacle>'+esc(oracle)+'</div>':'')
      +'</div></div>';
  }
  window.hovIn=function(idx){
    const list=(typeof filtered!=='undefined'?filtered:VIEW);
    const c=list[idx];if(!c)return;
    clearTimeout(timer);
    timer=setTimeout(function(){
      let html='';
      // numérotation n/total sur la liste courante (ex. 003/253)
      const pos=String(idx+1).padStart(3,'0')+'/'+String(list.length).padStart(3,'0');
      html+='<div class=hppos>'+pos+'</div>';
      if(c.faces&&c.faces.length){
        // DFC : les deux faces empilées, chacune son oracle
        html+=c.faces.map(f=>faceBlock(f.src,f.name,f.mana,f.type,f.oracle)).join('');
      }else{
        html+=faceBlock(c.src,c.name,c.mana,c.type,c.oracle);
      }
      if(c.rule){
        html+='<div class=hprule><div class=hprdate>Dernier ruling — '
          +esc(c.rule.d)+'</div>'+esc(c.rule.t)+'</div>';
      }
      card.innerHTML=html;
      hp.classList.add('on');
    },200);
  };
  window.hovOut=function(){clearTimeout(timer);hp.classList.remove('on');};
})();
</script>"""


def viewer_html() -> str:
    return ("<div id=vw><button id=vwx onclick=vwClose()>&times;</button>"
            "<div id=vwcap></div><div id=vwstage>"
            "<img id=vwimg src='' alt=''></div>"
            "<div id=vwbar>"
            "<button onclick=vwPrev() title='Précédent (←)'>&lsaquo;</button>"
            "<button onclick=vwZoom(-1) title='Zoom -'>&minus;</button>"
            "<button onclick=vwZoom(1) title='Zoom +'>+</button>"
            "<button onclick=vwRot() title='Rotation (r)'>&#8635;</button>"
            "<button id=vwflip onclick=vwFlip() title='Retourner (f)' "
            "style='display:none'>&#8646;</button>"
            "<button onclick=vwReset() title='Réinitialiser'>&#9633;</button>"
            "<button onclick=vwNext() title='Suivant (→)'>&rsaquo;</button>"
            "</div></div>")


SET_NAV_JS = """<script>
// Navigation clavier entre EXTENSIONS (point A). Viewer ferme uniquement :
// gauche = extension precedente (chrono), droite = suivante. Ignore si le
// focus est dans un champ, ou si le viewer plein ecran est ouvert.
(function(){
  document.addEventListener('keydown',function(e){
    const vw=document.getElementById('vw');
    if(vw&&vw.classList.contains('on'))return;
    const tag=(e.target.tagName||'').toLowerCase();
    if(tag==='input'||tag==='select'||tag==='textarea')return;
    if(e.key==='ArrowLeft'&&SET_PREV){location.href=SET_PREV;}
    else if(e.key==='ArrowRight'&&SET_NEXT){location.href=SET_NEXT;}
  });
})();
</script>"""


SET_JS = """<script>
// ---- Grille virtualisée ----
// Seules les cartes visibles à l'écran existent dans le DOM. On calcule quelles
// lignes sont dans la fenêtre (plus une marge), et on ne rend que celles-là.
const cont=document.getElementById('cards');
const cntEl=document.getElementById('count');
const emptyEl=document.getElementById('empty');
const qEl=document.getElementById('q');
const rarEl=document.getElementById('rar');
const sortEl=document.getElementById('sort');

let filtered=VIEW.slice();     // données après filtre/tri
let colW=256, rowH=390, cols=1, pad=16;   // géométrie (240px+cap), recalculée au layout
const OVER=3;                  // lignes de marge au-dessus/dessous

function measure(){
  const w=cont.clientWidth||cont.offsetWidth||900;
  cols=Math.max(1,Math.floor((w+pad)/(colW+pad)));
}
function natcn(a,b){
  const pa=(a.cn||'').match(/^(\\d+)(.*)$/),pb=(b.cn||'').match(/^(\\d+)(.*)$/);
  if(pa&&pb){const d=(+pa[1])-(+pb[1]);return d||pa[2].localeCompare(pb[2]);}
  return (a.cn||'').localeCompare(b.cn||'');
}
function applyFilterSort(){
  const q=qEl.value.toLowerCase().trim();
  const r=rarEl.value, sort=sortEl.value;
  filtered=VIEW.filter(c=>{
    const okq=!q||c.n.includes(q)||c.t.includes(q)||c.a.includes(q);
    return okq&&(!r||c.r===r);
  });
  filtered.sort((a,b)=>{
    if(sort==='name')return a.name.localeCompare(b.name);
    if(sort==='cmc')return (a.cmc-b.cmc)||natcn(a,b);
    return natcn(a,b);
  });
  cntEl.textContent=filtered.length+' / '+VIEW.length;
  emptyEl.style.display=filtered.length?'none':'';
}
function cardHTML(c,idx){
  const cap='<div class=cap><span class=nm>'
    +'<span class=dot style="background:'+c.dot+'"></span>'
    +escapeHtml(c.name)+'</span><span class=cn>'+escapeHtml(c.cn)+'</span></div>';
  // DFC : le badge ↺ retourne la vignette. L'état (_f) vit dans la donnée,
  // pas le DOM — sinon il se perdrait au défilement (grille virtualisée).
  const hasF=c.faces&&c.faces.length>1;
  const face=hasF?c.faces[c._f?1:0]:null;
  const src=face?face.src:c.src;
  const alt=face?face.name:c.name;
  const img='<img loading=lazy src="'+src+'" alt="'+escapeHtml(alt)
    +'" onclick="vwOpen('+idx+')" onmouseenter="hovIn('+idx+')" '
    +'onmouseleave="hovOut()">';
  const badge=hasF?('<button class=flipbadge title="Retourner" '
    +'onclick="event.stopPropagation();event.preventDefault();gridFlip('+idx+')">'
    +'\\u21ba</button>'):'';
  const inner=c.href?('<a href="'+c.href+'">'+img+'</a>'):img;
  return '<div class=card>'+inner+badge+cap+'</div>';
}
function gridFlip(idx){
  const c=filtered[idx];if(!c||!c.faces)return;
  c._f=!c._f;
  render();   // re-rend la fenêtre visible avec la face à jour
}
function escapeHtml(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML;}

let ticking=false;
function render(){
  ticking=false;
  measure();
  const rows=Math.ceil(filtered.length/cols);
  const totalH=rows*rowH;
  const scrollY=window.scrollY;
  const top=cont.getBoundingClientRect().top+scrollY;
  const viewTop=Math.max(0,scrollY-top);
  const first=Math.max(0,Math.floor(viewTop/rowH)-OVER);
  const visRows=Math.ceil(window.innerHeight/rowH)+OVER*2;
  const startIdx=first*cols;
  const endIdx=Math.min(filtered.length,(first+visRows)*cols);
  // conteneur à hauteur totale, cartes positionnées en absolu par rangée
  let html='<div style="position:relative;height:'+totalH+'px">';
  for(let i=startIdx;i<endIdx;i++){
    const row=Math.floor(i/cols), col=i%cols;
    html+='<div style="position:absolute;top:'+(row*rowH)+'px;left:'
      +(col*(colW+pad))+'px;width:'+colW+'px">'+cardHTML(filtered[i],i)+'</div>';
  }
  html+='</div>';
  cont.innerHTML=html;
}
function onScroll(){if(!ticking){ticking=true;requestAnimationFrame(render);}}
// VIEW pour le viewer doit suivre le filtre : on remappe vwOpen sur filtered
window.vwOpen=function(i){window.__view=filtered;vwStart(i);return false;};

function flt(){applyFilterSort();render();}
window.addEventListener('scroll',onScroll,{passive:true});
window.addEventListener('resize',()=>{measure();render();});
applyFilterSort();
// géométrie réelle mesurée après premier rendu d'une carte témoin
cont.innerHTML=cardHTML(filtered[0]||{name:'',cn:'',src:'',dot:'#000',href:''},0);
requestAnimationFrame(()=>{
  const el=cont.querySelector('.card');
  if(el){const r=el.getBoundingClientRect();
    if(r.width>40){colW=r.width;}          // garde-fous : dimensions plausibles
    if(r.height>60){rowH=r.height+18;}
  }
  render();
});
</script>"""


VIEWER_JS = """<script>
// Viewer plein écran. Travaille sur une liste courante (vwList) : c'est VIEW
// par défaut (pages carte), ou la liste filtrée (pages set, via vwOpen).
let vi=0,vz=1,vr=0,vf=0,vwList=(typeof VIEW!=='undefined'?VIEW:[]);
const vw=document.getElementById('vw'),vimg=document.getElementById('vwimg'),
      vcap=document.getElementById('vwcap');
function vwApply(){vimg.style.transform=`scale(${vz}) rotate(${vr}deg)`;}
function vwShow(){const v=vwList[vi];if(!v)return;
  // DFC : afficher la face courante (vf). Sinon l'image simple.
  const hasF=v.faces&&v.faces.length>1;
  const face=hasF?v.faces[vf]:null;
  vimg.src=face?face.src:v.src;
  vimg.alt=(face?face.name:v.name);
  const nm=face?face.name:v.name;
  vcap.textContent=nm+'  ·  '+(v.cn||'')+'   ('+(vi+1)+'/'+vwList.length+')'
    +(hasF?'   [f] '+(vf===0?'recto':'verso'):'');
  const fb=document.getElementById('vwflip');
  if(fb)fb.style.display=hasF?'':'none';
  vz=1;vr=0;vwApply();}
function vwStart(i){vw.classList.add('on');vi=i;vf=0;vwShow();}
function vwFlip(){const v=vwList[vi];
  if(v&&v.faces&&v.faces.length>1){vf=vf?0:1;vwShow();}}
// vwOpen par défaut (pages carte) ; les pages de set le redéfinissent pour
// pointer sur la liste filtrée.
if(typeof window.vwOpen==='undefined'){
  window.vwOpen=function(i){vwList=(typeof VIEW!=='undefined'?VIEW:vwList);
    vwStart(i);return false;};
}
window.vwStart=vwStart;
Object.defineProperty(window,'__view',{set(v){vwList=v;},get(){return vwList;}});
function vwClose(){vw.classList.remove('on');}
function vwNext(){vi=(vi+1)%vwList.length;vf=0;vwShow();}
function vwPrev(){vi=(vi-1+vwList.length)%vwList.length;vf=0;vwShow();}
function vwZoom(d){vz=Math.max(.3,Math.min(6,vz+d*.25));vwApply();}
function vwRot(){vr=(vr+90)%360;vwApply();}
function vwReset(){vz=1;vr=0;vwApply();}
document.addEventListener('keydown',e=>{
  if(!vw.classList.contains('on'))return;
  if(e.key==='Escape')vwClose();
  else if(e.key==='ArrowRight')vwNext();
  else if(e.key==='ArrowLeft')vwPrev();
  else if(e.key==='r'||e.key==='R')vwRot();
  else if(e.key==='f'||e.key==='F')vwFlip();
  else if(e.key==='+'||e.key==='=')vwZoom(1);
  else if(e.key==='-')vwZoom(-1);
});
vw.addEventListener('click',e=>{if(e.target===vw||e.target.id==='vwstage')vwClose();});
</script>"""


# ------------------------------------------------------------------ main


# codes réellement fournis par la fonte keyrune, extraits du CSS embarqué
def load_keyrune_codes(assets: Path) -> set[str]:
    css_path = assets / "keyrune.css"
    if not css_path.exists():
        return set()
    txt = css_path.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"\.ss-([a-z0-9]+):before", txt))


def fetch_set_icons(sets, assets: Path, cache: Path, offline: bool,
                    keyrune_codes: set[str]) -> set[str]:
    """Télécharge les SVG Scryfall pour les sets absents de keyrune.

    keyrune ne couvre que ~40 % des sets ; Scryfall a le reste. On ne
    récupère que le complément, mis en cache dans metadata/seticons/.
    Retourne l'ensemble des codes disposant d'un SVG local.
    """
    dest = assets / "seticons"
    dest.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    got: set[str] = set()
    for s in sets:
        code = s["code"].lower()
        if code in keyrune_codes:
            continue
        uri = s.get("icon_svg_uri")
        out = dest / f"{code}.svg"
        cached = cache / f"{code}.svg"
        if cached.exists() and cached.stat().st_size > 0:
            shutil.copy(cached, out)
            got.add(code)
            continue
        if offline or not uri:
            continue
        try:
            req = urllib.request.Request(uri, headers={"User-Agent": UA})
            data = urllib.request.urlopen(req, timeout=20).read()
            cached.write_bytes(data)
            out.write_bytes(data)
            got.add(code)
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return got


# ==================================================================
# ORCHESTRATION UNIFIÉE
# ==================================================================

def clean_site_dir(out: Path) -> None:
    """Vide entièrement site/ avant régénération, sans jamais suivre les liens.

    Le sous-dossier site/sets est un lien symbolique vers les images (des
    dizaines de Go). shutil.rmtree supprime le lien, PAS sa cible — vérifié —
    mais on ceinture quand même :
      - refus si le chemin ne se termine pas par 'site' (garde-fou anti-erreur
        de --data-dir) ;
      - rmtree travaille en syscalls (os.unlink/os.rmdir), donc aucune limite
        ARG_MAX du shell même à des dizaines de milliers de fichiers.
    """
    if out.name != "site":
        raise SystemExit(f"refus de nettoyer un dossier nommé {out.name!r} "
                         f"(attendu 'site') — vérifie --data-dir")
    if not out.exists():
        return
    if out.is_symlink():
        # site/ lui-même est un lien : on enlève le lien, pas la cible
        out.unlink()
        return
    shutil.rmtree(out)          # ne suit pas les liens internes (ex. sets/)


def _render_site(data_dir: Path, *, card_pages: bool, want_rulings: bool,
                 offline: bool, sets_filter=None, clean: bool = True) -> None:
    """Génère le site complet : fontes locales, icônes de repli, pages.

    Par défaut, vide site/ avant de régénérer (clean=True) : sans quoi les
    pages de cartes disparues (renumérotation, set retiré) s'accumuleraient
    en orphelins au fil des régénérations. --no-clean conserve l'existant.
    """
    out = data_dir / "site"
    if clean and out.exists():
        clean_site_dir(out)
        log.info("site/ nettoyé avant régénération")
    out.mkdir(parents=True, exist_ok=True)

    sets, cards_by_set, by_oracle, rulings = build_model(
        data_dir, want_rulings=want_rulings)
    if sets_filter:
        sets = [s for s in sets if s["code"] in sets_filter]
    total = sum(s["count"] for s in sets)
    log.info(f"site : {len(sets)} extension(s), {total} carte(s), "
             f"{len(by_oracle)} unique(s)"
             + (f", {len(rulings)} avec rulings" if rulings else ""))

    # fontes locales
    fonts_ok = prepare_fonts(out, data_dir / "metadata" / "fonts", offline)
    log.info("fontes embarquées (hors-ligne)" if fonts_ok
             else "fontes indisponibles (ni cache ni réseau)")

    # icônes : keyrune (fonte) + repli SVG Scryfall pour le reste
    assets = out / "assets"
    global KEYRUNE_CODES, SVG_ICON_CODES
    KEYRUNE_CODES = load_keyrune_codes(assets)
    try:
        scry_sets = api_get(f"{API}/sets")["data"]
        icon_uri = {s["code"].lower(): s.get("icon_svg_uri") for s in scry_sets}
        needing = [{"code": s["code"], "icon_svg_uri": icon_uri.get(s["code"])}
                   for s in sets]
    except Exception as e:                                # noqa: BLE001
        log.warn(f"liste des sets Scryfall indisponible "
                 f"(icônes de repli ignorées) : {e}")
        needing = []
    SVG_ICON_CODES = fetch_set_icons(
        needing, assets, data_dir / "metadata" / "seticons", offline,
        KEYRUNE_CODES) if needing else set()
    covered = sum(1 for s in sets
                  if s["code"] in KEYRUNE_CODES or s["code"] in SVG_ICON_CODES)
    log.info(f"icônes : {len(KEYRUNE_CODES & {s['code'] for s in sets})} keyrune"
             f" + {len(SVG_ICON_CODES)} SVG Scryfall "
             f"({covered}/{len(sets)} extensions couvertes)")

    fav = favicon_svg()
    (out / "style.css").write_text(css(), encoding="utf-8")
    (out / "index.html").write_text(render_index(sets, fav), encoding="utf-8")
    # sets est déjà trié par date ; prev/next = voisins chronologiques
    for i, s in enumerate(sets):
        prev = sets[i - 1] if i > 0 else None
        nxt = sets[i + 1] if i < len(sets) - 1 else None
        (out / f"set-{slug(s['code'])}.html").write_text(
            render_set(s, cards_by_set[s["code"]], fav, card_pages, prev, nxt,
                       rulings),
            encoding="utf-8")

    # Voisinage clavier : pour chaque set, la liste ordonnée (num carte) des
    # oracle_id de ses cartes ayant une page. Permet, sur une page de carte
    # ouverte avec #<set>, de sauter à la carte précédente/suivante DU MÊME
    # set (points A/B du plan). On indexe par set -> [oid, oid, ...].
    set_order: dict[str, list[str]] = {}
    if card_pages:
        for code, cds in cards_by_set.items():
            seq = [c["oid"] for c in cds if not c["oid"].startswith("noid-")]
            set_order[code] = seq

    n_card = 0
    if card_pages:
        for oid, group in by_oracle.items():
            if oid.startswith("noid-"):
                continue
            # voisines par set : {set: {"prev": slug|"", "next": slug|""}}
            neighbors = {}
            for c in group:
                code = c["set_code"]
                seq = set_order.get(code, [])
                try:
                    idx = seq.index(oid)
                except ValueError:
                    continue
                prev_oid = seq[idx - 1] if idx > 0 else ""
                next_oid = seq[idx + 1] if idx < len(seq) - 1 else ""
                neighbors[code] = {
                    "prev": f"card-{slug(prev_oid)}.html#{code}" if prev_oid else "",
                    "next": f"card-{slug(next_oid)}.html#{code}" if next_oid else "",
                }
            (out / f"card-{slug(oid)}.html").write_text(
                render_card(group, rulings, fav, neighbors=neighbors),
                encoding="utf-8")
            n_card += 1
        log.info(f"{n_card} page(s) de carte")

    link = out / "sets"
    if not link.exists():
        try:
            os.symlink(data_dir / "sets", link)
        except OSError:
            shutil.copytree(data_dir / "sets", link)
    log.ok(f"site généré : {out / 'index.html'}")


def cmd_sync(a) -> int:
    """Fait tout : images, rulings, fontes, web — chaque étape désactivable."""
    cfg = build_cfg(a)
    for d in (cfg.data_dir, cfg.images_dir, cfg.meta_dir):
        d.mkdir(parents=True, exist_ok=True)
    log.info(f"mtgc {VERSION} — sync vers {cfg.data_dir}")

    # 1. images
    if not a.no_images:
        try:
            bulk = fetch_bulk(a.bulk, cfg.meta_dir, force=a.force_bulk)
        except Exception as e:                            # noqa: BLE001
            log.err(f"bulk cartes indisponible : {e}")
            return 1
        if not a.no_rulings:
            try:
                fetch_bulk("rulings", cfg.meta_dir, force=a.force_bulk)
                log.info("bulk des rulings récupéré")
            except Exception as e:                        # noqa: BLE001
                log.warn(f"bulk des rulings indisponible : {e}")

        conn = open_manifest(cfg.manifest)
        log.info("planification…")
        jobs, stats = plan(lambda: iter_cards(bulk), cfg)
        log.info(f"{stats['cartes']} cartes lues, {len(jobs)} images à traiter")
        # tableau des volumes (toutes qualités) avant de télécharger, sauf si
        # on l'a explicitement coupé
        if jobs and not a.no_sizes:
            print_size_table(jobs, cfg)
        if a.dry_run:
            log.ok("--dry-run : rien téléchargé")
            return 0
        run_downloads(jobs, cfg, conn)
    else:
        log.info("étape images ignorée (--no-images)")

    # 2. web
    if not a.no_web:
        _render_site(cfg.data_dir, card_pages=not a.no_card_pages,
                     want_rulings=not a.no_rulings, offline=a.no_fonts,
                     sets_filter=cfg.sets, clean=not a.no_clean)
    else:
        log.info("étape web ignorée (--no-web)")

    if a.open:
        import webbrowser
        webbrowser.open((cfg.data_dir / "site" / "index.html").as_uri())
    return 0


def cmd_web(a) -> int:
    build_cfg(a)  # valide les chemins
    data_dir = Path(a.data_dir).expanduser().resolve()
    _render_site(data_dir, card_pages=not a.no_card_pages,
                 want_rulings=not a.no_rulings, offline=a.no_fonts,
                 sets_filter={s.lower() for s in a.set} if a.set else None,
                 clean=not a.no_clean)
    if a.open:
        import webbrowser
        webbrowser.open((data_dir / "site" / "index.html").as_uri())
    return 0


def _add_common_image_args(p) -> None:
    p.add_argument("--data-dir", default="~/mtg",
                   help="racine des donnees et du site "
                        "(defaut : ~/mtg, developpe en /home/<user>/mtg)")
    p.add_argument("--quality", default="large", choices=QUALITIES)
    p.add_argument("--quality-for", action="append", metavar="FMT:SETS")
    p.add_argument("--strict-quality", action="store_true")
    p.add_argument("--bulk", default="default_cards", choices=BULK_TYPES)
    p.add_argument("--set", action="append", default=[],
                   help="limiter à ces extensions (répétable)")
    p.add_argument("--lang", action="append", default=[])
    p.add_argument("--unique", default="prints", choices=["prints", "art", "link"])
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--force-bulk", action="store_true")
    p.add_argument("--dry-run", action="store_true")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="mtgc",
        description="MTGcyCLAUDEpedia — images, rulings, fontes et site, "
                    "en un seul outil.")
    ap.add_argument("--version", action="version", version=f"mtgc {VERSION}")
    sub = ap.add_subparsers(dest="cmd")

    # sync : tout d'un coup
    ps = sub.add_parser("sync", help="tout faire (défaut) : images + web")
    _add_common_image_args(ps)
    ps.add_argument("--no-images", action="store_true",
                    help="ne pas télécharger d'images")
    ps.add_argument("--no-rulings", action="store_true",
                    help="ne pas récupérer les rulings")
    ps.add_argument("--no-fonts", action="store_true",
                    help="ne pas télécharger les fontes (cache seul)")
    ps.add_argument("--no-web", action="store_true",
                    help="ne pas générer le site")
    ps.add_argument("--no-card-pages", action="store_true",
                    help="pas de page par carte (plus rapide)")
    ps.add_argument("--no-clean", action="store_true",
                    help="ne pas vider site/ avant de régénérer "
                         "(par défaut, site/ est nettoyé pour éviter les "
                         "pages orphelines ; les images liées sont préservées)")
    ps.add_argument("--no-sizes", action="store_true",
                    help="ne pas sonder les tailles avant téléchargement")
    ps.add_argument("--open", action="store_true")
    ps.set_defaults(func=cmd_sync)

    # web : site seul
    pw = sub.add_parser("web", help="générer le site seul")
    pw.add_argument("--data-dir", default="~/mtg",
                    help="racine des donnees et du site "
                         "(defaut : ~/mtg)")
    pw.add_argument("--set", action="append", default=[])
    pw.add_argument("--no-card-pages", action="store_true")
    pw.add_argument("--no-clean", action="store_true",
                    help="ne pas vider site/ avant de régénérer")
    pw.add_argument("--no-rulings", action="store_true")
    pw.add_argument("--no-fonts", action="store_true",
                    help="cache de fontes seul, sans téléchargement")
    pw.add_argument("--open", action="store_true")
    # attributs attendus par build_cfg
    pw.add_argument("--quality", default="large", choices=QUALITIES)
    pw.add_argument("--quality-for", action="append")
    pw.add_argument("--strict-quality", action="store_true")
    pw.add_argument("--bulk", default="default_cards", choices=BULK_TYPES)
    pw.add_argument("--lang", action="append", default=[])
    pw.add_argument("--unique", default="prints")
    pw.add_argument("--jobs", type=int, default=8)
    pw.add_argument("--force-bulk", action="store_true")
    pw.add_argument("--dry-run", action="store_true")
    pw.set_defaults(func=cmd_web)

    # verify / sizes : réutilisent l'ancien chemin images
    pv = sub.add_parser("verify", help="contrôler les fichiers présents")
    _add_common_image_args(pv)
    pv.set_defaults(func=lambda a: _cmd_verify(a))

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    return a.func(a)


def _cmd_verify(a) -> int:
    cfg = build_cfg(a)
    conn = open_manifest(cfg.manifest)
    n = adopt_orphans(conn, cfg.images_dir)
    if n:
        log.info(f"{n} fichier(s) adopté(s) depuis le disque")
    r = verify_files(conn, cfg)
    return 0 if r["bad"] == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.warn("interrompu — relancer la même commande pour reprendre")
        sys.exit(130)
