"""Le générateur web n'affiche que ce qui est sur le disque, sans perte."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mtgc", ROOT / "src" / "mtgc.py")
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


def test_natural_key_orders_numerically():
    xs = ["10", "2", "100a", "100b", "3"]
    assert sorted(xs, key=web.natural_key) == ["2", "3", "10", "100a", "100b"]


def test_sanitize_matches_downloader():
    assert web.sanitize_cn("232†") == "232-dagger"
    assert web.sanitize_cn("15★") == "15-star"


def test_image_names_new_scheme():
    v = web.image_names("5ed", "232", "normal")["front"]
    assert "5ed-232.jpg" in v and "5ed-232.png" in v


def test_image_names_transliterates_dagger():
    v = web.image_names("5ed", "232†", "normal")["front"]
    assert "5ed-232-dagger.jpg" in v


def test_image_names_dfc_faces():
    n = web.image_names("isd", "51", "transform")
    assert "isd-51-a.jpg" in n["front"] and "isd-51-b.jpg" in n["back"]


def test_build_model_loses_no_image(tmp_path):
    # data-dir minimal : deux images au schéma <code>-<num>, aucun bulk
    sets = tmp_path / "sets" / "tst"
    sets.mkdir(parents=True)
    for name in ("tst-1.jpg", "tst-232-dagger.jpg"):
        (sets / name).write_bytes(b"\xff\xd8\xff\xe0stub")
    (tmp_path / "metadata").mkdir()
    got_sets, cards, by_oracle, rulings = web.build_model(tmp_path)
    total = sum(s["count"] for s in got_sets)
    assert total == 2, "toute image sur disque doit apparaître"


def test_slug():
    assert web.slug("Fifth Edition") == "fifth-edition"


def test_sanitize_never_diverges_from_downloader():
    """Les deux sanitize doivent coïncider : sinon des images sont perdues."""
    import importlib.util
    # dans l'outil unifié, sanitize (téléchargeur) et sanitize_cn (web) doivent
    # toujours coïncider : sinon des images seraient perdues.
    for cn in ["232†", "15★", "100a", "1", "★123†", "42", "GRN-7"]:
        assert web.sanitize_cn(cn) == web.sanitize(cn), cn


def test_download_runs_without_nameerror(tmp_path, monkeypatch):
    """run_downloads doit s'exécuter sans NameError (bug cf. de la fusion 2.0).

    Un simple import ne détecte pas les alias manquants dans le corps d'une
    fonction : il faut réellement l'exécuter. On simule un job avec un
    téléchargeur bouchonné pour ne pas toucher le réseau.
    """
    import types
    # data-dir minimal
    (tmp_path / "sets").mkdir()
    conn = web.open_manifest(tmp_path / "m.sqlite3")

    # un job bidon qui écrit un octet, sans réseau
    job = web.Job.__new__(web.Job)
    for attr, val in [("url", "http://x/y.jpg"),
                      ("path", tmp_path / "sets" / "tst" / "tst-1.jpg"),
                      ("card_id", "id"), ("face", 0), ("fmt", "small"),
                      ("illustration", "il"), ("updated", ""),
                      ("set_code", "tst"), ("link_to", None)]:
        setattr(job, attr, val)

    monkeypatch.setattr(web, "download_file",
                        lambda url, dest, timeout=120: (dest.parent.mkdir(
                            parents=True, exist_ok=True),
                            dest.write_bytes(b"\xff\xd8\xff\xe0x"), 5)[-1])

    cfg = types.SimpleNamespace(jobs=2, dry_run=False, overwrite=False,
                                images_dir=tmp_path / "sets")
    res = web.run_downloads([job], cfg, conn)
    assert res["téléchargées"] == 1
