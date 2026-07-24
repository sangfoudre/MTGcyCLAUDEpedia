"""Test d'intégration sur données Scryfall réelles.

Usage : python3 tests/test_integration.py /chemin/echantillon.jsonl.gz
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtgc import db, images, ingest, latex  # noqa: E402
from mtgc.config import Config  # noqa: E402
from mtgc.query import search  # noqa: E402
from mtgc.util import human_bytes, setup_logging  # noqa: E402


def main(sample: Path, workdir: Path) -> int:
    setup_logging(False)
    cfg = Config()
    cfg.data_dir = workdir
    cfg.static_dir = workdir / "static"
    cfg.languages = ["en"]
    cfg.image_formats = ["large"]
    cfg.image_concurrency = 8
    cfg.ensure_dirs()

    conn = db.connect(cfg.db_path)
    db.init_schema(conn)

    print("\n=== 1. ingestion des sets (API réelle) ===")
    ingest.ingest_sets(conn, cfg)

    print("\n=== 2. ingestion des cartes (échantillon réel) ===")
    ingest.ingest_cards(conn, sample, cfg)
    ingest.mark_unique_artwork(conn)
    db.rebuild_fts(conn)
    db.analyze(conn)

    print("\n=== 3. statistiques ===")
    for table, n in db.counts(conn).items():
        print(f"  {table:<18} {n:>8}")

    print("\n=== 4. requêtes réelles ===")
    queries = [
        "c:r t:instant mv<=1",
        "t:creature pow>=7 tou>=7",
        'o:"draw a card" c<=u',
        "is:multicolor r:mythic",
        "a:rebecca",
        "t:legendary t:dragon",
        'o:/^Flying$/',
        "is:vanilla t:creature",
        "c>=wu t:creature mv=3",
        "is:transform",
        "f:commander t:planeswalker",
        "year>=2024 r:mythic",
        "keyword:trample c:g",
        "m:WW",
        "-is:reprint r:rare s:blb",
    ]
    for q in queries:
        try:
            res = search(conn, q, limit=3)
            sample_names = ", ".join(r["name"] for r in res.rows[:3]) or "—"
            warn = " [!]" if res.options.warnings else ""
            print(f"  {q:<34} {res.total:>6} → {sample_names[:58]}{warn}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {q:<34} ERREUR {type(exc).__name__}: {exc}")

    print("\n=== 5. téléchargement d'images (petit set) ===")
    target = conn.execute(
        "SELECT set_code, COUNT(*) n FROM cards WHERE is_unique_art=1 "
        "GROUP BY set_code HAVING n BETWEEN 12 AND 30 ORDER BY n DESC LIMIT 1"
    ).fetchone()
    if target:
        code = target["set_code"]
        print(f"  set choisi : {code} ({target['n']} cartes)")
        stats = images.download_images(conn, cfg, unique_art_only=True, sets=[code])
        print(f"  {stats['downloaded']} images, {stats['failed']} échecs, "
              f"{human_bytes(stats['bytes'])}")
    else:
        code = None
        print("  aucun set de taille adaptée")

    print("\n=== 6. génération du catalogue PDF ===")
    if code:
        cfg.cards_per_volume = 500
        produced = latex.build_catalog(conn, cfg, sets=[code], unique_art_only=True)
        for p in produced:
            print(f"  produit : {p} ({human_bytes(p.stat().st_size)})")
        if not produced:
            return 1
    conn.close()
    print("\nOK")
    return 0


if __name__ == "__main__":
    sample = Path(sys.argv[1])
    workdir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/mtgc-test")
    sys.exit(main(sample, workdir))
