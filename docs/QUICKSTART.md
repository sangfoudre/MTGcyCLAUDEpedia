# MTGcyCLAUDEpedia — Guide de démarrage rapide

Téléchargement de toutes les cartes Magic depuis Scryfall, un répertoire
par extension, chaque parution avec ses vrais frames, symboles et dates
de copyright.

---

## 1. Prérequis

Python 3.10 ou plus (syntaxe `str | None`). **Aucune dépendance** : le
script n'utilise que la bibliothèque standard.

```bash
python3 --version        # doit afficher 3.10+
chmod +x mtgc.py
```

Espace disque à prévoir — chiffres **mesurés**, pas estimés :

| Qualité | Dimensions | Moyenne/image | Total (120 091 images) |
|---|---|---|---|
| `small` | 146×204 | 15 Kio | ~1,7 Gio |
| `normal` | 488×680 | ~95 Kio | ~11 Gio |
| **`large`** | 672×936 | 257 Kio | **~29,4 Gio** |
| **`png`** | 745×1040 | 1 539 Kio | **~176 Gio** |
| `art_crop` | variable | ~60 Kio | ~7 Gio |

---

## 2. Premier essai (2 minutes)

Toujours commencer petit, sur une extension unique :

```bash
./mtgc.py sync --data-dir ~/mtg --set arn --quality small
```

Attendu : 92 fichiers dans `~/mtg/sets/arn/`. Si ça marche, la chaîne
complète fonctionne.

---

## 3. Chiffrer avant de lancer

`--dry-run` planifie tout sans écrire un octet :

```bash
./mtgc.py sync --data-dir ~/mtg --quality large --dry-run
```

Le premier appel télécharge le bulk Scryfall (~69 Mio compressés) et met
environ 20 s à l'analyser. Il est mis en cache 24 h.

---

## 4. Le grand run

```bash
./mtgc.py sync --data-dir ~/mtg --quality large --icons
```

Compter **~2 h 15** à la vitesse observée (~15 images/s). `--icons`
récupère en prime les icônes SVG des extensions, utiles aux futurs
catalogues PDF.

**Interrompre par `Ctrl-C` est sans danger** : l'écriture est atomique
(`.part` puis `rename`), aucun fichier tronqué ne subsiste. Relancer la
même commande reprend exactement là où ça s'était arrêté.

Pour un run long, détacher la session :

```bash
nohup ./mtgc.py sync --data-dir ~/mtg --quality large --icons \
      > ~/mtg/run.log 2>&1 &
tail -f ~/mtg/run.log
```

---

## 5. Ce que tu obtiens

```
~/mtg/
├── sets/
│   ├── lea/  186.jpg   295 fichiers
│   ├── leb/  187.jpg   302
│   ├── 5ed/  280.jpg   460
│   └── …               ~1 000 extensions
├── icons/              icônes SVG des extensions
├── metadata/           bulk Scryfall en cache
└── images.sqlite3      manifeste des téléchargements
```

Nommage : `{numéro de collection}.jpg`, avec suffixe `-a` / `-b` pour les
faces des cartes recto-verso. Vérifié sans aucune collision sur les
120 091 fichiers.

Le manifeste `images.sqlite3` (table `downloads`) trace `path`,
`card_id`, `face`, `set_code`, `fmt`, `illustration`, `bytes`,
`src_updated`, `fetched_at`. C'est `src_updated` qui permettra plus tard
de ne retélécharger que les images réellement mises à jour par Scryfall.

---

## 6. Les options qui comptent

| Option | Effet |
|---|---|
| `--quality png\|large\|normal\|small\|art_crop\|border_crop` | qualité voulue ; se rabat sur une inférieure si absente |
| `--strict-quality` | échouer plutôt que se rabattre |
| `--set CODE` | limiter à une extension (répétable) |
| `--jobs N` | téléchargements simultanés (défaut 8) |
| `--dry-run` | planifier sans écrire |
| `--icons` | icônes SVG des extensions |
| `--force-bulk` | forcer le rafraîchissement du bulk |
| `-v` | détailler les cartes sans image exploitable |

`--unique` vaut `prints` par défaut : **toutes les parutions pour de
vrai**. Les modes `art` (une image par illustration) et `link` (liens
symboliques) existent pour économiser la place, mais `link` affiche le
frame de la mauvaise édition — à éviter pour un catalogue.

---

## 7. Recettes courantes

```bash
# Un bloc entier
./mtgc.py sync --data-dir ~/mtg --set isd --set dka --set avr --quality png

# Illustrations seules, sans cadre ni texte (index par illustrateur)
./mtgc.py sync --data-dir ~/mtg --quality art_crop

# Anglais strict, en excluant les exclusivités linguistiques
./mtgc.py sync --data-dir ~/mtg --lang en --quality large

# Connexion fragile : réduire la concurrence
./mtgc.py sync --data-dir ~/mtg --quality large --jobs 3
```

---

## 8. En cas de souci

**Beaucoup d'erreurs HTTP 503** — le CDN bride sous charge. Le script
réessaie 6 fois avec backoff exponentiel et jitter, mais si ça persiste,
baisser `--jobs` à 3 ou 4. Aucun risque de perte : relancer reprend les
manquantes.

**« 0 cartes lues »** — bulk corrompu. `--force-bulk` pour le
retélécharger.

**Un set paraît incomplet** — vérifier avec `-v` : certaines entrées
(cartes d'art series, placeholders) n'ont pas d'image exploitable chez
Scryfall. Le compteur `sans image exploitable` les recense.

**Vérifier l'intégrité après coup** :

```bash
find ~/mtg/sets -name '*.part'          # doit être vide
find ~/mtg/sets -size -2k               # fichiers suspects
find ~/mtg/sets -type f | wc -l         # ~120 000 attendus
```

---

## 9. Périmètre linguistique

Le bulk `default_cards` contient l'anglais quand il existe, et la langue
d'origine sinon — donc toutes les cartes, sans doublon linguistique.
Réparti en 113 527 `en` et 2 644 autres : `es` 1207, `ja` 661, `fr` 430,
`it` 194, `zhs` 62, `ph` 49 (phyrexian), `de` 9, `qya` 7 (quenya), `dw` 5,
`ru` 5, `zht` 4.

Aucun filtre n'est appliqué par défaut, afin de ne perdre aucune carte
exclusive à une langue (promos japonaises, exclusivités régionales).

---

## 10. Ensuite

Le téléchargeur est terminé. Le reste du projet — base SQLite, moteur de
recherche en syntaxe Scryfall, pages web, catalogues PDF — est en cours
dans le paquet `mtgcycLAUDEpedia/`. Voir son `README.md`, en particulier le
tableau de sémantique de recherche établi par test contre l'API : c'est
la partie la plus coûteuse à retrouver.

---

## Licence

Données et images : Scryfall / Wizards of the Coast, sous Fan Content
Policy. Usage personnel local couvert. Ne pas placer derrière un
paywall, ne pas masquer copyright ni nom d'artiste.
