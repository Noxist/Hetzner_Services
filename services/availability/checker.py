"""
Core availability checker with urgency-based calendar filtering.

Uses the Google Calendar **Events API** (not FreeBusy) so that event
titles are available for per-event rules like ``title_suffix``.

Urgency levels (configurable via urgency_config.json):
  1 = Top priority  -- only the strongest-blocking calendars count.
  2 = Medium         -- more calendars are considered blocking.
  3 = Low            -- almost everything blocks; a real gap is needed.

Each calendar has a ``blocks_at`` value (1-3).  An event from that
calendar counts as a conflict when  blocks_at <= requested urgency.

Special rule ``title_suffix``:  If set for a calendar, only events
whose title ends with that suffix (as a standalone token, e.g. " X")
are treated as blocking.  Events without the suffix in that calendar
are always ignored regardless of urgency.
"""

import logging
import zoneinfo
from datetime import datetime, timedelta
from dataclasses import dataclass, field

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

log = logging.getLogger("availability.checker")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TIMEZONE = "Europe/Zurich"
TZ = zoneinfo.ZoneInfo(TIMEZONE)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Conflict:
    calendar: str
    event_title: str
    start: str
    end: str


@dataclass
class CheckResult:
    available: bool
    urgency: int
    requested_start: str
    requested_end: str
    checked_start: str      # includes buffers
    checked_end: str        # includes buffers
    travel_buffer_before: int
    travel_buffer_after: int
    conflicts: list[Conflict] = field(default_factory=list)
    overridden: list[Conflict] = field(default_factory=list)
    calendars_checked: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

class AvailabilityChecker:
    """
    Stateless availability checker backed by the Google Calendar Events API.

    The service account must have read access to every calendar listed.

    Args:
        credentials_file: Path to Google service-account JSON.
        calendar_config:  Mapping  calendar-display-name -> calendar-ID.
        urgency_rules:    Mapping  calendar-display-name -> rule dict
                          (``blocks_at``, optional ``title_suffix``).
        default_urgency:  Urgency used when the caller does not specify one.
    """

    def __init__(
        self,
        credentials_file: str,
        calendar_config: dict[str, str],
        urgency_rules: dict[str, dict],
        default_urgency: int = 2,
    ):
        self.calendar_config = calendar_config
        self.urgency_rules = urgency_rules
        self.default_urgency = default_urgency
        self.calendar_ids = list(calendar_config.values())

        creds = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
        self.service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        log.info(
            "AvailabilityChecker initialised  --  %d calendars, default urgency %d",
            len(calendar_config),
            default_urgency,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_events(
        self, calendar_id: str, time_min: datetime, time_max: datetime,
    ) -> list[dict]:
        """Fetch calendar events in [time_min, time_max)."""
        try:
            result = (
                self.service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=_iso(time_min),
                    timeMax=_iso(time_max),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                )
                .execute()
            )
            return result.get("items", [])
        except Exception as exc:
            log.error("Events.list failed for %s: %s", calendar_id, exc)
            return []

    def _event_blocks(
        self, calendar_name: str, event_summary: str, urgency: int,
    ) -> bool:
        """
        Decide whether a single event blocks at the given urgency.

        Returns True  => the event is a real conflict.
        Returns False => the event is overridden / ignored.
        """
        rule = self.urgency_rules.get(calendar_name)
        if rule is None:
            # Unknown calendar without a rule -- treat as blocking (safe).
            log.warning(
                "No urgency rule for calendar '%s' -- treating as always blocking",
                calendar_name,
            )
            return True

        blocks_at: int = rule.get("blocks_at", 1)
        title_suffix: str | None = rule.get("title_suffix")

        # If a title_suffix rule exists, only events whose title ends with
        # that suffix (as a standalone token) are ever considered blocking.
        if title_suffix is not None:
            if not event_summary.rstrip().endswith(title_suffix):
                return False

        return blocks_at <= urgency

    @staticmethod
    def _parse_event_times(event: dict) -> tuple[datetime, datetime]:
        """Extract tz-aware start / end from a Calendar event dict."""
        start_raw = event.get("start", {})
        end_raw = event.get("end", {})

        if "dateTime" in start_raw:
            start = datetime.fromisoformat(start_raw["dateTime"])
        else:
            # All-day event
            start = datetime.strptime(start_raw.get("date", ""), "%Y-%m-%d")
            start = start.replace(tzinfo=TZ)

        if "dateTime" in end_raw:
            end = datetime.fromisoformat(end_raw["dateTime"])
        else:
            end = datetime.strptime(end_raw.get("date", ""), "%Y-%m-%d")
            end = end.replace(tzinfo=TZ)

        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
        if end.tzinfo is None:
            end = end.replace(tzinfo=TZ)

        return start, end

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self,
        start: datetime,
        end: datetime,
        travel_buffer_before: int = 0,
        travel_buffer_after: int = 0,
        urgency: int | None = None,
    ) -> CheckResult:
        """
        Check whether ``[start, end)`` is free across all calendars,
        respecting the given urgency level.

        Args:
            start:  Appointment start (tz-aware or naive -> Europe/Zurich).
            end:    Appointment end.
            travel_buffer_before: Extra minutes to block before start.
            travel_buffer_after:  Extra minutes to block after end.
            urgency: 1-3 (None -> default_urgency from config).

        Returns a CheckResult with conflicts and overridden events.
        """
        if urgency is None:
            urgency = self.default_urgency

        checked_start = start - timedelta(minutes=travel_buffer_before)
        checked_end = end + timedelta(minutes=travel_buffer_after)

        conflicts: list[Conflict] = []
        overridden: list[Conflict] = []

        for cal_name, cal_id in self.calendar_config.items():
            events = self._fetch_events(cal_id, checked_start, checked_end)
            for event in events:
                # Skip events marked as "Free" (transparent) -- they
                # don't block time (e.g. Reclaim flex tasks).
                if event.get("transparency") == "transparent":
                    continue

                summary = event.get("summary", "")
                try:
                    ev_start, ev_end = self._parse_event_times(event)
                except Exception:
                    log.warning("Skipping unparseable event: %s", event.get("id"))
                    continue

                entry = Conflict(
                    calendar=cal_name,
                    event_title=summary,
                    start=_iso(ev_start),
                    end=_iso(ev_end),
                )

                if self._event_blocks(cal_name, summary, urgency):
                    conflicts.append(entry)
                else:
                    overridden.append(entry)

        if conflicts:
            log.debug(
                "Busy for %s -- %s  (%d conflicts, %d overridden, urgency %d)",
                _iso(start), _iso(end),
                len(conflicts), len(overridden), urgency,
            )

        return CheckResult(
            available=len(conflicts) == 0,
            urgency=urgency,
            requested_start=_iso(start),
            requested_end=_iso(end),
            checked_start=_iso(checked_start),
            checked_end=_iso(checked_end),
            travel_buffer_before=travel_buffer_before,
            travel_buffer_after=travel_buffer_after,
            conflicts=conflicts,
            overridden=overridden,
            calendars_checked=list(self.calendar_config.keys()),
        )

    def check_batch(
        self,
        slots: list[dict],
        travel_buffer_before: int = 0,
        travel_buffer_after: int = 0,
        urgency: int | None = None,
    ) -> list[CheckResult]:
        """
        Check multiple time slots at once.

        Each slot dict must have 'start' and 'end' as ISO-8601 strings.
        Per-slot 'travel_buffer_before', 'travel_buffer_after', and
        'urgency' override the top-level defaults.
        """
        results = []
        for slot in slots:
            s = datetime.fromisoformat(slot["start"])
            e = datetime.fromisoformat(slot["end"])
            buf_before = slot.get("travel_buffer_before", travel_buffer_before)
            buf_after = slot.get("travel_buffer_after", travel_buffer_after)
            slot_urgency = slot.get("urgency", urgency)
            results.append(self.check(s, e, buf_before, buf_after, slot_urgency))
        return results

    def free_windows(
        self,
        date: datetime,
        min_duration_minutes: int = 30,
        day_start_hour: int = 8,
        day_end_hour: int = 20,
        urgency: int | None = None,
    ) -> list[dict]:
        """
        Find all free windows on a given date, respecting urgency.

        Only events that actually block at the requested urgency level
        contribute to the busy intervals.
        """
        if urgency is None:
            urgency = self.default_urgency

        if date.tzinfo is None:
            date = date.replace(tzinfo=TZ)

        day_start = date.replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
        day_end = date.replace(hour=day_end_hour, minute=0, second=0, microsecond=0)

        busy_intervals: list[tuple[datetime, datetime]] = []

        for cal_name, cal_id in self.calendar_config.items():
            events = self._fetch_events(cal_id, day_start, day_end)
            for event in events:
                # Skip events marked as "Free" (transparent)
                if event.get("transparency") == "transparent":
                    continue
                summary = event.get("summary", "")
                if self._event_blocks(cal_name, summary, urgency):
                    try:
                        ev_start, ev_end = self._parse_event_times(event)
                    except Exception:
                        continue
                    busy_intervals.append((ev_start, ev_end))

        merged = _merge_intervals(busy_intervals)

        windows = []
        cursor = day_start
        for busy_start, busy_end in merged:
            if busy_start > cursor:
                gap_mins = (busy_start - cursor).total_seconds() / 60
                if gap_mins >= min_duration_minutes:
                    windows.append({
                        "start": _iso(cursor),
                        "end": _iso(busy_start),
                        "duration_minutes": int(gap_mins),
                    })
            cursor = max(cursor, busy_end)

        if cursor < day_end:
            gap_mins = (day_end - cursor).total_seconds() / 60
            if gap_mins >= min_duration_minutes:
                windows.append({
                    "start": _iso(cursor),
                    "end": _iso(day_end),
                    "duration_minutes": int(gap_mins),
                })

        return windows


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    """Ensure ISO-8601 with timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.isoformat()


def _merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    intervals.sort(key=lambda x: x[0])
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
