"""Accès réseau Scryfall.

Deux régimes distincts, c'est le point que la v1 ignorait :

* ``api.scryfall.com`` : **10 requêtes/seconde** documentées (2/s pour
  ``/cards/collection``, 10/minute pour ``/cards/manifest``). Un 429 bloque
  l'accès 30 s ; l'abus répété peut valoir un bannissement IP.
* ``*.scryfall.io`` (images, bulk) : **pas de rate limit**. On peut donc
  paralléliser franchement les images.

Toutes les requêtes portent un ``User-Agent`` explicite, exigé par Scryfall.
"""
from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from typing import Any

import json

from .config import USER_AGENT
from .util import get_logger

log = get_logger("net")

API_ROOT = "https://api.scryfall.com"

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json;q=0.9,*/*;q=0.8",
}


class RateLimiter:
    """Limiteur simple, thread-safe, à intervalle minimum."""

    def __init__(self, min_interval: float = 0.1) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


_api_limiter = RateLimiter(0.1)


def set_api_delay(delay: float) -> None:
    _api_limiter.min_interval = delay


def get_json(url: str, *, retries: int = 4) -> Any:
    """GET JSON sur l'API, avec rate limiting et backoff sur 429/5xx."""
    backoff = 1.0
    for attempt in range(1, retries + 1):
        _api_limiter.wait()
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                # Scryfall bloque 30 s après un 429 : on attend franchement.
                wait = 30.0
                log.warning("429 sur %s — pause %.0f s (tentative %d/%d)",
                            url, wait, attempt, retries)
                time.sleep(wait)
                continue
            if 500 <= exc.code < 600 and attempt < retries:
                log.warning("HTTP %d sur %s — retry dans %.1f s", exc.code, url, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries:
                log.warning("erreur réseau sur %s (%s) — retry dans %.1f s", url, exc, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue
            raise
    raise RuntimeError(f"échec définitif sur {url}")


def get_bulk_descriptors() -> list[dict]:
    """Liste des fichiers bulk. Les URL changent chaque jour : toujours
    re-résoudre, jamais coder une URL ``data.scryfall.io`` en dur."""
    data = get_json(f"{API_ROOT}/bulk-data")
    return data.get("data", [])


def find_bulk(bulk_type: str) -> dict:
    for b in get_bulk_descriptors():
        if b.get("type") == bulk_type:
            return b
    known = ", ".join(sorted(b.get("type", "?") for b in get_bulk_descriptors()))
    raise KeyError(f"bulk type inconnu : {bulk_type!r} (disponibles : {known})")


def get_all_sets() -> list[dict]:
    """Pagine ``/sets`` (une seule page en pratique, mais on gère ``has_more``)."""
    out: list[dict] = []
    url = f"{API_ROOT}/sets"
    while url:
        data = get_json(url)
        out.extend(data.get("data", []))
        url = data.get("next_page") if data.get("has_more") else None
    return out


def download_stream(url: str, chunk: int = 1 << 20):
    """Générateur d'octets. Pas de rate limiting : ``*.scryfall.io`` n'en a pas."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        while True:
            block = resp.read(chunk)
            if not block:
                break
            yield block
