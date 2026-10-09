"""Tests for calsync.py. Standard library only; run with

    python3 -m unittest discover -s tests

Everything runs against a throwaway home folder and a fake helper, so no test
touches real calendars, config or state.
"""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

# Point calsync at a scratch home and a fixed timezone *before* importing it:
# it resolves its paths at import, and weekend detection uses local time.
HOME = tempfile.mkdtemp(prefix="calsync-test-")
os.environ["HOME"] = HOME
os.environ["TZ"] = "America/New_York"
time.tzset()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import calsync  # noqa: E402


def utc(dt: datetime) -> str:
    """Format a time the way the helper does: UTC with a trailing Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ParseIso(unittest.TestCase):
    def test_trailing_z(self):
        # The helper's own format. Python before 3.11 rejected it outright.
        self.assertEqual(
            calsync.parse_iso("2026-10-10T14:00:00Z"),
            datetime(2026, 10, 10, 14, tzinfo=timezone.utc),
        )

    def test_fractional_seconds_with_z(self):
        parsed = calsync.parse_iso("2026-10-10T14:00:00.500Z")
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_offset(self):
        parsed = calsync.parse_iso("2026-10-10T10:00:00-04:00")
        self.assertEqual(parsed, datetime(2026, 10, 10, 14, tzinfo=timezone.utc))

    def test_garbage(self):
        for value in ("", "not a date", None):
            self.assertIsNone(calsync.parse_iso(value))


class Weekend(unittest.TestCase):
    # 2026-10-10 is a Saturday; the tests run in America/New_York (UTC-4).

    def test_saturday_event(self):
        self.assertTrue(calsync.is_weekend_only(
            "2026-10-10T14:00:00Z", "2026-10-10T15:00:00Z", False))

    def test_weekday_event(self):
        self.assertFalse(calsync.is_weekend_only(
            "2026-10-07T14:00:00Z", "2026-10-07T15:00:00Z", False))

    def test_local_date_not_utc_date(self):
        # Friday 11pm–midnight local is Saturday in UTC, but it is a Friday
        # event, and ending exactly at midnight does not touch Saturday.
        self.assertFalse(calsync.is_weekend_only(
            "2026-10-10T03:00:00Z", "2026-10-10T04:00:00Z", False))

    def test_trip_spanning_a_weekday(self):
        self.assertFalse(calsync.is_weekend_only(
            "2026-10-09T14:00:00Z", "2026-10-12T14:00:00Z", False))

    def test_unparseable_is_not_weekend(self):
        self.assertFalse(calsync.is_weekend_only("bad", "bad", False))


class EventKey(unittest.TestCase):
    def test_one_off_keyed_by_id_alone(self):
        # So a rescheduled event is recognised as the same event.
        a = {"externalId": "X", "start": "2026-10-07T14:00:00Z"}
        b = {"externalId": "X", "start": "2026-10-08T14:00:00Z"}
        self.assertEqual(calsync.event_key(a), calsync.event_key(b))

    def test_recurring_keyed_by_id_and_start(self):
        a = {"externalId": "X", "start": "2026-10-07T14:00:00Z", "recurring": True}
        b = {"externalId": "X", "start": "2026-10-14T14:00:00Z", "recurring": True}
        self.assertNotEqual(calsync.event_key(a), calsync.event_key(b))


class ComposeTitle(unittest.TestCase):
    cfg = {"generic_title": "Busy", "title_prefix": "[Personal] "}

    def test_modes(self):
        self.assertEqual(calsync.compose_title("Dentist", "generic", self.cfg), "Busy")
        self.assertEqual(calsync.compose_title("Dentist", "prefix", self.cfg),
                         "[Personal] Dentist")
        self.assertEqual(calsync.compose_title("Dentist", "copy", self.cfg), "Dentist")

    def test_unknown_mode_falls_back_to_generic(self):
        self.assertEqual(calsync.compose_title("Dentist", "bogus", self.cfg), "Busy")


class FakeBridge:
    """Stands in for CalSyncBridge.app, recording every call."""

    def __init__(self):
        self.events = []
        self.calls = []

    def __call__(self, command, payload=None, timeout=180):
        self.calls.append((command, payload))
        if command == "events":
            end = datetime.now(timezone.utc) + timedelta(days=60)
            return {"ok": True, "events": list(self.events), "windowEnd": utc(end)}
        if command == "create":
            return {"ok": True, "eventId": "mirror-1", "calendarTitle": "Work"}
        return {"ok": True}


class Scan(unittest.TestCase):
    def setUp(self):
        for path in (calsync.DB_PATH, calsync.CONFIG_PATH):
            try:
                os.remove(path)
            except OSError:
                pass
        calsync._db = None
        self.fake = FakeBridge()
        self.real_bridge, calsync.bridge = calsync.bridge, self.fake
        self.cfg = calsync.load_config()
        self.cfg.update(source_calendar_ids=["home"], target_calendar_id="work",
                        baseline_done=True, skip_weekends=True)

    def tearDown(self):
        calsync.bridge = self.real_bridge
        if calsync._db is not None:
            calsync._db.close()
            calsync._db = None

    def next_weekday(self, weekday: int) -> datetime:
        """15:00 local on the next given weekday (Monday is 0), at least 2 days out."""
        day = datetime.now().astimezone() + timedelta(days=2)
        while day.weekday() != weekday:
            day += timedelta(days=1)
        return day.replace(hour=15, minute=0, second=0, microsecond=0)

    def event(self, start: datetime, ev_id="E1"):
        return {"id": ev_id, "externalId": ev_id, "title": "Dentist",
                "start": utc(start), "end": utc(start + timedelta(hours=1)),
                "allDay": False, "calendarId": "home", "calendarTitle": "Home"}

    def status(self, key):
        row = calsync.db().execute(
            "SELECT status, pending_action FROM events WHERE key=?", (key,)).fetchone()
        return (row["status"], row["pending_action"]) if row else None

    def test_new_weekday_event_is_queued(self):
        self.fake.events = [self.event(self.next_weekday(2))]
        counts = calsync.scan(self.cfg)
        self.assertEqual(counts["new"], 1)
        self.assertEqual(self.status("E1"), ("pending", "new"))

    def test_weekend_event_is_skipped(self):
        self.fake.events = [self.event(self.next_weekday(5))]
        counts = calsync.scan(self.cfg)
        self.assertEqual(counts["weekend"], 1)
        self.assertEqual(self.status("E1"), ("weekend", None))

    def test_deleted_personal_event_offers_removing_the_block(self):
        # The regression behind 1.1.1: with "Z" dates unreadable, this never fired.
        self.fake.events = [self.event(self.next_weekday(2))]
        calsync.scan(self.cfg)
        ok, _ = calsync.approve("E1", "Busy", self.cfg)
        self.assertTrue(ok)

        self.fake.events = []
        counts = calsync.scan(self.cfg)
        self.assertEqual(counts["removed"], 1)
        self.assertEqual(self.status("E1"), ("approved", "delete"))

    def test_undecided_event_that_vanishes_is_dropped(self):
        self.fake.events = [self.event(self.next_weekday(2))]
        calsync.scan(self.cfg)
        self.fake.events = []
        counts = calsync.scan(self.cfg)
        self.assertEqual(counts["vanished"], 1)
        self.assertIsNone(self.status("E1"))

    def test_approval_sends_time_and_title_only(self):
        self.fake.events = [self.event(self.next_weekday(2))]
        calsync.scan(self.cfg)
        calsync.approve("E1", "Busy", self.cfg)
        create = [p for c, p in self.fake.calls if c == "create"][0]
        self.assertEqual(set(create), {"calendarId", "title", "start", "end", "allDay"})
        self.assertEqual(create["title"], "Busy")


class WebUiChecks(unittest.TestCase):
    def handler(self, headers):
        h = calsync.Handler.__new__(calsync.Handler)
        h.server = SimpleNamespace(cfg={"token": "secret-token", "port": 8787})
        h.headers = headers
        return h

    def test_token_from_query_or_cookie(self):
        self.assertTrue(self.handler({}).authorised({"token": ["secret-token"]}))
        self.assertTrue(self.handler(
            {"Cookie": "a=1; calsync_token=secret-token"}).authorised({}))

    def test_wrong_or_embedded_token_refused(self):
        self.assertFalse(self.handler({}).authorised({"token": ["nope"]}))
        self.assertFalse(self.handler(
            {"Cookie": "xcalsync_token=secret-token"}).authorised({}))
        self.assertFalse(self.handler({"Cookie": "garbage;;=="}).authorised({}))

    def test_host_must_be_loopback_on_our_port(self):
        self.assertTrue(self.handler({"Host": "127.0.0.1:8787"}).trusted_request(False))
        self.assertTrue(self.handler({"Host": "localhost:8787"}).trusted_request(False))
        self.assertFalse(self.handler({"Host": "evil.example:8787"}).trusted_request(False))
        self.assertFalse(self.handler({"Host": "127.0.0.1:9999"}).trusted_request(False))

    def test_post_from_another_origin_refused(self):
        ok = {"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787"}
        other_port = {"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:3000"}
        other_site = {"Host": "127.0.0.1:8787", "Referer": "https://evil.example/x"}
        self.assertTrue(self.handler(ok).trusted_request(True))
        self.assertFalse(self.handler(other_port).trusted_request(True))
        self.assertFalse(self.handler(other_site).trusted_request(True))


if __name__ == "__main__":
    unittest.main()
