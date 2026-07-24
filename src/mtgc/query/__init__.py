"""Moteur de recherche : requête Scryfall → SQL → lignes."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .compiler import ALIASES, HANDLERS, IS_PREDICATES, ORDER_COLUMNS, Compiler, UNSUPPORTED
from .nodes import Options
from .parser import QueryError, parse_query

__all__ = [
    "search", "build_sql", "QueryError", "SearchResult",
    "supported_keywords", "ALIASES", "IS_PREDICATES", "UNSUPPORTED",
]

#: tri par défaut (croissant) ; ces colonnes se lisent mieux en décroissant
_DESC_BY_DEFAULT = {"released", "release", "date", "usd", "eur", "cmc", "mv",
                    "power", "toughness", "rarity"}


@dataclass
class SearchResult:
    rows: list[sqlite3.Row]
    total: int
    sql: str
    params: list
    options: Options


def build_sql(query: str, *, limit: int | None = None, offset: int = 0,
              count_only: bool = False) -> tuple[str, list, Options]:
    """Compile une requête Scryfall en SQL paramétré."""
    ast = parse_query(query)
    comp = Compiler()
    where = comp.compile(ast)
    opts = comp.options

    # unique: cards | prints | art
    if opts.unique == "art":
        group = "GROUP BY COALESCE(cards.illustration_id, cards.id)"
    elif opts.unique in ("cards", "card"):
        group = "GROUP BY COALESCE(cards.oracle_id, cards.id)"
    else:
        group = ""

    # include:extras — par défaut on masque tokens/emblèmes/memorabilia
    extras_filter = ""
    if not opts.include_extras:
        extras_filter = (
            " AND cards.layout NOT IN ('token','double_faced_token','emblem',"
            "'art_series','scheme','planar','vanguard')"
            " AND cards.set_code NOT IN (SELECT code FROM sets WHERE set_type IN "
            "('token','memorabilia','minigame'))"
        )

    if count_only:
        if group:
            sql = (f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM cards "
                   f"WHERE {where}{extras_filter} {group})")
        else:
            sql = f"SELECT COUNT(*) AS n FROM cards WHERE {where}{extras_filter}"
        return sql, comp.params, opts

    order_key = opts.order if opts.order in ORDER_COLUMNS else "name"
    order_col = ORDER_COLUMNS[order_key]
    if opts.direction in ("asc", "desc"):
        direction = opts.direction.upper()
    else:
        direction = "DESC" if order_key in _DESC_BY_DEFAULT else "ASC"
    order_by = ", ".join(f"{c.strip()} {direction}" for c in order_col.split(","))
    order_by += ", cards.name ASC, cards.set_code ASC, cards.cn_num ASC"

    sql = (f"SELECT cards.* FROM cards WHERE {where}{extras_filter} "
           f"{group} ORDER BY {order_by}")
    params = list(comp.params)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    return sql, params, opts


def search(conn: sqlite3.Connection, query: str, *, limit: int | None = 100,
           offset: int = 0, with_total: bool = True) -> SearchResult:
    sql, params, opts = build_sql(query, limit=limit, offset=offset)
    rows = conn.execute(sql, params).fetchall()
    total = len(rows)
    if with_total:
        csql, cparams, _ = build_sql(query, count_only=True)
        total = conn.execute(csql, cparams).fetchone()["n"]
    return SearchResult(rows=rows, total=total, sql=sql, params=params, options=opts)


def supported_keywords() -> dict[str, list[str]]:
    """Inventaire pour la documentation et la commande ``mtgc syntax``."""
    canonical = sorted(HANDLERS)
    reverse: dict[str, list[str]] = {k: [] for k in canonical}
    for alias, target in ALIASES.items():
        if target in reverse:
            reverse[target].append(alias)
    return {k: sorted(v) for k, v in reverse.items()}
