# Contribuer

## Principes du projet

1. **Aucune perte silencieuse.** Un bug qui fait disparaître des cartes
   sans message est le pire défaut possible ici. Toute modification de la
   sélection ou du nommage doit s'accompagner d'un contrôle chiffré :
   « N illustrations attendues, N fichiers écrits ».
2. **Mesurer, pas estimer.** Les volumes, tailles et taux d'échec cités
   dans la documentation proviennent de mesures réelles. Si un chiffre
   est une extrapolation, le dire.
3. **Zéro dépendance pour le téléchargeur.** `mtgc-images.py` doit
   tourner sur une machine Linux nue avec Python seul.

## Vérifier une modification

```bash
make test                  # tests unitaires
make lint
python3 tests/api_parity.py     # parité avec l'API Scryfall (le test qui fait foi)
python3 tests/real_data_check.py
```

`real_data_check.py` porte des attentes **naïves** sur 13 assertions : il
compare aux champs racine des objets JSON, alors que la base applique la
sémantique Scryfall réelle. Ses échecs sur `c:*`, `id:*` et `o:*` sont
attendus. Se fier à `api_parity.py`.

## Respecter Scryfall

- En-tête `User-Agent` explicite, jamais celui par défaut de la
  bibliothèque HTTP.
- 10 requêtes/seconde maximum sur `api.scryfall.com` (2/s sur
  `/cards/collection`, 10/min sur `/cards/manifest`). Les origines
  `*.scryfall.io` (images, bulk) ne sont pas limitées.
- Ne jamais figer une URL de bulk : elles portent un horodatage et
  changent chaque jour. Toujours passer par `GET /bulk-data`.
- Un HTTP 429 entraîne un blocage de 30 secondes ; l'abus répété, un
  bannissement d'IP.

## Style

`ruff` avec la configuration de `pyproject.toml`. Commentaires et
documentation en français, identifiants et messages de log techniques en
anglais quand l'usage l'impose.
