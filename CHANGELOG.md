# Changelog

Versions follow [semantic versioning](https://semver.org). The version lives in
the `VERSION` file, which `build.sh` stamps into the app bundle — run
`calsync version` to see what is actually installed.

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
