# Changelog

Versions follow [semantic versioning](https://semver.org). The version lives in
the `VERSION` file, which `build.sh` stamps into the app bundle — run
`calsync version` to see what is actually installed.

## 1.1.2 — 2026-10-09

### Changed

- The tests now run automatically on GitHub for every push and pull request:
  on Linux under Python 3.9, 3.11 and 3.13, and on macOS, where the Swift
  helper is also compiled with `./build.sh`. No change to CalSync itself.

## 1.1.1 — 2026-10-09

### Fixes

- **Works on the macOS system Python (3.9) again.** Python before 3.11 could
  not read the dates the helper writes, so on those versions every date was
  treated as unknown, with no error. Weekend events were not skipped, a
  deleted personal event never offered to remove its work block (leaving
  stale busy time on your work calendar), and dates showed as raw text.
  Update if `calsync version` shows a Python older than 3.11.

### Changed

- Added a test suite (`tests/`, standard library only) covering date
  handling, weekend detection, event identity, scanning and approvals, and
  the web UI's token and origin checks.
- The README now says that switching to a different work calendar leaves
  blocks on the old one for you to delete by hand.

## 1.1.0 — 2026-10-09

### Features

- `calsync rotate-token` issues a new web UI token and restarts the agent,
  signing out every browser. Use it if the token may have leaked.
  `calsync open` and the Desktop launcher pick up the new token on their own.

### Privacy

- The review page now rejects form posts from any other page, including
  pages served by other web servers on this Mac. The cookie's SameSite
  setting alone did not cover those, because browsers treat every port on
  127.0.0.1 as the same site.
- Requests addressed to any host name other than `127.0.0.1` or `localhost`
  are refused, which blocks DNS-rebinding attacks.
- The token is checked in constant time against the parsed cookie, the cookie
  is now `HttpOnly`, and a tokenised link is swapped for the cookie straight
  away so the token does not stay in the address bar.

### Fixes

- A non-numeric "Look ahead" or "Check every" value on the Setup page no
  longer breaks the save; the previous value is kept.
- `calsync install` no longer fails intermittently with
  "Bootstrap failed: 5: Input/output error" when restarting a running agent.

## 1.0.4 — 2026-10-09

### Privacy

- The calendar helper now only does what CalSync itself needs. It reads its
  own copy of the config and refuses to read events from any calendar other
  than the personal calendars you chose, or to create, change or delete
  events anywhere but your chosen work calendar. Before, any program running
  as you could launch it and use its Calendar permission to read or change
  every calendar without macOS asking.
- Everything CalSync keeps on disk is now readable by your account only. The
  database, log and temporary helper files were created world-readable; they
  are now `0600` inside `0700` folders, and existing files are fixed the next
  time any `calsync` command runs.
- The web UI token is no longer written to the log, and copies left there by
  earlier versions are redacted.

### Note

- If you switch to a different work calendar, blocks CalSync made on the old
  one can no longer be updated or removed from CalSync. Delete them by hand.

## 1.0.3 — 2026-10-08

### Changed

- `CLAUDE.md` adds a final step after every version bump: rebuild the helper
  in the installed checkout, restart the background agent with
  `bin/calsync install`, and rebuild the Desktop launcher with
  `./make-shortcut.sh`. Without it, the review page and the launcher keep
  showing the previous version after an update. No code changes.

## 1.0.2 — 2026-10-08

### Changed

- `CLAUDE.md` now covers parallel agents. When a job is split across agents
  working in separate git worktrees, only the agent that merges into `main`
  bumps `VERSION` and writes the changelog entry, once per merged branch.
  Every change on `main` still moves the version; the split just stops
  parallel branches from conflicting on these two files. No code changes.

## 1.0.1 — 2026-09-22

### Changed

- Added `CLAUDE.md`, which makes the version bump a standing rule: every change
  to the repository bumps `VERSION` and adds a changelog entry in the same
  commit, so `calsync version` always identifies exactly what is running. It
  also records the constraints worth knowing before changing anything — the
  no-details rule for work blocks, the LaunchServices requirement, the pinned
  deployment target, and the event identity scheme.

## 1.0.0 — 2026-09-22

First versioned release. Everything below was built before versioning started.

### Features

- Watches any number of personal calendars through EventKit and queues new
  events for approval on a loopback web page.
- Approved events become **busy** blocks on a chosen work calendar. Nothing is
  written without an explicit approval.
- Per-event title choice: a generic title, the real title prefixed, the real
  title as-is, or anything you type.
- Weekend events are skipped automatically unless they also touch a weekday.
- Time changes on an approved one-off event follow through to its block.
  Deletions ask before removing anything.
- Notification banners from the background agent, so a browser need not be open.
- launchd agent that starts at login, plus a Desktop shortcut app.

### Privacy

- A work block carries **only** a time and the title you chose. Notes, location
  and URL are stripped in the helper itself on both create and update, so no
  caller can leak them.
- Earlier builds wrote a note naming the source event onto every block, which
  defeated the point of a generic title. `calsync scrub` clears that from blocks
  already created and runs once automatically on upgrade.

### Fixes

- `update` reported a deleted event as a failure, so the "block deleted outside
  CalSync" recovery path could never run. It now reports `missing` like `delete`.
- One-off events are keyed by identifier alone and repeating events by
  identifier plus start time, so rescheduling reads as a change rather than as a
  new event plus a vanished one.
- `build.sh` pins the deployment target. `swiftc` was emitting `minos 28.0` from
  a 26.x SDK, and LaunchServices refused the bundle with `-10825` while the
  binary still ran when executed directly.
- The helper is now a universal binary (arm64 + x86_64).
- `last_seen` uses a unique per-scan marker; second-precision timestamps made
  two scans in the same second indistinguishable, silently disabling stale and
  vanished detection.
- Log lines were written twice under launchd, once via the stdout redirect and
  once by an explicit append.
