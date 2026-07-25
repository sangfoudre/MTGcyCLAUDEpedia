#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mtgc-images 1.0 — téléchargement des images Scryfall, un répertoire par set.

Bibliothèque standard uniquement : rien à installer.

    ./mtgc-images.py --data-dir ~/mtg              # tout, illustrations uniques
    ./mtgc-images.py --data-dir ~/mtg --set isd    # un seul set
    ./mtgc-images.py --data-dir ~/mtg --dry-run    # planifier sans télécharger

Ce que fait la V1.0
  * résout /bulk-data à chaque exécution (les URL portent un horodatage et
    changent tous les jours : jamais d'URL en dur) ;
  * télécharge le bulk en .jsonl.gz et le lit en flux (aucun chargement de
    2 Go en mémoire) ;
  * range les images dans sets/<CODE_SET>/ ;
  * gère les cartes multi-faces : 5 layouts n'ont pas d'image à la racine,
    leurs images vivent dans card_faces[] ;
  * assainit les collector numbers (on rencontre ★, †, /, espaces) ;
  * reprend après interruption et ne retélécharge que ce qui a changé, en
    s'appuyant sur image_updated_at fourni par Scryfall ;
  * respecte les règles Scryfall : User-Agent explicite, 10 req/s sur
    api.scryfall.com. Les origines *.scryfall.io n'ont pas de limite : c'est
    là que vivent images et bulk, d'où la concurrence sur les images seules.

TODO (hors périmètre V1.0) : base SQLite complète et moteur de recherche.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import gzip
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "1.3.0"
API = "https://api.scryfall.com"
UA = f"MTGcyCLAUDEpedia/{VERSION} (+https://github.com/sangfoudre/MTGcyCLAUDEpedia)"
HEADERS = {"User-Agent": UA, "Accept": "application/json;q=0.9,*/*;q=0.8"}

#: délai mini entre deux appels à api.scryfall.com (10 req/s documenté)
API_DELAY = 0.1

#: qualités disponibles, de la meilleure à la moins bonne.
#: png = 745x1040, PNG transparent, coins arrondis — la meilleure.
#: (le script d'origine parcourait cette liste sans `break`, si bien que la
#:  dernière valeur écrasait toujours les précédentes et que seul `normal`
#:  était téléchargé ; ici l'ordre est respecté.)
QUALITY_DIMS = {"small": "146x204", "normal": "488x680", "large": "672x936",
                "png": "745x1040", "art_crop": "variable",
                "border_crop": "480x680"}
QUALITIES = ["png", "large", "normal", "small", "border_crop", "art_crop"]

#: layouts dont les images vivent UNIQUEMENT dans card_faces[]
#: (constaté sur les données réelles : ce sont exactement ces cinq-là)
DFC_LAYOUTS = {"transform", "modal_dfc", "reversible_card",
               "double_faced_token", "art_series"}

#: qualité de scan, du pire au meilleur — sert à départager deux prints
#: partageant la même illustration
IMAGE_STATUS_RANK = {"missing": 0, "placeholder": 1, "lowres": 2,
                     "highres_scan": 3}

BULK_TYPES = ["default_cards", "all_cards", "unique_artwork", "oracle_cards"]


# --------------------------------------------------------------- journal

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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="mtgc-images",
        description="Télécharge les images Scryfall, un répertoire par set.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""exemples :
  %(prog)s --data-dir ~/mtg
  %(prog)s --data-dir ~/mtg --set isd --set dka --quality png
  %(prog)s --data-dir ~/mtg --quality large   # ~30 Gio, toutes editions
  %(prog)s --data-dir ~/mtg --dry-run --verbose
""")
    p.add_argument("--data-dir", default="~/mtg",
                   help="racine des données (défaut : ~/mtg)")
    p.add_argument("--bulk", default="default_cards", choices=BULK_TYPES,
                   help="fichier bulk source (défaut : default_cards, qui "
                        "contient l'anglais quand il existe et la langue "
                        "d'origine sinon)")
    p.add_argument("--quality", default="png", choices=QUALITIES,
                   help="qualité voulue (défaut : png, 745x1040)")
    p.add_argument("--quality-for", action="append", metavar="FMT:SETS",
                   help="surcharge par set, ex. --quality-for png:mh3,blb "
                        "(repetable) : tout en --quality sauf ces sets-la")
    p.add_argument("--verify", action="store_true",
                   help="controler les fichiers presents (taille, en-tete) "
                        "sans rien telecharger")
    p.add_argument("--sizes", action="store_true",
                   help="afficher le tableau des volumes pour toutes les "
                        "qualites, meme hors --dry-run")
    p.add_argument("--strict-quality", action="store_true",
                   help="ne pas se rabattre sur une qualité inférieure")
    p.add_argument("--unique", default="prints",
                   choices=["prints", "art", "link"],
                   help="prints (defaut) = toutes les impressions pour "
                        "de vrai : chaque set a ses propres frames, "
                        "symboles et dates de copyright ; art = une seule "
                        "image par illustration ; link = image reelle dans "
                        "le set de premiere parution, liens symboliques "
                        "ailleurs (economise la place, mais affiche le "
                        "frame de la mauvaise edition)")
    p.add_argument("--set", action="append", metavar="CODE",
                   help="limiter à ces sets (répétable)")
    p.add_argument("--lang", action="append", metavar="LANG",
                   help="limiter à ces langues ; par défaut aucun filtre, "
                        "pour ne perdre aucune carte exclusive à une langue")
    p.add_argument("--jobs", type=int, default=8,
                   help="téléchargements simultanés (défaut : 8)")
    p.add_argument("--icons", action="store_true",
                   help="récupérer aussi les icônes SVG des sets")
    p.add_argument("--rulings", action="store_true",
                   help="récupérer aussi le bulk des rulings (~26 Mo), "
                        "nécessaire aux pages de carte du site web")
    p.add_argument("--force-bulk", action="store_true",
                   help="retélécharger le bulk même s'il est à jour")
    p.add_argument("--dry-run", action="store_true",
                   help="planifier sans rien télécharger")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    a = p.parse_args(argv)

    global log
    log = Log(verbose=a.verbose)
    cfg = build_cfg(a)

    log.info(f"mtgc-images {VERSION} — données dans {cfg.data_dir}")

    # Vérification pure : aucun accès réseau, aucun bulk à charger.
    if a.verify:
        conn = open_manifest(cfg.manifest)
        n = adopt_orphans(conn, cfg.images_dir)
        if n:
            log.info(f"{n} fichier(s) sur disque absent(s) du manifeste "
                     f"— adopté(s)")
        r = verify_files(conn, cfg)
        return 0 if r["bad"] == 0 else 1
    for d in (cfg.images_dir, cfg.icons_dir, cfg.meta_dir):
        d.mkdir(parents=True, exist_ok=True)

    try:
        bulk = fetch_bulk(a.bulk, cfg.meta_dir, force=a.force_bulk)
    except Exception as e:                                # noqa: BLE001
        log.err(f"récupération du bulk impossible : {e}")
        return 1

    if a.rulings:
        try:
            fetch_bulk("rulings", cfg.meta_dir, force=a.force_bulk)
            log.info("bulk des rulings récupéré")
        except Exception as e:                            # noqa: BLE001
            log.warn(f"bulk des rulings indisponible : {e}")

    log.info("lecture du bulk et planification…")
    t0 = time.time()
    jobs, stats = plan(lambda: iter_cards(bulk), cfg)
    log.ok(f"{stats['cartes']:,} cartes lues en {human_time(time.time() - t0)}"
           .replace(",", " "))
    log.info(f"  images à traiter    : {len(jobs):,}".replace(",", " "))
    log.info(f"  faces séparées      : {stats['faces']:,}".replace(",", " "))
    if cfg.unique == "art":
        log.info(f"  doublons d'illustration écartés : "
                 f"{stats['doublons_art']:,}".replace(",", " "))
    if stats["sans_image"]:
        log.info(f"  sans image exploitable : {stats['sans_image']:,}"
                 .replace(",", " "))
    if stats["ignorées_langue"]:
        log.info(f"  écartées par langue : {stats['ignorées_langue']:,}"
                 .replace(",", " "))

    conn = open_manifest(cfg.manifest)

    if a.icons and not cfg.dry_run:
        n = download_set_icons(cfg, conn)
        log.ok(f"{n} icône(s) de set récupérée(s)")

    if cfg.dry_run or a.sizes:
        print_size_table(jobs, cfg)

    res = run_downloads(jobs, cfg, conn)

    log.ok(f"terminé : {res['téléchargées']:,} téléchargée(s), "
           f"{res['ignorées']:,} déjà à jour, {res['échecs']:,} échec(s), "
           f"{human(res['octets'])}".replace(",", " "))
    if res["échecs"]:
        log.warn("relancer la commande reprendra uniquement les manquantes")
    conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(file=sys.stderr)
        log.warn("interrompu — relancer la commande reprendra où elle en était")
        sys.exit(130)
