"""Connexion SQLite et fonctions utilisateur (REGEXP, POPCOUNT)."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from .util import get_logger

log = get_logger("db")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_REGEX_CACHE: dict[str, re.Pattern] = {}


def _regexp(pattern: str, value: str | None) -> bool:
    """Support de ``WHERE col REGEXP ?`` (nécessaire pour la syntaxe ``//``)."""
    if value is None:
        return False
    rx = _REGEX_CACHE.get(pattern)
    if rx is None:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return False
        _REGEX_CACHE[pattern] = rx
    return rx.search(str(value)) is not None


def _popcount(mask: Any) -> int:
    try:
        return bin(int(mask) & 31).count("1")
    except (TypeError, ValueError):
        return 0


def _mana_matches(mana_cost: str | None, spec: str | None, op: str | None) -> int:
    """Comparaison de coûts de mana comme multiensembles de symboles.

    ``m:WW`` → la carte contient au moins deux ``{W}``. La comparaison en SQL
    pur est impraticable : une fonction utilisateur reste la voie la plus
    lisible, quitte à provoquer un balayage (à combiner avec un préfiltre).
    """
    from collections import Counter
    from .util import parse_mana_symbols

    have = Counter(parse_mana_symbols(mana_cost))
    # Le spec peut être écrit '{2}{W}{W}' ou simplement '2WW'
    if "{" in (spec or ""):
        want = Counter(parse_mana_symbols(spec))
    else:
        # Forme abrégée : '2WW', 'W/U', 'WP'. Le regex garde les symboles
        # hybrides et Phyrexians entiers plutôt que de les casser en lettres.
        tokens = re.findall(r"\d+|[A-Z](?:/[A-Z0-9]+)+|[A-Z]", (spec or "").upper())
        want = Counter(tokens)

    op = op or ":"
    subset = all(have.get(k, 0) >= v for k, v in want.items())
    superset = all(want.get(k, 0) >= v for k, v in have.items())
    equal = have == want

    if op in (":", ">="):
        return int(subset)
    if op == "=":
        return int(equal)
    if op == "!=":
        return int(not equal)
    if op == "<=":
        return int(superset)
    if op == ">":
        return int(subset and not equal)
    if op == "<":
        return int(superset and not equal)
    return int(subset)


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.create_function("regexp", 2, _regexp, deterministic=True)
    conn.create_function("popcount", 1, _popcount, deterministic=True)
    conn.create_function("mana_matches", 3, _mana_matches, deterministic=True)
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -200000")  # ~200 Mio
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def rebuild_fts(conn: sqlite3.Connection) -> None:
    log.info("reconstruction de l'index FTS5…")
    conn.execute("INSERT INTO cards_fts(cards_fts) VALUES('rebuild')")
    conn.commit()


def analyze(conn: sqlite3.Connection) -> None:
    conn.execute("ANALYZE")
    conn.commit()


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for table in ("sets", "cards", "card_faces", "artists", "legalities",
                  "rulings", "tags", "art_taggings", "oracle_taggings", "images"):
        try:
            out[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        except sqlite3.Error:
            out[table] = -1
    return out
