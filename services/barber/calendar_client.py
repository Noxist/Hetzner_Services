"""
Google Calendar integration for conflict detection and event creation.

Uses a service account to read/write the user's calendar.
The calendar must be shared with the service account's email address.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

log = logging.getLogger("barber.calendar")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TIMEZONE = "Europe/Zurich"


class CalendarClient:
    def __init__(self, credentials_file: str, calendar_id: str):
        self.calendar_id = calendar_id
        creds = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
        self.service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    def is_busy(self, start: datetime, end: datetime) -> bool:
        """
        Check whether there is any event overlapping [start, end).

        Uses the FreeBusy API for an accurate result across all event types.
        """
        body = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "timeZone": TIMEZONE,
            "items": [{"id": self.calendar_id}],
        }
        result = self.service.freebusy().query(body=body).execute()
        busy = result["calendars"][self.calendar_id]["busy"]
        if busy:
            log.debug("Busy slots found for %s - %s: %s", start, end, busy)
        return len(busy) > 0

    def create_event(
        self,
        summary: str,
        start: datetime,
        end: datetime,
        description: str = "",
        booking_id: Optional[str] = None,
    ) -> str:
        """
        Create a calendar event and return its Google event ID.
        """
        event_body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 60}]},
        }
        if booking_id:
            event_body["extendedProperties"] = {
                "private": {
                    "source": "barber-booker",
                    "booking_id": booking_id,
                }
            }

        created = self.service.events().insert(
            calendarId=self.calendar_id, body=event_body
        ).execute()
        log.info("Calendar event created: %s (%s)", created["id"], summary)
        return created["id"]

    def delete_event_by_booking_id(self, booking_id: str) -> bool:
        """Delete the calendar event associated with a Barberly booking ID."""
        events = (
            self.service.events()
            .list(
                calendarId=self.calendar_id,
                privateExtendedProperty=f"booking_id={booking_id}",
                singleEvents=True,
                maxResults=5,
            )
            .execute()
            .get("items", [])
        )
        for ev in events:
            self.service.events().delete(
                calendarId=self.calendar_id, eventId=ev["id"]
            ).execute()
            log.info("Deleted calendar event %s for booking %s", ev["id"], booking_id)
        return len(events) > 0

    def find_free_slots(
        self,
        candidates: list[dict],
        duration_minutes: int = 40,
    ) -> list[dict]:
        """
        Filter *candidates* (Barberly slot dicts) down to those where the
        calendar has no conflicts.
        """
        free: list[dict] = []
        for slot in candidates:
            start = datetime.fromisoformat(slot["from"])
            end = start + timedelta(minutes=duration_minutes)
            if not self.is_busy(start, end):
                free.append(slot)
        return free
