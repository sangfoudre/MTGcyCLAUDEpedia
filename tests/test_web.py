"""Le générateur web n'affiche que ce qui est sur le disque, sans perte."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mtgc_web", ROOT / "src" / "mtgc-web.py")
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


def test_natural_key_orders_numerically():
    xs = ["10", "2", "100a", "100b", "3"]
    assert sorted(xs, key=web.natural_key) == ["2", "3", "10", "100a", "100b"]


def test_sanitize_matches_downloader():
    assert web.sanitize_cn("232†") == "232-dagger"
    assert web.sanitize_cn("15★") == "15-star"


def test_stem_variants_covers_transliteration():
    v = web.stem_variants("232†")
    assert "232-dagger.jpg" in v and "232†.jpg" in v


def test_build_model_loses_no_image(tmp_path):
    # data-dir minimal : deux images, aucune métadonnée bulk
    sets = tmp_path / "sets" / "tst"
    sets.mkdir(parents=True)
    for name in ("1.jpg", "232-dagger.jpg"):
        (sets / name).write_bytes(b"\xff\xd8\xff\xe0stub")
    (tmp_path / "metadata").mkdir()
    got_sets, cards = web.build_model(tmp_path)
    total = sum(s["count"] for s in got_sets)
    assert total == 2, "toute image sur disque doit apparaître"


def test_slug():
    assert web.slug("Fifth Edition") == "fifth-edition"


def test_sanitize_never_diverges_from_downloader():
    """Les deux sanitize doivent coïncider : sinon des images sont perdues."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mtgc_images", ROOT / "src" / "mtgc-images.py")
    img = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(img)
    for cn in ["232†", "15★", "100a", "1", "★123†", "42", "GRN-7"]:
        assert web.sanitize_cn(cn) == img.sanitize(cn), cn
