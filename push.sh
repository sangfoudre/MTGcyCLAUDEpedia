#!/usr/bin/env bash
# push.sh — envoie le projet sur GitHub sans avoir à se souvenir des commandes.
#
# À lancer depuis le dossier du projet, après avoir posé une nouvelle version :
#     ./push.sh "message de commit"
#
# Ce script fait, dans l'ordre :
#   1. s'assure que le dépôt distant 'origin' pointe vers le bon dépôt GitHub
#      (le rajoute s'il manque — utile quand on vient de décompresser l'archive) ;
#   2. ajoute tous les changements et les commite avec ton message ;
#   3. pousse la branche main (en la reliant à origin la première fois) ;
#   4. pousse les tags de version.
#
# Solo et local faisant foi : en cas d'historique divergent après décompression
# d'archive, on force. Sûr ici car personne d'autre ne pousse sur ce dépôt.

set -euo pipefail

REPO_URL="https://github.com/sangfoudre/MTGcyCLAUDEpedia.git"
MSG="${1:-mise à jour}"

# 1. remote origin
if ! git remote get-url origin >/dev/null 2>&1; then
  echo "→ ajout du dépôt distant origin"
  git remote add origin "$REPO_URL"
else
  git remote set-url origin "$REPO_URL"
fi

# 2. commit (s'il y a quelque chose à committer)
git add -A
if git diff --cached --quiet; then
  echo "→ rien de nouveau à committer"
else
  git commit -m "$MSG"
  echo "→ commit : $MSG"
fi

# 3. push de la branche (force car local fait foi ; solo)
echo "→ envoi de la branche main"
git push --force --set-upstream origin main

# 4. push des tags
echo "→ envoi des tags de version"
git push --force origin --tags

echo "✓ terminé — vérifie sur $REPO_URL"
