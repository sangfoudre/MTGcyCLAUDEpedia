#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mtgc-migrate — MTGcyCLAUDEpedia
Renomme les images déjà téléchargées vers le schéma <code-set>-<numéro>.ext.

Avant 1.3.0 les fichiers étaient nommés <numéro>[-a|-b].ext (ex. 186.jpg,
rangés dans sets/lea/). Depuis 1.3.0 : <code-set>-<numéro>[-a|-b].ext
(ex. lea-186.jpg), pour un nom globalement unique.

Ce script fait la bascule sur place, sans rien retélécharger. Il est sûr
(idempotent, ne touche pas les fichiers déjà au bon format) et propose un
--dry-run.

Usage :
    ./mtgc-migrate.py --data-dir ~/mtg --dry-run
    ./mtgc-migrate.py --data-dir ~/mtg
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

VERSION = "1.3.0"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mtgc-migrate")
    ap.add_argument("--data-dir", default="~/mtg", type=Path)
    ap.add_argument("--dry-run", action="store_true",
                    help="lister les renommages sans les appliquer")
    ap.add_argument("--version", action="version",
                    version=f"mtgc-migrate {VERSION}")
    a = ap.parse_args(argv)

    images = a.data_dir.expanduser().resolve() / "sets"
    if not images.is_dir():
        raise SystemExit(f"introuvable : {images}")

    renamed = skipped = collisions = 0
    for set_dir in sorted(p for p in images.iterdir() if p.is_dir()):
        code = set_dir.name.lower()
        prefix = f"{code}-"
        for f in sorted(set_dir.iterdir()):
            if f.suffix not in (".jpg", ".png") or not f.is_file():
                continue
            if f.name.startswith(prefix):
                skipped += 1                       # déjà au nouveau format
                continue
            target = set_dir / f"{prefix}{f.name}"
            if target.exists():
                collisions += 1
                print(f"  collision, ignoré : {f.name} -> {target.name}",
                      file=sys.stderr)
                continue
            if a.dry_run:
                print(f"  {f.name}  ->  {target.name}")
            else:
                f.rename(target)
            renamed += 1

    verb = "à renommer" if a.dry_run else "renommé(s)"
    print(f"\n{renamed} fichier(s) {verb}, {skipped} déjà au format, "
          f"{collisions} collision(s)", file=sys.stderr)
    if a.dry_run and renamed:
        print("relancer sans --dry-run pour appliquer", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
