# Changelog

Toutes les évolutions notables de ce projet.

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/),
versionnage suivant [SemVer](https://semver.org/lang/fr/).

## [Non publié]

### À faire
- Brancher le générateur LaTeX sur SQLite (il attend encore le JSON aplati de 2020)
- Ingérer les bulk `art_tags` / `oracle_tags` pour `atag:` et `otag:`
- Corriger `c:m` sur Garruk Relentless (piste : `color_indicator` non propagé aux faces)
- Rejouer les 26 requêtes de parité API non exécutées
- Interface web (FastAPI + htmx)

---

## [1.1.0] — 2026-07-24

### Ajouté
- `--quality-for FMT:set1,set2` : qualité par extension. Permet « tout en
  `large` sauf ces trois sets en `png` ». Répétable.
- `--verify` : contrôle les fichiers présents (existence, taille, en-tête
  PNG/JPEG) sans aucun accès réseau. Écrit la liste des fichiers à
  reprendre dans `verify_failed.txt`.
- `--sizes` : tableau comparatif du volume pour les six qualités, en
  dehors du `--dry-run`.
- Tableau des volumes affiché automatiquement en `--dry-run`, avec les
  tailles **sondées en direct** par requêtes HEAD sur un échantillon
  aléatoire, plutôt que des constantes figées qui vieillissent mal.
- Adoption des fichiers orphelins : un manifeste perdu ou corrompu ne
  déclenche plus 120 000 téléchargements inutiles.

### Corrigé
- **La qualité n'entrait pas dans l'identité du fichier.** Demander
  `normal` après un passage en `large` ne changeait rien : le script
  répondait « déjà à jour » et conservait l'ancienne qualité, sans le
  signaler. La qualité est désormais comparée au manifeste.
- **Doublons d'extension.** `png` et `large` cohabitaient (`280.jpg` et
  `280.png` pour la même carte, 184 fichiers pour 92 cartes), sans
  qu'aucun outil en aval ne puisse savoir lequel utiliser. Un changement
  de qualité supprime maintenant l'ancien fichier.

### Mesuré
Tailles moyennes réelles, échantillon aléatoire de 40 cartes, HEAD sur
les six qualités, extrapolé aux 120 091 images du corpus :

| Qualité | Dimensions | Moyenne | Total |
|---|---|---|---|
| `small` | 146×204 | 13 Kio | 1,5 Gio |
| `art_crop` | variable | 74 Kio | 8,4 Gio |
| `border_crop` | 480×680 | 94 Kio | 10,7 Gio |
| `normal` | 488×680 | 104 Kio | 11,9 Gio |
| `large` | 672×936 | 174 Kio | 19,9 Gio |
| `png` | 745×1040 | 1 359 Kio | 155,6 Gio |

L'estimation de 29,4 Gio pour `large` annoncée en 1.0.0 était surévaluée :
elle reposait sur trois vieux sets (LEA, LEB, 5ED), dont les scans sont
plus lourds que la moyenne du corpus.

---

## [1.0.0] — 2026-07-23

Première version utilisable : téléchargement complet des images.

### Ajouté
- Téléchargeur autonome `mtgc-images.py`, **sans aucune dépendance**
  (bibliothèque standard seule).
- Un répertoire par extension, nommage `{numéro}[-a|-b].{ext}`, vérifié
  sans aucune collision sur les 120 091 entrées du corpus.
- Trois modes `--unique` : `prints` (défaut, toutes les impressions avec
  leurs vrais frames, symboles et copyrights), `art` (une image par
  illustration), `link` (liens symboliques relatifs vers la première
  parution chronologique).
- Reprise après interruption : écriture atomique `.part` puis `rename`,
  aucun fichier tronqué ne survit à un `Ctrl-C`.
- Mise à jour incrémentale via `image_updated_at` : seules les images
  réellement modifiées côté Scryfall sont reprises.
- Manifeste SQLite (`images.sqlite3`, table `downloads`).
- Gestion des deux formats de bulk : JSON Lines gzippé et tableau JSON
  monolithique.
- `--icons` : icônes SVG des extensions.

### Corrigé (par rapport au prototype de 2020)
- **Générateur épuisé** : la planification parcourait deux fois un
  générateur à usage unique, d'où « 0 carte lue ».
- **Faces manquantes** : la première passe ne lisait que
  l'`illustration_id` racine et la seconde ceux des faces ; toutes les
  cartes recto-verso étaient prises pour des doublons et supprimées
  (20 cartes transform perdues sur le seul set Innistrad).
- **Périmètre incohérent** : avec `--set`, le représentant était élu sur
  la totalité du bulk mais le téléchargement filtré par set — 177
  illustrations sur 455 disparaissaient sans le moindre message.
- **Qualité jamais respectée** : la liste des qualités était parcourue à
  l'envers sans `break`, si bien que `normal` écrasait systématiquement
  `png`. Aucun PNG pleine résolution n'avait jamais été téléchargé.
- **Retry insuffisant** : 3 tentatives laissaient 2,7 % d'échecs en
  HTTP 503 sous concurrence, soit ~3 300 cartes manquantes à l'échelle du
  corpus. Porté à 6 tentatives, backoff exponentiel plafonné à 30 s,
  jitter aléatoire, respect de `Retry-After`.
- **`line in "[]"`** : test de sous-chaîne au lieu d'appartenance.

---

## Composants et maturité

Le dépôt suit un numéro de version unique, mais ses composants n'ont pas
le même degré d'avancement :

| Composant | État |
|---|---|
| `src/mtgc-images.py` | **stable**, validé de bout en bout |
| `src/mtgc/query/` | **expérimental** : 21 requêtes conformes à l'API, 1 divergence connue, 26 non testées |
| `src/mtgc/ingest.py`, `db.py` | expérimental, ingestion validée sur 9 492 cartes réelles |
| `src/mtgc/latex.py` | **non fonctionnel** : attend encore le format JSON de 2020 |
