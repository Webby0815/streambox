#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

VERSION="${1:-}"

if [ -z "$VERSION" ]; then
  echo "Bitte Version angeben, z.B.:"
  echo "./push_update.sh 2.1.1"
  exit 1
fi

echo "$VERSION" > VERSION

echo "Git Status:"
git status --short

echo "Dateien hinzufügen..."
git add .

echo "Commit erstellen..."
git commit -m "StreamBox Version $VERSION" || echo "Nichts zu committen."

echo "Tag setzen..."
git tag -f "v$VERSION"

echo "Push nach GitHub..."
git push origin main
git push origin "v$VERSION" --force

echo "Fertig: StreamBox v$VERSION wurde zu GitHub hochgeladen."
