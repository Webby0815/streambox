#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

LOG_DIR="./data/update_logs"
BACKUP_DIR="./backups"
mkdir -p "$LOG_DIR" "$BACKUP_DIR"

LOG_FILE="$LOG_DIR/update_$(date +%Y%m%d_%H%M%S).log"

{
  echo "=== StreamBox Update $(date) ==="

  echo "[1/6] Git Status"
  git status --short || true

  echo "[2/6] Backup"
  tar -czf "$BACKUP_DIR/streambox_before_update_$(date +%Y%m%d_%H%M%S).tar.gz"     --exclude='./downloads'     --exclude='./music'     --exclude='./data/*.db-wal'     --exclude='./data/*.db-shm'     ./data ./.env ./VERSION 2>/dev/null || true

  echo "[3/6] Git Pull"
  git pull --ff-only

  echo "[4/6] Docker Stop"
  docker compose down

  echo "[5/6] Docker Build"
  docker compose build --no-cache

  echo "[6/6] Docker Start"
  docker compose up -d

  echo "=== Update fertig $(date) ==="
} 2>&1 | tee "$LOG_FILE"
