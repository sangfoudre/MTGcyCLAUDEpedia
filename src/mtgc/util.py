"""Petits utilitaires transverses : bitmask couleurs, tri naturel, log."""
from __future__ import annotations

import logging
import re
import re
import sys
from typing import Iterable

# --------------------------------------------------------------------- log

_LEVEL_COLORS = {
    logging.DEBUG: "\033[96m",
    logging.INFO: "\033[34m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[1m\033[31m",
    logging.CRITICAL: "\033[7m\033[31m",
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if sys.stderr.isatty():
            color = _LEVEL_COLORS.get(record.levelno, "")
            return f"{color}{msg}{_RESET}"
        return msg


def setup_logging(verbose: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ColorFormatter("%(levelname)-5s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ---------------------------------------------------------------- couleurs

COLOR_BITS = {"W": 1, "U": 2, "B": 4, "R": 8, "G": 16}
BIT_TO_COLOR = {v: k for k, v in COLOR_BITS.items()}
WUBRG = 31

#: surnoms de guildes / clans / shards, tels que Scryfall les accepte
COLOR_ALIASES: dict[str, str] = {
    # mono
    "white": "W", "blue": "U", "black": "B", "red": "R", "green": "G",
    # guildes (Ravnica)
    "azorius": "WU", "dimir": "UB", "rakdos": "BR", "gruul": "RG", "selesnya": "GW",
    "orzhov": "WB", "izzet": "UR", "golgari": "BG", "boros": "RW", "simic": "GU",
    # shards (Alara)
    "bant": "GWU", "esper": "WUB", "grixis": "UBR", "jund": "BRG", "naya": "RGW",
    # clans (Tarkir)
    "abzan": "WBG", "jeskai": "URW", "sultai": "BGU", "mardu": "RWB", "temur": "GUR",
    # quadricolores
    "chaos": "UBRG", "aggression": "BRGW", "altruism": "RGWU",
    "growth": "GWUB", "artifice": "WUBR",
    "glint": "UBRG", "dune": "BRGW", "ink": "RGWU",
    "witch": "GWUB", "yore": "WUBR",
    # penta / divers
    "wubrg": "WUBRG", "rainbow": "WUBRG", "five": "WUBRG",
}


def colors_to_mask(spec: str) -> int:
    """Convertit 'rg', 'izzet', 'WUBRG' ou 'c' en bitmask.

    ``c``/``colorless`` renvoie 0, ``m``/``multicolor`` n'est pas un masque et
    doit être traité en amont par le compilateur.
    """
    s = (spec or "").strip().lower()
    if s in ("c", "colorless"):
        return 0
    if s in COLOR_ALIASES:
        s = COLOR_ALIASES[s].lower()
    mask = 0
    for ch in s:
        bit = COLOR_BITS.get(ch.upper())
        if bit is None:
            raise ValueError(f"couleur inconnue : {ch!r} dans {spec!r}")
        mask |= bit
    return mask


def mask_to_colors(mask: int) -> str:
    """Renvoie les couleurs dans l'ordre WUBRG canonique."""
    return "".join(c for c in "WUBRG" if mask & COLOR_BITS[c])


def list_to_mask(colors: Iterable[str] | None) -> int:
    mask = 0
    for c in colors or ():
        mask |= COLOR_BITS.get(str(c).upper(), 0)
    return mask


def popcount(mask: int) -> int:
    return bin(mask & WUBRG).count("1")


def produced_mana_mask(produced: Iterable[str] | None) -> int:
    """``produced_mana`` peut contenir C (incolore) et parfois S ; on ne garde
    que WUBRG dans le masque."""
    return list_to_mask(produced)


# ------------------------------------------------------------- rareté

RARITY_RANK = {
    "common": 1,
    "uncommon": 2,
    "rare": 3,
    "special": 4,
    "mythic": 5,
    "bonus": 6,
}


def rarity_rank(rarity: str | None) -> int | None:
    return RARITY_RANK.get((rarity or "").lower())


# --------------------------------------------------- collector numbers

_CN_RE = re.compile(r"^(?P<prefix>[^\d]*?)(?P<num>\d+)(?P<suffix>.*)$")


def split_collector_number(cn: str | None) -> tuple[str, int | None, str]:
    """Découpe un collector number en (préfixe, entier, suffixe).

    Gère ``123``, ``123a``, ``★123``, ``123★``, ``GR1``, ``T-5``… Renvoie
    ``(cn, None, "")`` si aucun chiffre n'est trouvé. Cette décomposition est
    ce qui permet un tri naturel correct — le ``zfill()`` de la v1 produisait
    ``"00012" < "003"``.
    """
    cn = (cn or "").strip()
    if not cn:
        return ("", None, "")
    m = _CN_RE.match(cn)
    if not m:
        return (cn, None, "")
    return (m.group("prefix"), int(m.group("num")), m.group("suffix"))


def collector_sort_key(cn: str | None) -> tuple:
    prefix, num, suffix = split_collector_number(cn)
    return (prefix, num if num is not None else 1 << 31, suffix)


# ------------------------------------------------------- valeurs P/T/L

_NUM_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def numeric_or_none(value: str | None) -> float | None:
    """``"3"`` → 3.0 ; ``"*"``, ``"1+*"``, ``None`` → ``None``."""
    if value is None:
        return None
    v = str(value).strip()
    if _NUM_RE.match(v):
        return float(v)
    return None


# -------------------------------------------------------- symboles de mana

_SYMBOL_RE = re.compile(r"\{([^}]+)\}")


def parse_mana_symbols(mana_cost: str | None) -> list[str]:
    """``'{2}{W/U}{P}'`` → ``['2', 'W/U', 'P']`` (contenu brut, majuscules)."""
    if not mana_cost:
        return []
    return [s.upper() for s in _SYMBOL_RE.findall(mana_cost)]


def is_hybrid_symbol(sym: str) -> bool:
    return "/" in sym and "P" not in sym.split("/")


def is_phyrexian_symbol(sym: str) -> bool:
    return "P" in sym.split("/")


_REMINDER_RE = re.compile(r"\([^()]*\)")


def strip_reminder(text: str | None) -> str | None:
    """Retire le texte de rappel entre parenthèses.

    Scryfall distingue ``o:`` (texte oracle sans rappel) de ``fo:`` (texte
    complet). Vérifié : ``o:flying`` ne capte pas Somberwald Spider, dont
    "flying" n'apparaît que dans "(This creature can block creatures with
    flying.)", alors que ``fo:flying`` le capte.
    """
    if not text:
        return text
    prev = None
    out = text
    while prev != out:                 # parenthèses imbriquées
        prev = out
        out = _REMINDER_RE.sub("", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def slugify(text: str) -> str:
    """Nom de fichier sûr sur ext4 (et lisible)."""
    text = (text or "").strip()
    text = re.sub(r"[/\\\x00-\x1f]", "_", text)
    text = re.sub(r"\s+", "_", text)
    return text or "_"


def human_bytes(n: float) -> str:
    for unit in ("o", "Kio", "Mio", "Gio", "Tio"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} Pio"
