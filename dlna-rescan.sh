#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
sudo docker restart minidlna
echo "MiniDLNA wurde neu gestartet/rescan ausgelöst."
