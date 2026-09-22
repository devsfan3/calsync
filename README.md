# CalSync

Watch your personal calendars, get asked about anything new, and — only for
events you approve — put a matching **busy** block on your work calendar.

The problem it solves: your dentist appointment lives on a shared family
calendar your colleagues cannot see, so they book straight over it. CalSync
mirrors the *time* onto your work calendar without necessarily mirroring the
*details*.

Everything runs locally on your Mac. No server, no OAuth, no cloud credentials,
no third-party Python packages.

```
personal calendars ─┐
                    ├─> CalSyncBridge.app ──> calsync.py ──> review page
work calendar ──────┘       (EventKit)       (state + poll)    127.0.0.1:8787
                                                    ↑                │
                                                    └── you approve ──┘
                                                                     ↓
                                                    busy block on work calendar
```

## Requirements

| | |
| --- | --- |
| macOS | 13 (Ventura) or later. Developed on macOS 26–27. |
| Xcode Command Line Tools | For `swiftc`. Install with `xcode-select --install`. |
| Python | 3.9+ — the system `python3` is fine. Standard library only. |
| Calendar accounts | **Both** calendars must be visible in the built-in Calendar app. |

That last row is the one that trips people up. CalSync talks to EventKit, the
same framework Calendar.app uses, so any account you want it to touch has to be
added in **System Settings → General → Internet Accounts** (or **Calendar →
Add Account**). This includes work accounts:

- **Microsoft 365 / Exchange** — add it as an Exchange account. You do *not*
  need an Azure app registration, admin consent, or a Graph API token.
- **Google, iCloud, generic CalDAV** — all work the same way.

CalSync is not tied to any particular pairing. "Personal" is whichever
calendars you tell it to watch and "work" is whichever calendar you tell it to
write to; iCloud → Microsoft 365 is just the common case.

> **Why not automate Outlook directly?** New Outlook for Mac (16.x) removed
> essentially all of its AppleScript surface, so scripting the app is no longer
> viable. Going through EventKit works with old Outlook, new Outlook, or no
> Outlook at all — it only cares that the account is in macOS Calendar.

## Install

```bash
git clone https://github.com/devsfan3/calsync.git ~/calsync
cd ~/calsync
./build.sh
```

`build.sh` compiles the Swift helper and wraps it in `CalSyncBridge.app`. Put
the command on your `PATH`:

```bash
mkdir -p ~/.local/bin && ln -sf "$PWD/bin/calsync" ~/.local/bin/calsync
```

If `~/.local/bin` is not already on your `PATH`, add it:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && exec zsh
```

Then start the background agent and pick your calendars:

```bash
calsync install
calsync setup
```

`calsync install` registers a launchd agent that starts at login and restarts
itself if it dies. `calsync setup` opens the configuration page in your browser.

### Granting Calendar access

The first time the helper runs, macOS asks for Calendar access. **Click OK.**
The prompt names `CalSyncBridge`, not Terminal — that is intentional (see
[Design notes](#design-notes)).

If you dismissed the prompt or clicked Don't Allow, enable it manually in
**System Settings → Privacy & Security → Calendars → CalSyncBridge**.

### Choosing calendars

On the setup page:

1. **Personal calendars to watch** — tick the ones whose new events you want to
   be asked about.
2. **Work calendar to block** — pick exactly one. Only writable, non-subscribed
   calendars are listed.
3. Adjust the title style, look-ahead window and poll interval if you like, then
   **Save settings**.

The first scan is a **baseline**: everything already on your personal calendars
is recorded silently rather than queued, so you are not handed hundreds of
decisions on day one. Only events that appear *after* that are offered. If you
do want the existing ones, there is a *Review them anyway* button.

## Commands

| Command | What it does |
| --- | --- |
| `calsync open` | Open the review page |
| `calsync setup` | Open the configuration page |
| `calsync scan` | Check for new events right now |
| `calsync status` | Config, counts, and whether the agent is alive |
| `calsync test-notify` | Post a test notification banner |
| `calsync scrub` | Clear notes/location/URL from every block it created |
| `calsync install` | Install and start the launchd agent |
| `calsync uninstall` | Stop and remove the agent |
| `calsync serve` | Run in the foreground (what the agent runs) |

## How it behaves

**Nothing is written without your approval.** A scan only ever adds rows to a
queue. Your work calendar is touched when you click *Block this time*.

**The title is the only detail that can reach the work calendar.** Notes,
location and URL are stripped unconditionally, in the helper itself, on both
create and update — so no caller can leak them even by passing an extra field.
A colleague who can see your event details learns the time is taken and nothing
more. Early builds wrote a note naming the source event onto each block, which
quietly defeated the point of a generic title; `calsync scrub` clears that from
blocks already created, and it runs once automatically on upgrade.

**Titles are per-event.** Each pending event offers a generic title (`Busy`), a
prefixed real title (`[Personal] Dentist`), the real title as-is, or anything
you type. The default is configurable.

**Weekend events are skipped automatically.** Anything falling entirely on a
Saturday and/or Sunday is filed away without asking. An event that *also*
touches a weekday — a Friday-to-Monday trip, a Sunday evening running past
midnight — still costs work time, so it is still offered. Auto-skips are listed
under History → *Weekend skips*, and if one is later rescheduled onto a weekday
it returns to the queue. Turn the rule off and everything it skipped comes back;
turn it on and pending weekend events leave. Events you decided by hand are
never re-filed by the rule.

**Skips are permanent.** Skipping by hand means you are never asked about that
event again, even if it later changes.

**Time changes follow automatically.** If a one-off event you approved moves,
its work block moves with it — no second prompt. The *title* only follows if the
block still uses the copied title; a title you typed yourself is left alone.
Moving a single occurrence of a *repeating* event behaves differently: the old
occurrence prompts to remove its block and the new slot is offered fresh,
because EventKit gives every occurrence in a series the same identifier.

**Deletions ask first.** If an approved personal event disappears, its work
block is not removed silently — it appears in the queue as *Remove* / *Keep*.
Only future events inside the look-ahead window are considered, so the window
rolling past an old event is never mistaken for a deletion.

**Events deleted before you decide just vanish.** Nothing was written, so the
queue entry is dropped rather than left showing a ghost.

## Notifications

Banners come from the background agent, not the browser — you get them whether
or not a browser is open. One fires when a poll finds *more* events waiting than
last time, so you are not re-notified about a queue you have already seen.

`CalSyncBridge.app` posts them under its own bundle identity via
`UNUserNotificationCenter`. If that fails it falls back to `osascript`, which
posts as Script Editor.

Test with `calsync test-notify`. Be aware that **neither API reports whether a
banner was actually displayed** — both return success even when Notification
Center or a Focus mode suppresses it. Only your eyes confirm it.

## Desktop shortcut

```bash
./make-shortcut.sh              # writes CalSync.app to your Desktop
./make-shortcut.sh ~/Applications
```

The app holds no logic: it checks the agent is loaded (reinstalling it if not,
so it is never a dead link), then calls `calsync open`, which reads the current
port and token from the config. The icon is drawn by `src/make_icon.py` using
only the standard library — shapes come from signed distance fields, so edges
antialias without supersampling.

## Configuration

The setup page writes `~/.config/calsync/config.json` (mode `600` — it holds the
web UI token). You can edit it directly; restart the agent afterwards with
`calsync install`.

| Key | Default | Meaning |
| --- | --- | --- |
| `source_calendar_ids` | `[]` | Calendars to watch |
| `target_calendar_id` | `""` | Calendar that receives busy blocks |
| `lookahead_days` | `60` | How far ahead to look |
| `poll_minutes` | `20` | Minutes between scans |
| `port` | `8787` | Loopback port for the web UI |
| `token` | generated | Web UI access token |
| `default_title_mode` | `generic` | `generic`, `prefix`, or `copy` |
| `generic_title` | `Busy` | Title used by `generic` mode |
| `title_prefix` | `[Personal] ` | Prefix used by `prefix` mode |
| `include_all_day` | `true` | Offer all-day events |
| `skip_weekends` | `true` | Auto-skip weekend-only events |
| `notify` | `true` | Post notification banners |
| `baseline_done` | `false` | Set after the first scan |

Where things live:

| Path | |
| --- | --- |
| `~/.config/calsync/config.json` | Settings and web UI token |
| `~/.local/state/calsync/state.db` | SQLite decision state |
| `~/.local/state/calsync/calsync.log` | Agent log |
| `~/Library/LaunchAgents/local.calsync.agent.plist` | launchd definition |

The web UI listens on loopback only and requires a token, stored as a cookie the
first time you follow a tokenised link.

## Design notes

Three things in here are non-obvious enough to be worth writing down.

**TCC blames the *responsible process*, not the binary.** Run an EventKit binary
from a shell and macOS attributes the Calendar permission to whatever launched
the shell — your terminal, your IDE, your editor. If that app has no Calendar
entitlement the request is denied instantly, with no prompt at all. The fix is
to ship the privileged code as a signed `.app` with its own
`NSCalendarsFullAccessUsageDescription` and launch it through LaunchServices
(`open -W -a`), which makes it its own responsible process. That is the entire
reason `calbridge` takes `--in`/`--out` file paths rather than using stdin and
stdout: launched this way it has no usable stdio.

**`UNUserNotificationCenter` needs a real app and a run loop.** A plain
command-line process has neither, and notification requests are accepted and
then silently go nowhere — the API reports success either way. `cmdNotify`
creates an `NSApplication` with `.accessory` activation policy and pumps the
main run loop until the completion handler fires.

**One-off and repeating events need different identity.** EventKit gives every
occurrence of a series the same identifier, so occurrences must be keyed by
identifier *plus* start time or the whole series collapses into one row. But
applying that to one-off events means any reschedule looks like a new event plus
a vanished one, which breaks change tracking entirely. So repeating events are
keyed by identifier and start, one-off events by identifier alone.

## Troubleshooting

**"calendar access denied" and no prompt appears.** Something other than
CalSyncBridge is being blamed. Check **System Settings → Privacy & Security →
Calendars** for a `CalSyncBridge` entry and enable it. If there is no entry,
reset with `tccutil reset Calendar local.calsync.bridge` and run
`calsync status` again.

**`could not launch the bridge: ... error -10825`.** The bundle declares a
minimum macOS newer than the one you are running, so LaunchServices refuses it —
confusingly, the binary still runs fine when executed directly, which makes it
look like a registration problem. Left to itself `swiftc` targets the
toolchain's newest macOS, which can be a version that does not exist yet, so
`build.sh` pins the deployment target explicitly. If you see this, re-run
`./build.sh` and check `otool -l build/CalSyncBridge.app/Contents/MacOS/calbridge
| grep minos` reports 13.0.

**macOS asks for Calendar access again after a rebuild.** Expected. `build.sh`
re-signs the bundle, which changes its code hash, and TCC treats that as a
different program.

**No writable calendars listed as targets.** The setup page only offers
calendars that are writable and not subscribed. Read-only work calendars, or
accounts added as subscribed `.ics` feeds, cannot receive events — re-add the
account as a full Exchange/CalDAV account.

**The agent is not running.** `calsync status` reports it. Reinstall with
`calsync install`, and check `~/.local/state/calsync/calsync.log`.

**Port 8787 is taken.** Change `port` in `config.json`, then `calsync install`.

**Nothing happens while I am away.** The agent runs inside your GUI login
session, because the EventKit helper is launched through LaunchServices. Being
logged out at the login window means no polling — powered on is not enough. A
sleeping Mac also does not poll; it picks up at the next tick after waking.

**No notification banners.** Check for an active Focus mode, then **System
Settings → Notifications → CalSyncBridge**. Verify with `calsync test-notify`.

## Uninstall

```bash
calsync uninstall                 # stop and remove the agent
rm -rf ~/.config/calsync ~/.local/state/calsync   # forget all state
rm -rf ~/Desktop/CalSync.app ~/.local/bin/calsync
```

Work blocks CalSync already created stay on your calendar; remove them as you
would any other event.

## License

MIT — see [LICENSE](LICENSE).
