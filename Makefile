.PHONY: help install test lint clean images verify sizes

DATA ?= ~/mtg
QUALITY ?= large

help:
	@echo "make install   installe le paquet en mode editable + outils de dev"
	@echo "make test      lance la suite de tests"
	@echo "make lint      ruff"
	@echo "make sizes     tableau des volumes pour toutes les qualites"
	@echo "make images    telecharge tout (DATA=~/mtg QUALITY=large)"
	@echo "make verify    controle les fichiers deja presents"
	@echo "make clean     supprime les caches Python"

install:
	python3 -m pip install -e ".[dev]"

test:
	python3 -m pytest tests/ -q

lint:
	python3 -m ruff check src/ tests/

sizes:
	python3 src/mtgc-images.py --data-dir $(DATA) --quality $(QUALITY) --dry-run

images:
	python3 src/mtgc-images.py --data-dir $(DATA) --quality $(QUALITY) --icons

verify:
	python3 src/mtgc-images.py --data-dir $(DATA) --verify

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache
