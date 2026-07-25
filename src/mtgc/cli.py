"""Interface en ligne de commande."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import db, images, ingest, latex
from .config import Config, load_config
from .query import IS_PREDICATES, UNSUPPORTED, build_sql, search, supported_keywords
from .query.parser import QueryError
from .util import human_bytes, setup_logging


def _cfg(args) -> Config:
    cfg = load_config(args.config)
    if args.data_dir:
        cfg.data_dir = Path(args.data_dir).expanduser()
    if getattr(args, "static_dir", None):
        cfg.static_dir = Path(args.static_dir).expanduser()
    return cfg


# ------------------------------------------------------------- commandes

def cmd_ingest(args) -> int:
    cfg = _cfg(args)
    if args.bulk:
        cfg.bulk_type = args.bulk
    if args.lang:
        cfg.languages = [] if args.lang == ["any"] else args.lang
    ingest.run_ingest(cfg, force=args.force, with_tags=not args.no_tags,
                      with_rulings=not args.no_rulings)
    return 0


def cmd_images(args) -> int:
    cfg = _cfg(args)
    if args.format:
        cfg.image_formats = args.format
    if args.jobs:
        cfg.image_concurrency = args.jobs
    conn = db.connect(cfg.db_path)
    if args.icons:
        images.download_icons(conn, cfg)
    stats = images.download_images(
        conn, cfg, unique_art_only=args.unique_art, sets=args.set,
        refresh=args.refresh, limit=args.limit, dry_run=args.dry_run)
    print(f"{stats['downloaded']} téléchargée(s), {stats['failed']} échec(s), "
          f"{human_bytes(stats['bytes'])}")
    conn.close()
    return 0


def cmd_search(args) -> int:
    cfg = _cfg(args)
    if not cfg.db_path.exists():
        print(f"base absente : {cfg.db_path} — lance « mtgc ingest »", file=sys.stderr)
        return 2
    conn = db.connect(cfg.db_path, readonly=True)
    query = " ".join(args.query)
    try:
        if args.explain:
            sql, params, opts = build_sql(query, limit=args.limit)
            print(sql)
            print("-- params:", params)
            return 0
        res = search(conn, query, limit=args.limit, offset=args.offset)
    except QueryError as exc:
        print(f"erreur de requête : {exc}", file=sys.stderr)
        return 2
    for warn in res.options.warnings:
        print(f"[!] {warn}", file=sys.stderr)

    if args.json:
        out = [dict(r) for r in res.rows]
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for r in res.rows:
            mana = r["mana_cost"] or ""
            pt = ""
            if r["power"] is not None:
                pt = f" {r['power']}/{r['toughness']}"
            print(f"{r['name']:<34} {mana:<14} {r['type_line'] or '':<32} "
                  f"{(r['set_code'] or '').upper():>5} {r['collector_number'] or '':>6}"
                  f"{pt}")
    print(f"\n{len(res.rows)} affichée(s) sur {res.total} résultat(s)", file=sys.stderr)
    conn.close()
    return 0


def cmd_catalog(args) -> int:
    cfg = _cfg(args)
    if args.per_volume:
        cfg.cards_per_volume = args.per_volume
    conn = db.connect(cfg.db_path, readonly=True)
    produced = latex.build_catalog(
        conn, cfg, sets=args.set, unique_art_only=args.unique_art,
        volumes=args.volume, compile_pdf=not args.tex_only)
    for p in produced:
        print(p)
    conn.close()
    return 0 if produced else 1


def cmd_stats(args) -> int:
    cfg = _cfg(args)
    if not cfg.db_path.exists():
        print(f"base absente : {cfg.db_path}", file=sys.stderr)
        return 2
    conn = db.connect(cfg.db_path, readonly=True)
    print(f"base : {cfg.db_path} ({human_bytes(cfg.db_path.stat().st_size)})")
    for table, n in db.counts(conn).items():
        print(f"  {table:<18} {n:>9}")
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS b FROM images").fetchone()
    print(f"  images sur disque  {row['n']:>9}  ({human_bytes(row['b'])})")
    for key in ("ingested_at", "bulk_type"):
        print(f"  {key:<18} {db.get_meta(conn, key)}")
    print("\n  répartition par image_status :")
    for r in conn.execute("SELECT image_status AS s, COUNT(*) AS n FROM cards "
                          "GROUP BY s ORDER BY n DESC"):
        print(f"    {str(r['s']):<16} {r['n']:>9}")
    conn.close()
    return 0


def cmd_syntax(args) -> int:
    print("Mots-clés implémentés (alias entre parenthèses) :\n")
    for key, aliases in sorted(supported_keywords().items()):
        alias = f"  ({', '.join(aliases)})" if aliases else ""
        print(f"  {key}:{alias}")
    print("\nPrédicats is: / not: :\n")
    preds = sorted(IS_PREDICATES)
    for i in range(0, len(preds), 4):
        print("  " + "".join(f"{p:<20}" for p in preds[i:i + 4]))
    print("\nReconnus mais non implémentés :\n")
    for key, why in sorted(UNSUPPORTED.items()):
        print(f"  {key}: — {why}")
    return 0


def cmd_config(args) -> int:
    cfg = _cfg(args)
    for k, v in cfg.to_dict().items():
        print(f"{k} = {v!r}")
    print(f"\ndb_path     = {cfg.db_path}")
    print(f"images_dir  = {cfg.images_dir}")
    print(f"out_dir     = {cfg.out_dir}")
    return 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mtgc", description="MTGcyCLAUDEpedia — un Scryfall local")
    p.add_argument("-c", "--config", help="fichier TOML de configuration")
    p.add_argument("-d", "--data-dir", help="racine des données")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("ingest", help="télécharger les bulk et remplir SQLite")
    q.add_argument("--bulk", choices=["default_cards", "all_cards", "oracle_cards",
                                      "unique_artwork"],
                   help="fichier bulk source (défaut : default_cards)")
    q.add_argument("--lang", nargs="+",
                   help="langues à conserver, ou 'any' pour tout garder")
    q.add_argument("--force", action="store_true", help="re-télécharger les bulk")
    q.add_argument("--no-tags", action="store_true")
    q.add_argument("--no-rulings", action="store_true")
    q.set_defaults(func=cmd_ingest)

    i = sub.add_parser("images", help="télécharger les images par set")
    i.add_argument("--format", nargs="+",
                   choices=list(images.QUALITY_ORDER),
                   help="formats par préférence décroissante (défaut : png)")
    i.add_argument("--unique-art", action="store_true",
                   help="ne prendre qu'un représentant par illustration")
    i.add_argument("--set", nargs="+", help="limiter à ces codes de set")
    i.add_argument("--refresh", action="store_true",
                   help="re-télécharger même si déjà présent")
    i.add_argument("--icons", action="store_true", help="aussi les icônes de set")
    i.add_argument("--limit", type=int)
    i.add_argument("--jobs", type=int, help="téléchargements simultanés")
    i.add_argument("--dry-run", action="store_true")
    i.set_defaults(func=cmd_images)

    s = sub.add_parser("search", help="rechercher (syntaxe Scryfall)")
    s.add_argument("query", nargs="+")
    s.add_argument("-n", "--limit", type=int, default=50)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--json", action="store_true")
    s.add_argument("--explain", action="store_true", help="afficher le SQL généré")
    s.set_defaults(func=cmd_search)

    c = sub.add_parser("catalog", help="générer les catalogues PDF")
    c.add_argument("--set", nargs="+")
    c.add_argument("--volume", nargs="+", type=int, help="ne produire que ces volumes")
    c.add_argument("--per-volume", type=int, help="images par volume")
    c.add_argument("--unique-art", action="store_true")
    c.add_argument("--tex-only", action="store_true", help="ne pas compiler")
    c.add_argument("--static-dir", help="polices, fond, placeholder")
    c.set_defaults(func=cmd_catalog)

    sub.add_parser("stats", help="statistiques de la base").set_defaults(func=cmd_stats)
    sub.add_parser("syntax", help="mots-clés supportés").set_defaults(func=cmd_syntax)
    sub.add_parser("config", help="configuration effective").set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
