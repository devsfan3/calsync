#!/usr/bin/env python3
"""
CalSync — mirror approved personal-calendar events onto a work calendar as busy blocks.

Reads a set of iCloud calendars through EventKit, shows anything new on a
local web page, and — only for events you approve — creates a matching
"busy" block on the Microsoft 365 (Exchange) calendar.

Everything stays on this Mac: no network calls, no cloud credentials. Calendar
access goes through CalSyncBridge.app, a small signed helper that holds the
macOS Calendar permission.

Commands:
    calsync serve       run the web UI + background poller (used by launchd)
    calsync scan        do one scan now and print a summary
    calsync open        open the web UI in your browser
    calsync setup       open the calendar-picker page
    calsync status      show config and pending counts
    calsync test-notify send a test notification banner
    calsync install     install and start the launchd background agent
    calsync uninstall   stop and remove the launchd agent
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

HOME = os.path.expanduser("~")
ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "build", "CalSyncBridge.app")
CONFIG_DIR = os.path.join(HOME, ".config", "calsync")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_DIR = os.path.join(HOME, ".local", "state", "calsync")
DB_PATH = os.path.join(STATE_DIR, "state.db")
LOG_PATH = os.path.join(STATE_DIR, "calsync.log")
RUN_DIR = os.path.join(STATE_DIR, "run")
LABEL = "local.calsync.agent"
PLIST_PATH = os.path.join(HOME, "Library", "LaunchAgents", f"{LABEL}.plist")

MIRROR_MARKER = "[calsync]"

DEFAULT_CONFIG = {
    "source_calendar_ids": [],
    "target_calendar_id": "",
    "lookahead_days": 60,
    "poll_minutes": 20,
    "port": 8787,
    "token": "",
    "default_title_mode": "generic",  # generic | prefix | copy
    "generic_title": "Busy",
    "title_prefix": "[Personal] ",
    "include_all_day": True,
    "skip_weekends": True,
    "baseline_done": False,
    "notify": True,
}


def stdout_is_logfile() -> bool:
    """True when launchd has already pointed our stdout at the log file.

    Without this every line under the agent lands twice: once from print() via
    launchd's redirect, once from the explicit append below.
    """
    try:
        out = os.fstat(sys.stdout.fileno())
        target = os.stat(LOG_PATH)
    except (OSError, ValueError, AttributeError):
        return False
    return (out.st_dev, out.st_ino) == (target.st_dev, target.st_ino)


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    if stdout_is_logfile():
        return
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(24)
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)
    os.replace(tmp, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


# --------------------------------------------------------------------------
# EventKit bridge
# --------------------------------------------------------------------------

class BridgeError(RuntimeError):
    pass


_bridge_lock = threading.Lock()


def bridge(command: str, payload: dict | None = None, timeout: int = 180) -> dict:
    """Run one CalSyncBridge command and return its parsed JSON response.

    The helper is launched through LaunchServices rather than exec'd directly:
    that is what makes macOS attribute the Calendar permission to the helper
    itself instead of to whichever app happens to be our ancestor.
    """
    if not os.path.isdir(APP):
        raise BridgeError(f"{APP} is missing — run ./build.sh")

    os.makedirs(RUN_DIR, exist_ok=True)
    tag = secrets.token_hex(8)
    out_path = os.path.join(RUN_DIR, f"{tag}.out.json")
    in_path = os.path.join(RUN_DIR, f"{tag}.in.json")

    args = ["open", "-n", "-W", "-a", APP, "--args", command, "--out", out_path]
    if payload is not None:
        with open(in_path, "w") as fh:
            json.dump(payload, fh)
        args += ["--in", in_path]

    # One helper launch at a time: EventKit writes are not worth racing, and
    # serialising keeps the temp files and any permission prompt unambiguous.
    with _bridge_lock:
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            if not os.path.exists(out_path) and proc.returncode != 0:
                raise BridgeError(
                    f"could not launch the bridge: {proc.stderr.strip() or proc.returncode}"
                )
            # `open -W` returns once the helper exits, but the rename can lag a
            # touch on a busy machine.
            deadline = time.time() + 10
            while not os.path.exists(out_path) and time.time() < deadline:
                time.sleep(0.05)
            if not os.path.exists(out_path):
                raise BridgeError(f"the bridge produced no response for '{command}'")
            with open(out_path) as fh:
                result = json.load(fh)
        except subprocess.TimeoutExpired:
            raise BridgeError(f"the bridge timed out running '{command}'")
        finally:
            for path in (out_path, in_path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    if not result.get("ok"):
        raise BridgeError(result.get("error", "unknown bridge error"))
    return result


def notify(title: str, message: str) -> None:
    """Post a Notification Centre banner. Best effort — never fatal.

    The helper is preferred: it posts under CalSync's own bundle identity, so
    it gets its own entry in System Settings > Notifications and you can style
    or silence it independently. osascript posts as Script Editor instead,
    which works but cannot be configured separately — it is the fallback.
    """
    try:
        bridge("notify", {"title": title, "body": message}, timeout=45)
        return
    except Exception as exc:
        log(f"native notification failed ({exc}); falling back to osascript")

    script = (
        f'display notification {json.dumps(message)} '
        f'with title {json.dumps(title)}'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
    except Exception:
        pass


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    key             TEXT PRIMARY KEY,
    source_cal_id   TEXT NOT NULL,
    source_cal      TEXT NOT NULL DEFAULT '',
    source_event_id TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    start           TEXT NOT NULL DEFAULT '',
    end             TEXT NOT NULL DEFAULT '',
    all_day         INTEGER NOT NULL DEFAULT 0,
    location        TEXT NOT NULL DEFAULT '',
    signature       TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    pending_action  TEXT,
    mirror_event_id TEXT,
    mirror_title    TEXT,
    first_seen      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT '',
    last_seen       TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_pending ON events(pending_action);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

SCHEMA_VERSION = 1

_db_lock = threading.Lock()
_db: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _db
    if _db is None:
        os.makedirs(STATE_DIR, exist_ok=True)
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.executescript(SCHEMA)
        _db.commit()
    return _db


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def signature(ev: dict) -> str:
    raw = "|".join([
        ev.get("title", ""),
        ev.get("start", ""),
        ev.get("end", ""),
        str(bool(ev.get("allDay"))),
        ev.get("location", ""),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def event_key(ev: dict) -> str:
    """Stable identity for one event, or for one occurrence of a series.

    A repeating series shares a single identifier across every occurrence, so
    for those the start time has to be part of the key or the whole series
    collapses into one row.

    A one-off event is keyed by its identifier alone. That is what lets a
    rescheduled event be recognised as the same event — include the start and
    every time change would look like a brand-new event plus a vanished one.
    """
    base = ev.get("externalId") or ev.get("id") or ""
    if ev.get("recurring"):
        return f"{base}|{ev.get('start', '')}"
    return base


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def compose_title(source_title: str, mode: str, cfg: dict) -> str:
    source_title = source_title.strip() or "Personal event"
    if mode == "copy":
        return source_title
    if mode == "prefix":
        return f"{cfg['title_prefix']}{source_title}"
    return cfg["generic_title"]


WEEKEND_DAYS = {5, 6}  # datetime.weekday(): Monday is 0, so 5 = Sat, 6 = Sun
WEEKEND_NOTE = "falls on a weekend — skipped automatically"


def covered_dates(start_iso: str, end_iso: str, all_day: bool) -> list:
    """The local calendar dates an event actually touches."""
    start = parse_iso(start_iso)
    if start is None:
        return []
    start = start.astimezone()
    end = parse_iso(end_iso)
    end = start if end is None else end.astimezone()
    if end < start:
        end = start
    # A timed event ending exactly at midnight belongs to the day before it,
    # otherwise a Sunday 8pm–midnight event would look like it touches Monday.
    if (not all_day and end.date() > start.date()
            and (end.hour, end.minute, end.second) == (0, 0, 0)):
        end -= timedelta(seconds=1)

    days, cursor = [], start.date()
    while cursor <= end.date():
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def is_weekend_only(start_iso: str, end_iso: str, all_day: bool) -> bool:
    """True when an event falls entirely on a Saturday and/or Sunday.

    An event that also touches a weekday — a Friday-to-Monday trip, say — still
    costs work time, so it is not treated as a weekend event and you still get
    asked about it.
    """
    days = covered_dates(start_iso, end_iso, all_day)
    return bool(days) and all(d.weekday() in WEEKEND_DAYS for d in days)


def row_is_weekend(row) -> bool:
    return is_weekend_only(row["start"], row["end"], bool(row["all_day"]))


def schema_version(conn) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else 0


def migrate_keys(conn, events: list) -> int:
    """Move one-off events from the old '<uid>|<start>' key to plain '<uid>'.

    The original scheme keyed every event by identifier *and* start time, which
    made a rescheduled event look like a new event plus a vanished one. Rows
    written under the old scheme move across here keeping whatever decision you
    had already made, and any duplicate a mis-keyed scan created is dropped.

    Only events inside the current window can be migrated, which is all of them
    in practice — anything older has already passed and no longer matters.
    """
    moved = 0
    for ev in events:
        new_key = event_key(ev)
        base = ev.get("externalId") or ev.get("id") or ""
        old_key = f"{base}|{ev.get('start', '')}"
        if new_key == old_key:
            continue  # a recurring occurrence: same key under both schemes
        old = conn.execute("SELECT key FROM events WHERE key=?", (old_key,)).fetchone()
        if old is None:
            continue
        # The old row holds your decision, so it wins over any duplicate. Clear
        # a 'delete' prompt raised by the mis-keyed scan — it was never real.
        conn.execute("DELETE FROM events WHERE key=?", (new_key,))
        conn.execute(
            "UPDATE events SET key=?, pending_action=CASE WHEN pending_action='delete'"
            " THEN NULL ELSE pending_action END WHERE key=?",
            (new_key, old_key),
        )
        moved += 1
    return moved


def scan(cfg: dict) -> dict:
    """Pull the source calendars and reconcile them against stored state."""
    if not cfg["source_calendar_ids"]:
        return {"error": "no source calendars configured", "new": 0, "changed": 0, "removed": 0}
    if not cfg["target_calendar_id"]:
        return {"error": "no target calendar configured", "new": 0, "changed": 0, "removed": 0}

    result = bridge("events", {
        "calendarIds": cfg["source_calendar_ids"],
        "days": int(cfg["lookahead_days"]),
    })
    events = result["events"]
    window_end = result["windowEnd"]
    stamp = now_iso()
    # last_seen doubles as "was this row touched by *this* scan", so it needs to
    # be unique per scan. A second-precision timestamp is not: two scans inside
    # the same second would look identical and nothing would be detected as
    # stale or vanished.
    seen = f"{stamp}#{secrets.token_hex(4)}"

    baselining = not cfg.get("baseline_done")
    counts = {"new": 0, "changed": 0, "removed": 0, "baselined": 0,
              "weekend": 0, "vanished": 0}
    conn = db()

    with _db_lock:
        if schema_version(conn) < SCHEMA_VERSION:
            moved = migrate_keys(conn, events)
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
            log(f"migrated {moved} events to the current key scheme")

        seen_keys = set()
        for ev in events:
            if ev.get("status") == "canceled":
                continue
            if ev.get("allDay") and not cfg.get("include_all_day", True):
                continue

            key = event_key(ev)
            seen_keys.add(key)
            sig = signature(ev)
            row = conn.execute("SELECT * FROM events WHERE key = ?", (key,)).fetchone()

            fields = (
                ev.get("calendarId", ""), ev.get("calendarTitle", ""), ev.get("id", ""),
                ev.get("title", ""), ev.get("start", ""), ev.get("end", ""),
                1 if ev.get("allDay") else 0, ev.get("location", ""), sig, seen,
            )

            weekend = cfg.get("skip_weekends", True) and is_weekend_only(
                ev.get("start", ""), ev.get("end", ""), bool(ev.get("allDay"))
            )

            if row is None:
                if baselining:
                    status, action, note, bucket = "baseline", None, "", "baselined"
                elif weekend:
                    status, action, note, bucket = "weekend", None, WEEKEND_NOTE, "weekend"
                else:
                    status, action, note, bucket = "pending", "new", "", "new"
                conn.execute(
                    "INSERT INTO events (key, source_cal_id, source_cal, source_event_id,"
                    " title, start, end, all_day, location, signature, last_seen,"
                    " status, pending_action, note, first_seen, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key,) + fields + (status, action, note, stamp, stamp),
                )
                counts[bucket] += 1
                continue

            conn.execute(
                "UPDATE events SET source_cal_id=?, source_cal=?, source_event_id=?,"
                " title=?, start=?, end=?, all_day=?, location=?, signature=?, last_seen=?"
                " WHERE key=?",
                fields + (key,),
            )

            if row["signature"] == sig:
                continue

            # The underlying event moved or was retitled.
            counts["changed"] += 1
            if row["status"] == "approved" and row["mirror_event_id"]:
                apply_update(conn, row, ev, cfg, stamp)
            elif row["status"] in ("pending", "weekend"):
                # Re-file in whichever direction the move went: onto the weekend
                # and it drops out of the queue, off the weekend and it comes back.
                if weekend:
                    conn.execute(
                        "UPDATE events SET status='weekend', pending_action=NULL, note=?,"
                        " updated_at=? WHERE key=?",
                        (WEEKEND_NOTE, stamp, key),
                    )
                else:
                    conn.execute(
                        "UPDATE events SET status='pending', pending_action='new', note='',"
                        " updated_at=? WHERE key=?",
                        (stamp, key),
                    )
            # 'skipped' and 'baseline' rows stay quiet — you already passed on them.

        # Anything previously mirrored that has vanished from the window needs a
        # decision. Restricted to future events inside the window so that the
        # window rolling forward past an old event is never read as a deletion.
        # The bounds are compared as datetimes, not strings: stored timestamps
        # carry local UTC offsets, so lexical ordering would be wrong.
        candidates = conn.execute(
            "SELECT * FROM events WHERE status='approved' AND mirror_event_id IS NOT NULL"
            " AND mirror_event_id != '' AND last_seen != ?"
            " AND (pending_action IS NULL OR pending_action != 'delete')",
            (seen,),
        ).fetchall()
        horizon = parse_iso(window_end)
        floor = datetime.now(timezone.utc)
        stale = []
        for row in candidates:
            start = parse_iso(row["start"])
            if start is None or start.tzinfo is None:
                continue
            if floor < start < horizon:
                stale.append(row)

        for row in stale:
            conn.execute(
                "UPDATE events SET pending_action='delete', updated_at=? WHERE key=?",
                (stamp, row["key"]),
            )
            counts["removed"] += 1

        # Undecided rows whose source event was deleted before you got to it.
        # Nothing was ever written to the work calendar, so they can just go —
        # leaving them would show ghosts in the queue forever.
        ghosts = conn.execute(
            "SELECT key, start FROM events WHERE status IN ('pending','weekend')"
            " AND last_seen != ?",
            (seen,),
        ).fetchall()
        for row in ghosts:
            start = parse_iso(row["start"])
            if start is None or start.tzinfo is None:
                continue
            if floor < start < horizon:
                conn.execute("DELETE FROM events WHERE key=?", (row["key"],))
                counts["vanished"] += 1

        conn.commit()

    if baselining:
        cfg["baseline_done"] = True
        save_config(cfg)
        log(f"baseline: recorded {counts['baselined']} existing events without prompting")

    log(f"scan: {counts['new']} new, {counts['changed']} changed, "
        f"{counts['removed']} removed, {counts['weekend']} weekend, "
        f"{counts['vanished']} vanished")
    return counts


def apply_weekend_setting(enabled: bool) -> int:
    """Re-file stored events after the weekend rule is switched on or off.

    Only touches rows that have not been decided yet — an event you approved or
    skipped by hand keeps whatever you chose.
    """
    conn = db()
    stamp = now_iso()
    changed = 0
    with _db_lock:
        if enabled:
            for row in conn.execute("SELECT * FROM events WHERE status='pending'").fetchall():
                if row_is_weekend(row):
                    conn.execute(
                        "UPDATE events SET status='weekend', pending_action=NULL, note=?,"
                        " updated_at=? WHERE key=?",
                        (WEEKEND_NOTE, stamp, row["key"]),
                    )
                    changed += 1
        else:
            for row in conn.execute("SELECT * FROM events WHERE status='weekend'").fetchall():
                conn.execute(
                    "UPDATE events SET status='pending', pending_action='new', note='',"
                    " updated_at=? WHERE key=?",
                    (stamp, row["key"]),
                )
                changed += 1
        conn.commit()
    return changed


def apply_update(conn, row, ev: dict, cfg: dict, stamp: str) -> None:
    """Push a time change through to an already-approved mirror.

    Times always follow the source. The title only follows it when the mirror
    is still using the copied title — a title you typed yourself is yours.
    """
    payload = {
        "eventId": row["mirror_event_id"],
        "start": ev.get("start"),
        "end": ev.get("end"),
        "allDay": bool(ev.get("allDay")),
    }
    if row["mirror_title"] == row["title"]:
        payload["title"] = ev.get("title", "")

    try:
        res = bridge("update", payload)
    except BridgeError as exc:
        log(f"update failed for {row['key']}: {exc}")
        conn.execute(
            "UPDATE events SET note=?, updated_at=? WHERE key=?",
            (f"update failed: {exc}", stamp, row["key"]),
        )
        return

    if res.get("missing"):
        # The work block was deleted by hand; treat the pairing as broken.
        conn.execute(
            "UPDATE events SET mirror_event_id='', status='pending', pending_action='new',"
            " note='the work block was deleted outside CalSync', updated_at=? WHERE key=?",
            (stamp, row["key"]),
        )
        return

    new_title = payload.get("title", row["mirror_title"])
    conn.execute(
        "UPDATE events SET mirror_title=?, note='time updated to match the personal event',"
        " updated_at=? WHERE key=?",
        (new_title, stamp, row["key"]),
    )
    log(f"updated mirror for '{row['title']}' -> {ev.get('start')}")


# --------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------

def approve(key: str, title: str, cfg: dict) -> tuple[bool, str]:
    conn = db()
    with _db_lock:
        row = conn.execute("SELECT * FROM events WHERE key=?", (key,)).fetchone()
        if row is None:
            return False, "that event is no longer tracked"
        if row["status"] == "approved" and row["mirror_event_id"]:
            return False, "already on your work calendar"

        payload = {
            "calendarId": cfg["target_calendar_id"],
            "title": title,
            "start": row["start"],
            "end": row["end"],
            "allDay": bool(row["all_day"]),
            "notes": f"{MIRROR_MARKER} mirrors “{row['title']}” from {row['source_cal']}.",
        }

    try:
        res = bridge("create", payload)
    except BridgeError as exc:
        return False, str(exc)

    with _db_lock:
        conn.execute(
            "UPDATE events SET status='approved', pending_action=NULL, mirror_event_id=?,"
            " mirror_title=?, note='', updated_at=? WHERE key=?",
            (res.get("eventId", ""), title, now_iso(), key),
        )
        conn.commit()
    log(f"approved '{row['title']}' -> '{title}' on {res.get('calendarTitle', 'work')}")
    return True, f"blocked as “{title}”"


def skip(key: str) -> tuple[bool, str]:
    conn = db()
    with _db_lock:
        conn.execute(
            "UPDATE events SET status='skipped', pending_action=NULL, updated_at=? WHERE key=?",
            (now_iso(), key),
        )
        conn.commit()
    return True, "skipped — you will not be asked again"


def remove_mirror(key: str) -> tuple[bool, str]:
    conn = db()
    with _db_lock:
        row = conn.execute("SELECT * FROM events WHERE key=?", (key,)).fetchone()
        if row is None:
            return False, "that event is no longer tracked"
        mirror_id = row["mirror_event_id"]

    if mirror_id:
        try:
            bridge("delete", {"eventId": mirror_id})
        except BridgeError as exc:
            return False, str(exc)

    with _db_lock:
        conn.execute(
            "UPDATE events SET status='removed', pending_action=NULL, mirror_event_id='',"
            " note='work block removed', updated_at=? WHERE key=?",
            (now_iso(), key),
        )
        conn.commit()
    log(f"removed mirror for '{row['title']}'")
    return True, "work block removed"


def keep_mirror(key: str) -> tuple[bool, str]:
    conn = db()
    with _db_lock:
        conn.execute(
            "UPDATE events SET pending_action=NULL, note='kept after the personal event"
            " disappeared', updated_at=? WHERE key=?",
            (now_iso(), key),
        )
        conn.commit()
    return True, "kept on your work calendar"


def pending_rows() -> list[sqlite3.Row]:
    conn = db()
    with _db_lock:
        return conn.execute(
            "SELECT * FROM events WHERE pending_action IS NOT NULL ORDER BY start"
        ).fetchall()


def weekend_rows(limit: int = 100) -> list[sqlite3.Row]:
    conn = db()
    with _db_lock:
        return conn.execute(
            "SELECT * FROM events WHERE status='weekend' ORDER BY start LIMIT ?", (limit,)
        ).fetchall()


def weekend_count() -> int:
    conn = db()
    with _db_lock:
        return conn.execute(
            "SELECT COUNT(*) c FROM events WHERE status='weekend'"
        ).fetchone()["c"]


def recent_rows(limit: int = 40) -> list[sqlite3.Row]:
    conn = db()
    with _db_lock:
        return conn.execute(
            "SELECT * FROM events WHERE status IN ('approved','skipped','removed')"
            " ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


# --------------------------------------------------------------------------
# Presentation helpers
# --------------------------------------------------------------------------

def parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def friendly_when(row) -> str:
    start = parse_iso(row["start"])
    end = parse_iso(row["end"])
    if start is None:
        return row["start"]
    start = start.astimezone()
    day = start.strftime("%a %-d %b %Y")
    if row["all_day"]:
        return f"{day} · all day"
    if end is None:
        return f"{day} · {start.strftime('%-I:%M %p')}"
    end = end.astimezone()
    same_day = start.date() == end.date()
    tail = end.strftime("%-I:%M %p") if same_day else end.strftime("%a %-d %b, %-I:%M %p")
    return f"{day} · {start.strftime('%-I:%M %p')} – {tail}"


def relative_day(row) -> str:
    start = parse_iso(row["start"])
    if start is None:
        return ""
    delta = (start.astimezone().date() - datetime.now().astimezone().date()).days
    if delta < 0:
        return "past"
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta < 7:
        return f"in {delta} days"
    if delta < 14:
        return "next week"
    return f"in {delta // 7} weeks"


# --------------------------------------------------------------------------
# Web UI
# --------------------------------------------------------------------------

STYLE = """
:root{
  --bg:#f6f6f4; --panel:#fff; --ink:#1a1a18; --muted:#6b6b63; --line:#e3e3dd;
  --accent:#2f6f4f; --accent-ink:#fff; --warn:#8a4b2a; --warn-bg:#fbf1e9;
  --chip:#eeeee8; --danger:#8f3a3a;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#141414; --panel:#1d1d1c; --ink:#ececea; --muted:#9a9a92; --line:#2f2f2d;
    --accent:#66b088; --accent-ink:#10231a; --warn:#e0a878; --warn-bg:#2a2118;
    --chip:#2a2a28; --danger:#d98080;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 ui-sans-serif,-apple-system,"SF Pro Text",Segoe UI,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:28px 20px 80px}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:20px;margin:0;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px}
nav{display:flex;gap:14px;margin:18px 0 22px;font-size:13px;flex-wrap:wrap}
nav a{color:var(--muted);text-decoration:none;padding-bottom:3px;border-bottom:2px solid transparent}
nav a.on{color:var(--ink);border-color:var(--accent)}
nav a:hover{color:var(--ink)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:16px 18px;margin-bottom:12px}
.card.del{border-color:var(--warn);background:var(--warn-bg)}
.etitle{font-weight:600;font-size:16px;margin:0 0 2px}
.meta{color:var(--muted);font-size:13px}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:1px 9px;
  font-size:11px;color:var(--muted);margin-left:6px;vertical-align:1px}
.opts{margin:14px 0 12px;display:flex;flex-direction:column;gap:7px}
.opt{display:flex;align-items:center;gap:9px;font-size:14px}
.opt input[type=radio]{accent-color:var(--accent);margin:0}
.opt label{cursor:pointer}
input[type=text]{font:inherit;padding:5px 9px;border:1px solid var(--line);border-radius:6px;
  background:var(--bg);color:var(--ink);min-width:230px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
button{font:inherit;font-weight:500;padding:7px 15px;border-radius:7px;border:1px solid var(--line);
  background:var(--panel);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--muted)}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
button:disabled{background:var(--chip);color:var(--ink);cursor:default;border-color:var(--line)}
button.danger{color:var(--danger)}
.empty{text-align:center;color:var(--muted);padding:56px 20px;
  border:1px dashed var(--line);border-radius:10px}
.empty .big{font-size:32px;margin-bottom:10px}
.flash{background:var(--accent);color:var(--accent-ink);padding:9px 14px;border-radius:8px;
  margin-bottom:16px;font-size:14px}
.flash.bad{background:var(--danger);color:#fff}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:500;padding:7px 8px;border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
.st{font-size:11px;padding:1px 8px;border-radius:999px;background:var(--chip);color:var(--muted)}
.scroll{overflow-x:auto}
fieldset{border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:0 0 16px;
  background:var(--panel)}
legend{padding:0 6px;font-weight:600;font-size:14px}
.cals{max-height:340px;overflow-y:auto;display:flex;flex-direction:column;gap:2px}
.cal{display:flex;align-items:center;gap:9px;padding:3px 4px;border-radius:5px;font-size:14px}
.cal:hover{background:var(--chip)}
.cal input{accent-color:var(--accent);margin:0}
.cal .src{color:var(--muted);font-size:12px;margin-left:auto}
.note{color:var(--muted);font-size:12px;margin-top:8px}
.banner{background:var(--warn-bg);border:1px solid var(--warn);color:var(--warn);
  padding:11px 14px;border-radius:8px;margin-bottom:16px;font-size:13px}
"""

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CalSync</title><style>{style}</style></head><body><div class="wrap">
<header><h1>CalSync</h1><span class="sub">{sub}</span></header>
<nav>{nav}</nav>{flash}{body}</div></body></html>"""


def esc(text) -> str:
    return html.escape(str(text or ""))


class Handler(BaseHTTPRequestHandler):
    server_version = "CalSync"

    # Keep the console clean; real events go through log().
    def log_message(self, fmt, *args):
        pass

    # -- helpers ----------------------------------------------------------

    @property
    def cfg(self) -> dict:
        return self.server.cfg

    def authorised(self, query: dict) -> bool:
        token = self.cfg["token"]
        if (query.get("token") or [None])[0] == token:
            return True
        cookie = self.headers.get("Cookie", "")
        return f"calsync_token={token}" in cookie

    def send_html(self, body: str, code: int = 200, set_token: bool = False) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if set_token:
            self.send_header(
                "Set-Cookie",
                f"calsync_token={self.cfg['token']}; Path=/; SameSite=Strict; Max-Age=31536000",
            )
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, target: str) -> None:
        self.send_response(303)
        self.send_header("Location", target)
        self.end_headers()

    def render(self, page: str, body: str, flash: str = "", bad: bool = False) -> str:
        cfg = self.cfg
        n = len(pending_rows())
        tabs = [("/", "Pending" + (f" ({n})" if n else "")), ("/history", "History"),
                ("/setup", "Setup")]
        nav = "".join(
            f'<a href="{href}" class="{"on" if href == page else ""}">{esc(label)}</a>'
            for href, label in tabs
        )
        flash_html = (
            f'<div class="flash{" bad" if bad else ""}">{esc(flash)}</div>' if flash else ""
        )
        sub = f"every {cfg['poll_minutes']} min · {cfg['lookahead_days']} days ahead"
        return PAGE.format(style=STYLE, sub=esc(sub), nav=nav, flash=flash_html, body=body)

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self.authorised(query):
            self.send_html("<h1>403</h1><p>Missing or bad token.</p>", 403)
            return
        fresh = (query.get("token") or [None])[0] is not None
        flash = (query.get("msg") or [""])[0]
        bad = (query.get("err") or [""])[0] == "1"

        if parsed.path == "/":
            body = self.pending_body()
        elif parsed.path == "/history":
            body = self.history_body((query.get("weekend") or [""])[0] == "1")
        elif parsed.path == "/setup":
            body = self.setup_body()
        elif parsed.path == "/scan":
            try:
                counts = scan(self.cfg)
                msg = (f"Scan done — {counts.get('new', 0)} new, "
                       f"{counts.get('changed', 0)} changed, {counts.get('removed', 0)} removed, "
                       f"{counts.get('weekend', 0)} weekend")
                err = "1" if counts.get("error") else "0"
                if counts.get("error"):
                    msg = counts["error"]
            except BridgeError as exc:
                msg, err = str(exc), "1"
            self.redirect(f"/?msg={quote(msg)}&err={err}")
            return
        else:
            self.send_html("<h1>404</h1>", 404)
            return

        self.send_html(self.render(parsed.path, body, flash, bad), set_token=fresh)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self.authorised(parse_qs(parsed.query)):
            self.send_html("<h1>403</h1>", 403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode())

        def field(name, default=""):
            return (form.get(name) or [default])[0]

        if parsed.path == "/action":
            key = field("key")
            action = field("action")
            if action == "approve":
                mode = field("mode", "generic")
                row = next((r for r in pending_rows() if r["key"] == key), None)
                source_title = row["title"] if row else ""
                if mode == "custom":
                    title = field("custom").strip() or self.cfg["generic_title"]
                else:
                    title = compose_title(source_title, mode, self.cfg)
                ok, msg = approve(key, title, self.cfg)
            elif action == "skip":
                ok, msg = skip(key)
            elif action == "remove":
                ok, msg = remove_mirror(key)
            elif action == "keep":
                ok, msg = keep_mirror(key)
            else:
                ok, msg = False, "unknown action"
            self.redirect(f"/?msg={quote(msg)}&err={'0' if ok else '1'}")
            return

        if parsed.path == "/setup":
            cfg = self.cfg
            cfg["source_calendar_ids"] = form.get("sources", [])
            cfg["target_calendar_id"] = field("target")
            cfg["lookahead_days"] = max(1, min(365, int(field("days", "60") or 60)))
            cfg["poll_minutes"] = max(1, min(1440, int(field("poll", "20") or 20)))
            cfg["default_title_mode"] = field("mode", "generic")
            cfg["generic_title"] = field("generic", "Busy").strip() or "Busy"
            cfg["include_all_day"] = field("allday") == "on"
            cfg["notify"] = field("notify") == "on"

            was_skipping = cfg.get("skip_weekends", True)
            cfg["skip_weekends"] = field("weekends") == "on"
            save_config(cfg)

            msg = "Settings saved"
            if cfg["skip_weekends"] != was_skipping:
                moved = apply_weekend_setting(cfg["skip_weekends"])
                if moved:
                    msg += (f" — {moved} weekend event{'s' if moved != 1 else ''} "
                            + ("skipped" if cfg["skip_weekends"] else "put back in the queue"))
            self.redirect(f"/setup?msg={quote(msg)}")
            return

        if parsed.path == "/rebaseline":
            conn = db()
            with _db_lock:
                conn.execute(
                    "UPDATE events SET status='pending', pending_action='new'"
                    " WHERE status='baseline'"
                )
                conn.commit()
            self.redirect("/?msg=Existing+events+queued+for+review")
            return

        self.send_html("<h1>404</h1>", 404)

    # -- views ------------------------------------------------------------

    def pending_body(self) -> str:
        cfg = self.cfg
        parts = []

        if not cfg["source_calendar_ids"] or not cfg["target_calendar_id"]:
            parts.append(
                '<div class="banner">CalSync is not configured yet — '
                'choose your calendars on the <a href="/setup">Setup</a> tab.</div>'
            )

        conn = db()
        with _db_lock:
            baselined = conn.execute(
                "SELECT COUNT(*) c FROM events WHERE status='baseline'"
            ).fetchone()["c"]
        if baselined:
            parts.append(
                f'<div class="banner">{baselined} events already on your personal calendar '
                'were recorded as a starting point and will not be offered. '
                '<form method="post" action="/rebaseline" style="display:inline">'
                '<button type="submit">Review them anyway</button></form></div>'
            )

        rows = pending_rows()
        if not rows:
            parts.append(
                '<div class="empty"><div class="big">✓</div>'
                '<div>Nothing waiting for you.</div>'
                f'<div class="sub" style="margin-top:8px">Checked every {cfg["poll_minutes"]}'
                ' minutes.</div></div>'
            )
        else:
            for row in rows:
                parts.append(
                    self.delete_card(row) if row["pending_action"] == "delete"
                    else self.new_card(row)
                )

        parts.append(
            '<div class="row" style="margin-top:20px">'
            '<a href="/scan"><button>Check now</button></a></div>'
        )

        skipped = weekend_count() if cfg.get("skip_weekends", True) else 0
        if skipped:
            parts.append(
                f'<div class="note" style="margin-top:14px">{skipped} weekend '
                f'event{"s" if skipped != 1 else ""} skipped automatically — '
                '<a href="/history?weekend=1">see them</a>.</div>'
            )
        return "".join(parts)

    def new_card(self, row) -> str:
        cfg = self.cfg
        source_title = row["title"] or "Personal event"
        mode = cfg["default_title_mode"]
        options = [
            ("generic", esc(cfg["generic_title"])),
            ("prefix", esc(cfg["title_prefix"] + source_title)),
            ("copy", esc(source_title)),
        ]
        opts = "".join(
            f'<div class="opt"><input type="radio" name="mode" id="{esc(row["key"])}-{value}"'
            f' value="{value}"{" checked" if value == mode else ""}>'
            f'<label for="{esc(row["key"])}-{value}">{label}</label></div>'
            for value, label in options
        )
        loc = f'<div class="meta">{esc(row["location"])}</div>' if row["location"] else ""
        note = f'<div class="meta">{esc(row["note"])}</div>' if row["note"] else ""
        return f"""
<div class="card"><form method="post" action="/action">
<input type="hidden" name="key" value="{esc(row['key'])}">
<p class="etitle">{esc(source_title)}<span class="chip">{esc(relative_day(row))}</span></p>
<div class="meta">{esc(friendly_when(row))} · {esc(row['source_cal'])}</div>{loc}{note}
<div class="opts">{opts}
<div class="opt"><input type="radio" name="mode" id="{esc(row['key'])}-custom" value="custom">
<label for="{esc(row['key'])}-custom"><input type="text" name="custom"
 placeholder="Something else…" onfocus="this.closest('.opt').querySelector('input[type=radio]').checked=true">
</label></div></div>
<div class="row">
<button class="primary" name="action" value="approve" type="submit">Block this time</button>
<button name="action" value="skip" type="submit">Skip</button></div>
</form></div>"""

    def delete_card(self, row) -> str:
        return f"""
<div class="card del"><form method="post" action="/action">
<input type="hidden" name="key" value="{esc(row['key'])}">
<p class="etitle">{esc(row['title'] or 'Personal event')}
<span class="chip">no longer on your personal calendar</span></p>
<div class="meta">{esc(friendly_when(row))} · was blocked as
 “{esc(row['mirror_title'])}”</div>
<div class="row" style="margin-top:12px">
<button class="primary" name="action" value="remove" type="submit">Remove the work block</button>
<button name="action" value="keep" type="submit">Keep it</button></div>
</form></div>"""

    def history_body(self, weekend: bool = False) -> str:
        # Weekend skips live on their own tab: on a family calendar they would
        # otherwise bury the decisions you actually made.
        toggle = (
            '<div class="row" style="margin-bottom:12px">'
            f'<a href="/history"><button{"" if weekend else " disabled"}>Your decisions'
            '</button></a>'
            f'<a href="/history?weekend=1"><button{" disabled" if weekend else ""}>'
            f'Weekend skips ({weekend_count()})</button></a></div>'
        )
        rows = weekend_rows() if weekend else recent_rows()
        if not rows:
            empty = "No weekend events skipped yet." if weekend else "No decisions yet."
            return toggle + f'<div class="empty"><div>{empty}</div></div>'
        cells = []
        for row in rows:
            cells.append(
                f"<tr><td>{esc(friendly_when(row))}</td>"
                f"<td>{esc(row['title'])}<div class='meta'>{esc(row['source_cal'])}</div></td>"
                f"<td>{esc(row['mirror_title'] or '—')}</td>"
                f"<td><span class='st'>{esc(row['status'])}</span></td></tr>"
            )
        return (
            toggle + '<div class="card scroll"><table><tr><th>When</th><th>Personal event</th>'
            '<th>On work calendar</th><th></th></tr>' + "".join(cells) + "</table></div>"
        )

    def setup_body(self) -> str:
        cfg = self.cfg
        try:
            cals = bridge("calendars")["calendars"]
        except BridgeError as exc:
            return f'<div class="banner">Could not read your calendars: {esc(exc)}</div>'

        cals.sort(key=lambda c: (c["source"].lower(), c["title"].lower()))
        sources, targets, current_source = [], [], None
        for cal in cals:
            checked = "checked" if cal["id"] in cfg["source_calendar_ids"] else ""
            if cal["source"] != current_source:
                current_source = cal["source"]
                header = (f'<div class="meta" style="margin:10px 0 3px;font-weight:600">'
                          f'{esc(current_source)}</div>')
                sources.append(header)
            sources.append(
                f'<label class="cal"><input type="checkbox" name="sources"'
                f' value="{esc(cal["id"])}" {checked}><span>{esc(cal["title"])}</span>'
                f'<span class="src">{esc(cal["sourceType"])}</span></label>'
            )
            if cal["writable"] and not cal["subscribed"]:
                sel = "checked" if cal["id"] == cfg["target_calendar_id"] else ""
                targets.append(
                    f'<label class="cal"><input type="radio" name="target"'
                    f' value="{esc(cal["id"])}" {sel}><span>{esc(cal["title"])}</span>'
                    f'<span class="src">{esc(cal["source"])}</span></label>'
                )

        modes = "".join(
            f'<div class="opt"><input type="radio" name="mode" id="m-{v}" value="{v}"'
            f'{" checked" if cfg["default_title_mode"] == v else ""}>'
            f'<label for="m-{v}">{esc(t)}</label></div>'
            for v, t in [("generic", "A generic title"),
                         ("prefix", "The real title, prefixed"),
                         ("copy", "The real title as-is")]
        )
        return f"""
<form method="post" action="/setup">
<fieldset><legend>Personal calendars to watch</legend>
<div class="cals">{''.join(sources)}</div>
<div class="note">New events on these calendars get offered to you.</div></fieldset>

<fieldset><legend>Work calendar to block</legend>
<div class="cals">{''.join(targets)}</div>
<div class="note">Approved events are created here and marked busy.</div></fieldset>

<fieldset><legend>Default title for a work block</legend>
<div class="opts">{modes}</div>
<div class="row"><label class="meta">Generic title</label>
<input type="text" name="generic" value="{esc(cfg['generic_title'])}"></div>
<div class="note">You can still change the title on any individual event.</div></fieldset>

<fieldset><legend>Behaviour</legend>
<div class="row" style="margin-bottom:9px"><label class="meta">Look ahead</label>
<input type="text" name="days" value="{cfg['lookahead_days']}" style="min-width:70px"> days
<label class="meta" style="margin-left:16px">Check every</label>
<input type="text" name="poll" value="{cfg['poll_minutes']}" style="min-width:70px"> minutes</div>
<div class="opt"><input type="checkbox" name="weekends" id="weekends"
 {"checked" if cfg.get("skip_weekends", True) else ""}><label for="weekends">Skip events that
 fall on a weekend</label></div>
<div class="note" style="margin:-2px 0 10px 25px">An event that also touches a weekday —
 a Friday-to-Monday trip — is still offered.</div>
<div class="opt"><input type="checkbox" name="allday" id="allday"
 {"checked" if cfg["include_all_day"] else ""}><label for="allday">Include all-day events</label></div>
<div class="opt"><input type="checkbox" name="notify" id="notify"
 {"checked" if cfg["notify"] else ""}><label for="notify">Send a notification when
 something is waiting</label></div></fieldset>

<button class="primary" type="submit">Save settings</button>
</form>"""


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, cfg):
        self.cfg = cfg
        super().__init__(addr, handler)


def poller(server: Server) -> None:
    """Background scan loop. Notifies only when the pending count grows."""
    last_pending = len(pending_rows())
    while True:
        cfg = server.cfg
        time.sleep(max(60, int(cfg["poll_minutes"]) * 60))
        try:
            server.cfg = load_config()
            scan(server.cfg)
        except BridgeError as exc:
            log(f"scan failed: {exc}")
            continue
        except Exception as exc:  # keep the loop alive whatever happens
            log(f"scan error: {exc}")
            continue

        count = len(pending_rows())
        if count > last_pending and server.cfg.get("notify"):
            new = count - last_pending
            notify(
                "CalSync",
                f"{new} personal event{'s' if new != 1 else ''} waiting — "
                f"http://127.0.0.1:{server.cfg['port']}",
            )
        last_pending = count


def url(cfg: dict, path: str = "/") -> str:
    return f"http://127.0.0.1:{cfg['port']}{path}?token={cfg['token']}"


# --------------------------------------------------------------------------
# launchd
# --------------------------------------------------------------------------

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>
        <string>serve</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ProcessType</key><string>Background</string>
    <key>StandardOutPath</key><string>{log}</string>
    <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def install_agent(cfg: dict) -> None:
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PLIST_PATH, "w") as fh:
        fh.write(PLIST.format(
            label=LABEL, python=sys.executable,
            script=os.path.abspath(__file__), log=LOG_PATH,
        ))
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    proc = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", PLIST_PATH],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"launchctl bootstrap failed: {proc.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    print(f"Agent installed and running.\nWeb UI: {url(cfg)}")


def uninstall_agent() -> None:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    try:
        os.remove(PLIST_PATH)
    except OSError:
        pass
    print("Agent stopped and removed.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_serve(cfg: dict) -> None:
    server = Server(("127.0.0.1", int(cfg["port"])), Handler, cfg)
    threading.Thread(target=poller, args=(server,), daemon=True).start()
    log(f"serving on {url(cfg)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_scan(cfg: dict) -> None:
    counts = scan(cfg)
    if counts.get("error"):
        print(f"error: {counts['error']}", file=sys.stderr)
        sys.exit(1)
    pending = len(pending_rows())
    print(f"{counts['new']} new, {counts['changed']} changed, {counts['removed']} removed, "
          f"{counts['weekend']} skipped as weekend · {pending} waiting for a decision")
    if pending:
        print(f"Review them at {url(cfg)}")


def cmd_status(cfg: dict) -> None:
    try:
        cals = {c["id"]: c for c in bridge("calendars")["calendars"]}
    except BridgeError as exc:
        cals = {}
        print(f"warning: {exc}", file=sys.stderr)

    def name(cal_id):
        cal = cals.get(cal_id)
        return f"{cal['title']} ({cal['source']})" if cal else cal_id or "— not set —"

    print("Watching:")
    for cal_id in cfg["source_calendar_ids"] or ["— none —"]:
        print(f"  · {name(cal_id)}")
    print(f"Blocking on: {name(cfg['target_calendar_id'])}")
    print(f"Window: {cfg['lookahead_days']} days · polls every {cfg['poll_minutes']} min")
    print(f"Weekend events: {'skipped automatically' if cfg.get('skip_weekends', True) else 'offered like any other'}")

    conn = db()
    with _db_lock:
        rows = conn.execute(
            "SELECT status, COUNT(*) c FROM events GROUP BY status"
        ).fetchall()
    tally = ", ".join(f"{r['c']} {r['status']}" for r in rows) or "nothing tracked yet"
    print(f"Tracked: {tally}")
    print(f"Pending decisions: {len(pending_rows())}")

    uid = os.getuid()
    proc = subprocess.run(["launchctl", "print", f"gui/{uid}/{LABEL}"],
                          capture_output=True, text=True)
    print(f"Background agent: {'running' if proc.returncode == 0 else 'not installed'}")
    print(f"Web UI: {url(cfg)}")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "open"
    cfg = load_config()

    if command == "serve":
        cmd_serve(cfg)
    elif command == "scan":
        cmd_scan(cfg)
    elif command == "status":
        cmd_status(cfg)
    elif command == "test-notify":
        notify("CalSync", "Test banner — notifications are reaching you.")
        print("Notification posted. If no banner appeared, check System Settings >"
              " Notifications for CalSyncBridge and Script Editor.")
    elif command == "install":
        install_agent(cfg)
    elif command == "uninstall":
        uninstall_agent()
    elif command in ("open", "setup"):
        target = url(cfg, "/setup" if command == "setup" else "/")
        webbrowser.open(target)
        print(target)
    else:
        print(__doc__.strip())
        sys.exit(1)


if __name__ == "__main__":
    main()
