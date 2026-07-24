"""Fixtures partagées. Fournit `conn` : base en mémoire peuplée par CARDS."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mtgc import db  # noqa: E402
from mtgc.util import (list_to_mask, numeric_or_none, popcount,  # noqa: E402
                       rarity_rank, split_collector_number, strip_reminder)


@pytest.fixture()
def conn():
    from test_query import CARDS

    c = db.connect(":memory:")
    db.init_schema(c)
    c.execute("INSERT INTO sets(id, code, name, set_type, released_at) "
              "SELECT DISTINCT ?, ?, ?, 'expansion', '2000-01-01'", ("x", "x", "x"))
    seen = set()
    for row in CARDS:
        (cid, name, colors, ci, type_line, oracle, cmc, pw, tu,
         rarity, set_code, cn, artist, layout) = row
        if set_code not in seen:
            c.execute("INSERT OR IGNORE INTO sets(id, code, name, set_type, "
                      "released_at) VALUES(?,?,?,?,?)",
                      (set_code, set_code, set_code.upper(), "expansion",
                       "2000-01-01"))
            seen.add(set_code)
        _, cn_num, cn_suf = split_collector_number(cn)
        cmask, cimask = list_to_mask(colors), list_to_mask(ci)
        c.execute(
            "INSERT INTO cards(id, oracle_id, name, lang, layout, set_code, "
            "collector_number, cn_num, cn_suffix, rarity, rarity_rank, cmc, "
            "type_line, oracle_text, oracle_plain, power, toughness, pow_num, "
            "tou_num, colors, color_identity, color_count, ci_count, artist, "
            "legalities_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, cid, name, "en", layout, set_code, cn, cn_num, cn_suf,
             rarity, rarity_rank(rarity), cmc, type_line, oracle,
             strip_reminder(oracle), pw, tu, numeric_or_none(pw),
             numeric_or_none(tu), cmask, cimask, popcount(cmask),
             popcount(cimask), artist, json.dumps({"modern": "legal"})))
    c.commit()
    db.rebuild_fts(c)
    return c
