# Hetzner Services

Übersicht über alle Services, Scripts und Automationen auf dem Hetzner Server (`noxist-core`).

---

## Server-Architektur

```
/home/leandro/
├── services/                        # Shared infrastructure + lightweight services
│   ├── docker-compose.yml           # Caddy, Cloudflared, MQTT, Printer
│   ├── Caddyfile                    # Reverse-Proxy Konfiguration
│   ├── .env                         # Secrets (nicht im Repo)
│   ├── cloudflared/                 # Cloudflare Tunnel Config
│   ├── mqtt/                        # Mosquitto MQTT Broker
│   └── printer/                     # Thermal Printer API (eigenes Repo)
│
├── auto_reserve/                    # RoomBooker Applikation (eigenes Repo)
│   └── docker-compose.yml           # Eigener Compose-Stack
│
└── auto_reserve_data/               # RoomBooker Runtime-Daten
    ├── jobs.json, rooms.json, ...   # Konfig & State
    ├── debug_dumps/                 # HTML-Dumps zur Fehleranalyse
    └── logs/                        # Applikations-Logs
```

---

## Services

### 1. RoomBooker (`auto_reserve`)

Automatisiertes Raumbuchungssystem für [raumreservation.ub.unibe.ch](https://raumreservation.ub.unibe.ch).

| Detail | Wert |
|--------|------|
| **Container** | `roombooker_app` |
| **Port** | `5000` (intern) |
| **Subdomain** | `bibliothek.thegrandprinter...tech` |
| **Repo** | [Noxist/room_booker_hetzner](https://github.com/Noxist/room_booker_hetzner) |
| **Stack** | Python, Flask, Playwright |

**Features:**
- Automatische Raumbuchung mit Account-Rotation
- 14-Tage-Vorausbuchung mit Scheduler
- Gap-Splitting (lange Buchungen → mehrere 4h-Blöcke)
- Room-Scoring nach Verfügbarkeit, Distanz & Gewichtung
- Google Calendar Sync (Placeholder → bestätigte Events)
- Web-Dashboard + CLI
- Overlap-Detection mit 6 Auflösungsoptionen

**Cron-Jobs:**
```
# Täglich 00:01 - Neue Slots prüfen (14-Tage-Fenster)
1 0 * * * python3 main.py --process-jobs

# Alle 6 Stunden - Filler-Check
0 */6 * * * python3 main.py --process-jobs
```

---

### 2. Thermal Printer API (`printer`)

Druckt dynamische Inhalte auf einen Thermodrucker via MQTT.

| Detail | Wert |
|--------|------|
| **Container** | `printer-api` |
| **Port** | `8080` (intern, via Caddy) |
| **Domain** | `thegrandprinterofmemesandunfinitetodosservanttonox.tech` |
| **Repo** | [Noxist/printer](https://github.com/Noxist/printer) *(falls vorhanden)* |
| **Stack** | Python, FastAPI, MQTT |

**Features:**
- Content-Sources: Wetter, News, Witze, Zitate, DM-Angebote
- Text → Monochrome PNG → Base64 → MQTT → Drucker
- Web-UI für manuelle Druckaufträge
- Guest-Token System
- Queue-basiertes Drucken

---

### 3. MQTT Broker (`mqtt`)

Eclipse Mosquitto als zentraler Message Broker.

| Detail | Wert |
|--------|------|
| **Container** | `mqtt` |
| **Port** | `1883` |
| **Image** | `eclipse-mosquitto:2` |

Wird vom Printer-Service für die Kommunikation mit dem physischen Drucker genutzt.

---

### 4. Caddy (`caddy`)

Reverse-Proxy für alle Web-Services.

| Detail | Wert |
|--------|------|
| **Container** | `caddy` |
| **Ports** | `80`, `443` |

**Routing:**
| Domain | → Service |
|--------|-----------|
| `thegrandprinter...tech` | `printer-api:8080` |
| `bibliothek.thegrandprinter...tech` | `roombooker_app:5000` |

---

### 5. Cloudflare Tunnel (`cloudflared`)

Tunnelt den gesamten Traffic sicher über Cloudflare (kein offener Port nötig).

| Detail | Wert |
|--------|------|
| **Container** | `cloudflared` |
| **Image** | `cloudflare/cloudflared:latest` |

---

## Netzwerk

Alle Container laufen im gleichen Docker-Netzwerk `shared_services`:

```
Internet
  │
  ▼
Cloudflare Tunnel (cloudflared)
  │
  ▼
Caddy (Reverse Proxy, :80/:443)
  ├──► printer-api:8080
  └──► roombooker_app:5000

MQTT Broker (:1883)
  └──► Thermodrucker (physisch, via MQTT)
```

---

## Neue Services hinzufügen

### Variante A: Teil des bestehenden Compose-Stacks

Für leichtgewichtige Services, die die shared Infrastruktur (Caddy, MQTT) mitnutzen:

1. Neuen Ordner unter `services/` erstellen
2. Service in `services/docker-compose.yml` hinzufügen
3. Subdomain in `Caddyfile` eintragen
4. Hostname in `cloudflared/config.yml` hinzufügen

### Variante B: Eigener Compose-Stack

Für grössere/unabhängige Applikationen:

1. Eigenen Ordner unter `~/` erstellen (z.B. `~/neuer_service/`)
2. Eigenes `docker-compose.yml` mit `networks: shared_services (external: true)`
3. Caddy-Routing in `services/Caddyfile` ergänzen

### Konvention

- Jeder Service braucht sein eigenes **Git-Repository** für den Source Code
- **Secrets** gehören in `.env` Dateien (nie ins Repo)
- **Runtime-Daten** werden als Docker-Volumes oder in separaten `_data/` Ordnern gespeichert
- Container-Name sollte beschreibend sein (z.B. `myservice_app`)

---

## Docker-Befehle

```bash
# Alle Services starten
cd ~/services && docker compose up -d

# RoomBooker starten
cd ~/auto_reserve && docker compose up -d

# Status aller Container
docker ps

# Logs eines Services
docker logs -f <container_name>

# Netzwerk erstellen (einmalig)
docker network create shared_services
```

---

## Ordner-Struktur Bewertung

Die aktuelle Struktur ist **sauber und skalierbar**:

- `services/` = Shared Infrastructure + eingebettete leichte Services
- `auto_reserve/` = Eigenständige App mit eigenem Compose-Stack
- Klare Trennung von Code (`auto_reserve/`) und Daten (`auto_reserve_data/`)
- Gemeinsames Docker-Netzwerk verbindet alles

Für neue Services einfach dem gleichen Muster folgen (siehe oben).
