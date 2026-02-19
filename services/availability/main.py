"""
Availability API -- standalone micro-service.

Exposes a simple REST interface: send a time slot, get back yes/no
with conflict details.  Designed to be reusable by any service
(barber-booker, room-booker, future planners, etc.).

Supports configurable **urgency levels** (1-3) that control which
calendars are considered blocking.  See urgency_config.json for the
mapping.

Endpoints:
  GET  /health                    -- liveness probe
  POST /check                     -- is a single slot free?
  POST /check-batch               -- check multiple slots at once
  GET  /free-windows?date=...     -- find free windows on a date
  GET  /calendars                 -- list configured calendars
  GET  /urgency-config            -- show active urgency configuration
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from checker import AvailabilityChecker, CheckResult

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("availability")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CREDS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "/data/google_credentials.json")


def _load_calendar_config() -> dict[str, str]:
    """Load calendar name -> ID mapping from the JSON config file."""
    config_path = os.getenv("CALENDARS_CONFIG", "/app/calendars.json")
    if os.path.exists(config_path):
        try:
            data = json.loads(open(config_path).read())
            if isinstance(data, dict):
                cals = data.get("calendars", data)
                return {k: v for k, v in cals.items() if isinstance(v, str)}
        except Exception as exc:
            log.warning("Failed to load %s: %s", config_path, exc)
    return {}


def _load_urgency_config() -> dict:
    """Load urgency rules from the JSON config file."""
    config_path = os.getenv("URGENCY_CONFIG", "/app/urgency_config.json")
    if os.path.exists(config_path):
        try:
            return json.loads(open(config_path).read())
        except Exception as exc:
            log.warning("Failed to load %s: %s", config_path, exc)
    return {"default_urgency": 2, "calendar_rules": {}}


calendar_config = _load_calendar_config()
urgency_config = _load_urgency_config()
default_urgency: int = urgency_config.get("default_urgency", 2)
calendar_rules: dict = urgency_config.get("calendar_rules", {})

checker: Optional[AvailabilityChecker] = None

if os.path.exists(CREDS_FILE) and calendar_config:
    try:
        checker = AvailabilityChecker(
            CREDS_FILE,
            calendar_config,
            calendar_rules,
            default_urgency,
        )
        log.info(
            "Checker active  --  %d calendars, default urgency %d",
            len(calendar_config), default_urgency,
        )
    except Exception as exc:
        log.error("Failed to init AvailabilityChecker: %s", exc)
else:
    log.warning(
        "Checker NOT active. creds_exists=%s, calendars=%d",
        os.path.exists(CREDS_FILE), len(calendar_config),
    )


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SlotCheck(BaseModel):
    start: str = Field(..., description="ISO-8601 start time")
    end: str = Field(..., description="ISO-8601 end time")
    travel_buffer_before: int = Field(0, description="Minutes to add before start")
    travel_buffer_after: int = Field(0, description="Minutes to add after end")
    urgency: Optional[int] = Field(None, ge=1, le=3, description="Urgency 1-3 (None = config default)")


class BatchCheck(BaseModel):
    slots: list[SlotCheck]
    travel_buffer_before: int = Field(0, description="Default buffer before (per-slot overrides win)")
    travel_buffer_after: int = Field(0, description="Default buffer after (per-slot overrides win)")
    urgency: Optional[int] = Field(None, ge=1, le=3, description="Default urgency (per-slot overrides win)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_checker():
    if checker is None:
        raise HTTPException(
            status_code=503,
            detail="Availability checker not configured. Set GOOGLE_CREDENTIALS_FILE and CALENDAR_IDS.",
        )
    return checker


def _result_to_dict(r: CheckResult) -> dict:
    return {
        "available": r.available,
        "urgency": r.urgency,
        "requested_start": r.requested_start,
        "requested_end": r.requested_end,
        "checked_start": r.checked_start,
        "checked_end": r.checked_end,
        "travel_buffer_before": r.travel_buffer_before,
        "travel_buffer_after": r.travel_buffer_after,
        "conflicts": [
            {"calendar": c.calendar, "event_title": c.event_title, "start": c.start, "end": c.end}
            for c in r.conflicts
        ],
        "overridden": [
            {"calendar": c.calendar, "event_title": c.event_title, "start": c.start, "end": c.end}
            for c in r.overridden
        ],
        "calendars_checked": r.calendars_checked,
    }


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Availability API",
    description=(
        "Standalone availability checker. Send a time slot, get yes/no. "
        "Checks multiple Google Calendars via the Events API. "
        "Supports urgency levels (1-3) to control which calendars block."
    ),
    version="2.0.0",
)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "checker_active": checker is not None,
        "calendars": len(calendar_config),
        "default_urgency": default_urgency,
    }


@app.post("/check")
async def check_slot(body: SlotCheck):
    """
    Check if a single time slot is free.

    The ``urgency`` field controls which calendars count as blocking:
      - 1 = top priority (only strongest calendars block)
      - 2 = medium (default)
      - 3 = low priority (almost everything blocks)

    Example payload:
    ```json
    {
      "start": "2026-02-20T10:00:00+01:00",
      "end": "2026-02-20T10:40:00+01:00",
      "travel_buffer_before": 30,
      "travel_buffer_after": 20,
      "urgency": 2
    }
    ```
    """
    chk = _require_checker()
    start = datetime.fromisoformat(body.start)
    end = datetime.fromisoformat(body.end)

    result = chk.check(
        start, end,
        travel_buffer_before=body.travel_buffer_before,
        travel_buffer_after=body.travel_buffer_after,
        urgency=body.urgency,
    )
    return _result_to_dict(result)


@app.post("/check-batch")
async def check_batch(body: BatchCheck):
    """
    Check multiple slots at once.

    Per-slot buffers and urgency override the top-level defaults.
    """
    chk = _require_checker()
    slots = []
    for s in body.slots:
        slots.append({
            "start": s.start,
            "end": s.end,
            "travel_buffer_before": s.travel_buffer_before or body.travel_buffer_before,
            "travel_buffer_after": s.travel_buffer_after or body.travel_buffer_after,
            "urgency": s.urgency if s.urgency is not None else body.urgency,
        })
    results = chk.check_batch(slots, urgency=body.urgency)
    return {
        "count": len(results),
        "results": [_result_to_dict(r) for r in results],
    }


@app.get("/free-windows")
async def free_windows(
    date: str = Query(..., description="Date to check (YYYY-MM-DD)"),
    min_duration: int = Query(30, description="Minimum free window in minutes"),
    day_start: int = Query(8, description="Day starts at this hour (0-23)"),
    day_end: int = Query(20, description="Day ends at this hour (0-23)"),
    urgency: Optional[int] = Query(None, ge=1, le=3, description="Urgency 1-3 (None = config default)"),
):
    """
    Find all free windows on a given date.

    Useful for finding *when* someone is available, not just yes/no.
    The ``urgency`` parameter controls which calendars count as blocking.
    """
    chk = _require_checker()
    try:
        dt = datetime.fromisoformat(date)
    except ValueError:
        # Try parsing as bare date
        dt = datetime.strptime(date, "%Y-%m-%d")

    windows = chk.free_windows(
        dt,
        min_duration_minutes=min_duration,
        day_start_hour=day_start,
        day_end_hour=day_end,
        urgency=urgency,
    )
    return {
        "date": date,
        "urgency": urgency if urgency is not None else default_urgency,
        "free_windows": windows,
        "count": len(windows),
    }


@app.get("/calendars")
async def list_calendars():
    """Show which calendars are being checked and their urgency rules."""
    return {
        "calendars": {
            name: {
                "id": cal_id,
                "rule": calendar_rules.get(name, {}),
            }
            for name, cal_id in calendar_config.items()
        },
        "checker_active": checker is not None,
    }


@app.get("/urgency-config")
async def get_urgency_config():
    """Show the active urgency configuration."""
    return urgency_config
