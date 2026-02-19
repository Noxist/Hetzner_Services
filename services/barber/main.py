"""
Barber-Booker -- FastAPI micro-service for automated Pompadour Barbershop bookings.

Endpoints:
  GET  /health              -- liveness probe
  GET  /status              -- current scheduler state, last booking, etc.
  GET  /slots               -- available slots (with optional calendar filter)
  POST /book                -- trigger an immediate booking cycle
  POST /cancel/{booking_id} -- cancel a booking
  GET  /history             -- booking history

The scheduler runs as an APScheduler background job.
"""

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

import barberly
from calendar_client import CalendarClient
from scheduler import (
    AVAILABILITY_URL,
    DATA_DIR,
    HISTORY_FILE,
    run_booking_cycle,
    _load_history,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("barber")

# ---------------------------------------------------------------------------
# Calendar (optional -- needs GOOGLE_CREDENTIALS_FILE + CALENDAR_ID)
# ---------------------------------------------------------------------------
calendar: Optional[CalendarClient] = None

CREDS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "/data/google_credentials.json")
CALENDAR_ID = os.getenv("CALENDAR_ID", "")

if os.path.exists(CREDS_FILE) and CALENDAR_ID:
    try:
        calendar = CalendarClient(CREDS_FILE, CALENDAR_ID)
        log.info("Google Calendar integration active (calendar=%s)", CALENDAR_ID)
    except Exception as exc:
        log.warning("Calendar init failed: %s", exc)

# ---------------------------------------------------------------------------
# APScheduler
# ---------------------------------------------------------------------------
sched = AsyncIOScheduler(timezone="Europe/Zurich")


async def _scheduled_booking():
    log.info("Scheduled booking cycle triggered")
    try:
        result = await run_booking_cycle(calendar=calendar)
        if result:
            bookings = result.get("bookings", [])
            if bookings:
                for b in bookings:
                    log.info(
                        "Auto-booking [%s]: %s %s",
                        b.get("label"),
                        b.get("slot", {}).get("dateIso"),
                        b.get("slot", {}).get("timeFrom"),
                    )
            else:
                log.info("Cycle completed: %s", result.get("message", "no bookings needed"))
    except Exception as exc:
        log.exception("Scheduled booking cycle failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_interval_hours = int(os.getenv("CHECK_INTERVAL_HOURS", "6"))
    sched.add_job(
        _scheduled_booking,
        IntervalTrigger(hours=check_interval_hours),
        id="barber_booking",
        replace_existing=True,
        next_run_time=None,  # don't run immediately on startup
    )
    sched.start()
    log.info("Scheduler started (check every %d hours)", check_interval_hours)
    yield
    sched.shutdown(wait=False)
    log.info("Scheduler shut down")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Barber-Booker",
    description="Automated haircut booking for Pompadour Barbershop",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status")
async def status():
    history = _load_history()
    last = history[-1] if history else None
    jobs = []
    for job in sched.get_jobs():
        jobs.append({
            "id": job.id,
            "next_run": str(job.next_run_time) if job.next_run_time else None,
        })
    check_interval_hours = int(os.getenv("CHECK_INTERVAL_HOURS", "6"))
    return {
        "service": "barber-booker",
        "version": "2.0.0",
        "calendar_active": calendar is not None,
        "availability_api": AVAILABILITY_URL or "(not configured)",
        "check_interval_hours": check_interval_hours,
        "strategy": "dual-booking (3wk + 4wk from last attended)",
        "total_bookings": len(history),
        "last_booking": last,
        "scheduler_jobs": jobs,
    }


@app.get("/slots")
async def get_slots(
    year: int = Query(default=None),
    month: int = Query(default=None),
    filter_availability: bool = Query(default=True, description="Exclude slots that conflict with calendar/travel"),
):
    """Fetch available slots.  Defaults to current + next month."""
    from scheduler import _filter_slots_by_availability

    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month

    all_slots: list[dict] = []
    months_to_check = [(year, month)]
    next_month = month + 1
    next_year = year
    if next_month > 12:
        next_month = 1
        next_year += 1
    months_to_check.append((next_year, next_month))

    for y, m in months_to_check:
        try:
            slots = await barberly.fetch_slots(y, m)
            all_slots.extend(slots)
        except Exception as exc:
            log.warning("Slot fetch for %d/%02d failed: %s", y, m, exc)

    if filter_availability and all_slots:
        all_slots = await _filter_slots_by_availability(all_slots)

    return {
        "count": len(all_slots),
        "slots": all_slots,
    }


@app.post("/book")
async def trigger_booking(
    force: bool = Query(default=False, description="Ignore interval check"),
    dry_run: bool = Query(default=False, description="Preview only, don't book"),
):
    """Manually trigger a booking cycle."""
    try:
        result = await run_booking_cycle(
            force=force,
            dry_run=dry_run,
            calendar=calendar,
        )
    except Exception as exc:
        log.exception("Booking cycle failed")
        raise HTTPException(status_code=500, detail=str(exc))

    if result is None:
        return {"message": "Skipped (too soon for next booking)", "result": None}
    return {"message": "Booking cycle completed", "result": result}


@app.post("/cancel/{booking_id}")
async def cancel(booking_id: str):
    """Cancel a Barberly booking and remove the calendar event."""
    try:
        result = await barberly.cancel_booking(booking_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cancel API error: {exc}")

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Cancel failed"))

    # Remove from calendar
    if calendar:
        try:
            calendar.delete_event_by_booking_id(booking_id)
        except Exception as exc:
            log.warning("Calendar event deletion failed: %s", exc)

    # Mark in history
    history = _load_history()
    for entry in history:
        if entry.get("booking_id") == booking_id:
            entry["cancelled"] = True
            entry["cancelled_at"] = datetime.now().isoformat()
    HISTORY_FILE.write_text(json.dumps(history, indent=2, default=str))

    return {"message": "Booking cancelled", "booking_id": booking_id}


@app.get("/history")
async def get_history():
    return _load_history()


@app.get("/stylists")
async def get_stylists():
    """List available barbers."""
    return await barberly.get_stylists()


@app.get("/services")
async def get_services():
    """List available services."""
    return await barberly.get_services()
