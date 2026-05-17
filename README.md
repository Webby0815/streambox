# StreamBox SuperPi

Optimierte Raspberry-Pi-4-Version.

## Neu / verbessert

- schöneres UI mit moderneren Karten, Audio-Ansicht und flüssiger Queue
- MP3 wird mit Audio-Player angezeigt
- endgültig löschen entfernt auch echte Dateien
- Status-Seite: Dateien neu einlesen + MiniDLNA aktualisieren
- SQLite optimiert gegen `database is locked`
- Dockerfile robuster
- HDD-Struktur fest auf `/media/pi/EXTERN HDD/Streambox`
- neue Styles: Cinema Poster, Clean Hell
- MiniDLNA gibt Video und Musik frei

## Installation

```bash
cd "/media/pi/EXTERN HDD/Streambox/SYSTEM"
sudo docker-compose down
sudo docker-compose build --no-cache
sudo docker-compose up -d
```

## Nach dem Start

Browser:

```text
http://RASPI-IP:28050
```

Statusseite:

```text
/admin/status
```

Dort den Button **Dateien neu einlesen + MiniDLNA aktualisieren** nutzen, wenn Dateien manuell in `downloads` oder `music` gelegt wurden.


## Nächtliche Papierkorb-Löschung

In `.env` setzen:

```env
CRON_TOKEN=ein-langer-geheimer-token
```

Crontab:

```bash
crontab -e
```

Eintrag für 03:30 Uhr:

```cron
30 3 * * * curl -fsS "http://127.0.0.1:28050/cron/purge-trash?token=DEIN_CRON_TOKEN" >/dev/null 2>&1
```

Ablauf:
- Mediathek: Löschen verschiebt in Papierkorb.
- Papierkorb: endgültige Löschung vormerken.
- Cron: löscht vorgemerkte Dateien nachts wirklich.


## Fast Delete Fix

Diese Version macht „Zur Löschung vormerken“ extrem schnell:
- nur DB-Flag `delete_pending=1`
- keine Dateioperationen
- kein MiniDLNA-Rescan
- keine Thumbnails
- echte Löschung ausschließlich per `/cron/purge-trash`

Nach Update einmal auf Statusseite:
`Datenbank-Migration ausführen`

Oder per Shell:
```bash
docker exec streambox python - <<'PY'
import sqlite3
con=sqlite3.connect('/srv/streambox/data/media.db')
cols=[r[1] for r in con.execute("PRAGMA table_info(videos)").fetchall()]
if "delete_pending" not in cols:
    con.execute("ALTER TABLE videos ADD COLUMN delete_pending INTEGER DEFAULT 0")
if "delete_pending_at" not in cols:
    con.execute("ALTER TABLE videos ADD COLUMN delete_pending_at TEXT")
con.commit()
con.close()
PY
```


## Dailymotion Fix
Diese Version nutzt fuer Dailymotion denselben stabilen yt-dlp-Aufruf wie der erfolgreiche Direkt-Test:
- Browser User-Agent
- Referer/Origin Header
- dailymotion:client=web
- Format bv*+ba/b/best

Start:
```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```
