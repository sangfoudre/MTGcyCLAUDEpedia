"""Compilation de l'AST vers du SQL paramétré.

Chaque mot-clé est une entrée de ``HANDLERS`` : ajouter un mot-clé revient à
ajouter une fonction, sans toucher au parseur.
"""
from __future__ import annotations

from typing import Callable

from ..util import COLOR_ALIASES, colors_to_mask
from .nodes import And, Bare, ExactName, Filter, Node, Not, Options, Or
from .parser import QueryError

WUBRG = 31

#: alias → clé canonique
ALIASES: dict[str, str] = {
    "color": "c", "colour": "c",
    "identity": "id", "ci": "id",
    "type": "t",
    "oracle": "o", "fulloracle": "fo",
    "flavor": "ft", "flavour": "ft", "flavortext": "ft",
    "n": "name",
    "artist": "a",
    "rarity": "r",
    "e": "s", "set": "s", "edition": "s",
    "settype": "st",
    "block": "b",
    "number": "cn", "num": "cn",
    "cmc": "mv", "manavalue": "mv",
    "power": "pow", "toughness": "tou", "loyalty": "loy", "powtou": "pt",
    "mana": "m",
    "format": "f",
    "language": "lang",
    "watermark": "wm",
    "kw": "keyword",
    "arttag": "atag", "art": "atag",
    "oracletag": "otag", "function": "otag",
    "not": "is", "has": "is",
    "collectornumber": "cn",
}

#: clés reconnues mais non implémentées — on prévient au lieu de mentir
UNSUPPORTED = {
    "cube": "listes de cube (données externes non embarquées)",
    "devotion": "dévotion (calcul de symboles non implémenté)",
    "new": "détection de nouveauté (art/frame/artist) non implémentée",
    "prefer": "directive d'affichage sans effet en local",
    "display": "directive d'affichage sans effet en local",
    "cheapest": "comparaison multi-devises non implémentée",
}

#: prédicats ``is:`` → fragment SQL booléen
IS_PREDICATES: dict[str, str] = {
    # layouts
    "split": "cards.layout = 'split'",
    "flip": "cards.layout = 'flip'",
    "transform": "cards.layout = 'transform'",
    "meld": "cards.layout = 'meld'",
    "leveler": "cards.layout = 'leveler'",
    "adventure": "cards.layout = 'adventure'",
    "mdfc": "cards.layout = 'modal_dfc'",
    "reversible": "cards.layout = 'reversible_card'",
    "planar": "cards.layout = 'planar'",
    "scheme": "cards.layout = 'scheme'",
    "vanguard": "cards.layout = 'vanguard'",
    "emblem": "cards.layout = 'emblem'",
    "class": "cards.layout = 'class'",
    "saga": "cards.type_line LIKE '%Saga%'",
    "dfc": ("cards.layout IN ('transform','modal_dfc','double_faced_token',"
            "'reversible_card','art_series')"),
    "token": ("cards.layout IN ('token','double_faced_token') "
              "OR cards.type_line LIKE 'Token%'"),
    # types
    "permanent": ("(cards.type_line LIKE '%Artifact%' OR cards.type_line LIKE '%Creature%' "
                  "OR cards.type_line LIKE '%Enchantment%' OR cards.type_line LIKE '%Land%' "
                  "OR cards.type_line LIKE '%Planeswalker%' OR cards.type_line LIKE '%Battle%')"),
    "spell": ("cards.type_line NOT LIKE '%Land%' AND cards.layout NOT IN "
              "('token','double_faced_token','emblem','scheme','vanguard','planar','art_series')"),
    "historic": ("(cards.type_line LIKE '%Legendary%' OR cards.type_line LIKE '%Artifact%' "
                 "OR cards.type_line LIKE '%Saga%')"),
    "bear": "cards.pow_num = 2 AND cards.tou_num = 2",
    "vanilla": ("cards.type_line LIKE '%Creature%' "
                "AND (cards.oracle_text IS NULL OR cards.oracle_text = '')"),
    # impressions
    "foil": "cards.finishes_json LIKE '%\"foil\"%'",
    "nonfoil": "cards.finishes_json LIKE '%\"nonfoil\"%'",
    "etched": "cards.finishes_json LIKE '%\"etched\"%'",
    "promo": "cards.promo = 1",
    "reprint": "cards.reprint = 1",
    "firstprint": "cards.reprint = 0",
    "firstprinting": "cards.reprint = 0",
    "reserved": "cards.reserved = 1",
    "digital": "cards.digital = 1",
    "oversized": "cards.oversized = 1",
    "textless": "cards.textless = 1",
    "full": "cards.full_art = 1",
    "fullart": "cards.full_art = 1",
    "booster": "cards.booster = 1",
    "spotlight": "cards.story_spotlight = 1",
    "variation": "cards.variation = 1",
    "gamechanger": "cards.game_changer = 1",
    "unique": "cards.is_unique_art = 1",
    "hires": "cards.image_status = 'highres_scan'",
    "lowres": "cards.image_status IN ('lowres','placeholder','missing')",
    "borderless": "cards.border_color = 'borderless'",
    "extended": "cards.frame_effects_json LIKE '%extendedart%'",
    "extendedart": "cards.frame_effects_json LIKE '%extendedart%'",
    "showcase": "cards.frame_effects_json LIKE '%showcase%'",
    "colorshifted": "cards.frame_effects_json LIKE '%colorshifted%'",
    "watermark": "cards.watermark IS NOT NULL AND cards.watermark <> ''",
    "paper": "cards.games_json LIKE '%\"paper\"%'",
    "arena": "cards.games_json LIKE '%\"arena\"%'",
    "mtgo": "cards.games_json LIKE '%\"mtgo\"%'",
    "funny": ("cards.set_code IN (SELECT code FROM sets WHERE set_type = 'funny')"),
    # couleurs
    "multicolor": "cards.color_count > 1",
    "multicolored": "cards.color_count > 1",
    "monocolor": "cards.color_count = 1",
    "monocolored": "cards.color_count = 1",
    "colorless": "cards.colors = 0",
    "hybrid": "cards.mana_cost LIKE '%/%' AND cards.mana_cost NOT LIKE '%/P%'",
    "phyrexian": "(cards.mana_cost LIKE '%/P%' OR cards.mana_cost LIKE '%P/%')",
    # divers
    "commander": ("cards.id IN (SELECT card_id FROM legalities WHERE format='commander' "
                  "AND status IN ('legal','restricted')) AND (cards.type_line LIKE "
                  "'%Legendary%Creature%' OR cards.oracle_text LIKE '%can be your commander%')"),
    "modal": "cards.oracle_text LIKE '%Choose one%'",
    "illustration": "cards.illustration_id IS NOT NULL",
}

ORDER_COLUMNS: dict[str, str] = {
    "name": "cards.name",
    "set": "cards.set_code, cards.cn_num, cards.cn_suffix",
    "released": "cards.released_at",
    "release": "cards.released_at",
    "date": "cards.released_at",
    "rarity": "cards.rarity_rank",
    "cmc": "cards.cmc",
    "mv": "cards.cmc",
    "power": "cards.pow_num",
    "toughness": "cards.tou_num",
    "loyalty": "cards.loy_num",
    "artist": "cards.artist",
    "collector": "cards.cn_num, cards.cn_suffix",
    "cn": "cards.cn_num, cards.cn_suffix",
    "edhrec": "cards.edhrec_rank",
    "usd": "CAST(json_extract(cards.prices_json,'$.usd') AS REAL)",
    "eur": "CAST(json_extract(cards.prices_json,'$.eur') AS REAL)",
    "color": "cards.colors",
}

_STAT_COLUMNS = {
    "pow": "cards.pow_num", "power": "cards.pow_num",
    "tou": "cards.tou_num", "toughness": "cards.tou_num",
    "loy": "cards.loy_num", "loyalty": "cards.loy_num",
    "cmc": "cards.cmc", "mv": "cards.cmc",
}

_NUM_OPS = {":": "=", "=": "=", "!=": "!=", ">": ">", "<": "<", ">=": ">=", "<="
            : "<="}


class Compiler:
    def __init__(self) -> None:
        self.params: list = []
        self.options = Options()

    # ------------------------------------------------------------- outils

    def p(self, value) -> str:
        self.params.append(value)
        return "?"

    def _fts(self, column: str, value: str) -> str:
        """Recherche sous-chaîne via FTS5 trigram (≥3 caractères), sinon LIKE."""
        if len(value) < 3:
            return f"cards.{column} LIKE {self.p(f'%{value}%')} ESCAPE '\\'"
        escaped = value.replace('"', '""')
        return (f"cards.rowid IN (SELECT rowid FROM cards_fts "
                f"WHERE cards_fts MATCH {self.p(f'{column} : \"{escaped}\"')})")

    def _text(self, column: str, value: str, regex: bool) -> str:
        if regex:
            return f"cards.{column} REGEXP {self.p(value)}"
        return self._fts(column, value)

    def _num(self, column: str, op: str, value: str) -> str:
        sql_op = _NUM_OPS.get(op, "=")
        v = value.strip().lower()
        if v in _STAT_COLUMNS:
            # `pow>tou` compare deux colonnes : le membre droit doit vivre dans
            # la même table que le gauche, sinon on comparerait la face à la
            # racine (NULL pour une double-face).
            other = _STAT_COLUMNS[v]
            if column.startswith("cf.") or column.startswith("(cf."):
                other = other.replace("cards.", "cf.")
            return f"{column} {sql_op} {other}"
        if v == "even":
            return f"CAST({column} AS INTEGER) % 2 = 0 AND {column} IS NOT NULL"
        if v == "odd":
            return f"CAST({column} AS INTEGER) % 2 = 1"
        try:
            num = float(v)
        except ValueError:
            raise QueryError(f"valeur numérique attendue, obtenu {value!r}")
        return f"{column} {sql_op} {self.p(num)}"

    def _mask(self, column: str, count_column: str, op: str, value: str) -> str:
        v = value.strip().lower()
        if v in ("m", "multicolor", "multicolored"):
            return f"{count_column} > 1"
        if v in ("c", "colorless"):
            return f"{column} = 0"
        try:
            mask = colors_to_mask(v)
        except ValueError as exc:
            raise QueryError(str(exc)) from exc
        if op in (":", ">="):
            return f"({column} & {self.p(mask)}) = {self.p(mask)}"
        if op == "=":
            return f"{column} = {self.p(mask)}"
        if op == "!=":
            return f"{column} != {self.p(mask)}"
        if op == "<=":
            return f"({column} & {self.p(WUBRG - mask)}) = 0"
        if op == "<":
            return (f"({column} & {self.p(WUBRG - mask)}) = 0 "
                    f"AND {column} != {self.p(mask)}")
        if op == ">":
            return (f"({column} & {self.p(mask)}) = {self.p(mask)} "
                    f"AND {column} != {self.p(mask)}")
        raise QueryError(f"opérateur {op!r} non supporté pour les couleurs")

    # ------------------------------------------------------------ visiteur

    def compile(self, node: Node | None) -> str:
        if node is None:
            return "1=1"
        if isinstance(node, And):
            parts = [self.compile(c) for c in node.children]
            parts = [p for p in parts if p != "1=1"]
            return "(" + " AND ".join(parts) + ")" if parts else "1=1"
        if isinstance(node, Or):
            return "(" + " OR ".join(self.compile(c) for c in node.children) + ")"
        if isinstance(node, Not):
            return f"NOT ({self.compile(node.child)})"
        if isinstance(node, Bare):
            return self._fts("name", node.value)
        if isinstance(node, ExactName):
            return f"lower(cards.name) = {self.p(node.value.lower())}"
        if isinstance(node, Filter):
            return self._filter(node)
        raise QueryError(f"nœud inconnu : {node!r}")

    def _filter(self, f: Filter) -> str:
        key = ALIASES.get(f.key, f.key)
        if key in UNSUPPORTED:
            self.options.warnings.append(f"{f.key}: ignoré — {UNSUPPORTED[key]}")
            return "1=1"
        handler = HANDLERS.get(key)
        if handler is None:
            self.options.warnings.append(f"mot-clé inconnu ignoré : {f.key}:")
            return "1=1"
        return handler(self, f)


# Vraies double-faces : pas de `colors`/`power` à la racine, chaque face est
# évaluée indépendamment (vérifié contre l'API : Civilized Scholar // Homicidal
# Brute répond à c:r ET c:u ET c=r ET c=u, mais NI à c>=ur NI à c:m).
# split/adventure/flip/prepare sont au contraire une carte physique unique.
DFC_LAYOUTS = ("transform", "modal_dfc", "reversible_card",
               "double_faced_token", "art_series")
_DFC_SQL = "cards.layout IN " + str(DFC_LAYOUTS)


def _per_face(root_sql: str, face_sql: str) -> str:
    """Racine pour les cartes simples, OR sur les faces pour les double-faces."""
    return (f"(({_DFC_SQL}) AND EXISTS(SELECT 1 FROM card_faces cf "
            f"WHERE cf.card_id = cards.id AND ({face_sql})) "
            f"OR NOT ({_DFC_SQL}) AND ({root_sql}))")


# ----------------------------------------------------------- handlers

def _h_color(c: Compiler, f: Filter) -> str:
    root = c._mask("cards.colors", "cards.color_count", f.op, f.value)
    face = c._mask("cf.colors", "0", f.op, f.value)
    if "cf.colors" not in face:          # cas 'm'/'c' sans colonne de comptage
        face = c._mask("cf.colors", "(SELECT 1)", f.op, f.value)
    return _per_face(root, face)


def _h_identity(c: Compiler, f: Filter) -> str:
    """``id:`` est un SOUS-ENSEMBLE, contrairement à ``c:``.

    Vérifié : Witchbane Orb (incolore) répond à ``id:g`` comme à ``id:c``,
    et Travel Preparations (identité GW) répond à ``id:gw`` mais pas ``id:g``.
    C'est la sémantique Commander « jouable dans un deck de ces couleurs ».
    L'identité couleur est fournie à la racine même pour les double-faces :
    aucune évaluation par face ici.
    """
    op = "<=" if f.op == ":" else f.op
    return c._mask("cards.color_identity", "cards.ci_count", op, f.value)


def _h_produces(c: Compiler, f: Filter) -> str:
    return c._mask("cards.produced_mana", "0", f.op, f.value)


def _h_type(c: Compiler, f: Filter) -> str:
    return c._text("type_line", f.value, f.regex)


def _h_oracle(c: Compiler, f: Filter) -> str:
    if "~" in f.value and not f.regex:
        # '~' désigne le nom de la carte elle-même
        return (f"instr(lower(cards.oracle_plain), "
                f"lower(replace({c.p(f.value)}, '~', cards.name))) > 0")
    # o: ignore le texte de rappel entre parenthèses ; fo: le conserve.
    return c._text("oracle_plain", f.value, f.regex)


def _h_full_oracle(c: Compiler, f: Filter) -> str:
    """``fo:`` — texte oracle complet, texte de rappel inclus."""
    return c._text("oracle_text", f.value, f.regex)


def _h_flavor(c: Compiler, f: Filter) -> str:
    return c._text("flavor_text", f.value, f.regex)


def _h_name(c: Compiler, f: Filter) -> str:
    if f.op == "=":
        return f"lower(cards.name) = {c.p(f.value.lower())}"
    return c._text("name", f.value, f.regex)


def _h_artist(c: Compiler, f: Filter) -> str:
    return c._text("artist", f.value, f.regex)


def _h_artists(c: Compiler, f: Filter) -> str:
    return c._num("cards.artist_count", f.op, f.value)


def _h_rarity(c: Compiler, f: Filter) -> str:
    from ..util import RARITY_RANK
    v = f.value.strip().lower()
    rank = RARITY_RANK.get(v)
    if rank is None:
        raise QueryError(f"rareté inconnue : {f.value!r}")
    op = _NUM_OPS.get(f.op, "=")
    return f"cards.rarity_rank {op} {c.p(rank)}"


def _h_set(c: Compiler, f: Filter) -> str:
    return f"cards.set_code = {c.p(f.value.strip().lower())}"


def _h_settype(c: Compiler, f: Filter) -> str:
    return (f"cards.set_code IN (SELECT code FROM sets WHERE set_type = "
            f"{c.p(f.value.strip().lower())})")


def _h_block(c: Compiler, f: Filter) -> str:
    v = f.value.strip().lower()
    return (f"cards.set_code IN (SELECT code FROM sets WHERE lower(block) = {c.p(v)} "
            f"OR lower(block_code) = {c.p(v)})")


def _h_cn(c: Compiler, f: Filter) -> str:
    v = f.value.strip()
    if v.isdigit():
        return c._num("cards.cn_num", f.op, v)
    return f"lower(cards.collector_number) = {c.p(v.lower())}"


def _h_mv(c: Compiler, f: Filter) -> str:
    return c._num("cards.cmc", f.op, f.value)


def _h_pow(c: Compiler, f: Filter) -> str:
    # Vérifié : Delver of Secrets // Insectile Aberration (1/1 -> 3/2) répond
    # à la fois à pow=1 et à pow>=3. Chaque face compte séparément.
    return _per_face(c._num("cards.pow_num", f.op, f.value),
                     c._num("cf.pow_num", f.op, f.value))


def _h_tou(c: Compiler, f: Filter) -> str:
    return _per_face(c._num("cards.tou_num", f.op, f.value),
                     c._num("cf.tou_num", f.op, f.value))


def _h_loy(c: Compiler, f: Filter) -> str:
    return _per_face(c._num("cards.loy_num", f.op, f.value),
                     c._num("cf.loy_num", f.op, f.value))


def _h_pt(c: Compiler, f: Filter) -> str:
    return _per_face(c._num("(cards.pow_num + cards.tou_num)", f.op, f.value),
                     c._num("(cf.pow_num + cf.tou_num)", f.op, f.value))


def _h_mana(c: Compiler, f: Filter) -> str:
    op = f.op if f.op in (":", "=", ">=", "<=", ">", "<", "!=") else ":"
    return (f"mana_matches(cards.mana_cost, {c.p(f.value)}, {c.p(op)}) = 1")


def _h_format(c: Compiler, f: Filter) -> str:
    return (f"cards.id IN (SELECT card_id FROM legalities WHERE format = "
            f"{c.p(f.value.strip().lower())} AND status IN ('legal','restricted'))")


def _h_banned(c: Compiler, f: Filter) -> str:
    return (f"cards.id IN (SELECT card_id FROM legalities WHERE format = "
            f"{c.p(f.value.strip().lower())} AND status = 'banned')")


def _h_restricted(c: Compiler, f: Filter) -> str:
    return (f"cards.id IN (SELECT card_id FROM legalities WHERE format = "
            f"{c.p(f.value.strip().lower())} AND status = 'restricted')")


def _h_lang(c: Compiler, f: Filter) -> str:
    v = f.value.strip().lower()
    if v == "any":
        return "1=1"
    return f"cards.lang = {c.p(v)}"


def _h_year(c: Compiler, f: Filter) -> str:
    return c._num("cards.released_year", f.op, f.value)


def _h_date(c: Compiler, f: Filter) -> str:
    v = f.value.strip().lower()
    op = _NUM_OPS.get(f.op, "=")
    if len(v) <= 5 and not v[0].isdigit():
        return (f"cards.released_at {op} (SELECT released_at FROM sets "
                f"WHERE code = {c.p(v)})")
    return f"cards.released_at {op} {c.p(v)}"


def _h_simple(column: str) -> Callable[[Compiler, Filter], str]:
    def handler(c: Compiler, f: Filter) -> str:
        return f"lower(cards.{column}) = {c.p(f.value.strip().lower())}"
    return handler


def _h_frame(c: Compiler, f: Filter) -> str:
    """``frame:`` couvre à la fois l'édition de cadre (1993, 2015, future…)
    et les ``frame_effects`` (extendedart, showcase, etched…)."""
    v = f.value.strip().lower()
    frame_eq = f"lower(cards.frame) = {c.p(v)}"
    effect_like = f"cards.frame_effects_json LIKE {c.p('%' + v + '%')}"
    return f"({frame_eq} OR {effect_like})"


def _h_watermark(c: Compiler, f: Filter) -> str:
    return f"lower(cards.watermark) = {c.p(f.value.strip().lower())}"


def _h_game(c: Compiler, f: Filter) -> str:
    return f"cards.games_json LIKE {c.p('%\"' + f.value.strip().lower() + '\"%')}"


def _h_keyword(c: Compiler, f: Filter) -> str:
    return f"lower(cards.keywords_json) LIKE {c.p('%\"' + f.value.strip().lower() + '\"%')}"


def _h_atag(c: Compiler, f: Filter) -> str:
    v = f.value.strip().lower()
    return (f"cards.illustration_id IN (SELECT t.illustration_id FROM art_taggings t "
            f"JOIN tags g ON g.id = t.tag_id WHERE lower(g.slug) = {c.p(v)} "
            f"OR lower(g.label) = {c.p(v)})")


def _h_otag(c: Compiler, f: Filter) -> str:
    v = f.value.strip().lower()
    return (f"cards.oracle_id IN (SELECT t.oracle_id FROM oracle_taggings t "
            f"JOIN tags g ON g.id = t.tag_id WHERE lower(g.slug) = {c.p(v)} "
            f"OR lower(g.label) = {c.p(v)})")


def _h_price(field: str) -> Callable[[Compiler, Filter], str]:
    def handler(c: Compiler, f: Filter) -> str:
        col = f"CAST(json_extract(cards.prices_json, '$.{field}') AS REAL)"
        return c._num(col, f.op, f.value)
    return handler


def _h_edhrec(c: Compiler, f: Filter) -> str:
    return c._num("cards.edhrec_rank", f.op, f.value)


def _h_prints(c: Compiler, f: Filter) -> str:
    sub = ("(SELECT COUNT(*) FROM cards c2 WHERE c2.oracle_id = cards.oracle_id)")
    return c._num(sub, f.op, f.value)


def _h_sets_count(c: Compiler, f: Filter) -> str:
    sub = ("(SELECT COUNT(DISTINCT c2.set_code) FROM cards c2 "
           "WHERE c2.oracle_id = cards.oracle_id)")
    return c._num(sub, f.op, f.value)


def _h_in(c: Compiler, f: Filter) -> str:
    """``in:`` — la carte est passée par ce set / cette langue / ce jeu."""
    v = f.value.strip().lower()
    if v in ("paper", "arena", "mtgo"):
        return (f"cards.oracle_id IN (SELECT oracle_id FROM cards c2 "
                f"WHERE c2.games_json LIKE {c.p('%\"' + v + '\"%')})")
    if len(v) == 2:  # code de langue
        return (f"cards.oracle_id IN (SELECT oracle_id FROM cards c2 "
                f"WHERE c2.lang = {c.p(v)})")
    return (f"cards.oracle_id IN (SELECT oracle_id FROM cards c2 "
            f"WHERE c2.set_code = {c.p(v)})")


def _h_is(c: Compiler, f: Filter) -> str:
    v = f.value.strip().lower()
    frag = IS_PREDICATES.get(v)
    if frag is None:
        c.options.warnings.append(f"is:{v} inconnu — ignoré")
        return "1=1"
    # 'not:' est la négation de 'is:'
    if f.key == "not":
        return f"NOT ({frag})"
    return f"({frag})"


def _h_option(name: str) -> Callable[[Compiler, Filter], str]:
    def handler(c: Compiler, f: Filter) -> str:
        value = f.value.strip().lower()
        if name == "include":
            c.options.include_extras = value in ("extras", "all", "true", "1")
        else:
            setattr(c.options, name, value)
        return "1=1"
    return handler


HANDLERS: dict[str, Callable[[Compiler, Filter], str]] = {
    "c": _h_color,
    "id": _h_identity,
    "produces": _h_produces,
    "t": _h_type,
    "o": _h_oracle,
    "fo": _h_full_oracle,
    "ft": _h_flavor,
    "name": _h_name,
    "a": _h_artist,
    "artists": _h_artists,
    "r": _h_rarity,
    "s": _h_set,
    "st": _h_settype,
    "b": _h_block,
    "cn": _h_cn,
    "mv": _h_mv,
    "pow": _h_pow,
    "tou": _h_tou,
    "loy": _h_loy,
    "pt": _h_pt,
    "m": _h_mana,
    "f": _h_format,
    "banned": _h_banned,
    "restricted": _h_restricted,
    "lang": _h_lang,
    "year": _h_year,
    "date": _h_date,
    "border": _h_simple("border_color"),
    "frame": _h_frame,
    "stamp": _h_simple("security_stamp"),
    "layout": _h_simple("layout"),
    "wm": _h_watermark,
    "game": _h_game,
    "keyword": _h_keyword,
    "atag": _h_atag,
    "otag": _h_otag,
    "usd": _h_price("usd"),
    "eur": _h_price("eur"),
    "tix": _h_price("tix"),
    "edhrec": _h_edhrec,
    "prints": _h_prints,
    "sets": _h_sets_count,
    "in": _h_in,
    "is": _h_is,
    "order": _h_option("order"),
    "direction": _h_option("direction"),
    "unique": _h_option("unique"),
    "include": _h_option("include"),
}
