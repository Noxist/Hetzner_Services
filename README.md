# Hetzner Services

Übersicht über alle Services, Scripts und Automationen auf dem Hetzner Server (`noxist-core`).

---

## Server-Architektur

```
/home/leandro/
├── services/                        # Shared infrastructure + alle Services
│   ├── docker-compose.yml           # Caddy, Cloudflared, MQTT, Printer, Bio-Dashboard
│   ├── Caddyfile                    # Reverse-Proxy Konfiguration
│   ├── .env                         # Secrets (nicht im Repo)
│   ├── cloudflared/                 # Cloudflare Tunnel Config
│   ├── mqtt/                        # Mosquitto MQTT Broker
│   ├── printer/                     # Thermal Printer API
│   └── bio-dashboard/               # Life Manager / Bio-Dashboard
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

### 1. Life Manager / Bio-Dashboard (`bio-dashboard`)

Quantified-Self System mit pharmakokinetischem Modell, Health-Tracking und adaptivem Modell-Training.

| Detail | Wert |
|--------|------|
| **Container** | `bio-dashboard` |
| **Ports** | `8000` (FastAPI), `8501` (Streamlit) |
| **Subdomains** | `bio.thegrandprinter...tech` (UI), `bioapi.thegrandprinter...tech` (API) |
| **Repo** | [Noxist/life_manager](https://github.com/Noxist/life_manager) |
| **Stack** | Python, FastAPI, Streamlit, Plotly, SQLite |

**Features:**
- Pharmakokinetik-Modellierung (Bateman-Funktionen): Elvanse, Medikinet IR/retard, Koffein
- Bio-Score: Circadian-Rhythmus + Substanz-Boosts + Schlaf-Modifier
- Home Assistant Integration (Pixel Watch Health-Sensoren: HR, HRV, SpO2, Schlaf)
- Subjektive Bewertung (Fokus, Laune, Energie, Appetit, Innere Unruhe)
- Mahlzeiten-Tracking
- Adaptives Modell-Training (Elvanse-Wirkungskurve personalisieren)
- Log-Reminder (5x/Tag optimaler Schedule)
- CNS-Last Warnung
- Korrelationsanalyse (Substanz-Level vs. Fokus)
- Tages-Timeline mit Intake-Markern

**Geplante Etappen:**
- Etappe 2: Auto-Barber
- Etappe 3: Super Mega Ultra Planer
- Etappe 4: Daily Briefing

---

### 2. RoomBooker (`auto_reserve`)

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

### 3. Thermal Printer API (`printer`)

Druckt dynamische Inhalte auf einen Thermodrucker via MQTT.

| Detail | Wert |
|--------|------|
| **Container** | `printer-api` |
| **Port** | `8080` (intern, via Caddy) |
| **Domain** | `thegrandprinterofmemesandunfinitetodosservanttonox.tech` |
| **Stack** | Python, FastAPI, MQTT |

**Features:**
- Content-Sources: Wetter, News, Witze, Zitate, DM-Angebote
- Text → Monochrome PNG → Base64 → MQTT → Drucker
- Web-UI für manuelle Druckaufträge
- Guest-Token System
- Queue-basiertes Drucken

---

### 4. MQTT Broker (`mqtt`)

Eclipse Mosquitto als zentraler Message Broker.

| Detail | Wert |
|--------|------|
| **Container** | `mqtt` |
| **Port** | `1883` |
| **Image** | `eclipse-mosquitto:2` |

Wird vom Printer-Service und potenziell weiteren Services für Messaging genutzt.

---

### 5. Caddy (`caddy`)

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
| `bio.thegrandprinter...tech` | `bio-dashboard:8501` |
| `bioapi.thegrandprinter...tech` | `bio-dashboard:8000` |

---

### 6. Cloudflare Tunnel (`cloudflared`)

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
  ├──► roombooker_app:5000
  ├──► bio-dashboard:8501  (Streamlit UI)
  └──► bio-dashboard:8000  (FastAPI API)

MQTT Broker (:1883)
  └──► Thermodrucker (physisch, via MQTT)

Home Assistant (extern, Nabu Casa)
  └──► bio-dashboard (Health-Sensor Polling alle 15min)
```

---

## Repos

| Repo | Beschreibung | Server-Pfad |
|------|-------------|-------------|
| [Noxist/Hetzner_Services](https://github.com/Noxist/Hetzner_Services) | Dieses Repo -- Gesamtübersicht | — |
| [Noxist/life_manager](https://github.com/Noxist/life_manager) | Bio-Dashboard / Life Manager | `services/bio-dashboard/` |
| [Noxist/room_booker_hetzner](https://github.com/Noxist/room_booker_hetzner) | RoomBooker | `auto_reserve/` |

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
# Alle Services starten (inkl. Printer, Bio-Dashboard, MQTT, Caddy, Cloudflared)
cd ~/services && docker compose up -d

# RoomBooker starten (eigener Stack)
cd ~/auto_reserve && docker compose up -d

# Einzelnen Service rebuilden
cd ~/services && docker compose up -d --build bio-dashboard

# Status aller Container
docker ps

# Logs eines Services
docker logs -f <container_name>

# Netzwerk erstellen (einmalig)
docker network create shared_services
```
