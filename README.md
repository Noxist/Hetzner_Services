# Hetzner Services

This repository tracks the shared infrastructure layer for the Hetzner host.

Live path on the server:

- `/home/leandro/services`

This repo is the Git-backed copy of the stack-level deployment files that wire Cloudflare Tunnel, Caddy, and the shared Docker services together.

## Layout standard

Code lives under:

- `/home/leandro/services/<app-name>`

Persistent runtime data lives under:

- `/home/leandro/service_data/<app-name>`

Examples:

- `/home/leandro/services/auto_reserve`
- `/home/leandro/services/printer`
- `/home/leandro/services/bio-dashboard`
- `/home/leandro/service_data/auto_reserve`

Compatibility symlinks still exist for RoomBooker:

- `/home/leandro/auto_reserve` -> `/home/leandro/services/auto_reserve`
- `/home/leandro/auto_reserve_data` -> `/home/leandro/service_data/auto_reserve`

## What this repo owns

Tracked here:

- `services/docker-compose.yml`
- `services/Caddyfile`
- `services/cloudflared/config.yml`
- `services/adventurelog/nginx.conf`
- top-level operations documentation

Tracked in app-specific repos instead:

- `services/auto_reserve` -> `https://github.com/Noxist/room_booker_hetzner.git`
- `services/printer` -> `https://github.com/Noxist/printer.git`
- `services/bio-dashboard` -> `https://github.com/Noxist/life_manager.git`
- `services/ocr` -> `https://github.com/Noxist/Bulk_OCR.git`
- `services/organizer` -> `https://github.com/Noxist/organizer.git`
- `services/availability` -> `https://github.com/Noxist/availability.git`
- `services/barber` -> `https://github.com/Noxist/barber.git`
- `services/ocr-auth` -> `https://github.com/Noxist/ocr-auth.git`
- `services/watch-service` -> `https://github.com/Noxist/watch-service.git`

## Security model

- All public web traffic must enter through Cloudflare Tunnel.
- Cloudflare Tunnel forwards to `caddy` for the shared stack and directly to `adventurelog-proxy` for `trip.*`.
- Caddy forwards only to internal Docker services on the `shared_services` network.
- Admin interfaces are protected by Cloudflare Access.
- Public exceptions are explicit and limited to tokenized guest flows.
- APIs still require application-layer secrets.

Current active public hostnames:

- `thegrandprinterofmemesandunfinitetodosservanttonox.tech` -> `printer-api:8080`
- `bibliothek.thegrandprinterofmemesandunfinitetodosservanttonox.tech` -> `roombooker_app:5000`
- `bio.thegrandprinterofmemesandunfinitetodosservanttonox.tech` -> `bio-dashboard:8501`
- `bioapi.thegrandprinterofmemesandunfinitetodosservanttonox.tech` -> `bio-dashboard:8000`
- `ocr.thegrandprinterofmemesandunfinitetodosservanttonox.tech` -> `ocr-auth:8080`
- `trip.thegrandprinterofmemesandunfinitetodosservanttonox.tech` -> `adventurelog-proxy:80`
- `watch.thegrandprinterofmemesandunfinitetodosservanttonox.tech` -> `watch-service:3000`

Currently deactivated public hostnames:

- `barber.thegrandprinterofmemesandunfinitetodosservanttonox.tech`
- `availability.thegrandprinterofmemesandunfinitetodosservanttonox.tech`
- `organizer.thegrandprinterofmemesandunfinitetodosservanttonox.tech`
- `homebox.thegrandprinterofmemesandunfinitetodosservanttonox.tech`

## Secret locations

Shared stack secrets live in:

- `/home/leandro/services/.env`

Important variables:

- `APP_API_KEY`
- `BIO_API_KEY`
- `WATER_WATCH_TOKEN`
- `ADMIN_SECRET`
- `CF_ACCESS_TEAM`
- `CF_ACCESS_AUD`
- `TUNNEL_TOKEN`

Dedicated service env files:

- `/home/leandro/services/ocr/server/.env`
- `/home/leandro/services/watch-service/.env`
- `/home/leandro/services/adventurelog/.env`

## Operational notes

- `availability`, `barber-booker`, `organizer`, and `homebox` stay disabled behind the `manual` compose profile until explicitly reactivated.
- `printer-api` handles admin UI access only through Cloudflare Access headers; `/ui/login` is intentionally dead and is not a fallback login page.
- `bibliothek` is intended to be admin-only and is now expected to require Cloudflare Access both in Cloudflare and at the RoomBooker app layer.
- `watch` keeps `/watch/*` public by token while `/admin*` is Cloudflare-protected.
- `ocr` is routed through `ocr-auth`, which validates Cloudflare Access JWTs before reaching the OCR backend.
- `trip` is routed directly from Cloudflare Tunnel to `adventurelog-proxy`; the proxy needs larger response-header buffers because AdventureLog can emit large auth/session headers on dashboard routes.

## Deploy

From `/home/leandro/services`:

```bash
docker compose up -d caddy cloudflared printer-api bio-dashboard bulk-ocr ocr-auth adventurelog-db adventurelog-server adventurelog-web adventurelog-proxy
docker compose stop availability barber-booker organizer homebox
```

From `/home/leandro/services/watch-service`:

```bash
docker compose up -d --build watch-service
```

From `/home/leandro/services/auto_reserve`:

```bash
docker compose up -d --build
```