"""Nœuds de l'AST de recherche."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


class Node:
    pass


@dataclass
class And(Node):
    children: Sequence[Node]


@dataclass
class Or(Node):
    children: Sequence[Node]


@dataclass
class Not(Node):
    child: Node


@dataclass
class Filter(Node):
    """``key op value`` — ex. ``c >= rg``, ``o : "draw a card"``."""
    key: str
    op: str
    value: str
    regex: bool = False


@dataclass
class Bare(Node):
    """Mot nu : recherche sur le nom de la carte."""
    value: str


@dataclass
class ExactName(Node):
    """``!"Lightning Bolt"`` — nom exact."""
    value: str


@dataclass
class Options:
    """Directives extraites de la requête (``order:``, ``unique:``…)."""
    order: str = "name"
    direction: str = "auto"
    unique: str = "cards"
    include_extras: bool = False
    warnings: list[str] = field(default_factory=list)
