.PHONY: help sync web verify sizes test lint clean

DATA ?= ~/mtg
QUALITY ?= large

help:
	@echo "make sync      tout : images + rulings + fontes + site (DATA=~/mtg QUALITY=large)"
	@echo "make web       (re)generer le site seul"
	@echo "make verify    controler les fichiers presents"
	@echo "make sizes     tableau des volumes (dry-run)"
	@echo "make test      lancer la suite de tests"
	@echo "make lint      ruff"
	@echo "make clean     supprimer les caches Python"

sync:
	python3 src/mtgc.py sync --data-dir $(DATA) --quality $(QUALITY)

web:
	python3 src/mtgc.py web --data-dir $(DATA)

verify:
	python3 src/mtgc.py verify --data-dir $(DATA)

sizes:
	python3 src/mtgc.py sync --data-dir $(DATA) --quality $(QUALITY) --dry-run

test:
	python3 -m pytest tests/ -q

lint:
	python3 -m ruff check src/ tests/

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache
