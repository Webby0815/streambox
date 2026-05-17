#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "Stoppe StreamBox..."
docker compose down

echo "Baue StreamBox neu..."
docker compose build --no-cache

echo "Starte StreamBox..."
docker compose up -d

echo "Status:"
docker ps

echo "Logs:"
docker logs streambox --tail=40
