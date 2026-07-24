"""Ingestion des données Scryfall vers SQLite.

Le bulk est lu en **streaming gzip ligne à ligne** (JSONL). La v1 faisait
``json.loads(open(f).read())`` sur le fichier entier : aujourd'hui ce serait
2,5 Gio de JSON en RAM pour ``all_cards``.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import db, net
from .config import Config
from .util import (
    get_logger, human_bytes, list_to_mask, numeric_or_none, popcount,
    produced_mana_mask, rarity_rank, split_collector_number, strip_reminder
)

log = get_logger("ingest")

#: layouts dont les images vivent dans ``card_faces[]`` et non à la racine
SPLIT_IMAGE_LAYOUTS = {
    "transform", "modal_dfc", "double_faced_token", "art_series",
    "reversible_card", "battle",
}


# --------------------------------------------------------------- download

def download_bulk(cfg: Config, bulk_type: str, *, force: bool = False) -> Path:
    """Télécharge le ``.jsonl.gz`` du bulk demandé si nécessaire."""
    desc = net.find_bulk(bulk_type)
    uri = desc.get("jsonl_download_uri") or desc["download_uri"]
    name = uri.rsplit("/", 1)[-1]
    dest = cfg.metadata_dir / name
    cfg.metadata_dir.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not force:
        log.info("bulk déjà présent : %s (%s)", dest.name, human_bytes(dest.stat().st_size))
        return dest

    # purge des versions antérieures du même type
    for old in cfg.metadata_dir.glob(f"{bulk_type.replace('_', '-')}-*"):
        if old != dest:
            old.unlink(missing_ok=True)

    total = int(desc.get("size") or 0)
    log.info("téléchargement %s → %s (%s)", bulk_type, dest.name,
             human_bytes(total) if total else "taille inconnue")
    tmp = dest.with_suffix(dest.suffix + ".part")
    got = 0
    with tmp.open("wb") as fh:
        for chunk in net.download_stream(uri):
            fh.write(chunk)
            got += len(chunk)
    tmp.replace(dest)
    log.info("téléchargé %s", human_bytes(got))
    return dest


def iter_jsonl(path: Path) -> Iterator[dict]:
    """Itère les objets d'un ``.jsonl.gz`` (ou ``.jsonl``, ou ``.json`` tableau)."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
        first = fh.read(1)
        fh.seek(0)
        if first == "[":
            # ancien format : tableau JSON monolithique (toujours servi par
            # download_uri). On le supporte, mais jsonl.gz est préférable.
            for obj in json.load(fh):
                yield obj
            return
        for line in fh:
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            yield json.loads(line)


# ------------------------------------------------------------------ sets

def ingest_sets(conn: sqlite3.Connection, cfg: Config) -> int:
    sets = net.get_all_sets()
    rows = []
    for s in sets:
        released = s.get("released_at")
        rows.append((
            s["id"], s["code"], s.get("mtgo_code"), s.get("arena_code"), s["name"],
            s.get("set_type"), released,
            int(released[:4]) if released else None,
            s.get("block_code"), s.get("block"), s.get("parent_set_code"),
            s.get("card_count"), s.get("printed_size"),
            int(bool(s.get("digital"))), int(bool(s.get("foil_only"))),
            int(bool(s.get("nonfoil_only"))),
            s.get("icon_svg_uri"),
            str(cfg.icons_dir / s["code"] / f"{s['code']}.svg"),
            s.get("scryfall_uri"),
        ))
    conn.executemany(
        """INSERT INTO sets(id, code, mtgo_code, arena_code, name, set_type,
               released_at, released_year, block_code, block, parent_set_code,
               card_count, printed_size, digital, foil_only, nonfoil_only,
               icon_svg_uri, icon_path, scryfall_uri)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               code=excluded.code, name=excluded.name, set_type=excluded.set_type,
               released_at=excluded.released_at, released_year=excluded.released_year,
               block=excluded.block, block_code=excluded.block_code,
               parent_set_code=excluded.parent_set_code,
               card_count=excluded.card_count, printed_size=excluded.printed_size,
               icon_svg_uri=excluded.icon_svg_uri""",
        rows,
    )
    conn.commit()
    log.info("%d sets ingérés", len(rows))
    return len(rows)


# ----------------------------------------------------------------- cards

_CARD_COLUMNS = [
    "id", "oracle_id", "name", "lang", "layout", "set_code", "set_id",
    "collector_number", "cn_num", "cn_suffix", "cn_prefix", "rarity",
    "rarity_rank", "released_at", "released_year", "mana_cost", "cmc",
    "type_line", "oracle_text", "oracle_plain", "flavor_text", "power", "toughness", "loyalty",
    "defense", "pow_num", "tou_num", "loy_num", "colors", "color_identity",
    "color_count", "ci_count", "produced_mana", "artist", "artist_count",
    "illustration_id", "border_color", "frame", "security_stamp", "watermark",
    "image_status", "image_updated_at", "highres_image", "full_art", "textless",
    "digital", "promo", "reprint", "variation", "variation_of", "reserved",
    "booster", "story_spotlight", "oversized", "game_changer", "content_warning",
    "face_count", "edhrec_rank", "penny_rank", "scryfall_uri",
    "image_uris_json", "prices_json", "legalities_json", "finishes_json",
    "games_json", "keywords_json", "promo_types_json", "frame_effects_json",
    "multiverse_ids_json", "all_parts_json",
]

_CARD_SQL = (
    f"INSERT OR REPLACE INTO cards({', '.join(_CARD_COLUMNS)}) "
    f"VALUES({', '.join('?' * len(_CARD_COLUMNS))})"
)


def _j(value) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def _card_row(c: dict) -> tuple:
    prefix, num, suffix = split_collector_number(c.get("collector_number"))
    released = c.get("released_at")
    faces = c.get("card_faces") or []

    # Une carte multi-faces n'a pas toujours colors/oracle_text à la racine :
    # on agrège depuis les faces plutôt que de perdre l'information.
    colors = c.get("colors")
    if colors is None and faces:
        agg = 0
        for f in faces:
            agg |= list_to_mask(f.get("colors"))
        colors_mask = agg
    else:
        colors_mask = list_to_mask(colors)

    oracle_text = c.get("oracle_text")
    if oracle_text is None and faces:
        oracle_text = "\n//\n".join(f.get("oracle_text") or "" for f in faces).strip()

    flavor_text = c.get("flavor_text")
    if flavor_text is None and faces:
        parts = [f.get("flavor_text") for f in faces if f.get("flavor_text")]
        flavor_text = "\n//\n".join(parts) if parts else None

    mana_cost = c.get("mana_cost")
    if mana_cost is None and faces:
        mana_cost = " // ".join(f.get("mana_cost") or "" for f in faces).strip()

    ci_mask = list_to_mask(c.get("color_identity"))
    artist_ids = c.get("artist_ids") or []

    illustration_id = c.get("illustration_id")
    if not illustration_id and faces:
        illustration_id = faces[0].get("illustration_id")

    return (
        c["id"], c.get("oracle_id"), c.get("name"), c.get("lang"), c.get("layout"),
        (c.get("set") or "").lower(), c.get("set_id"),
        c.get("collector_number"), num, suffix, prefix,
        c.get("rarity"), rarity_rank(c.get("rarity")),
        released, int(released[:4]) if released else None,
        mana_cost, c.get("cmc"),
        c.get("type_line"), oracle_text, strip_reminder(oracle_text), flavor_text,
        c.get("power"), c.get("toughness"), c.get("loyalty"), c.get("defense"),
        numeric_or_none(c.get("power")), numeric_or_none(c.get("toughness")),
        numeric_or_none(c.get("loyalty")),
        colors_mask, ci_mask, popcount(colors_mask), popcount(ci_mask),
        produced_mana_mask(c.get("produced_mana")),
        c.get("artist"), len(artist_ids),
        illustration_id,
        c.get("border_color"), c.get("frame"), c.get("security_stamp"),
        c.get("watermark"),
        c.get("image_status"), c.get("image_updated_at"),
        int(bool(c.get("highres_image"))), int(bool(c.get("full_art"))),
        int(bool(c.get("textless"))), int(bool(c.get("digital"))),
        int(bool(c.get("promo"))), int(bool(c.get("reprint"))),
        int(bool(c.get("variation"))), c.get("variation_of"),
        int(bool(c.get("reserved"))), int(bool(c.get("booster"))),
        int(bool(c.get("story_spotlight"))), int(bool(c.get("oversized"))),
        int(bool(c.get("game_changer"))), int(bool(c.get("content_warning"))),
        len(faces),
        c.get("edhrec_rank"), c.get("penny_rank"), c.get("scryfall_uri"),
        _j(c.get("image_uris")), _j(c.get("prices")), _j(c.get("legalities")),
        _j(c.get("finishes")), _j(c.get("games")), _j(c.get("keywords")),
        _j(c.get("promo_types")), _j(c.get("frame_effects")),
        _j(c.get("multiverse_ids")), _j(c.get("all_parts")),
    )


def _face_rows(c: dict) -> list[tuple]:
    rows = []
    for i, f in enumerate(c.get("card_faces") or []):
        rows.append((
            c["id"], i, f.get("name"), f.get("mana_cost"), f.get("type_line"),
            f.get("oracle_text"), strip_reminder(f.get("oracle_text")),
            f.get("flavor_text"), f.get("power"),
            f.get("toughness"), f.get("loyalty"), f.get("defense"),
            numeric_or_none(f.get("power")), numeric_or_none(f.get("toughness")),
            numeric_or_none(f.get("loyalty")),
            list_to_mask(f.get("colors")), f.get("artist"),
            f.get("illustration_id"), f.get("watermark"),
            _j(f.get("image_uris")),
        ))
    return rows


def ingest_cards(conn: sqlite3.Connection, path: Path, cfg: Config) -> int:
    """Charge le bulk. Le filtrage linguistique conserve toujours les cartes
    dont aucune version dans les langues demandées n'existe (fallback)."""
    wanted_langs = {l.lower() for l in (cfg.languages or [])}
    cur = conn.cursor()
    cur.execute("BEGIN")

    card_batch: list[tuple] = []
    face_batch: list[tuple] = []
    legal_batch: list[tuple] = []
    artist_batch: list[tuple] = []
    cardartist_batch: list[tuple] = []

    n_total = n_kept = 0
    # oracle_id/illustration_id vus dans une langue voulue → sert au fallback
    seen_ok: set[str] = set()
    deferred: list[dict] = []

    def flush() -> None:
        if card_batch:
            cur.executemany(_CARD_SQL, card_batch)
            card_batch.clear()
        if face_batch:
            cur.executemany(
                "INSERT OR REPLACE INTO card_faces(card_id, face_index, name, "
                "mana_cost, type_line, oracle_text, oracle_plain, flavor_text, "
                "power, toughness, loyalty, defense, pow_num, tou_num, loy_num, "
                "colors, artist, illustration_id, watermark, image_uris_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                face_batch)
            face_batch.clear()
        if legal_batch:
            cur.executemany(
                "INSERT OR REPLACE INTO legalities(card_id, format, status) "
                "VALUES(?,?,?)", legal_batch)
            legal_batch.clear()
        if artist_batch:
            cur.executemany(
                "INSERT OR IGNORE INTO artists(id, name) VALUES(?,?)", artist_batch)
            artist_batch.clear()
        if cardartist_batch:
            cur.executemany(
                "INSERT OR REPLACE INTO card_artists(card_id, artist_id, ord) "
                "VALUES(?,?,?)", cardartist_batch)
            cardartist_batch.clear()

    def accept(c: dict) -> None:
        nonlocal n_kept
        n_kept += 1
        card_batch.append(_card_row(c))
        face_batch.extend(_face_rows(c))
        for fmt, status in (c.get("legalities") or {}).items():
            legal_batch.append((c["id"], fmt, status))
        names = c.get("artist_ids") or []
        artist_names = [a.strip() for a in (c.get("artist") or "").split("&")]
        for i, aid in enumerate(names):
            nm = artist_names[i] if i < len(artist_names) else (c.get("artist") or "")
            artist_batch.append((aid, nm))
            cardartist_batch.append((c["id"], aid, i))
        if len(card_batch) >= 5000:
            flush()

    for c in iter_jsonl(path):
        n_total += 1
        if n_total % 50000 == 0:
            log.info("  %d objets lus, %d retenus…", n_total, n_kept)
        lang = (c.get("lang") or "").lower()
        if not wanted_langs or lang in wanted_langs:
            accept(c)
            key = c.get("oracle_id") or c.get("illustration_id") or c["id"]
            seen_ok.add(key)
        else:
            # candidat au fallback : décidé après le premier passage
            deferred.append(c)

    # ---- fallback linguistique : on récupère ce qui n'existe dans aucune
    # des langues demandées (promos JP exclusives, Hobby Japan, etc.)
    n_fallback = 0
    if wanted_langs and deferred:
        by_key: dict[str, dict] = {}
        for c in deferred:
            key = c.get("oracle_id") or c.get("illustration_id") or c["id"]
            if key in seen_ok:
                continue
            prev = by_key.get(key)
            if prev is None or _fallback_score(c) > _fallback_score(prev):
                by_key[key] = c
        for c in by_key.values():
            accept(c)
            n_fallback += 1

    flush()
    conn.commit()
    log.info("%d objets lus, %d cartes retenues (dont %d par fallback linguistique)",
             n_total, n_kept, n_fallback)
    return n_kept


_IMAGE_STATUS_SCORE = {"highres_scan": 3, "lowres": 2, "placeholder": 1, "missing": 0}


def _fallback_score(c: dict) -> tuple:
    return (
        _IMAGE_STATUS_SCORE.get(c.get("image_status") or "", 0),
        int(bool(c.get("highres_image"))),
        -(len(c.get("lang") or "")),
    )


# --------------------------------------------------------- unique artwork

def mark_unique_artwork(conn: sqlite3.Connection) -> int:
    """Élit un représentant par ``illustration_id``.

    Reproduit l'esprit du bulk ``unique_artwork`` **sans perdre de carte** :
    toutes les cartes restent en base, seul le drapeau change.
    """
    conn.execute("UPDATE cards SET is_unique_art = 0")
    conn.execute("""
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY illustration_id
                       ORDER BY
                           CASE image_status
                               WHEN 'highres_scan' THEN 0
                               WHEN 'lowres'       THEN 1
                               WHEN 'placeholder'  THEN 2
                               ELSE 3 END,
                           highres_image DESC,
                           CASE WHEN lang = 'en' THEN 0 ELSE 1 END,
                           digital ASC,
                           released_at ASC,
                           id ASC
                   ) AS rn
            FROM cards
            WHERE illustration_id IS NOT NULL AND illustration_id <> ''
        )
        UPDATE cards SET is_unique_art = 1
        WHERE id IN (SELECT id FROM ranked WHERE rn = 1)
    """)
    # Les cartes sans illustration_id (tokens anciens, cartes sans art) sont
    # conservées comme uniques : mieux vaut un doublon qu'une perte.
    conn.execute("""
        UPDATE cards SET is_unique_art = 1
        WHERE illustration_id IS NULL OR illustration_id = ''
    """)
    conn.commit()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM cards WHERE is_unique_art = 1").fetchone()["n"]
    log.info("%d représentants d'illustration unique marqués", n)
    return n


# --------------------------------------------------------------- annexes

def ingest_rulings(conn: sqlite3.Connection, path: Path) -> int:
    conn.execute("DELETE FROM rulings")
    rows = [(r.get("oracle_id"), r.get("source"), r.get("published_at"), r.get("comment"))
            for r in iter_jsonl(path) if r.get("object") == "ruling"]
    conn.executemany(
        "INSERT INTO rulings(oracle_id, source, published_at, comment) VALUES(?,?,?,?)",
        rows)
    conn.commit()
    log.info("%d rulings ingérés", len(rows))
    return len(rows)


def ingest_tags(conn: sqlite3.Connection, path: Path, kind: str) -> int:
    """``kind`` vaut ``art`` (illustration_id) ou ``oracle`` (oracle_id)."""
    table = "art_taggings" if kind == "art" else "oracle_taggings"
    idcol = "illustration_id" if kind == "art" else "oracle_id"
    tag_rows, link_rows = [], []
    for t in iter_jsonl(path):
        if t.get("object") != "tag":
            continue
        tag_rows.append((t["id"], t.get("label"), t.get("slug"), t.get("type")))
        for tg in t.get("taggings") or []:
            target = tg.get(idcol)
            if target:
                link_rows.append((t["id"], target, tg.get("weight")))
    conn.executemany(
        "INSERT OR REPLACE INTO tags(id, label, slug, type) VALUES(?,?,?,?)", tag_rows)
    conn.executemany(
        f"INSERT OR REPLACE INTO {table}(tag_id, {idcol}, weight) VALUES(?,?,?)",
        link_rows)
    conn.commit()
    log.info("%d tags %s, %d associations", len(tag_rows), kind, len(link_rows))
    return len(link_rows)


# ------------------------------------------------------------------ main

def run_ingest(cfg: Config, *, force: bool = False, with_tags: bool = True,
               with_rulings: bool = True) -> None:
    cfg.ensure_dirs()
    net.set_api_delay(cfg.api_delay)
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)

    ingest_sets(conn, cfg)

    bulk_path = download_bulk(cfg, cfg.bulk_type, force=force)
    ingest_cards(conn, bulk_path, cfg)
    mark_unique_artwork(conn)

    if with_rulings:
        ingest_rulings(conn, download_bulk(cfg, "rulings", force=force))
    if with_tags:
        try:
            ingest_tags(conn, download_bulk(cfg, "art_tags", force=force), "art")
            ingest_tags(conn, download_bulk(cfg, "oracle_tags", force=force), "oracle")
        except KeyError as exc:
            log.warning("tags indisponibles (%s) — atag:/otag: seront inertes", exc)

    db.rebuild_fts(conn)
    db.analyze(conn)
    db.set_meta(conn, "ingested_at", datetime.now(timezone.utc).isoformat())
    db.set_meta(conn, "bulk_type", cfg.bulk_type)
    conn.commit()

    for table, n in db.counts(conn).items():
        log.info("  %-16s %8d", table, n)
    conn.close()
