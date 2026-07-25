"""Parité avec Scryfall : compare le moteur local à l'API sur un set complet.

C'est la validation la plus forte disponible : pour chaque requête, on demande
le même résultat à api.scryfall.com et à la base locale, puis on compare les
ensembles d'identifiants (pas seulement les comptes).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtgc import db, ingest  # noqa: E402
from mtgc.config import Config  # noqa: E402
from mtgc.query import search, QueryError  # noqa: E402

UA = {"User-Agent": "MTGcyCLAUDEpedia/2.0", "Accept": "application/json"}
SET = "isd"
CARDS = Path("/home/claude/isd.json")
SETS = Path("/home/claude/sets.json")

# Requêtes de parité. Toutes portent sur le set, pour rester comparables.
QUERIES = [
    "t:creature",
    "t:instant",
    "t:land",
    "t:legendary",
    "t:vampire",
    "t:werewolf",
    "c:r",
    "c=r",
    "c:u",
    "c>=ur",
    "c<=ur",
    "c:c",
    "c:m",
    "id:g",
    "id<=wu",
    "r:common",
    "r:mythic",
    "r>=rare",
    "cmc=3",
    "cmc>=6",
    "cmc<2",
    "pow>=4",
    "tou<=1",
    "pow>tou",
    "o:flying",
    "o:trample",
    'o:"draw a card"',
    "ft:blood",
    "is:transform",
    "is:dfc",
    "is:permanent",
    "is:spell",
    "is:vanilla",
    "is:reprint",
    "is:promo",
    "is:foil",
    "a:Hamm",
    "border:black",
    "frame:2003",
    "watermark:none",
    "t:creature c:g",
    "t:creature -c:g",
    "r:rare OR r:mythic",
    "(c:r OR c:u) t:instant",
    "t:creature pow>=3 cmc<=3",
    "keyword:flying",
    "is:split",
    "mv=2 t:creature c:w",
]


def api_ids(query: str) -> set[str] | None:
    """Identifiants renvoyés par Scryfall pour `set:SET query`."""
    q = f"set:{SET} ({query})"
    url = ("https://api.scryfall.com/cards/search?q="
           + urllib.parse.quote(q)
           + "&unique=prints&include_extras=true&include_variations=true")
    ids: set[str] = set()
    while url:
        time.sleep(0.12)
        try:
            req = urllib.request.Request(url, headers=UA)
            d = json.load(urllib.request.urlopen(req))
        except urllib.error.HTTPError as e:
            if e.code == 404:      # aucun résultat
                return set()
            raise
        ids |= {c["id"] for c in d["data"]}
        url = d.get("next_page") if d.get("has_more") else None
    return ids


def build() -> "sqlite3.Connection":
    tmp = Path("/tmp/mtgc_parity")
    tmp.mkdir(parents=True, exist_ok=True)
    dbf = tmp / "mtgc.sqlite3"
    if dbf.exists():
        dbf.unlink()

    cards = json.loads(CARDS.read_text())
    jsonl = tmp / "cards.jsonl"
    with jsonl.open("w", encoding="utf-8") as fh:
        for c in cards:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")

    cfg = Config(data_dir=tmp, static_dir=tmp / "static")
    cfg.languages = []
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)
    sets = json.loads(SETS.read_text())["data"]
    conn.executemany(
        "INSERT OR IGNORE INTO sets(id, code, name, set_type, released_at, "
        "released_year, card_count, digital) VALUES(?,?,?,?,?,?,?,?)",
        [(s["id"], s["code"], s["name"], s.get("set_type"), s.get("released_at"),
          int(s["released_at"][:4]) if s.get("released_at") else None,
          s.get("card_count"), int(bool(s.get("digital")))) for s in sets],
    )
    conn.commit()
    ingest.ingest_cards(conn, jsonl, cfg)
    ingest.mark_unique_artwork(conn)
    db.rebuild_fts(conn)
    db.analyze(conn)
    return conn


def main() -> int:
    conn = build()
    n = conn.execute("SELECT count(*) FROM cards").fetchone()[0]
    print(f"Base locale : {n} cartes du set '{SET}'\n")
    print(f"{'requête':<34} {'API':>5} {'local':>6}  écart")
    print("-" * 72)

    ok = miss = err = 0
    diffs = []
    for q in QUERIES:
        try:
            want = api_ids(q)
        except Exception as e:                       # noqa: BLE001
            print(f"{q:<34} !! API: {type(e).__name__}")
            err += 1
            continue
        try:
            local = search(conn, f"set:{SET} ({q}) unique:prints include:extras",
                           limit=None)
            got = {r["id"] for r in local.rows}
        except QueryError as e:
            print(f"{q:<34} {len(want):>5} {'—':>6}  NON SUPPORTÉ ({e})")
            err += 1
            continue

        if got == want:
            print(f"{q:<34} {len(want):>5} {len(got):>6}  ok")
            ok += 1
        else:
            extra, lack = got - want, want - got
            print(f"{q:<34} {len(want):>5} {len(got):>6}  "
                  f"+{len(extra)} / -{len(lack)}")
            miss += 1
            diffs.append((q, extra, lack))

    print("-" * 72)
    print(f"identiques {ok} | divergents {miss} | erreurs {err} "
          f"| total {len(QUERIES)}")

    if diffs:
        print("\n=== DÉTAIL DES DIVERGENCES ===")
        for q, extra, lack in diffs:
            print(f"\n  {q}")
            for label, ids in (("en trop", extra), ("manquants", lack)):
                for i in list(ids)[:4]:
                    r = conn.execute(
                        "SELECT name, type_line, mana_cost, layout, oracle_text "
                        "FROM cards WHERE id=?", (i,)).fetchone()
                    if r:
                        print(f"    {label:9} {r['name'][:36]:38} "
                              f"{(r['type_line'] or '')[:30]:32} {r['mana_cost'] or ''}")
                    else:
                        print(f"    {label:9} {i} (absent de la base locale)")
    return 1 if (miss or err) else 0


if __name__ == "__main__":
    raise SystemExit(main())
