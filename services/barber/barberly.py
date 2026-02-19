"""
Barberly API client for Pompadour Barbershop.

All interaction with the external Barberly booking platform is encapsulated here.
"""

import httpx
import logging
from typing import Optional

log = logging.getLogger("barber.barberly")

BASE_URL = "https://bs-api-customers.azurewebsites.net/api"
ORIGIN = "https://pompadourbarbershop.barberly.app"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-CH,de;q=0.9",
    "Origin": ORIGIN,
    "Referer": f"{ORIGIN}/",
    "Content-Type": "application/json",
    "x-tenant": "",
}

# --- Static IDs -----------------------------------------------------------

LOCATION_ID = "bd00effc-bbbe-41e1-b5e8-c521a7f163a7"
EMPLOYEE_ID = "65c10e70-f413-4f65-b037-74e4c7ae5a5b"  # Emina Koepplin (Meister)
CUSTOMER_ID = "75e2b7b4-e14d-437d-a813-bb63f311771c"

SERVICE = {
    "id": "557c27b9-e397-41c4-9193-cb28c90774b8",
    "name": "The Signature Haarschnitt",
    "price": 78.0,
}

# Booking state codes from Barberly API
# Verified against the Barberly UI ("Abgesagt" badge = cancelled).
STATE_CONFIRMED = 1   # upcoming/active
STATE_CANCELLED = 2   # cancelled by customer or shop ("Abgesagt")
STATE_COMPLETED = 3   # attended (no badge in UI)

# Cancel link template
CANCEL_LINK = f"{ORIGIN}/bookings"


async def fetch_slots(year: int, month: int, employee_id: str = EMPLOYEE_ID) -> list[dict]:
    """Return all available time-slot dicts for *employee_id* in the given month."""
    url = f"{BASE_URL}/bookings/v2/location/{LOCATION_ID}/{year}/{month}/dates?supportsWeekOption=true"
    body = {
        "EmployeeId": employee_id,
        "ServiceIds": [SERVICE["id"]],
    }
    async with httpx.AsyncClient(headers=HEADERS, timeout=20) as client:
        resp = await client.post(url, json=body)
        resp.raise_for_status()

    weeks = resp.json()
    slots: list[dict] = []
    for week in weeks:
        for day in week:
            if day.get("enabled"):
                slots.extend(day.get("timeSlots", []))
    return slots


async def book_slot(
    slot: dict,
    customer: dict,
    employee_id: str = EMPLOYEE_ID,
) -> dict:
    """
    Book a single time-slot.

    Returns the API response dict.  On success ``resp["succeded"]`` is ``True``
    and ``resp["id"]`` contains the new booking ID.
    """
    payload = {
        "locationId": LOCATION_ID,
        "employeeId": employee_id,
        "serviceIds": [SERVICE["id"]],
        "services": [SERVICE],
        "price": SERVICE["price"],
        "customer": customer,
        "timeSlot": slot,
    }

    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
        resp = await client.post(f"{BASE_URL}/bookings/v2/save", json=payload)
        resp.raise_for_status()

    result = resp.json()
    if result.get("succeded"):
        log.info("Booking succeeded: id=%s date=%s %s", result["id"], slot.get("dateIso"), slot.get("timeFrom"))
    else:
        log.warning("Booking failed: %s", result.get("error"))
    return result


async def cancel_booking(booking_id: str) -> dict:
    """Cancel a booking by its ID.  Returns ``{"success": True/False, ...}``."""
    url = f"{BASE_URL}/bookings/{booking_id}/cancel"
    async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
        resp = await client.post(url)
        resp.raise_for_status()
    result = resp.json()
    log.info("Cancel booking %s: %s", booking_id, result)
    return result


async def get_stylists() -> list[dict]:
    """Return the list of available stylists."""
    async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
        resp = await client.get(f"{BASE_URL}/stylists")
        resp.raise_for_status()
    return resp.json()


async def get_services() -> list[dict]:
    """Return the list of available services."""
    async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
        resp = await client.get(f"{BASE_URL}/services")
        resp.raise_for_status()
    return resp.json()


async def fetch_booking_history(upcoming: bool = False) -> list[dict]:
    """
    Fetch all bookings for the customer from Barberly.

    Args:
        upcoming: True for future bookings, False for past bookings.

    Returns list of booking dicts with keys: id, timeSlot, state, employeeId, etc.
    State codes: 1=confirmed, 2=completed, 3=cancelled.
    """
    url = f"{BASE_URL}/bookings/customer/{CUSTOMER_ID}?upcoming={'true' if upcoming else 'false'}"
    async with httpx.AsyncClient(headers=HEADERS, timeout=20) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    data = resp.json()
    log.info("Fetched %d %s bookings from Barberly", len(data), "upcoming" if upcoming else "past")
    return data
