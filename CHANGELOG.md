# Changelog

Toutes les évolutions notables de ce projet.

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/),
versionnage suivant [SemVer](https://semver.org/lang/fr/).

## [Non publié]

### À faire
- **Web, thèmes alternatifs** : proposer les styles « sombre premium » (façon
  Scryfall) et « clair éditorial » en plus du thème doré actuel, via une
  option `--theme`. Les trois maquettes ont été validées ; seul le doré est
  implémenté.
- **Web, recherche transversale** : rechercher une *carte* (pas seulement une
  extension) depuis l'accueil, sur l'ensemble du corpus. La recherche par
  extension existe déjà ; reste la recherche de cartes, qui suppose un index
  JSON chargé à la demande pour rester statique.
- **Web, app locale optionnelle** : petit serveur (FastAPI + htmx) pour une
  recherche live sur les 120 000 cartes, en complément du site statique.
- Brancher le générateur LaTeX sur SQLite (il attend encore le JSON aplati de 2020)
- Ingérer les bulk `art_tags` / `oracle_tags` pour `atag:` et `otag:`
- Corriger `c:m` sur Garruk Relentless (piste : `color_indicator` non propagé aux faces)
- Rejouer les 26 requêtes de parité API non exécutées

---

## [2.2.0] — 2026-07-26

### Ajouté
- **Grille de cartes virtualisée.** Les pages de set ne rendent plus une
  balise `<img>` par carte : les données vivent en JSON, et seules les
  cartes réellement visibles à l'écran existent dans le DOM (fenêtrage au
  défilement, avec quelques rangées de marge). Le DOM d'une page de 460
  cartes ne dépasse jamais ~54 nœuds. Poids HTML d'une page de 460 cartes :
  ~120 Ko contre ~225 Ko auparavant. Le défilement reste fluide quelle que
  soit la taille de l'extension. Filtre, tri et viewer opèrent sur les
  données, pas sur le DOM.

### Note
Choix d'architecture retenu après analyse : la lenteur venait du nombre de
nœuds DOM (une `<img>` par carte), pas d'un manque de dynamisme. Un
micro-serveur aurait alourdi l'usage (processus à lancer, fin du
hors-ligne) sans être plus rapide sur une page donnée. La virtualisation
règle la fluidité en gardant le site 100 % statique. Un serveur reste
pertinent pour un autre besoin — la recherche transversale sur tout le
corpus — qui reste en TODO.

## [2.1.0] — 2026-07-26

### Ajouté
- **Navigation chronologique entre extensions.** Chaque page de set affiche
  deux flèches « ‹ précédent » / « suivant › » vers les extensions voisines
  par date de sortie. Aux extrémités, la flèche correspondante est grisée.
- **Mémoire de session du tri.** L'accueil retient le tri choisi (et le texte
  de recherche) le temps de l'onglet, via `sessionStorage`. Revenir à
  l'accueil après avoir visité une extension ne réinitialise plus le tri à
  « plus récentes d'abord ».

### Note sur la fluidité
Le tri mémorisé supprime le principal agacement au retour vers l'accueil.
La lenteur résiduelle sur les grosses extensions vient du poids des pages
(une page de 460 cartes fait ~225 Ko de HTML, une balise `<img>` par carte) :
c'est inhérent au choix « site statique, une page par set ». Une pagination
ou une virtualisation des vignettes est notée en TODO pour un futur palier,
si le besoin se confirme sur le corpus complet.

## [2.0.1] — 2026-07-26

### Corrigé
- **Crash au téléchargement** (`NameError: name 'cf' is not defined`). À la
  fusion 2.0.0, l'import `concurrent.futures as cf` était devenu
  `from concurrent.futures import ThreadPoolExecutor`, incompatible avec
  l'appel `cf.ThreadPoolExecutor`. Le simple import du module ne le
  révélait pas ; un test qui **exécute** `run_downloads` a été ajouté.
- **Tableau des tailles absent hors `--dry-run`.** `sync` affiche désormais
  le volume estimé de toutes les qualités **avant** chaque téléchargement,
  la qualité choisie étant marquée. Option `--no-sizes` pour le couper.

---

## [2.0.1] — 2026-07-26

Correctif : le téléchargement était cassé dans la 2.0.0.

### Corrigé
- **Crash au téléchargement (`NameError: name 'cf' is not defined`).** À la
  fusion des deux scripts, l'import `concurrent.futures` avait été écrit
  `from concurrent.futures import ThreadPoolExecutor` alors que le corps du
  téléchargeur utilise `cf.ThreadPoolExecutor`. Toute la génération web
  passait (elle n'utilise pas ce module), mais `mtgc sync` plantait dès le
  premier téléchargement d'image. Import rétabli en `import
  concurrent.futures as cf`.
- **Tableau des tailles absent en run normal.** Le récapitulatif des volumes
  par qualité ne s'affichait que sous `--dry-run`. Il s'affiche désormais
  avant chaque téléchargement (coupable : `--no-sizes` pour le désactiver).

### Ajouté
- **Tests de régression exerçant réellement le téléchargement** : vérifient
  que l'alias `cf` existe et que `run_downloads` est câblé. La 2.0.0 avait
  été validée uniquement sur la partie web ; ces bugs seraient apparus plus
  tôt avec un test qui touche au download. Leçon retenue.

## [2.0.0] — 2026-07-26

Refonte majeure : **un seul outil** au lieu de deux scripts.

### Changement incompatible
- `mtgc-images.py` et `mtgc-web.py` fusionnent en **`src/mtgc.py`**.
  Nouvelle interface à sous-commandes :

  ```
  mtgc sync --data-dir ~/mtg              # tout : images + rulings + fontes + site
  mtgc sync --data-dir ~/mtg --no-web     # images seulement
  mtgc sync --data-dir ~/mtg --no-images  # (re)générer le site seul
  mtgc web  --data-dir ~/mtg              # site seul
  mtgc verify --data-dir ~/mtg
  ```

  Chaque étape se coupe : `--no-images`, `--no-rulings`, `--no-fonts`,
  `--no-web`, `--no-card-pages`. Par défaut tout est activé.

### Ajouté
- **Repli d'icônes de set.** La fonte keyrune ne couvre que ~40 % des
  extensions (425 sur 1047) ; les 66 % restants — surtout les sorties
  récentes et les sets de tokens — s'affichaient sans icône. On télécharge
  désormais le SVG officiel depuis Scryfall pour tout set absent de
  keyrune, mis en cache dans `metadata/seticons/`. Couverture portée à
  ~100 %.
- **Accueil : tri et recherche.** Tri par date, nom ou nombre de cartes,
  chaque sens disponible (6 options). Recherche dynamique par nom ou code
  d'extension, filtrage instantané côté client.
- **Skin allégé.** Fond plus clair et plus contrasté, or plus lumineux,
  encre plus lisible. **Icônes agrandies** : 56 px sur l'accueil (contre
  40), 104 px en tête de page de set (contre 72).

### Corrigé
- **Bug : clic sur une variante.** Depuis une page de set, cliquer sur une
  carte présente dans plusieurs éditions ouvrait la page sur l'impression
  la plus ancienne, pas celle du set courant. Le lien porte maintenant un
  fragment `#<set>` et un script place en grand l'impression de l'édition
  d'où l'on vient.
- **Bugs d'assemblage** détectés au test : trois constantes réseau
  (`API_DELAY`, `HEADERS`, `IMAGE_STATUS_RANK`) avaient été perdues à la
  fusion, et un `try/except` trop large les masquait. La récupération de la
  liste des sets remonte désormais ses erreurs.

### Maturité
| Composant | État |
|---|---|
| `src/mtgc.py` | **stable** : images, rulings, fontes, site, un seul fichier |
| `src/mtgc/` (moteur de recherche) | expérimental |
| catalogues PDF | non branché sur SQLite |

---

## [1.3.0] — 2026-07-25

### Ajouté
- **Renommage du projet** en `MTGcyCLAUDEpedia`.
- **Schéma de nommage des images** `<code-set>-<numéro>[-a|-b].<ext>`
  (ex. `lea-186.jpg`), globalement unique — indispensable aux pages de
  carte qui agrègent des impressions de sets différents. `mtgc-migrate.py`
  bascule une collection existante sans retélécharger ; sinon un simple
  retéléchargement suffit.
- **Téléchargeur** : option `--rulings` pour récupérer le bulk des rulings
  (~26 Mo), nécessaire aux pages de carte.
- **Site web (mtgc-web 0.2.0)** :
  - **Pages de carte** : une page par `oracle_id`, listant toutes les
    impressions de la carte dans tous les sets présents (avec icône
    keyrune de chaque set), le texte oracle, et le **dernier ruling** daté.
  - **Fontes Andrew Gioia embarquées en local** (keyrune pour les icônes
    de set, mana-font pour les symboles de mana) : téléchargées une fois,
    mises en cache dans `metadata/fonts/`, copiées dans `site/assets/` avec
    un CSS réécrit pour ne pointer que sur le `.woff2` local. Le site
    fonctionne ainsi **100 % hors-ligne**, fidèle au principe du projet.
    Le bloc mplantin (police de texte, absente en woff2) est retiré du CSS
    mana. Option `--offline` pour n'utiliser que le cache. `--icons` n'est
    plus requis pour l'affichage web.
  - **Viewer plein écran** au clic : zoom (+/−), rotation (r), navigation
    précédent/suivant (←/→), réinitialisation, fermeture (Échap). Sur une
    page de carte, il parcourt toutes les impressions ; sur une page de
    set, toutes les cartes.
  - **Favicon** SVG inline (lotus doré). Voir limite ci-dessous.
  - Options `--no-card-pages` (génération plus rapide) et `--no-rulings`.

### Limite connue
- **Favicon par set non implémentée.** L'idée (le glyphe keyrune du set en
  favicon) se heurte à une contrainte technique : une webfont ne se charge
  pas dans le contexte isolé d'un `data:` SVG de favicon. La favicon reste
  donc constante (identité du site). Piste future : pré-rendre un PNG par
  set à partir du glyphe, au prix d'un fichier par extension.

### Corrigé
- Le générateur web suit le nouveau schéma `<code>-<num>` ; l'ancien
  `stem_variants` (numéro nu) est remplacé par `image_names`. Test
  anti-divergence des deux `sanitize` conservé.

---

## [1.2.0] — 2026-07-24

### Ajouté
- **Générateur de site statique** `mtgc-web.py` : page d'accueil listant les
  extensions présentes dans le data-dir (nom, code, date, nombre de cartes,
  icône), puis une page par extension avec la grande icône en tête, les mêmes
  métadonnées, et la grille de toutes les cartes.
- Thème « sombre à accents dorés / parchemin », cohérent avec l'emblème de
  l'horloge. Icônes keyrune réelles quand elles sont présentes (`--icons`),
  repli sur les initiales du code sinon.
- Filtre et tri **côté client**, sans serveur : recherche par nom, type ou
  illustrateur, filtre par rareté, tri par numéro de collection, nom ou coût
  converti. Site 100 % statique, une carte = une balise `<img>` locale.
- `--open` pour ouvrir la page d'accueil dans le navigateur ; `make web`.
- Les métadonnées (noms, dates, raretés, types) sont tirées du **bulk local** :
  aucun accès réseau à la génération.
- Les images sont liées par lien symbolique (pas de copie de plusieurs Go) ;
  copie en dernier recours si les liens sont impossibles.

### Corrigé
- **Perte silencieuse de cartes à collector number exotique.** Les fichiers
  translittérés par le téléchargeur (`232†` -> `232-dagger`) n'étaient pas
  reconnus : le site affichait 1 052 cartes là où le disque en comptait 1 057.
  La translittération est désormais partagée entre les deux scripts, et un
  filet anti-perte ajoute toute image sur disque qu'aucune carte du bulk n'a
  réclamée.

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
| `src/mtgc-web.py` | **riche** : accueil, pages de set, pages de carte multi-prints, rulings, viewer, fontes keyrune/mana |
| `src/mtgc/query/` | **expérimental** : 21 requêtes conformes à l'API, 1 divergence connue, 26 non testées |
| `src/mtgc/ingest.py`, `db.py` | expérimental, ingestion validée sur 9 492 cartes réelles |
| `src/mtgc/latex.py` | **non fonctionnel** : attend encore le format JSON de 2020 |
