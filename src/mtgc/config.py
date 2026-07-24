"""Configuration : plus de chemins en dur type ``/EXT/MTG``."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path

DEFAULT_CONFIG_PATHS = [
    Path("./mtgc.toml"),
    Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "mtgc" / "mtgc.toml",
]

#: Scryfall exige un User-Agent explicite et un Accept.
USER_AGENT = "MTGcyclopedia/2.0 (+https://github.com/sangfoudre/MTGcyclopedia)"


@dataclass
class Config:
    #: racine des données (base, images, icônes, métadonnées)
    data_dir: Path = Path("~/mtg").expanduser()
    #: fichiers statiques (polices, fond parchemin, placeholder)
    static_dir: Path = Path("~/mtg/static").expanduser()

    #: bulk à ingérer : default_cards (recommandé) ou all_cards (exhaustif)
    bulk_type: str = "default_cards"
    #: langues à conserver ; [] = toutes. Les cartes dont la langue est la
    #: seule disponible sont conservées quoi qu'il arrive (fallback).
    languages: list[str] = field(default_factory=lambda: ["en"])

    #: qualité téléchargée, par ordre de préférence réelle (le premier trouvé gagne)
    image_formats: list[str] = field(default_factory=lambda: ["png"])
    #: nombre de téléchargements simultanés (origines *.scryfall.io : pas de rate limit)
    image_concurrency: int = 8
    #: délai mini entre deux appels à api.scryfall.com (10 req/s documenté)
    api_delay: float = 0.1

    #: catalogue PDF
    cards_per_volume: int = 5000
    latex_engine: str = "lualatex"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mtgc.sqlite3"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "sets"

    @property
    def icons_dir(self) -> Path:
        return self.data_dir / "icons"

    @property
    def metadata_dir(self) -> Path:
        return self.data_dir / "metadata"

    @property
    def build_dir(self) -> Path:
        return self.data_dir / "build"

    @property
    def out_dir(self) -> Path:
        return self.data_dir / "out"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.images_dir, self.icons_dir,
                  self.metadata_dir, self.build_dir, self.out_dir):
            p.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["data_dir"] = str(self.data_dir)
        d["static_dir"] = str(self.static_dir)
        return d


def load_config(path: str | os.PathLike | None = None) -> Config:
    candidates = [Path(path)] if path else DEFAULT_CONFIG_PATHS
    for p in candidates:
        p = Path(p).expanduser()
        if p.is_file():
            with p.open("rb") as fh:
                raw = tomllib.load(fh)
            return _from_dict(raw)
    return Config()


def _from_dict(raw: dict) -> Config:
    cfg = Config()
    for key, value in (raw or {}).items():
        if not hasattr(cfg, key):
            continue
        if key in ("data_dir", "static_dir"):
            value = Path(str(value)).expanduser()
        setattr(cfg, key, value)
    return cfg
