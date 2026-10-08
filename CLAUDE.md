# Working on CalSync

## Bump the version on every change

**Every change to this repository bumps `VERSION` in the same commit.** No
exceptions — not for a one-line fix, not for a typo, not for a comment. The
point is that `calsync version` on any machine identifies exactly what is
running, and that only holds if the number moves whenever the code does.

Pick the digit by what changed, following [semver](https://semver.org):

| Bump | When |
| --- | --- |
| **patch** (1.0.0 → 1.0.1) | Bug fix, docs, comments, refactor — nothing a user has to react to. |
| **minor** (1.0.1 → 1.1.0) | New feature or option, new command, new behaviour that is backwards compatible. |
| **major** (1.1.0 → 2.0.0) | Anything a user must act on: a config key renamed or removed, a command removed, a default reversed, a schema migration they cannot roll back. |

Every bump also gets a `CHANGELOG.md` entry under a heading for the new
version, dated, grouped under Features / Privacy / Fixes as appropriate. Write
the entry for someone deciding whether to update, not as a restatement of the
diff.

So the sequence for any piece of work is:

1. Make the change.
2. Edit `VERSION`.
3. Add the `CHANGELOG.md` entry.
4. `./build.sh` — always, since the version is stamped into the bundle at build
   time and `calsync version` will otherwise report the old one and warn.
5. Commit all of it together.
6. Once the commit is on `main` in the installed checkout, refresh the three
   things that hold a version of their own:
   - `./build.sh` again, if step 4 ran in a worktree. That build went into the
     worktree's own `build/`, not the helper the installed copy uses.
   - `bin/calsync install` restarts the background agent. The running agent
     reads `VERSION` once at startup, so until it restarts the review page
     header still shows the old number.
   - `./make-shortcut.sh` rebuilds the Desktop launcher, which has the version
     stamped into its `Info.plist`. Pass the same destination it was built
     to, if that was not the Desktop.

   Run both from the installed checkout, never from a worktree: `install`
   points the agent at whichever copy of `calsync.py` runs it, and the
   launcher records the path of the checkout that built it. Skip this step
   on a machine where CalSync is not installed.

### When several agents work in parallel

The steps above are for one agent working alone. When a job is split across
parallel agents, each in its own git worktree, `VERSION` and `CHANGELOG.md`
are shared files: if every branch edits them, every merge conflicts on them.
So the job divides:

- **Worker agents** never edit `VERSION` or `CHANGELOG.md`. Each one puts the
  bump it proposes (patch / minor / major) and its draft changelog text in its
  commit message or final report.
- **The integrator**, the one agent that merges branches into `main` one at a
  time, does steps 2–5 for each merged branch: it bumps `VERSION`, writes the
  `CHANGELOG.md` entry, runs `./build.sh`, and commits, in the merge commit or
  in a commit right after it. Step 6 also belongs to the integrator, once the
  merged result is on `main` in the installed checkout. The rule is unchanged: every change that lands
  on `main` moves the version.

Tag and release when the work is a meaningful stopping point, not on every
commit:

```bash
git tag -a v1.2.3 -m "CalSync 1.2.3" && git push origin main v1.2.3
gh release create v1.2.3 --title "CalSync 1.2.3" --notes "..."
```

## Layout

| Path | |
| --- | --- |
| `VERSION` | Single source of truth for the version |
| `calsync.py` | State, scanning, web UI, launchd agent, CLI |
| `src/calbridge.swift` | EventKit + Notification Centre helper |
| `build.sh` | Compiles the helper into `CalSyncBridge.app` |
| `make-shortcut.sh` | Builds the Desktop launcher app |
| `src/make_icon.py` | Draws the icon with the standard library only |

`calsync.py` and the Swift helper are built separately. Editing Python takes
effect on the next run; editing Swift needs `./build.sh`. `calsync version`
warns when the two disagree — if you see that warning, you forgot step 4.

## Constraints worth knowing before you change anything

**A work block carries only a time and its title.** Notes, location,
structuredLocation and URL are cleared in `stripDetails()` on both create and
update. Do not add a field that describes the source event — the whole point is
that a colleague who can see event details learns only that the time is taken.

**The helper must be launched through LaunchServices** (`open -W -a`), never
exec'd directly. macOS attributes the Calendar permission to the *responsible*
process, so a bare exec is blamed on the terminal and denied with no prompt.
This is why the helper uses `--in`/`--out` files rather than stdio.

**Pin the deployment target.** `swiftc` left alone targets the toolchain's
newest macOS, which can be a version that does not exist yet; LaunchServices
then refuses the bundle with `-10825` while the binary still runs when exec'd
directly.

**Event identity differs by kind.** One-off events are keyed by identifier
alone; repeating events by identifier plus start time. Changing this breaks
reschedule tracking — see the design notes in `README.md`.

**No third-party Python packages.** Standard library only, so the tool installs
with nothing but a clone and `./build.sh`.

## Before committing

- `python3 -c "import ast; ast.parse(open('calsync.py').read())"`
- `./build.sh` succeeds and `calsync version` shows the new number with no
  mismatch warning and no `-dirty` suffix after the commit.
- No secrets: the repo must never contain the web UI token, calendar
  identifiers, account addresses, or real event titles. Sweep the staged diff,
  not just the working tree.
