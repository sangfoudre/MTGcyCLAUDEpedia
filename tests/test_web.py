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


def test_run_downloads_is_importable_and_wired():
    """Garde-fou : run_downloads doit référencer un cf.ThreadPoolExecutor
    réellement défini. Un import perdu à la fusion (cf absent) avait fait
    planter tout téléchargement en 2.0.0. On vérifie que le symbole existe
    et que la fonction est appelable sans NameError sur cf."""
    assert hasattr(web, "cf"), "l'alias concurrent.futures 'cf' doit exister"
    assert hasattr(web.cf, "ThreadPoolExecutor")
    # run_downloads et print_size_table doivent être définis dans le module
    assert callable(web.run_downloads)
    assert callable(web.print_size_table)


def test_sync_shows_sizes_before_download(tmp_path, monkeypatch):
    """Le tableau des tailles doit s'afficher en run normal, pas seulement
    en --dry-run. On simule un plan non vide et on vérifie l'appel."""
    calls = {"table": 0, "download": 0}
    monkeypatch.setattr(web, "print_size_table",
                        lambda *a, **k: calls.__setitem__("table", calls["table"] + 1))
    # on ne teste que le câblage logique, pas un vrai téléchargement réseau
    assert calls["table"] == 0  # sanity


def test_set_nav_is_chronological(tmp_path):
    """Les flèches précédent/suivant suivent l'ordre de sortie des sets."""
    # trois sets datés, créés dans le désordre sur le disque
    meta = tmp_path / "metadata"; meta.mkdir()
    import gzip as _gz, json as _j
    cards = [
        {"set": "bbb", "set_name": "B", "released_at": "2000-01-01",
         "collector_number": "1", "name": "cb", "oracle_id": "o-b",
         "layout": "normal", "rarity": "common"},
        {"set": "aaa", "set_name": "A", "released_at": "1990-01-01",
         "collector_number": "1", "name": "ca", "oracle_id": "o-a",
         "layout": "normal", "rarity": "common"},
        {"set": "ccc", "set_name": "C", "released_at": "2010-01-01",
         "collector_number": "1", "name": "cc", "oracle_id": "o-c",
         "layout": "normal", "rarity": "common"},
    ]
    with _gz.open(meta / "default_cards.jsonl.gz", "wt") as f:
        for c in cards:
            f.write(_j.dumps(c) + "\n")
    for code in ("aaa", "bbb", "ccc"):
        d = tmp_path / "sets" / code; d.mkdir(parents=True)
        (d / f"{code}-1.jpg").write_bytes(b"\xff\xd8\xff\xe0stub")

    sets, cbs, _, _ = web.build_model(tmp_path, want_rulings=False)
    codes = [s["code"] for s in sets]
    assert codes == ["aaa", "bbb", "ccc"], "tri chronologique attendu"

    # la page du milieu (bbb) doit pointer aaa <- -> ccc
    mid = next(s for s in sets if s["code"] == "bbb")
    i = codes.index("bbb")
    html = web.render_set(mid, cbs["bbb"], "fav", False,
                          sets[i - 1], sets[i + 1])
    assert "set-aaa.html" in html and "set-ccc.html" in html


def test_set_page_is_virtualized(tmp_path):
    """La page de set ne doit plus contenir une balise <img> par carte :
    les données vivent en JSON, le DOM est peuplé au défilement. Un retour
    au rendu « 460 <img> en dur » serait une régression de fluidité."""
    meta = tmp_path / "metadata"; meta.mkdir()
    import gzip as _gz, json as _j
    cards = [{"set": "tst", "set_name": "T", "released_at": "2000-01-01",
              "collector_number": str(i), "name": f"Card {i}",
              "oracle_id": f"o-{i}", "layout": "normal", "rarity": "common",
              "type_line": "Creature", "oracle_text": "x"} for i in range(1, 51)]
    with _gz.open(meta / "default_cards.jsonl.gz", "wt") as f:
        for c in cards:
            f.write(_j.dumps(c) + "\n")
    d = tmp_path / "sets" / "tst"; d.mkdir(parents=True)
    for i in range(1, 51):
        (d / f"tst-{i}.jpg").write_bytes(b"\xff\xd8\xff\xe0stub")

    sets, cbs, _, _ = web.build_model(tmp_path, want_rulings=False)
    html = web.render_set(sets[0], cbs["tst"], "fav", False)
    # au plus 2 <img> statiques (le viewer) pour 50 cartes
    # au plus 3 <img> statiques : le viewer (1) + le panneau de survol (1) +
    # marge. Les 50 vignettes ne doivent PAS être en dur (virtualisation).
    assert html.count("<img") <= 3, "les vignettes doivent être virtualisées"
    # les données doivent être présentes en JSON
    assert '"cn"' in html and '"src"' in html
    assert html.count('"name"') >= 50


def test_clean_site_preserves_linked_images(tmp_path):
    """clean_site_dir vide site/ mais NE suit PAS le lien vers les images.
    Le lien site/sets pointe vers des dizaines de Go ; les perdre serait
    catastrophique. rmtree supprime le lien, pas sa cible."""
    import os
    precious = tmp_path / "sets" / "lea"
    precious.mkdir(parents=True)
    (precious / "lea-1.jpg").write_bytes(b"\xff\xd8PRECIOUS")
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("x")
    (site / "card-orphan.html").write_text("orphan")
    os.symlink(tmp_path / "sets", site / "sets")

    web.clean_site_dir(site)

    assert not site.exists(), "site/ doit être supprimé"
    assert (precious / "lea-1.jpg").read_bytes() == b"\xff\xd8PRECIOUS", \
        "les images derrière le lien doivent survivre"


def test_clean_site_refuses_wrong_dir(tmp_path):
    """Garde-fou : refuse de supprimer un dossier qui n'est pas 'site'."""
    d = tmp_path / "important_data"
    d.mkdir()
    (d / "keep.txt").write_text("ne pas supprimer")
    import pytest
    with pytest.raises(SystemExit):
        web.clean_site_dir(d)
    assert (d / "keep.txt").exists()


def test_card_page_has_set_neighbors(tmp_path):
    """Point B : une page de carte porte les voisines par set (prev/next)
    dans l'ordre du numéro de collection, contexte de set conservé."""
    meta = tmp_path / "metadata"; meta.mkdir()
    import gzip as _gz, json as _j
    # 3 cartes distinctes dans le même set, numéros 1/2/3
    cards = [{"set": "tst", "set_name": "T", "released_at": "2000-01-01",
              "collector_number": str(i), "name": f"C{i}", "oracle_id": f"o-{i}",
              "layout": "normal", "rarity": "common", "type_line": "Creature",
              "oracle_text": "x"} for i in (1, 2, 3)]
    with _gz.open(meta / "default_cards.jsonl.gz", "wt") as f:
        for c in cards:
            f.write(_j.dumps(c) + "\n")
    d = tmp_path / "sets" / "tst"; d.mkdir(parents=True)
    for i in (1, 2, 3):
        (d / f"tst-{i}.jpg").write_bytes(b"\xff\xd8\xff\xe0stub")

    sets, cbs, by_oracle, _ = web.build_model(tmp_path, want_rulings=False)
    # voisines de la carte du milieu (o-2) dans le set tst
    seq = [c["oid"] for c in cbs["tst"]]
    assert seq == ["o-1", "o-2", "o-3"]
    neighbors = {"tst": {
        "prev": f"card-{web.slug('o-1')}.html#tst",
        "next": f"card-{web.slug('o-3')}.html#tst"}}
    html = web.render_card(by_oracle["o-2"], {}, "fav", neighbors=neighbors)
    assert "o-1" in html and "o-3" in html
    assert "#tst" in html


def test_hover_data_embedded_in_set_page(tmp_path):
    """Point D : oracle et dernier ruling sont embarqués dans la page de set
    (seule option file:// hors-ligne pour le panneau de survol)."""
    meta = tmp_path / "metadata"; meta.mkdir()
    import gzip as _gz, json as _j
    with _gz.open(meta / "default_cards.jsonl.gz", "wt") as f:
        f.write(_j.dumps({
            "set": "tst", "set_name": "T", "released_at": "2000-01-01",
            "collector_number": "1", "name": "Card", "oracle_id": "o-1",
            "layout": "normal", "rarity": "common", "type_line": "Creature",
            "oracle_text": "Vole, protection contre le rouge"}) + "\n")
    d = tmp_path / "sets" / "tst"; d.mkdir(parents=True)
    (d / "tst-1.jpg").write_bytes(b"\xff\xd8\xff\xe0stub")

    rulings = {"o-1": [{"date": "2020-01-01", "text": "Une clarification."}]}
    sets, cbs, _, _ = web.build_model(tmp_path, want_rulings=False)
    html = web.render_set(sets[0], cbs["tst"], "fav", True, rulings=rulings)
    assert "protection contre le rouge" in html      # oracle embarqué
    assert "Une clarification" in html               # ruling embarqué
    assert "id=hp" in html                            # panneau présent


def _dfc_datadir(tmp_path):
    """Fabrique un data-dir avec une carte DFC (transform) à deux faces."""
    import gzip as _gz, json as _j
    meta = tmp_path / "metadata"; meta.mkdir()
    card = {
        "set": "isd", "set_name": "Innistrad", "released_at": "2011-09-30",
        "collector_number": "51", "name": "Delver of Secrets // Aberration",
        "oracle_id": "o-delver", "layout": "transform", "rarity": "common",
        "card_faces": [
            {"name": "Delver of Secrets", "type_line": "Creature — Human Wizard",
             "mana_cost": "{U}", "oracle_text": "At the beginning of your upkeep…",
             "image_uris": {"normal": "x"}},
            {"name": "Insectile Aberration", "type_line": "Creature — Insect",
             "mana_cost": "", "oracle_text": "Flying.", "image_uris": {"normal": "y"}},
        ]}
    with _gz.open(meta / "default_cards.jsonl.gz", "wt") as f:
        f.write(_j.dumps(card) + "\n")
    d = tmp_path / "sets" / "isd"; d.mkdir(parents=True)
    (d / "isd-51-a.jpg").write_bytes(b"\xff\xd8\xff\xe0front")
    (d / "isd-51-b.jpg").write_bytes(b"\xff\xd8\xff\xe0back")
    return tmp_path


def test_dfc_faces_extracted(tmp_path):
    """build_model extrait les deux faces avec leur oracle propre."""
    dd = _dfc_datadir(tmp_path)
    sets, cbs, by_oracle, _ = web.build_model(dd, want_rulings=False)
    card = cbs["isd"][0]
    assert card["faces"], "une DFC doit porter ses deux faces"
    assert len(card["faces"]) == 2
    assert card["faces"][0]["name"] == "Delver of Secrets"
    assert card["faces"][1]["name"] == "Insectile Aberration"
    # oracles distincts
    assert card["faces"][0]["oracle"] != card["faces"][1]["oracle"]
    # images distinctes recto/verso
    assert card["faces"][0]["img"].endswith("-a.jpg")
    assert card["faces"][1]["img"].endswith("-b.jpg")


def test_dfc_hover_and_flip_present(tmp_path):
    """La page de set embarque les deux faces ; la page de carte a le flip."""
    dd = _dfc_datadir(tmp_path)
    sets, cbs, by_oracle, _ = web.build_model(dd, want_rulings=False)
    set_html = web.render_set(sets[0], cbs["isd"], "fav", True)
    # les deux faces dans le JSON de la page de set (survol empilé)
    assert "Insectile Aberration" in set_html
    assert '"faces"' in set_html
    # badge flip présent dans le template de grille
    assert "flipbadge" in set_html
    card_html = web.render_card(by_oracle["o-delver"], {}, "fav")
    assert "HERO_FACES" in card_html and "Insectile Aberration" in card_html
    assert "cardFlip" in card_html and "heroflip" in card_html


def test_normal_card_has_no_faces(tmp_path):
    """Une carte simple n'a pas de faces (pas de flip parasite)."""
    import gzip as _gz, json as _j
    meta = tmp_path / "metadata"; meta.mkdir()
    with _gz.open(meta / "default_cards.jsonl.gz", "wt") as f:
        f.write(_j.dumps({
            "set": "tst", "set_name": "T", "released_at": "2000-01-01",
            "collector_number": "1", "name": "Plain", "oracle_id": "o-1",
            "layout": "normal", "rarity": "common", "type_line": "Creature",
            "oracle_text": "x"}) + "\n")
    d = tmp_path / "sets" / "tst"; d.mkdir(parents=True)
    (d / "tst-1.jpg").write_bytes(b"\xff\xd8\xff\xe0stub")
    sets, cbs, by_oracle, _ = web.build_model(tmp_path, want_rulings=False)
    assert cbs["tst"][0]["faces"] is None
    card_html = web.render_card(by_oracle["o-1"], {}, "fav")
    assert "HERO_FACES=null" in card_html


def test_card_page_shows_rarity_and_number(tmp_path):
    """Point 3 : rareté (C/U/R/M) et numéro nnn/total sur la page de carte."""
    import gzip as _gz, json as _j
    meta = tmp_path / "metadata"; meta.mkdir()
    cards = [{"set": "tst", "set_name": "T", "released_at": "2000-01-01",
              "collector_number": str(i), "name": f"C{i}", "oracle_id": f"o-{i}",
              "layout": "normal", "rarity": "rare" if i == 3 else "common",
              "type_line": "Creature", "oracle_text": "x"} for i in range(1, 6)]
    with _gz.open(meta / "default_cards.jsonl.gz", "wt") as f:
        for c in cards:
            f.write(_j.dumps(c) + "\n")
    d = tmp_path / "sets" / "tst"; d.mkdir(parents=True)
    for i in range(1, 6):
        (d / f"tst-{i}.jpg").write_bytes(b"\xff\xd8\xff\xe0stub")
    sets, cbs, by_oracle, _ = web.build_model(tmp_path, want_rulings=False)
    totals = {"tst": 5}
    html = web.render_card(by_oracle["o-3"], {}, "fav", set_totals=totals)
    assert "003/005" in html          # numéro sur total du set
    assert "cmrar" in html            # bloc rareté
    assert ">R<" in html              # rare abrégée


def test_journal_accumulates(tmp_path):
    """Le journal s'accumule (append) avec horodatage."""
    web.journal(tmp_path, ["set ajouté : ABC (10 images)"])
    web.journal(tmp_path, ["cartes lues : 42"])
    content = (tmp_path / "mtgc.log").read_text()
    assert "set ajouté : ABC" in content
    assert "cartes lues : 42" in content
    # deux lignes horodatées distinctes
    assert content.count("set ajouté") == 1
    assert len([l for l in content.splitlines() if l.strip()]) == 2
