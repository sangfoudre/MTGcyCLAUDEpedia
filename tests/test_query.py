"""Tests du moteur de recherche : parseur, compilateur, exécution SQL réelle."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtgc import db  # noqa: E402
from mtgc.query import QueryError, build_sql, search  # noqa: E402
from mtgc.query.parser import parse_query  # noqa: E402
from mtgc.util import colors_to_mask, split_collector_number, collector_sort_key  # noqa: E402


# ------------------------------------------------------------ fixtures

CARDS = [
    # (id, name, colors, ci, type_line, oracle, cmc, pow, tou, rarity, set, cn, artist, layout)
    ("1", "Lightning Bolt", "R", "R", "Instant",
     "Lightning Bolt deals 3 damage to any target.", 1, None, None, "common", "lea", "161",
     "Christopher Rush", "normal"),
    ("2", "Birds of Paradise", "G", "G", "Creature — Bird",
     "Flying\n{T}: Add one mana of any color.", 1, "0", "1", "rare", "lea", "094",
     "Mark Poole", "normal"),
    ("3", "Niv-Mizzet, Parun", "UR", "UR", "Legendary Creature — Dragon Wizard",
     "This spell can't be countered.\nFlying\nWhenever you draw a card, Niv-Mizzet, "
     "Parun deals 1 damage to any target.", 6, "5", "5", "rare", "grn", "192",
     "Chris Rahn", "normal"),
    ("4", "Deathrite Shaman", "BG", "BG", "Creature — Elf Shaman",
     "{T}: Exile target land card from a graveyard. Add one mana of any color.",
     1, "1", "2", "rare", "rtr", "213", "Steve Argyle", "normal"),
    ("5", "Grizzly Bears", "G", "G", "Creature — Bear", "", 2, "2", "2",
     "common", "lea", "195", "Jeff A. Menges", "normal"),
    ("6", "Delver of Secrets", "U", "U", "Creature — Human Insect",
     "At the beginning of your upkeep, look at the top card of your library.",
     1, "1", "1", "common", "isd", "51", "Nils Hamm", "transform"),
]

SETS = [
    ("lea", "Limited Edition Alpha", "core", "1993-08-05"),
    ("grn", "Guilds of Ravnica", "expansion", "2018-10-05"),
    ("rtr", "Return to Ravnica", "expansion", "2012-10-05"),
    ("isd", "Innistrad", "expansion", "2011-09-30"),
]

COLOR_BITS = {"W": 1, "U": 2, "B": 4, "R": 8, "G": 16}


def _mask(s):
    return sum(COLOR_BITS[c] for c in s)


def make_db() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.executemany(
        "INSERT INTO sets(id, code, name, set_type, released_at, released_year) "
        "VALUES(?,?,?,?,?,?)",
        [(c, c, n, t, d, int(d[:4])) for c, n, t, d in SETS])
    rank = {"common": 1, "uncommon": 2, "rare": 3, "mythic": 5}
    for (cid, name, colors, ci, tl, oracle, cmc, pw, tu, rarity, st, cn,
         artist, layout) in CARDS:
        conn.execute(
            """INSERT INTO cards(id, oracle_id, name, lang, layout, set_code,
                   collector_number, cn_num, cn_suffix, cn_prefix, rarity,
                   rarity_rank, released_at, released_year, cmc, type_line,
                   oracle_text, power, toughness, pow_num, tou_num, colors,
                   color_identity, color_count, ci_count, artist, artist_count,
                   illustration_id, is_unique_art, mana_cost, finishes_json,
                   games_json, legalities_json, prices_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, f"o{cid}", name, "en", layout, st, cn, int(cn), "", "",
             rarity, rank[rarity], dict((c, d) for c, _, _, d in SETS)[st],
             int(dict((c, d) for c, _, _, d in SETS)[st][:4]),
             cmc, tl, oracle, pw, tu,
             float(pw) if pw and pw.isdigit() else None,
             float(tu) if tu and tu.isdigit() else None,
             _mask(colors), _mask(ci), len(colors), len(ci), artist, 1,
             f"illus{cid}", 1, "{R}" if colors == "R" else "{G}",
             json.dumps(["nonfoil", "foil"]), json.dumps(["paper"]),
             json.dumps({"modern": "legal"}), json.dumps({"usd": "1.50"})))
        conn.execute("INSERT INTO legalities(card_id, format, status) VALUES(?,?,?)",
                     (cid, "modern", "legal"))
    conn.commit()
    db.rebuild_fts(conn)
    return conn


def names(conn, query, **kw):
    return sorted(r["name"] for r in search(conn, query, with_total=False, **kw).rows)


# -------------------------------------------------------------- parseur

def test_parser():
    assert parse_query("") is None
    assert parse_query("bolt") is not None
    # ne doit pas lever
    for q in ['c:r t:instant', '-t:creature', 'a OR b', '(c:r OR c:u) t:creature',
              'o:"draw a card"', '!"Lightning Bolt"', 'o:/dea?ls/', 'mv>=3 pow<=2']:
        assert parse_query(q) is not None, q
    for bad in ['o:"unclosed', '(c:r']:
        try:
            parse_query(bad)
            raise AssertionError(f"aurait dû échouer : {bad}")
        except QueryError:
            pass
    print("  parseur                       OK")


def test_util():
    assert colors_to_mask("rg") == 8 + 16
    assert colors_to_mask("izzet") == 2 + 8
    assert colors_to_mask("c") == 0
    assert split_collector_number("123a") == ("", 123, "a")
    assert split_collector_number("★12") == ("★", 12, "")
    assert split_collector_number("GR1") == ("GR", 1, "")
    # le bug de la v1 : zfill produisait "00012" < "003"
    assert sorted(["12", "3", "100"], key=collector_sort_key) == ["3", "12", "100"]
    print("  utilitaires                   OK")


# ------------------------------------------------------------ recherche

def test_search(conn):
    checks = [
        # (requête, noms attendus)
        ("bolt", ["Lightning Bolt"]),
        ('!"lightning bolt"', ["Lightning Bolt"]),
        ("c:r", ["Lightning Bolt", "Niv-Mizzet, Parun"]),
        ("c=r", ["Lightning Bolt"]),
        ("c>=ur", ["Niv-Mizzet, Parun"]),
        ("c<=g", ["Birds of Paradise", "Grizzly Bears"]),
        ("c:m", ["Deathrite Shaman", "Niv-Mizzet, Parun"]),
        ("id:bg", ["Deathrite Shaman"]),
        ("t:creature c:g", ["Birds of Paradise", "Deathrite Shaman", "Grizzly Bears"]),
        ("t:instant", ["Lightning Bolt"]),
        ('o:"any color"', ["Birds of Paradise", "Deathrite Shaman"]),
        ("o:flying", ["Birds of Paradise", "Niv-Mizzet, Parun"]),
        ("-t:creature", ["Lightning Bolt"]),
        ("c:r OR c:g", ["Birds of Paradise", "Deathrite Shaman", "Grizzly Bears",
                        "Lightning Bolt", "Niv-Mizzet, Parun"]),
        ("(c:u OR c:g) t:creature", ["Birds of Paradise", "Deathrite Shaman",
                                     "Delver of Secrets", "Grizzly Bears",
                                     "Niv-Mizzet, Parun"]),
        ("mv>=6", ["Niv-Mizzet, Parun"]),
        ("mv=1", ["Birds of Paradise", "Deathrite Shaman", "Delver of Secrets",
                  "Lightning Bolt"]),
        ("pow>=5", ["Niv-Mizzet, Parun"]),
        ("pow=tou", ["Grizzly Bears", "Niv-Mizzet, Parun", "Delver of Secrets"]),
        ("r:rare", ["Birds of Paradise", "Deathrite Shaman", "Niv-Mizzet, Parun"]),
        ("r>=rare", ["Birds of Paradise", "Deathrite Shaman", "Niv-Mizzet, Parun"]),
        ("s:lea", ["Birds of Paradise", "Grizzly Bears", "Lightning Bolt"]),
        ("st:core", ["Birds of Paradise", "Grizzly Bears", "Lightning Bolt"]),
        ("year<=1993", ["Birds of Paradise", "Grizzly Bears", "Lightning Bolt"]),
        ("year>2015", ["Niv-Mizzet, Parun"]),
        ("a:rahn", ["Niv-Mizzet, Parun"]),
        ('a:"Mark Poole"', ["Birds of Paradise"]),
        ("is:bear", ["Grizzly Bears"]),
        ("is:vanilla", ["Grizzly Bears"]),
        ("is:transform", ["Delver of Secrets"]),
        ("is:multicolor", ["Deathrite Shaman", "Niv-Mizzet, Parun"]),
        ("not:multicolor t:instant", ["Lightning Bolt"]),
        ("f:modern t:instant", ["Lightning Bolt"]),
        # la regex matche bien les deux cartes qui infligent des blessures
        ("o:/dea.s \\d+ damage/", ["Lightning Bolt", "Niv-Mizzet, Parun"]),
        ("o:/deals 3 damage/", ["Lightning Bolt"]),
        ("t:creature -c:g pow>=1", ["Delver of Secrets", "Niv-Mizzet, Parun"]),
        ("cn:161", ["Lightning Bolt"]),
        ("lang:en t:instant", ["Lightning Bolt"]),
        ("lang:any t:instant", ["Lightning Bolt"]),
        ("usd<2 t:instant", ["Lightning Bolt"]),
    ]
    failures = []
    for query, expected in checks:
        try:
            got = names(conn, query)
        except Exception as exc:  # noqa: BLE001
            failures.append((query, expected, f"EXCEPTION {type(exc).__name__}: {exc}"))
            continue
        if got != sorted(expected):
            failures.append((query, sorted(expected), got))
    for query, expected, got in failures:
        print(f"  ÉCHEC {query!r}\n        attendu {expected}\n        obtenu  {got}")
    print(f"  recherche                     {len(checks) - len(failures)}/{len(checks)}")
    return failures


def test_options(conn):
    r = search(conn, "t:creature order:cmc direction:asc", with_total=False)
    cmcs = [row["cmc"] for row in r.rows]
    assert cmcs == sorted(cmcs), cmcs
    r = search(conn, "c:r unique:art", with_total=False)
    assert r.options.unique == "art"
    r = search(conn, "t:creature cube:vintage", with_total=False)
    assert any("cube" in w for w in r.options.warnings), r.options.warnings
    r = search(conn, "t:creature bogus:xyz", with_total=False)
    assert any("bogus" in w for w in r.options.warnings), r.options.warnings
    print("  options & avertissements      OK")


def test_mana():
    from mtgc.db import _mana_matches
    assert _mana_matches("{2}{W}{W}", "WW", ":") == 1
    assert _mana_matches("{2}{W}", "WW", ":") == 0
    assert _mana_matches("{2}{W}{W}", "{2}{W}{W}", "=") == 1
    assert _mana_matches("{W/U}", "W/U", ":") == 1
    print("  coûts de mana                 OK")


if __name__ == "__main__":
    print("Tests moteur de recherche")
    test_util()
    test_parser()
    test_mana()
    conn = make_db()
    fails = test_search(conn)
    test_options(conn)
    print("OK" if not fails else f"{len(fails)} échec(s)")
    sys.exit(1 if fails else 0)
