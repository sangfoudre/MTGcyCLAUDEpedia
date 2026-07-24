"""Téléchargement des images de cartes et des icônes de set.

Corrections par rapport à la v1 :

* le choix du format parcourt réellement la préférence et **s'arrête au
  premier trouvé** (la v1 écrasait sa variable à chaque tour et finissait
  toujours sur ``normal``) ;
* un échec unitaire n'interrompt plus la campagne ;
* reprise par comparaison ``image_updated_at`` plutôt que simple existence,
  ce qui permet de rattraper un passage ``lowres`` → ``highres_scan`` ;
* concurrence réelle (les origines ``*.scryfall.io`` n'ont pas de rate limit).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import USER_AGENT, Config
from .util import get_logger, human_bytes, slugify

log = get_logger("images")

#: ordre de qualité décroissante réelle
QUALITY_ORDER = ["png", "large", "border_crop", "normal", "small", "art_crop"]

EXTENSIONS = {"png": ".png", "large": ".jpg", "normal": ".jpg", "small": ".jpg",
              "art_crop": ".jpg", "border_crop": ".jpg"}


@dataclass
class Job:
    card_id: str
    face_index: int
    fmt: str
    url: str
    path: Path
    src_updated: str | None


def pick_format(uris: dict, preferences: list[str]) -> tuple[str, str] | None:
    """Renvoie ``(format, url)`` du premier format préféré disponible.

    C'est le correctif du bug n°1 : la boucle **retourne** dès qu'elle trouve.
    """
    for fmt in preferences:
        url = uris.get(fmt)
        if url:
            return fmt, url
    for fmt in QUALITY_ORDER:
        url = uris.get(fmt)
        if url:
            return fmt, url
    return None


def _filename(cn: str | None, illustration_id: str | None, face_index: int,
              fmt: str) -> str:
    base = slugify(cn or "x")
    if illustration_id:
        base += "_" + illustration_id[:8]
    if face_index:
        base += f"_f{face_index}"
    return base + EXTENSIONS.get(fmt, ".img")


def collect_jobs(conn: sqlite3.Connection, cfg: Config, *,
                 unique_art_only: bool = False,
                 sets: list[str] | None = None,
                 refresh: bool = False) -> list[Job]:
    """Construit la liste des téléchargements à effectuer."""
    where = ["1=1"]
    params: list = []
    if unique_art_only:
        where.append("cards.is_unique_art = 1")
    if sets:
        where.append("cards.set_code IN (%s)" % ",".join("?" * len(sets)))
        params += [s.lower() for s in sets]

    sql = (f"SELECT id, set_code, collector_number, illustration_id, face_count, "
           f"image_uris_json, image_updated_at, image_status "
           f"FROM cards WHERE {' AND '.join(where)}")

    known: dict[tuple[str, int, str], sqlite3.Row] = {}
    for row in conn.execute("SELECT card_id, face_index, fmt, path, src_updated FROM images"):
        known[(row["card_id"], row["face_index"], row["fmt"])] = row

    jobs: list[Job] = []
    for card in conn.execute(sql, params):
        set_dir = cfg.images_dir / (card["set_code"] or "unknown")
        entries: list[tuple[int, dict]] = []

        uris = json.loads(card["image_uris_json"]) if card["image_uris_json"] else None
        if uris:
            entries.append((0, uris))
        else:
            # multi-faces : les images vivent dans card_faces
            for face in conn.execute(
                    "SELECT face_index, image_uris_json FROM card_faces "
                    "WHERE card_id = ? ORDER BY face_index", (card["id"],)):
                if face["image_uris_json"]:
                    entries.append((face["face_index"], json.loads(face["image_uris_json"])))

        for face_index, face_uris in entries:
            picked = pick_format(face_uris, cfg.image_formats)
            if not picked:
                continue
            fmt, url = picked
            path = set_dir / _filename(card["collector_number"],
                                       card["illustration_id"], face_index, fmt)
            prev = known.get((card["id"], face_index, fmt))
            if prev and not refresh:
                same_version = (prev["src_updated"] or "") == (card["image_updated_at"] or "")
                if same_version and Path(prev["path"]).exists():
                    continue
            elif not prev and path.exists() and not refresh:
                # fichier présent mais absent de la base : on l'enregistre
                jobs.append(Job(card["id"], face_index, fmt, url, path,
                                card["image_updated_at"]))
                continue
            jobs.append(Job(card["id"], face_index, fmt, url, path,
                            card["image_updated_at"]))
    return jobs


def _fetch(job: Job) -> tuple[Job, int | None, str | None]:
    """Renvoie ``(job, octets, erreur)``. Ne lève jamais."""
    try:
        if job.path.exists() and job.path.stat().st_size > 0:
            return job, job.path.stat().st_size, None
        job.path.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(job.url, headers={"User-Agent": USER_AGENT})
        tmp = job.path.with_suffix(job.path.suffix + ".part")
        with urllib.request.urlopen(req, timeout=60) as resp:
            expected = resp.headers.get("Content-Length")
            data = resp.read()
        if expected and len(data) != int(expected):
            tmp.unlink(missing_ok=True)
            return job, None, f"taille incohérente ({len(data)} != {expected})"
        tmp.write_bytes(data)
        tmp.replace(job.path)
        return job, len(data), None
    except urllib.error.HTTPError as exc:
        return job, None, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 — un échec ne doit rien interrompre
        return job, None, f"{type(exc).__name__}: {exc}"


def download_images(conn: sqlite3.Connection, cfg: Config, *,
                    unique_art_only: bool = False,
                    sets: list[str] | None = None,
                    refresh: bool = False,
                    limit: int | None = None,
                    dry_run: bool = False) -> dict:
    jobs = collect_jobs(conn, cfg, unique_art_only=unique_art_only, sets=sets,
                        refresh=refresh)
    if limit:
        jobs = jobs[:limit]
    log.info("%d image(s) à télécharger (concurrence %d, format préféré %s)",
             len(jobs), cfg.image_concurrency, ", ".join(cfg.image_formats))
    if dry_run:
        for j in jobs[:20]:
            log.info("  %s → %s", j.fmt, j.path)
        if len(jobs) > 20:
            log.info("  … et %d autres", len(jobs) - 20)
        return {"planned": len(jobs), "downloaded": 0, "failed": 0, "bytes": 0}

    lock = threading.Lock()
    stats = {"planned": len(jobs), "downloaded": 0, "failed": 0, "bytes": 0}
    pending: list[tuple] = []
    now = datetime.now(timezone.utc).isoformat()

    with ThreadPoolExecutor(max_workers=cfg.image_concurrency) as pool:
        futures = [pool.submit(_fetch, j) for j in jobs]
        for i, fut in enumerate(as_completed(futures), 1):
            job, size, err = fut.result()
            with lock:
                if err:
                    stats["failed"] += 1
                    log.warning("échec %s (%s) : %s", job.card_id, job.fmt, err)
                else:
                    stats["downloaded"] += 1
                    stats["bytes"] += size or 0
                    pending.append((job.card_id, job.face_index, job.fmt,
                                    str(job.path), size, job.src_updated, now))
                if len(pending) >= 500:
                    _flush(conn, pending)
                if i % 500 == 0:
                    log.info("  %d/%d — %s", i, len(jobs), human_bytes(stats["bytes"]))
    _flush(conn, pending)
    log.info("terminé : %d téléchargées, %d échecs, %s",
             stats["downloaded"], stats["failed"], human_bytes(stats["bytes"]))
    return stats


def _flush(conn: sqlite3.Connection, pending: list[tuple]) -> None:
    if not pending:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO images(card_id, face_index, fmt, path, bytes, "
        "src_updated, downloaded_at) VALUES(?,?,?,?,?,?,?)", pending)
    conn.commit()
    pending.clear()


# ------------------------------------------------------------ icônes set

def download_icons(conn: sqlite3.Connection, cfg: Config) -> int:
    rows = conn.execute(
        "SELECT code, icon_svg_uri, icon_path FROM sets "
        "WHERE icon_svg_uri IS NOT NULL").fetchall()
    jobs = []
    for r in rows:
        path = Path(r["icon_path"])
        if path.exists():
            continue
        jobs.append(Job(r["code"], 0, "svg", r["icon_svg_uri"], path, None))
    log.info("%d icône(s) de set à télécharger", len(jobs))
    ok = 0
    with ThreadPoolExecutor(max_workers=cfg.image_concurrency) as pool:
        for fut in as_completed([pool.submit(_fetch, j) for j in jobs]):
            _, size, err = fut.result()
            if err:
                log.warning("icône : %s", err)
            else:
                ok += 1
    log.info("%d icônes récupérées", ok)
    return ok
