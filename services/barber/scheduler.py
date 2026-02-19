"""
Scheduler for automated Pompadour Barbershop bookings.

Strategy:
1. Fetch real booking history from the Barberly API.
2. Determine the last ATTENDED appointment (state=2).
3. Book TWO appointments: one ~3 weeks out, one ~4 weeks out.
4. Filter candidates through the Availability API (calendar + commute).
5. Score slots using day-weights, time preferences, and optional bio-score.
6. Never rebook a slot the user previously cancelled.
7. Always create a Google Calendar event with a cancel link.
8. Runs every 6 hours to catch newly released slots fast.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

from barberly import (
    fetch_slots,
    book_slot,
    cancel_booking,
    fetch_booking_history,
    SERVICE,
    CANCEL_LINK,
    STATE_COMPLETED,
    STATE_CANCELLED,
    STATE_CONFIRMED,
    EMPLOYEE_ID,
)

log = logging.getLogger("barber.scheduler")

DATA_DIR = Path(os.getenv("BARBER_DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

HISTORY_FILE = DATA_DIR / "booking_history.json"
CONFIG_FILE = DATA_DIR / "config.json"
DAY_WEIGHTS_FILE = Path(__file__).parent / "day_weights.json"
CANCELLED_SLOTS_FILE = DATA_DIR / "cancelled_slots.json"
OVERRIDES_FILE = DATA_DIR / "overrides.json"

CUSTOMER_DEFAULT = {
    "customerId": "75e2b7b4-e14d-437d-a813-bb63f311771c",
    "firstName": "Leandro",
    "lastName": "Aeschbacher",
    "email": "leandro.aeschbacher77@gmail.com",
    "phoneNumber": "+41791321932",
}

# Preferred time windows (hour ranges, 24h format).
PREFERRED_HOURS = [(9, 12), (14, 17)]

# Booking windows relative to last attended appointment
BOOKING_WINDOW_EARLY_DAYS = 21   # ~3 weeks
BOOKING_WINDOW_LATE_DAYS = 28    # ~4 weeks
BOOKING_TOLERANCE_DAYS = 3       # +/- tolerance for finding slots near the target

# Urgency level for the availability API
URGENCY = 2


# ---------------------------------------------------------------------------
# Local data helpers
# ---------------------------------------------------------------------------

def _load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []


def _save_history(history: list[dict]):
    HISTORY_FILE.write_text(json.dumps(history, indent=2, default=str))


def _load_cancelled_slots() -> set[str]:
    """Load slot fingerprints that the user cancelled (never rebook these)."""
    if CANCELLED_SLOTS_FILE.exists():
        return set(json.loads(CANCELLED_SLOTS_FILE.read_text()))
    return set()


def _save_cancelled_slots(slots: set[str]):
    CANCELLED_SLOTS_FILE.write_text(json.dumps(sorted(slots), indent=2))


def _slot_fingerprint(slot: dict) -> str:
    """Unique key for a timeslot to detect re-booking cancelled ones."""
    return f"{slot.get('dateIso', '')}_{slot.get('timeFrom', '')}_{slot.get('timeTo', '')}"


def _load_day_weights() -> dict:
    """Load day-weight configuration."""
    override = DATA_DIR / "day_weights.json"
    path = override if override.exists() else DAY_WEIGHTS_FILE
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as exc:
            log.warning("Failed to load day_weights.json: %s", exc)
    return {}


def _load_overrides() -> dict:
    """
    Load manual overrides from /data/overrides.json.

    Supported keys:
      last_attended_date:   ISO date string, bypasses Barberly API entirely.
      exclude_booking_ids:  List of booking IDs whose state=2 should be
                            treated as incorrect (e.g. API marked as attended
                            but user actually cancelled).
    """
    if OVERRIDES_FILE.exists():
        try:
            return json.loads(OVERRIDES_FILE.read_text())
        except Exception as exc:
            log.warning("Failed to load overrides.json: %s", exc)
    return {}


# ---------------------------------------------------------------------------
# Barberly history integration
# ---------------------------------------------------------------------------

async def _fetch_last_attended_date() -> Optional[datetime]:
    """
    Get the date of the last ATTENDED appointment.

    Priority:
    1. Manual override in /data/overrides.json  (last_attended_date)
    2. Barberly API history (state=2), excluding any IDs in
       overrides.exclude_booking_ids
    3. Local booking history as fallback
    """
    overrides = _load_overrides()

    # 1. Manual override
    manual = overrides.get("last_attended_date")
    if manual:
        log.info("Using manual last_attended_date override: %s", manual)
        return datetime.fromisoformat(manual)

    exclude_ids = set(overrides.get("exclude_booking_ids", []))

    # 2. Barberly API
    try:
        past_bookings = await fetch_booking_history(upcoming=False)
        attended = [
            b for b in past_bookings
            if b.get("state") == STATE_COMPLETED
            and b.get("timeSlot")
            and b.get("id") not in exclude_ids
        ]
        if not attended:
            log.warning("No attended appointments after exclusions")
            return None
        attended.sort(
            key=lambda b: b["timeSlot"].get("dateIso", ""),
            reverse=True,
        )
        last_date = attended[0]["timeSlot"]["dateIso"]
        log.info("Last attended appointment: %s (id=%s)", last_date, attended[0]["id"][:8])
        return datetime.fromisoformat(last_date)
    except Exception as exc:
        log.warning("Failed to fetch booking history: %s", exc)

    # 3. Local fallback
    history = _load_history()
    completed = [h for h in history if not h.get("cancelled")]
    if completed:
        last = max(completed, key=lambda h: h.get("date", ""))
        return datetime.fromisoformat(last["date"])
    return None


async def _fetch_upcoming_booking_ids() -> set[str]:
    """Get IDs of all currently upcoming bookings."""
    try:
        upcoming = await fetch_booking_history(upcoming=True)
        return {
            b["id"]
            for b in upcoming
            if b.get("state") in (STATE_CONFIRMED, STATE_COMPLETED)
        }
    except Exception as exc:
        log.warning("Failed to fetch upcoming bookings: %s", exc)
        return set()


async def _sync_cancelled_slots():
    """
    Sync cancelled slot fingerprints from Barberly history.
    Ensures we never rebook a timeslot the user explicitly cancelled.
    """
    try:
        past = await fetch_booking_history(upcoming=False)
        cancelled = _load_cancelled_slots()
        for b in past:
            if b.get("state") == STATE_CANCELLED and b.get("timeSlot"):
                fp = _slot_fingerprint(b["timeSlot"])
                cancelled.add(fp)
        _save_cancelled_slots(cancelled)
    except Exception as exc:
        log.warning("Failed to sync cancelled slots: %s", exc)


# ---------------------------------------------------------------------------
# Slot scoring
# ---------------------------------------------------------------------------

def _score_slot(
    slot: dict,
    target_date: datetime,
    day_weights: dict,
    bio_score: Optional[float] = None,
) -> float:
    """
    Score a slot for ranking. Higher is better.

    Factors:
    - Proximity to target date  (+0..15, peaks at target, decays with distance)
    - Preferred time window     (+10)
    - Day-of-week weight        (*multiplier from config)
    - Date-specific override    (*multiplier, takes priority)
    - Bio-score                 (+0..5)
    - Custom preferences        (+bonus from config)
    """
    score = 0.0

    # --- Time preference ---
    hour = slot.get("hourFrom", 12)
    for lo, hi in PREFERRED_HOURS:
        if lo <= hour < hi:
            score += 10
            break

    # --- Proximity to target date ---
    try:
        dt = datetime.fromisoformat(slot["from"])
        days_off = abs((dt.date() - target_date.date()).days)
        score += max(0, 15 - days_off * 2)
    except Exception:
        pass

    # --- Day weight ---
    try:
        dt = datetime.fromisoformat(slot["from"])
        date_iso = slot.get("dateIso", "")

        date_overrides = day_weights.get("date_overrides", {})
        if date_iso in date_overrides:
            override = date_overrides[date_iso]
            weight = override.get("weight", 1.0) if isinstance(override, dict) else 1.0
        else:
            weekday = str(dt.weekday())
            wd_config = day_weights.get("day_weights", {}).get(weekday, {})
            weight = wd_config.get("weight", 1.0) if isinstance(wd_config, dict) else 1.0

        score *= max(weight, 0.01)
    except Exception:
        pass

    # --- Custom preferences ---
    try:
        prefs = day_weights.get("custom_preferences", {})
        dt = datetime.fromisoformat(slot["from"])
        for key, pref in prefs.items():
            if not isinstance(pref, dict) or not pref.get("enabled"):
                continue
            if key == "institut_tuesday" and dt.weekday() == 1:
                score += pref.get("bonus", 0)
    except Exception:
        pass

    # --- Bio-score ---
    if bio_score is not None:
        score += bio_score * 5

    return round(score, 2)


# ---------------------------------------------------------------------------
# Bio-dashboard integration (optional)
# ---------------------------------------------------------------------------

async def _get_bio_score(dt: datetime) -> Optional[float]:
    bio_url = os.getenv("BIO_API_URL")
    bio_key = os.getenv("BIO_API_KEY")
    if not bio_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{bio_url}/api/score",
                params={"timestamp": dt.isoformat()},
                headers={"Authorization": f"Bearer {bio_key}"} if bio_key else {},
            )
            if resp.status_code == 200:
                return resp.json().get("score")
    except Exception as exc:
        log.debug("Bio-dashboard unavailable: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Printer integration (optional)
# ---------------------------------------------------------------------------

async def _print_receipt(booking: dict, slot: dict):
    printer_url = os.getenv("PRINTER_URL")
    printer_key = os.getenv("PRINTER_API_KEY")
    if not printer_url:
        return

    lines = [
        "================================",
        "   POMPADOUR BARBERSHOP",
        "   Schifflaube 52, 3011 Bern",
        "================================",
        "",
        f"  Datum:   {slot.get('dateIso', '?')}",
        f"  Zeit:    {slot.get('timeFrom', '?')} - {slot.get('timeTo', '?')}",
        f"  Service: {SERVICE['name']}",
        f"  Preis:   CHF {SERVICE['price']:.2f}",
        "",
        f"  Booking: {booking.get('id', '?')[:8]}...",
        "",
        f"  Stornieren: {CANCEL_LINK}",
        "",
        "================================",
    ]
    text = "\n".join(lines)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{printer_url}/api/print/text",
                json={"text": text},
                headers={"X-API-Key": printer_key} if printer_key else {},
            )
        log.info("Receipt printed successfully")
    except Exception as exc:
        log.warning("Failed to print receipt: %s", exc)


# ---------------------------------------------------------------------------
# Availability API integration (commute-aware)
# ---------------------------------------------------------------------------

AVAILABILITY_URL = os.getenv("AVAILABILITY_URL", "")
TRAVEL_BUFFER_BEFORE = int(os.getenv("TRAVEL_BUFFER_BEFORE", "60"))  # minutes
TRAVEL_BUFFER_AFTER = int(os.getenv("TRAVEL_BUFFER_AFTER", "30"))    # minutes
COMMUTE_TOLERANCE_MINUTES = 10  # 10 min delays are OK


async def _check_availability(slot: dict) -> dict:
    """
    Ask the availability API whether a barber slot is free.
    Returns the full check result dict, or a synthetic 'available' result on error.
    """
    if not AVAILABILITY_URL:
        return {"available": True, "conflicts": []}

    try:
        payload = {
            "start": slot["from"],
            "end": slot["to"],
            "travel_buffer_before": TRAVEL_BUFFER_BEFORE,
            "travel_buffer_after": TRAVEL_BUFFER_AFTER,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{AVAILABILITY_URL}/check", json=payload)
            if resp.status_code == 200:
                return resp.json()
            log.warning("Availability API returned %d", resp.status_code)
    except Exception as exc:
        log.warning("Availability API unreachable: %s", exc)

    return {"available": True, "conflicts": []}


async def _is_slot_available(slot: dict) -> bool:
    """
    Check availability with commute tolerance.
    If the only conflicts are <= 10 min of overlap, allow it.
    """
    result = await _check_availability(slot)

    if result.get("available"):
        return True

    conflicts = result.get("conflicts", [])
    if not conflicts:
        return True

    for conflict in conflicts:
        try:
            c_start = datetime.fromisoformat(conflict["start"])
            c_end = datetime.fromisoformat(conflict["end"])
            req_start = datetime.fromisoformat(result.get("checked_start", slot["from"]))
            req_end = datetime.fromisoformat(result.get("checked_end", slot["to"]))

            overlap_start = max(c_start, req_start)
            overlap_end = min(c_end, req_end)
            overlap_minutes = max(0, (overlap_end - overlap_start).total_seconds() / 60)

            if overlap_minutes > COMMUTE_TOLERANCE_MINUTES:
                return False
        except Exception:
            return False

    log.info(
        "Slot %s %s has minor conflict (<=%dmin tolerance), allowing",
        slot.get("dateIso"), slot.get("timeFrom"), COMMUTE_TOLERANCE_MINUTES,
    )
    return True


async def _filter_slots_by_availability(slots: list[dict]) -> list[dict]:
    """Filter slots through the availability API."""
    if not AVAILABILITY_URL:
        return slots
    free = []
    for slot in slots:
        if await _is_slot_available(slot):
            free.append(slot)
    return free


# ---------------------------------------------------------------------------
# Calendar event creation (always creates, with cancel link)
# ---------------------------------------------------------------------------

def _create_calendar_event(calendar, slot: dict, booking_id: str):
    """Create a Google Calendar event with cancel link. Always called on success."""
    if not calendar:
        log.warning("No calendar client -- cannot create event for booking %s", booking_id)
        return
    try:
        start = datetime.fromisoformat(slot["from"])
        end = datetime.fromisoformat(slot["to"])
        description = (
            f"Automatisch gebucht via Barber-Booker.\n"
            f"\n"
            f"Barbier: Emina Koepplin (Meister)\n"
            f"Service: {SERVICE['name']}\n"
            f"Preis: CHF {SERVICE['price']:.2f}\n"
            f"\n"
            f"Booking-ID: {booking_id}\n"
            f"\n"
            f"Termin stornieren:\n"
            f"{CANCEL_LINK}\n"
        )
        calendar.create_event(
            summary="Haarschnitt - Pompadour",
            start=start,
            end=end,
            description=description,
            booking_id=booking_id,
        )
        log.info("Calendar event created for booking %s on %s", booking_id[:8], slot.get("dateIso"))
    except Exception as exc:
        log.warning("Calendar event creation failed: %s", exc)


# ---------------------------------------------------------------------------
# Single booking execution
# ---------------------------------------------------------------------------

async def _book_single_slot(
    slot: dict,
    calendar,
    dry_run: bool = False,
    label: str = "",
) -> Optional[dict]:
    """Book a single slot and create calendar event."""
    log.info("Booking %s: %s %s", label, slot.get("dateIso"), slot.get("timeFrom"))

    if dry_run:
        return {"dry_run": True, "slot": slot, "label": label}

    result = await book_slot(slot, CUSTOMER_DEFAULT)

    if not result.get("succeded"):
        log.error("Booking failed for %s: %s", label, result.get("error"))
        return None

    booking_id = result["id"]

    # Always create calendar event
    _create_calendar_event(calendar, slot, booking_id)

    # Persist to local history
    history = _load_history()
    history.append({
        "booking_id": booking_id,
        "date": slot.get("dateIso"),
        "time_from": slot.get("timeFrom"),
        "time_to": slot.get("timeTo"),
        "employee_id": EMPLOYEE_ID,
        "service": SERVICE["name"],
        "price": SERVICE["price"],
        "booked_at": datetime.now().isoformat(),
        "label": label,
    })
    _save_history(history)

    # Print receipt
    await _print_receipt(result, slot)

    return result


# ---------------------------------------------------------------------------
# Main booking routine
# ---------------------------------------------------------------------------

async def run_booking_cycle(
    force: bool = False,
    dry_run: bool = False,
    calendar=None,
) -> Optional[dict]:
    """
    Execute one booking cycle.

    Books up to TWO appointments:
    - 'early' slot: ~3 weeks after last attended appointment
    - 'late' slot:  ~4 weeks after last attended appointment

    Skips windows that already have bookings.
    Returns a summary dict.
    """
    # Sync cancelled slots from Barberly
    await _sync_cancelled_slots()
    cancelled_fps = _load_cancelled_slots()

    # Determine last attended appointment from Barberly API
    last_attended = await _fetch_last_attended_date()
    if not last_attended:
        log.warning("No attended appointments found. Using today as baseline.")
        last_attended = datetime.now()

    # Calculate target dates
    early_target = last_attended + timedelta(days=BOOKING_WINDOW_EARLY_DAYS)
    late_target = last_attended + timedelta(days=BOOKING_WINDOW_LATE_DAYS)

    log.info(
        "Last attended: %s | Early target: %s | Late target: %s",
        last_attended.strftime("%Y-%m-%d"),
        early_target.strftime("%Y-%m-%d"),
        late_target.strftime("%Y-%m-%d"),
    )

    # Check which windows already have bookings (from Barberly directly)
    needs_early = True
    needs_late = True

    try:
        upcoming_barberly = await fetch_booking_history(upcoming=True)
        for b in upcoming_barberly:
            if b.get("state") not in (STATE_CONFIRMED, STATE_COMPLETED):
                continue
            ts = b.get("timeSlot") or {}
            date_iso = ts.get("dateIso", "")
            if not date_iso:
                continue
            try:
                booking_date = datetime.fromisoformat(date_iso)
                days_after = (booking_date - last_attended).days
                if needs_early and (BOOKING_WINDOW_EARLY_DAYS - BOOKING_TOLERANCE_DAYS) <= days_after <= (BOOKING_WINDOW_EARLY_DAYS + BOOKING_TOLERANCE_DAYS):
                    needs_early = False
                    log.info("Early window already booked: %s (id=%s)", date_iso, b["id"][:8])
                if needs_late and (BOOKING_WINDOW_LATE_DAYS - BOOKING_TOLERANCE_DAYS) <= days_after <= (BOOKING_WINDOW_LATE_DAYS + BOOKING_TOLERANCE_DAYS):
                    needs_late = False
                    log.info("Late window already booked: %s (id=%s)", date_iso, b["id"][:8])
            except Exception:
                continue
    except Exception as exc:
        log.warning("Could not check upcoming Barberly bookings: %s", exc)

    if not needs_early and not needs_late and not force:
        log.info("Both booking windows covered. Nothing to do.")
        return {"message": "Both windows covered", "needs_early": False, "needs_late": False}

    # Collect slots for the relevant months
    now = datetime.now()
    months_to_scan = set()
    for target in [early_target, late_target]:
        months_to_scan.add((target.year, target.month))
        adj = target + timedelta(days=15)
        months_to_scan.add((adj.year, adj.month))
        prev = target - timedelta(days=15)
        if (prev.year, prev.month) >= (now.year, now.month):
            months_to_scan.add((prev.year, prev.month))

    all_slots: list[dict] = []
    for year, month in sorted(months_to_scan):
        if (year, month) < (now.year, now.month):
            continue
        try:
            slots = await fetch_slots(year, month)
            all_slots.extend(slots)
            log.info("Fetched %d slots for %d/%02d", len(slots), year, month)
        except Exception as exc:
            log.warning("Failed to fetch slots for %d/%02d: %s", year, month, exc)

    if not all_slots:
        log.warning("No available slots found at all.")
        return {"message": "No slots available", "needs_early": needs_early, "needs_late": needs_late}

    # Remove cancelled slot fingerprints
    before_filter = len(all_slots)
    all_slots = [s for s in all_slots if _slot_fingerprint(s) not in cancelled_fps]
    if len(all_slots) < before_filter:
        log.info("Filtered out %d previously-cancelled slots", before_filter - len(all_slots))

    # Filter by availability (calendar + commute + travel)
    all_slots = await _filter_slots_by_availability(all_slots)
    log.info("%d slots remaining after availability check", len(all_slots))

    if not all_slots:
        log.warning("All available slots conflict with calendar/commute.")
        return {"message": "All slots conflict", "needs_early": needs_early, "needs_late": needs_late}

    # Load day weights
    day_weights = _load_day_weights()

    results = {"bookings": [], "needs_early": needs_early, "needs_late": needs_late}
    booked_fingerprints: set[str] = set()  # prevent booking same slot twice in one cycle

    for window_label, target_date, needed in [
        ("early (~3wk)", early_target, needs_early),
        ("late (~4wk)", late_target, needs_late),
    ]:
        if not needed and not force:
            continue

        # Filter slots to the target window (+/- tolerance + extra days)
        # Also exclude any slot we already booked in this cycle
        window_slots = []
        for slot in all_slots:
            fp = _slot_fingerprint(slot)
            if fp in booked_fingerprints:
                continue
            try:
                dt = datetime.fromisoformat(slot["from"])
                days_off = abs((dt.date() - target_date.date()).days)
                if days_off <= BOOKING_TOLERANCE_DAYS + 4:
                    window_slots.append(slot)
            except Exception:
                continue

        if not window_slots:
            log.warning("No slots near %s window (%s)", window_label, target_date.strftime("%Y-%m-%d"))
            continue

        # Score and rank
        scored = []
        for slot in window_slots:
            bio = await _get_bio_score(datetime.fromisoformat(slot["from"]))
            s = _score_slot(slot, target_date, day_weights, bio)
            scored.append((s, slot))
        scored.sort(key=lambda x: x[0], reverse=True)

        best_score, best_slot = scored[0]
        log.info(
            "Best %s slot: %s %s (score=%.1f, %d candidates)",
            window_label,
            best_slot.get("dateIso"),
            best_slot.get("timeFrom"),
            best_score,
            len(scored),
        )

        booking_result = await _book_single_slot(
            best_slot, calendar, dry_run=dry_run, label=window_label,
        )
        if booking_result:
            booked_fingerprints.add(_slot_fingerprint(best_slot))
            results["bookings"].append({
                "label": window_label,
                "slot": best_slot,
                "score": best_score,
                "result": booking_result,
            })

    return results
