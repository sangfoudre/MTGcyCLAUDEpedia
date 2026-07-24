"""Lexer + parseur à descente récursive pour la syntaxe Scryfall.

Grammaire::

    query    := or_expr
    or_expr  := and_expr ( "OR" and_expr )*
    and_expr := unary+                  # juxtaposition = AND implicite
    unary    := "-"? term
    term     := "(" or_expr ")" | filter | exact | bare
    filter   := KEY OP VALUE
    OP       := ":" | "=" | "!=" | ">" | "<" | ">=" | "<="
    VALUE    := '"…"' | /regex/ | mot

Écrit à la main : contrôle total sur les guillemets, les regex ``//`` et le
préfixe ``!`` du nom exact, pour zéro dépendance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .nodes import And, Bare, ExactName, Filter, Node, Not, Or


class QueryError(ValueError):
    """Requête syntaxiquement invalide."""


# --------------------------------------------------------------- tokens

LPAREN, RPAREN, NOT, OR, TERM, END = "LPAREN", "RPAREN", "NOT", "OR", "TERM", "END"


@dataclass
class Token:
    kind: str
    value: object = None
    pos: int = 0


_KEY_OP_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(>=|<=|!=|=|:|>|<)")


class Lexer:
    def __init__(self, text: str) -> None:
        self.text = text or ""
        self.i = 0
        self.n = len(self.text)

    def tokens(self) -> list[Token]:
        out: list[Token] = []
        while True:
            tok = self._next()
            out.append(tok)
            if tok.kind == END:
                return out

    # -- primitives ------------------------------------------------------

    def _peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.text[j] if j < self.n else ""

    def _skip_ws(self) -> None:
        while self.i < self.n and self.text[self.i].isspace():
            self.i += 1

    def _read_quoted(self, quote: str) -> str:
        self.i += 1  # quote ouvrante
        buf = []
        while self.i < self.n:
            ch = self.text[self.i]
            if ch == "\\" and self.i + 1 < self.n:
                buf.append(self.text[self.i + 1])
                self.i += 2
                continue
            if ch == quote:
                self.i += 1
                return "".join(buf)
            buf.append(ch)
            self.i += 1
        raise QueryError(f"guillemet non fermé à partir de la position {self.i}")

    def _read_regex(self) -> str:
        self.i += 1  # slash ouvrant
        buf = []
        while self.i < self.n:
            ch = self.text[self.i]
            if ch == "\\" and self.i + 1 < self.n:
                buf.append(ch)
                buf.append(self.text[self.i + 1])
                self.i += 2
                continue
            if ch == "/":
                self.i += 1
                return "".join(buf)
            buf.append(ch)
            self.i += 1
        raise QueryError("expression régulière non fermée")

    def _read_bare(self) -> str:
        buf = []
        while self.i < self.n:
            ch = self.text[self.i]
            if ch.isspace() or ch in "()":
                break
            if ch in "\"'":
                buf.append(self._read_quoted(ch))
                continue
            buf.append(ch)
            self.i += 1
        return "".join(buf)

    # -- token suivant ---------------------------------------------------

    def _next(self) -> Token:
        self._skip_ws()
        if self.i >= self.n:
            return Token(END, pos=self.i)

        start = self.i
        ch = self._peek()

        if ch == "(":
            self.i += 1
            return Token(LPAREN, pos=start)
        if ch == ")":
            self.i += 1
            return Token(RPAREN, pos=start)
        if ch == "-" and self._peek(1) and not self._peek(1).isspace():
            self.i += 1
            return Token(NOT, pos=start)

        # OR / AND nus
        m = re.match(r"(?i)(or|and)\b", self.text[self.i:])
        if m:
            self.i += m.end()
            word = m.group(1).lower()
            if word == "or":
                return Token(OR, pos=start)
            return self._next()  # AND est implicite : on l'ignore

        # nom exact : !"…" ou !mot
        if ch == "!":
            self.i += 1
            nxt = self._peek()
            value = self._read_quoted(nxt) if nxt in "\"'" else self._read_bare()
            return Token(TERM, ExactName(value), pos=start)

        # clé + opérateur ?
        m = _KEY_OP_RE.match(self.text, self.i)
        if m:
            key, op = m.group(1).lower(), m.group(2)
            self.i = m.end()
            nxt = self._peek()
            if nxt in "\"'":
                value, is_rx = self._read_quoted(nxt), False
            elif nxt == "/":
                value, is_rx = self._read_regex(), True
            else:
                value, is_rx = self._read_bare(), False
            return Token(TERM, Filter(key, op, value, regex=is_rx), pos=start)

        # mot nu (ou chaîne entre guillemets)
        if ch in "\"'":
            return Token(TERM, Bare(self._read_quoted(ch)), pos=start)
        word = self._read_bare()
        if not word:
            self.i += 1  # caractère isolé imprévu : on avance pour ne pas boucler
            return self._next()
        return Token(TERM, Bare(word), pos=start)


# --------------------------------------------------------------- parseur

class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.toks = tokens
        self.i = 0

    @property
    def cur(self) -> Token:
        return self.toks[self.i]

    def eat(self, kind: str) -> Token:
        tok = self.cur
        if tok.kind != kind:
            raise QueryError(f"attendu {kind}, obtenu {tok.kind} en position {tok.pos}")
        self.i += 1
        return tok

    def parse(self) -> Node | None:
        if self.cur.kind == END:
            return None
        node = self.or_expr()
        if self.cur.kind != END:
            raise QueryError(f"jeton inattendu en position {self.cur.pos}")
        return node

    def or_expr(self) -> Node:
        left = self.and_expr()
        if self.cur.kind != OR:
            return left
        parts = [left]
        while self.cur.kind == OR:
            self.i += 1
            parts.append(self.and_expr())
        return Or(parts)

    def and_expr(self) -> Node:
        parts = [self.unary()]
        while self.cur.kind in (TERM, LPAREN, NOT):
            parts.append(self.unary())
        return parts[0] if len(parts) == 1 else And(parts)

    def unary(self) -> Node:
        if self.cur.kind == NOT:
            self.i += 1
            return Not(self.unary())
        return self.term()

    def term(self) -> Node:
        if self.cur.kind == LPAREN:
            self.i += 1
            node = self.or_expr()
            self.eat(RPAREN)
            return node
        if self.cur.kind == TERM:
            return self.eat(TERM).value  # type: ignore[return-value]
        raise QueryError(f"terme attendu en position {self.cur.pos}")


def parse_query(text: str) -> Node | None:
    return Parser(Lexer(text).tokens()).parse()
