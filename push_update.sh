#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

VERSION="${1:-}"

if [ -z "$VERSION" ]; then
  echo "Bitte Version angeben, z.B.:"
  echo "./push_update.sh 2.1.1"
  exit 1
fi

echo "$VERSION" > VERSION

echo "Prüfe .gitignore..."
touch .gitignore

grep -qxF ".env" .gitignore || echo ".env" >> .gitignore
grep -qxF "data/" .gitignore || echo "data/" >> .gitignore
grep -qxF "downloads/" .gitignore || echo "downloads/" >> .gitignore
grep -qxF "music/" .gitignore || echo "music/" >> .gitignore
grep -qxF "cookies/" .gitignore || echo "cookies/" >> .gitignore
grep -qxF "backups/" .gitignore || echo "backups/" >> .gitignore
grep -qxF "*.db" .gitignore || echo "*.db" >> .gitignore
grep -qxF "*.sqlite" .gitignore || echo "*.sqlite" >> .gitignore
grep -qxF "*.db-wal" .gitignore || echo "*.db-wal" >> .gitignore
grep -qxF "*.db-shm" .gitignore || echo "*.db-shm" >> .gitignore

echo "Entferne sensible Dateien aus Git-Tracking..."
git rm --cached .env 2>/dev/null || true
git rm -r --cached data downloads music cookies backups 2>/dev/null || true

echo "Git Status:"
git status --short

echo "Dateien hinzufügen..."
git add .

echo "Sicherheitscheck..."
if git diff --cached --name-only | grep -E '(^|/)\.env$|cookies/|data/|downloads/|music/|backups/'; then
  echo "ABBRUCH: Sensible Dateien würden hochgeladen."
  exit 1
fi

echo "Commit erstellen..."
git commit -m "StreamBox Version $VERSION" || echo "Nichts zu committen."

echo "Tag setzen..."
git tag -f "v$VERSION"

echo "Push nach GitHub..."
git push origin main
git push origin "v$VERSION" --force

echo "Fertig: StreamBox v$VERSION wurde sicher zu GitHub hochgeladen."