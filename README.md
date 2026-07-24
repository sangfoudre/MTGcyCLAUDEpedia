# MTGcyclopedia

**Scryfall en local** : toutes les cartes Magic, rangées par extension,
avec leurs vrais cadres, symboles et dates de copyright — plus une base
SQLite, un moteur de recherche en syntaxe Scryfall et des catalogues PDF.

[![CI](https://github.com/sangfoudre/MTGcyclopedia/actions/workflows/ci.yml/badge.svg)](https://github.com/sangfoudre/MTGcyclopedia/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Licence](https://img.shields.io/badge/licence-MIT-green)

---

## Démarrage

Aucune dépendance : Python 3.10+ et la bibliothèque standard suffisent.

```bash
git clone https://github.com/sangfoudre/MTGcyclopedia.git
cd MTGcyclopedia

# 1. essai sur une extension (2 minutes)
python3 src/mtgc-images.py --data-dir ~/mtg --set arn --quality small

# 2. chiffrer avant de se lancer
python3 src/mtgc-images.py --data-dir ~/mtg --quality large --dry-run

# 3. le grand run (~20 Gio, quelques heures)
python3 src/mtgc-images.py --data-dir ~/mtg --quality large --icons
```

Guide complet : [`docs/QUICKSTART.md`](docs/QUICKSTART.md).

---

## Maturité des composants

| Composant | État |
|---|---|
| `src/mtgc-images.py` — téléchargeur | **stable**, validé de bout en bout |
| `src/mtgc/ingest.py`, `db.py` — base SQLite | expérimental, validé sur 9 492 cartes réelles |
| `src/mtgc/query/` — moteur de recherche | expérimental : 21 requêtes conformes à l'API, 1 divergence connue |
| `src/mtgc/latex.py` — catalogues PDF | **non fonctionnel**, attend le portage sur SQLite |

---

## Volumétrie

Tailles moyennes **mesurées** (échantillon aléatoire, requêtes HEAD),
pour les 120 091 images du corpus (116 171 cartes, l'écart venant des
recto-verso dont chaque face compte) :

| Qualité | Dimensions | Moyenne | Total |
|---|---|---|---|
| `small` | 146×204 | 13 Kio | 1,5 Gio |
| `art_crop` | variable | 74 Kio | 8,4 Gio |
| `border_crop` | 480×680 | 94 Kio | 10,7 Gio |
| `normal` | 488×680 | 104 Kio | 11,9 Gio |
| `large` | 672×936 | 174 Kio | **19,9 Gio** |
| `png` | 745×1040 | 1 359 Kio | **155,6 Gio** |

`--dry-run` affiche ce tableau pour votre sélection, avec des tailles
sondées en direct.

---

## Qualité par extension

Tout en `large`, sauf trois extensions en `png` :

```bash
python3 src/mtgc-images.py --data-dir ~/mtg \
        --quality large --quality-for png:mh3,blb,dsk
```

**Un seul fichier par carte.** La qualité est enregistrée au manifeste ;
en changer remplace le fichier au lieu d'en accumuler un par format.

---

## Reprise, mise à jour, vérification

| Besoin | Commande |
|---|---|
| Reprendre après interruption | relancer la même commande |
| Mettre à jour les images révisées | relancer la même commande |
| Contrôler le disque | `--verify` |
| Voir les volumes sans télécharger | `--dry-run` ou `--sizes` |

L'écriture est atomique (`.part` puis `rename`) : un `Ctrl-C` ne laisse
jamais de fichier tronqué. Le manifeste enregistre `image_updated_at`
pour chaque image ; seules celles que Scryfall a réellement révisées sont
reprises. `--verify` contrôle existence, taille et en-tête PNG/JPEG sans
aucun accès réseau, et liste les fichiers à reprendre.

Si le manifeste est perdu, les fichiers présents sont **adoptés** plutôt
que retéléchargés.

---

## Site web (`mtgc-web.py`)

Génère un site statique à partir du data-dir : une page d'accueil avec
toutes les extensions présentes (nom, code, date, nombre de cartes,
icône), puis une page par extension avec la grande icône en tête et la
grille de toutes ses cartes.

```bash
python3 src/mtgc-web.py --data-dir ~/mtg --open
```

Thème « sombre à accents dorés », icônes keyrune réelles si `--icons` a
été passé au téléchargement. Filtre et tri (nom, type, illustrateur,
rareté, coût) se font côté client, sans serveur : le site est 100 %
statique et fonctionne par simple ouverture de `index.html`. Les
métadonnées viennent du bulk local, donc zéro accès réseau à la
génération.

Deux autres thèmes (« sombre premium » façon Scryfall, « clair
éditorial ») et une recherche transversale à toutes les extensions sont
prévus — voir le `CHANGELOG.md`.

---

## Modes de téléchargement (`--unique`)

Chaque parution d'une carte est un objet distinct : cadre, symbole
d'extension, numéro de collection, date de copyright.

| Mode | Fichiers réels | Fidélité |
|---|---|---|
| **`prints` (défaut)** | toutes les impressions | **exacte** |
| `art` | 1 par illustration | compact ; un set de rééditions paraît vide |
| `link` | 1 par illustration + liens symboliques | complet, mais affiche le cadre de la mauvaise édition |

Preuve que les parutions diffèrent — Birds of Paradise, même
`illustration_id` :

| Fichier | Octets | sha256 |
|---|---|---|
| `lea/186.jpg` | 238 846 | `e56fad15cf57` |
| `leb/187.jpg` | 270 559 | `7b4dd5bebe73` |
| `5ed/280.jpg` | 191 607 | `8b6024deb907` |

---

## Périmètre linguistique

Le bulk `default_cards` contient l'anglais quand il existe, et la langue
d'origine sinon : toutes les cartes, sans doublon linguistique.
113 527 `en` et 2 644 autres — `es` 1207, `ja` 661, `fr` 430, `it` 194,
`zhs` 62, `ph` 49 (phyrexian), `de` 9, `qya` 7 (quenya), `dw` 5, `ru` 5,
`zht` 4. Aucun filtre par défaut, pour ne perdre aucune exclusivité.

---

## Sémantique de recherche — établie par test contre l'API

C'est la partie la plus coûteuse à retrouver. Chaque règle ci-dessous a été
vérifiée par requête ciblée sur `api.scryfall.com`, pas déduite de la
documentation. Elles sont implémentées dans `mtgc/query/compiler.py`.

| Règle | Preuve |
|---|---|
| Les **couleurs** sont évaluées **face par face** (OR entre faces), jamais sur l'union | Civilized Scholar // Homicidal Brute (avant U, arrière R) répond à `c:r`, `c:u`, `c=r`, `c=u` mais **pas** à `c>=ur` ni `c:m` |
| `o:` **exclut** le texte de rappel entre parenthèses ; `fo:` l'inclut | Somberwald Spider (« Reach (…block creatures with flying.) ») : `o:flying` ✗, `fo:flying` ✓, `o:reach` ✓ |
| `id:` est un **sous-ensemble** (⊆), contrairement à `c:` qui est ⊇ | Witchbane Orb, incolore, répond à `id:g` **et** `id:c` ; Travel Preparations (identité GW) répond à `id:gw` mais pas `id:g` |
| `pow`/`tou`/`loy` sont évalués **face par face** | Delver of Secrets // Insectile Aberration (1/1 → 3/2) répond à `pow=1` **et** `pow>=3` |
| `r>=rare` inclut la rareté `special` | ordre : common < uncommon < rare < special < mythic < bonus |
| Défauts implicites : `unique:cards` (GROUP BY oracle_id) et masquage des extras | conforme au comportement du site |

Seuls 5 layouts portent des illustrations distinctes par face :
`transform`, `modal_dfc`, `reversible_card`, `double_faced_token`,
`art_series`. Les autres munis de `card_faces` (`split`, `adventure`,
`flip`, `prepare`) sont une carte physique unique — vérifié : ceux-là
possèdent bien `colors` à la racine, les cinq premiers non.

## Corrections à la documentation Scryfall couramment citée

Vérifié en direct le 2026-07-23 :

- Il y a **7 fichiers bulk**, pas 4 : `oracle_cards`, `unique_artwork`,
  `default_cards`, `all_cards`, **`rulings`**, **`art_tags`**,
  **`oracle_tags`**. Les deux derniers rendent `atag:`/`otag:`
  implémentables localement, sans dépendre du service Tagger.
- L'ancien `download_uri` (tableau JSON) **fonctionne toujours** ; il
  coexiste avec `jsonl_download_uri`. Les deux sont gérés par `iter_cards`.
- Il y a **24 `set_type`**, pas 23 : `eternal` manque des listes usuelles.
- Le champ **`image_updated_at`** existe au niveau carte : il permet la
  resynchronisation incrémentale des images directement depuis le bulk,
  sans passer par `/cards/manifest`.
- Volumétrie du 2026-07-23 : `all_cards` 2,58 Go · `default_cards` 558 Mo ·
  `unique_artwork` 265 Mo · `oracle_cards` 180 Mo.
- Limites : 10 req/s sur `api.scryfall.com`, 2/s sur `/cards/collection`,
  10/min sur `/cards/manifest`. Les origines `*.scryfall.io` (images,
  bulk) ne sont **pas** limitées. Un 429 bloque 30 s.

## Tests

| Fichier | Nature | État |
|---|---|---|
| `tests/test_query.py`, `test_integration.py` | unitaires, base en mémoire | **5/5 passent** |
| `tests/api_parity.py` | **fait foi** : compare les *identifiants* renvoyés par le moteur local et par l'API sur un set complet | 21 requêtes identiques, 1 divergence connue, 26 non exécutées (quota) |
| `tests/real_data_check.py` | ingestion de 9 492 cartes réelles | intégrité **OK** ; 13 assertions de requêtes ont des **attentes naïves** |

`real_data_check.py` compare aux champs *racine* des objets JSON via des
prédicats Python simples. Or la base applique la sémantique Scryfall
réelle (faces, sous-ensembles, texte de rappel). Ses 13 « échecs » sur
`c:*`, `id:*`, `o:*` sont donc **attendus** et ne signalent pas de bug :
ils mesurent l'écart entre une lecture naïve et la sémantique correcte.
Se fier à `api_parity.py`.

Prérequis des deux bancs d'essai : `sample.json`, `sets.json`, `isd.json`
dans `/home/claude` ou `/tmp` (voir en-tête des fichiers pour les
regénérer depuis l'API).

## Divergence ouverte

`c:m` (multicolore) rate **Garruk Relentless // Garruk, the Veil-Cursed**.
La condition « une face doit être multicolore » est trop stricte. Piste :
la face arrière tire sa couleur du `color_indicator`, que l'ingestion ne
propage pas encore au niveau des faces (`card_faces.colors` vaut 0 quand
`colors` est absent mais `color_indicator` présent).

## Reste à faire

1. Rejouer les 26 requêtes de parité non exécutées (backoff 0,35 s + retry 429).
2. Corriger `c:m` via `color_indicator` sur les faces.
3. Brancher `mtgc/latex.py` sur SQLite (il attend encore le JSON aplati de 2020).
4. Ingérer `art_tags` / `oracle_tags` → `atag:` / `otag:`.
5. Resync incrémentale via `image_updated_at`.
6. Interface web (FastAPI + htmx) — un site 100 % statique est irréaliste
   à plus de 100 000 cartes.

## Héritage du projet de 2020

À reprendre du dépôt d'origine : le template LaTeX (grille 3×3, section par
set, `fancyhdr` avec icône SVG, fond parchemin), les quatre polices
(`keyrune`, `mana`, `Beleren2016-Bold`, `mplantin`), le placeholder
transparent `PHalpha.png`, et le découpage en volumes de ~5 000 cartes.
Le backend de 2020 est en revanche à abandonner : modèle de données
corrompu par le hack des collector numbers `123a`/`123b`, aucun
`User-Agent`, aucun rate limiting, et une boucle de téléchargement qui
avortait entièrement au premier échec.

## Licence des données

Données et images : Scryfall / Wizards of the Coast, sous Fan Content
Policy. Usage personnel local couvert. Pas de paywall, pas de
repackaging, copyright et nom d'artiste à préserver.
