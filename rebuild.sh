#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/3] Stoppe Container..."
docker compose down

echo "[2/3] Baue Image neu..."
docker compose build --no-cache

echo "[3/3] Starte Container..."
docker compose up -d

echo "Fertig."
docker ps
