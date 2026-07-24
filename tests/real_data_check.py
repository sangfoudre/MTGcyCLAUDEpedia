"""ATTENTION — attentes NAÏVES pour 13 assertions de requêtes.

Ce banc compare aux champs *racine* des objets JSON via des prédicats
Python simples, alors que la base applique la sémantique Scryfall réelle
(couleurs et stats face par face, ``id:`` en sous-ensemble, ``o:`` sans
texte de rappel). Ses échecs sur c:* / id:* / o:* sont ATTENDUS.
Le test faisant foi est ``api_parity.py``.

Banc d'essai sur données Scryfall réelles (échantillon default_cards du jour).

Ingère l'échantillon dans une base temporaire, puis exécute une batterie de
requêtes en vérifiant les résultats contre des calculs Python indépendants.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtgc import db, ingest  # noqa: E402
from mtgc.config import Config  # noqa: E402
from mtgc.query import search, QueryError  # noqa: E402

SAMPLE = Path("/home/claude/sample.json")


def load_sample() -> list[dict]:
    return json.loads(SAMPLE.read_text())


def build_db(cards: list[dict], tmp: Path) -> sqlite3.Connection:
    """Écrit l'échantillon en .jsonl puis le fait ingérer par le vrai code."""
    jsonl = tmp / "sample.jsonl"
    with jsonl.open("w", encoding="utf-8") as fh:
        for c in cards:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")

    cfg = Config(data_dir=tmp, static_dir=tmp / "static")
    cfg.languages = []  # aucun filtre : on veut tout voir
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)

    # les sets doivent précéder les cartes (FK cards.set_code -> sets.code)
    sets = json.loads(Path("/home/claude/sets.json").read_text())["data"]
    conn.executemany(
        "INSERT OR IGNORE INTO sets(id, code, name, set_type, released_at, "
        "released_year, card_count, digital) VALUES(?,?,?,?,?,?,?,?)",
        [(s["id"], s["code"], s["name"], s.get("set_type"), s.get("released_at"),
          int(s["released_at"][:4]) if s.get("released_at") else None,
          s.get("card_count"), int(bool(s.get("digital")))) for s in sets],
    )
    conn.commit()
    print(f"  sets : {len(sets)}")

    t0 = time.time()
    n = ingest.ingest_cards(conn, jsonl, cfg)
    dt = time.time() - t0
    print(f"  ingestion : {n} cartes en {dt:.2f}s  ({n/dt:,.0f} cartes/s)")

    t0 = time.time()
    r = ingest.mark_unique_artwork(conn)
    print(f"  dédup illustrations : {r} représentants en {time.time()-t0:.2f}s")
    db.rebuild_fts(conn)
    db.analyze(conn)
    return conn


def check(label: str, got, want, notes: str = "") -> bool:
    ok = got == want
    flag = "OK  " if ok else "FAIL"
    extra = f"   [{notes}]" if notes else ""
    print(f"  [{flag}] {label:<52} got={got:<7} want={want}{extra}")
    return ok


def main() -> int:
    cards = load_sample()
    print(f"Échantillon : {len(cards)} cartes réelles Scryfall\n")

    tmp = Path("/tmp/mtgc_real")
    tmp.mkdir(parents=True, exist_ok=True)
    for f in tmp.glob("*.sqlite3"):
        f.unlink()

    print("== INGESTION ==")
    conn = build_db(cards, tmp)
    c = db.counts(conn)
    print(f"  tables : {c}\n")

    fails = 0

    # ---------------------------------------------------------- intégrité
    print("== INTÉGRITÉ ==")
    n_db = conn.execute("SELECT count(*) FROM cards").fetchone()[0]
    fails += not check("aucune carte perdue", n_db, len(cards))

    # faces
    want_faces = sum(len(c.get("card_faces", [])) for c in cards)
    got_faces = conn.execute("SELECT count(*) FROM card_faces").fetchone()[0]
    fails += not check("faces ingérées", got_faces, want_faces)

    # illustration_id résolu racine OU face
    def has_illus(c):
        if c.get("illustration_id"):
            return True
        return any(f.get("illustration_id") for f in c.get("card_faces", []))

    want_illus = sum(1 for c in cards if has_illus(c))
    got_illus = conn.execute(
        "SELECT count(*) FROM cards WHERE illustration_id IS NOT NULL"
    ).fetchone()[0]
    fails += not check("cartes avec illustration_id", got_illus, want_illus,
                       "racine ou 1re face")

    # ------------------------------------------------------------ requêtes
    print("\n== REQUÊTES (vérifiées contre calcul Python indépendant) ==")

    def py_count(pred):
        return sum(1 for c in cards if pred(c))

    RAW = " unique:prints include:extras"

    def sql_count(q, raw=True):
        try:
            return len(search(conn, q + (RAW if raw else ""), limit=None).rows)
        except QueryError as e:
            print(f"    !! QueryError sur {q!r}: {e}")
            return -1

    W, U, B, R, G = 1, 2, 4, 8, 16

    tests = [
        ("t:creature",
         lambda c: "creature" in (c.get("type_line") or "").lower()),
        ("t:instant",
         lambda c: "instant" in (c.get("type_line") or "").lower()),
        ("r:mythic", lambda c: c["rarity"] == "mythic"),
        ("r>=rare", lambda c: c["rarity"] in ("rare", "mythic")),
        ("e:lea", lambda c: c["set"] == "lea"),
        ("lang:ja", lambda c: c["lang"] == "ja"),
        ("is:promo", lambda c: c.get("promo") is True),
        ("is:reprint", lambda c: c.get("reprint") is True),
        ("is:fullart", lambda c: c.get("full_art") is True),
        ("is:textless", lambda c: c.get("textless") is True),
        ("is:digital", lambda c: c.get("digital") is True),
        ("is:reserved", lambda c: c.get("reserved") is True),
        ("border:black", lambda c: c.get("border_color") == "black"),
        ("frame:1993", lambda c: c.get("frame") == "1993"),
        ("cmc=3", lambda c: c.get("cmc") == 3),
        ("cmc>=7", lambda c: (c.get("cmc") or 0) >= 7),
        ("stamp:oval", lambda c: c.get("security_stamp") == "oval"),
        ("watermark:*", None),  # placeholder, retiré plus bas
    ]
    tests = [t for t in tests if t[1] is not None]

    for q, pred in tests:
        got, want = sql_count(q), py_count(pred)
        fails += not check(q, got, want)

    # ------------------------------------------------------- couleurs
    print("\n== COULEURS (bitmask WUBRG) ==")

    def cols(c):
        return set(c.get("colors") or [])

    def ci(c):
        return set(c.get("color_identity") or [])

    color_tests = [
        ("c:r", lambda c: "R" in cols(c)),
        ("c=r", lambda c: cols(c) == {"R"}),
        ("c>=ur", lambda c: {"U", "R"} <= cols(c)),
        ("c<=ur", lambda c: cols(c) <= {"U", "R"} and cols(c) != set()),
        ("c:c", lambda c: cols(c) == set() and "image_uris" in c or False),
        ("c:m", lambda c: len(cols(c)) > 1),
        ("id<=wu", lambda c: ci(c) <= {"W", "U"}),
        ("id:g", lambda c: "G" in ci(c)),
    ]
    for q, pred in color_tests:
        if q == "c:c":
            # incolore = colors vide, mais seulement pour les cartes AYANT le champ
            pred = lambda c: "colors" in c and not c["colors"]
        got, want = sql_count(q), py_count(pred)
        fails += not check(q, got, want)

    # ------------------------------------------------------ texte / FTS
    print("\n== TEXTE (FTS5 trigram) ==")
    text_tests = [
        ('o:flying', lambda c: "flying" in (c.get("oracle_text") or "").lower()),
        ('o:"draw a card"',
         lambda c: "draw a card" in (c.get("oracle_text") or "").lower()),
        ('a:"Mark Poole"',
         lambda c: "mark poole" in (c.get("artist") or "").lower()),
        ('ft:the', lambda c: "the" in (c.get("flavor_text") or "").lower()),
    ]
    for q, pred in text_tests:
        got, want = sql_count(q), py_count(pred)
        fails += not check(q, got, want)

    # --------------------------------------------------------- booléens
    print("\n== BOOLÉENS / NÉGATION / PARENTHÈSES ==")
    bool_tests = [
        ("t:creature c:r",
         lambda c: "creature" in (c.get("type_line") or "").lower()
                   and "R" in cols(c)),
        ("t:creature -c:r",
         lambda c: "creature" in (c.get("type_line") or "").lower()
                   and "R" not in cols(c)),
        ("r:mythic OR r:rare",
         lambda c: c["rarity"] in ("mythic", "rare")),
        ("(c:r OR c:u) t:instant",
         lambda c: ("R" in cols(c) or "U" in cols(c))
                   and "instant" in (c.get("type_line") or "").lower()),
        ('!"Lightning Bolt"', lambda c: c["name"] == "Lightning Bolt"),
    ]
    for q, pred in bool_tests:
        got, want = sql_count(q), py_count(pred)
        fails += not check(q, got, want)

    # ------------------------------------------------------- tri naturel
    print("\n== TRI NATUREL DES COLLECTOR NUMBERS ==")
    rows = conn.execute(
        "SELECT collector_number FROM cards WHERE set_code='lea' "
        "ORDER BY cn_num, cn_suffix LIMIT 12"
    ).fetchall()
    order = [r[0] for r in rows]
    print(f"    lea : {order}")
    nums = [int(x) for x in order if x.isdigit()]
    fails += not check("ordre croissant", nums == sorted(nums), True)

    # collector numbers exotiques préservés
    odd = conn.execute(
        "SELECT count(*) FROM cards WHERE collector_number GLOB '*[★†]*'"
    ).fetchone()[0]
    want_odd = py_count(lambda c: any(ch in c["collector_number"] for ch in "★†"))
    fails += not check("collector numbers ★/† préservés", odd, want_odd)

    # ---------------------------------------------------- dédup illustrations
    print("\n== DÉDUPLICATION DES ILLUSTRATIONS ==")
    distinct = conn.execute(
        "SELECT count(DISTINCT illustration_id) FROM cards "
        "WHERE illustration_id IS NOT NULL"
    ).fetchone()[0]
    repr_n = conn.execute(
        "SELECT count(*) FROM cards WHERE is_unique_art=1 AND illustration_id IS NOT NULL"
    ).fetchone()[0]
    fails += not check("1 représentant par illustration", repr_n, distinct)

    # les cartes sans illustration_id doivent quand même être représentées
    orphan_repr = conn.execute(
        "SELECT count(*) FROM cards WHERE illustration_id IS NULL AND is_unique_art=1"
    ).fetchone()[0]
    orphan_tot = conn.execute(
        "SELECT count(*) FROM cards WHERE illustration_id IS NULL"
    ).fetchone()[0]
    fails += not check("cartes sans illustration conservées", orphan_repr, orphan_tot,
                       "zéro perte")

    # le représentant doit être le meilleur scan disponible
    bad = conn.execute("""
        SELECT count(*) FROM cards a
        WHERE a.is_unique_art=1 AND a.image_status='lowres'
          AND EXISTS (SELECT 1 FROM cards b
                      WHERE b.illustration_id=a.illustration_id
                        AND b.image_status='highres_scan')
    """).fetchone()[0]
    fails += not check("représentant = meilleur scan", bad, 0)

    print("\n" + "=" * 72)
    print(f"  {'TOUT PASSE' if fails == 0 else f'{fails} ÉCHEC(S)'}")
    print("=" * 72)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
