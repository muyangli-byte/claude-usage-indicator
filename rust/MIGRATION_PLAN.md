# Python → Rust Seamless Migration Plan

> Goal: existing users update *once, normally*, and at some later point their tray
> silently becomes the Rust build — same icon, same menu, same number, same
> notifications, same version string. Nobody should be able to tell. Nobody should
> ever be left with **no working tray**.
>
> This plan is grounded in the actual repo (verified file:line) and was
> adversarially red-teamed; the architecture below is the *result* of that
> red-teaming, not the naive first idea.

---

## 0. The one big idea

The deployed Python clients auto-update by **`git fetch --depth 1 origin main` +
`git reset --hard FETCH_HEAD`**, then they relaunch via the systemd unit
`claude-usage-indicator.service`, whose `ExecStart=…/run.sh` ends with
`exec venv/bin/python claude_usage_indicator.py`. We **cannot** retroactively change
how already-shipped clients update — we can only change what they pull.

So the carrier is: **make `run.sh` a language switch.**

```
# run.sh (shipped on main, permanent) — sketch
DIR="$(cd "$(dirname "$0")" && pwd)"
CUI_BIN="$HOME/.local/share/claude-usage-indicator-bin/cui"      # SIBLING dir, OUTSIDE the git tree
SENTINEL="$HOME/.config/claude-usage-indicator/use-rust"          # OUTSIDE the git tree
if [ -f "$SENTINEL" ] && [ -x "$CUI_BIN" ] && "$CUI_BIN" --version >/dev/null 2>&1; then
    exec "$CUI_BIN"                                               # → Rust
fi
exec "$DIR/venv/bin/python" "$DIR/claude_usage_indicator.py"      # → Python (always-safe fallback)
```

Migration = download a *verified* Rust binary into the sibling dir, write the
sentinel, restart the service. Rollback = delete the sentinel, restart. The unit
file is **never rewritten**; the Python tree is **never deleted**; the binary lives
**outside** the git tree so `git reset --hard` / `install.sh`'s `mv …→.bak` can't
nuke it. This single decision neutralises most of the catastrophic failure modes
(ExecStart-rewrite stranding, binary-in-git-tree wiped, won't-load binary defeats
its own update loop, crash-loop brick).

---

## 1. Ground truth we must respect (cannot change for shipped clients)

| Fact | Consequence |
|---|---|
| Update = `git reset --hard origin/main` of `muyangli-byte/claude-usage-indicator`, then `pip install -r requirements.txt`, then `systemctl --user restart claude-usage-indicator.service` (`cli.py:180-219`) | The next thing on `main` **must stay a runnable Python app** with a venv-installable `requirements.txt`. Migration code rides in *as Python*. |
| Detection = fetch `VERSION` file on `main` (contents API `?ref=main`, raw fallback) + dotted-int compare (`api.py:153/167`) | A bump to the `VERSION` file is the *only* way to make clients re-check & to fire ntfy. `VERSION` must stay at repo root, dotted-numeric. |
| ntfy is one broadcast topic `claude-usage-indicator-muyangli-byte-7c1e9a`; `notify-release.yml` fires **only on a push to `main` that changes `VERSION`** | We **cannot segment** the fleet by VERSION. A VERSION bump notifies *everyone*. Therefore canary cannot be keyed on VERSION — it is keyed on a separate manifest read per-machine. |
| Update is **manual** — detection only shows a banner; the user clicks "Update now". No silent auto-update exists. | Users who never click never update. The long tail on 2.11.0 is permanent. |
| Self-update **aborts on a dirty tree or non-git install** (`cli.py:173`) | A slice of the fleet will never receive the cutover. Coexistence must be permanent, not a hard cutover. |
| `rust-release.yml` builds the binary **only when a GitHub Release is `published`**; downloads resolve at `releases/latest/download/cui-<arch>-linux` (matches `selfupdate.rs`). No Release exists yet. | The cutover *introduces* the first GitHub Release. The asset must exist & be verified **before** any client is told to migrate. |
| Shipped asset is built on `ubuntu-latest` (glibc ~2.39) → **floor too high**; only `x86_64`; needs a D-Bus session bus + `systemd --user`; binary's measured floor on an old box was glibc 2.30 | We must **pin CI to an old glibc** and **measure** the real floor, and **exclude** non-x86_64 / old-glibc / no-D-Bus / no-user-systemd machines from migration. |

---

## 2. Phased shape (why two releases, not one)

A single "flip everything at once" commit is the dangerous path the red-team kept
breaking. Instead:

- **R1 — the Bridge (v2.12.0, pure Python, safe everywhere).** Lands via the
  existing git-reset update. Changes **no** user-visible behaviour. It only:
  1. ships the new switch-`run.sh`, the migration code (`cui/migrate.py` + a
     detached startup hook), the preflight, and the bash restore-watchdog
     (`systemd --user` timer, installed **dormant**);
  2. adds `bin/`, `cui`, `cui.*`, `*.new`, `use-rust`, standby/marker files to the
     **tracked** `.gitignore` *in the same commit* (so future `git reset --hard`
     never wipes Rust artifacts);
  3. ships `migration.json` on `main` with **`percent: 0`** (nobody migrates yet);
  4. bumps `VERSION` → `2.12.0` (this is what makes existing clients pull R1 and
     fires ntfy once — harmless, it's still Python).
- **Dwell** (days): let R1 propagate; confirm the *Python* bridge is healthy on
  the test matrix and that excluded-machine classes still run fine.
- **R2 — open the canary.** No new VERSION bump (must not re-fan-out). Just edit
  `migration.json` `percent` upward over days. Each bridge client, on its poll
  cadence, reads `migration.json`, computes its bucket, and — if in-bucket and
  preflight passes — performs the swap to the **already-published** Rust binary.

The Rust binary shipped in the R1 Release is compiled with `VERSION = 2.12.0`, so
after the swap the tray still reads "v2.12.0" — *imperceptible*.

> The user's lived experience: they click "Update now" **once** (→ Python 2.12.0).
> Later, when their bucket opens, the tray flickers for a service restart and comes
> back byte-identical — now Rust. No banner, no version change, no action.

---

## 3. Identity unification (make the Rust **prod** build indistinguishable)

Introduce a real build distinction so dev can still coexist: a cargo feature
`dev` (or a compile-time `CUI_DEV` env via `build.rs`). **Prod** (default) ships
clean; **dev** keeps the `-rust-dev` identity for local side-by-side testing.

Checklist (all `rust/cui/src/…` unless noted), prod values on the right:

| Where | Now (dev) | Prod must be |
|---|---|---|
| `config.rs` `APP_ID` | `claude-usage-indicator-rust-dev` | `claude-usage-indicator` |
| `config.rs` `SERVICE` | `…-rust-dev.service` | `claude-usage-indicator.service` |
| `config.rs` `VERSION` | hardcoded `2.11.0` | **sourced from root `VERSION`** via `build.rs` `include_str!("../../VERSION")` → no drift |
| `tray.rs` `label()` | `format!("[rust] {}", …)` | drop the `[rust] ` prefix |
| `tray.rs` `title()` | `Claude Usage` | `Claude usage` (match Python case) |
| `tray.rs` About / `Quit (rust-dev)` | dev strings; no Uninstall | `About (GitHub) v{VERSION}`; **remove Quit**, **add `Uninstall…`** (Python parity) wired to a real Rust uninstall |
| `tray.rs` pre-fetch label | `Claude usage…` | `Claude usage waiting...` (match Python) |
| `ntfy.rs` User-Agent | `{APP_ID}/{VERSION}` → leaks `rust-dev` on ntfy.sh logs | `claude-usage-indicator/{VERSION}` |
| `selfupdate.rs` cache dir | `~/.cache/claude-usage-indicator-rust-dev/` (keyed on APP_ID) | `~/.cache/claude-usage-indicator/` |
| `tray.rs` feedback body | `…(rust-dev) v{V}` | `Claude Usage Indicator v{V}` |
| `cli.rs`/`main.rs` `--version`/`--help`/`--doctor` header/startup log | `(rust-dev)` tokens | strip all `rust-dev`/`(rust)` tokens |

**CI gate:** fail the release build if the prod binary still contains the strings
`rust-dev` or `[rust]` (`strings cui | grep -q 'rust-dev' && exit 1`).

Already identical (no work): config.json path+keys, ntfy topic, notification
appname, version-check UA, icon names.

---

## 4. Build & compatibility — the gating fix

The shipped asset's glibc floor decides which machines can be migrated at all.

- **Pin the CI build base low + measure + hard-fail.** Build the release on an old
  glibc (e.g. `ubuntu-20.04` ≈ glibc 2.31, or `cargo-zigbuild`/manylinux for
  ≈2.28/2.27). After build, `objdump -T cui | grep GLIBC_ | sort -V | tail -1` and
  **`exit 1` if the floor exceeds the declared target** (the current draft only
  `echo`s it — that's a silent-regression trap when the runner image is retired).
- **The preflight floor constant must be measured from the *shipped asset*,** not
  the dev binary, and set with margin (require glibc strictly above the measured
  floor).
- **Arch:** only `x86_64` is built today. Either add an `aarch64` runner/cross
  build before cutover, or formally leave arm64 users on Python (the preflight's
  404-on-missing-asset already keeps them safe). **Decision needed.**
- **musl static is not cleanly feasible** (BoringSSL/boring-sys2 + cmake/bindgen is
  fragile on musl) — don't pursue it; rely on the low-glibc gnu build instead.
- **Runtime hard requirements** the preflight must verify on the box (not just
  download): a D-Bus **session** bus with a live `StatusNotifierWatcher` (the thing
  that actually renders a tray — stock GNOME-Wayland without the AppIndicator
  extension has none), and `systemd --user`.

---

## 5. The migration mechanism (detached, preflight-gated, atomic)

Runs as Python (in R1), **detached** and **after the tray is already registered**,
so the user always has a working tray during the probe.

```
# cui/migrate.py — sketch of the gate + swap, launched via
#   systemd-run --user --collect <py> -m cui.migrate
# guarded by flock on a lockfile OUTSIDE the git tree; idempotent; logged.
1. flock or exit. If sentinel already present and Rust healthy → exit (done).
2. Fetch migration.json. FAIL-CLOSED: unreadable → do NOT migrate, retry next poll.
   Honour kill-switch (rollback_all) and block window.
3. Bucket gate: seed = /etc/machine-id (fallback: persisted per-install UUID in
   CONFIG_DIR if machine-id empty/placeholder); bucket = hash(seed)%100;
   proceed only if bucket < migration.json.percent.
4. PREFLIGHT — fitness of the BINARY ONLY (never gate on creds/login/network):
     - arch == x86_64 (else the asset 404s anyway);
     - download cui-x86_64-linux from releases/latest to the SIBLING bin dir as
       cui.new; verify size + (decision §11) checksum/signature;
     - chmod +x; run `cui.new --version` (proves glibc/arch/loads) → must exit 0
       and NOT contain 'rust-dev';
     - confirm a live StatusNotifierWatcher + systemd --user are present.
   ANY failure → remove cui.new, record a back-off marker, stay Python. Never abort
   on "keyring locked"/"offline"/"logged out" — those are irrelevant to fitness and
   are common at login (the Python app has the identical dependency).
5. SWAP (atomic, ordered to avoid two trays):
     - `systemctl --user disable --now claude-usage-indicator.service` (stop+disable
       together so Restart=always can't relaunch mid-swap); poll until the process
       is gone;
     - `mv -f cui.new cui` (rename within the sibling dir);
     - write the `use-rust` sentinel;
     - `systemctl --user enable --now claude-usage-indicator.service` → run.sh now
       execs Rust.
6. Leave the Python tree + venv intact (rollback standby). Write a success marker.
```

Notes resolving red-team gaps:
- **Detached** (systemd-run --collect) so the swap's own restart can't kill the
  migrator (same trick the existing self-updater uses).
- **Binary outside the git tree** so `git reset --hard` / `install.sh` backups
  can't remove it; markers/sentinel outside too.
- **Preflight gates on "the binary runs here", not "the network/creds work now"** —
  a locked KWallet or captive portal must not block a swap (and must not, since the
  Python build needs creds too).
- **`is-active` is not "the tray is visible"** — require a `StatusNotifierWatcher`
  on the bus as the real success signal.

---

## 6. Rollback & self-healing — must not depend on the Rust binary

Three independent safety layers:

1. **run.sh fallback (always-on, zero state):** if the sentinel is absent or
   `cui --version` fails, run.sh execs Python. A binary that won't load can never
   take over.
2. **External bash watchdog** (`systemd --user` timer shipped dormant in R1, runs
   every few minutes — **not** written in Rust, so a broken Rust can't disable it):
     - the Rust binary writes a **heartbeat** `~/.cache/claude-usage-indicator/alive`
       (mtime) every poll — *this is new code to add to the Rust poller*;
     - watchdog restores Python (`rm sentinel; systemctl --user restart …`) if:
       (a) the service is `failed`, or (b) the unit is active but heartbeat is stale
       beyond a threshold (crash-loop/hang), or (c) `migration.json` kill-switch is
       set;
     - on restore, **health-check the Python venv** (`venv/bin/python -c pass`,
       rebuild via `/usr/bin/python3 -m venv` if it rotted during retention) so we
       never relaunch a broken Python;
     - **time-boxed pin** (epoch + attempt count): allow one cooldown re-attempt
       before pinning to Python, so a one-off login-time D-Bus race doesn't pin
       forever.
3. **Global clawback:** `migration.json.rollback_all` — honoured by the **bash
   watchdog** (not the Rust process), so it works even when Rust is the broken
   thing. To propagate within ~60 s instead of 24 h, post a manual ntfy ping (the
   watchdog/poller re-reads the manifest on any ntfy event).

> The "Rust won't even start after Python is gone" hole is closed by construction:
> Python is **never** removed, and run.sh + the watchdog both fall back to it.

---

## 7. Gradual rollout (灰度) without VERSION segmentation

- **`migration.json` on `main`** (read by every bridge client on its poll cadence
  and on ntfy events), e.g.:
  ```json
  {
    "schema": 1,
    "target": "2.12.0",          // the Rust version to migrate TO
    "percent": 0,                 // 0→1→5→20→60→100 over days
    "min_glibc": "2.31",          // measured floor + margin
    "arch": ["x86_64"],
    "rollback_all": false,        // global kill-switch
    "block_below": "0.0.0"        // force-update floor (CI-checked ≤ target)
  }
  ```
- **Bucket** by a stable per-machine seed (see §5 step 3). Rolling `percent` up is
  monotonic (a machine that was in-bucket stays in-bucket).
- **Route *every* "should I migrate" decision through this gate** — not just the
  poller. The manual "Check for updates" menu item and `cmd_check` must consult
  `migration.json` too, or they bypass the canary.
- **CI validation of the manifest:** reject the commit if `target` != root
  `VERSION`, if either isn't canonical dotted-numeric of fixed length, or if
  `block_below > target`. (Empty/`-rc` strings silently disable updates fleet-wide
  via the tuple compare — guard against it.)
- ntfy can't scope, but that's fine: a VERSION bump (R1) notifies everyone to pull
  the *safe Python bridge*; the *Rust* swap is gated entirely by `migration.json`,
  which never fires ntfy. **Hard rule: `VERSION` must not change between R1 and the
  end of the canary** (add a tripwire).

---

## 8. Release ordering (the sequence that must not be reordered)

1. Land all §3 identity edits + §4 CI pin + §5/§6/§7 code on `rust-migration`,
   verify the dev build, and **measure the real shipped-asset glibc floor** by
   downloading a test asset from a *draft/canary* release and `objdump -T`.
2. **Publish the GitHub Release `v2.12.0`** (binary built by `rust-release.yml`).
   Verify end-to-end: `curl -IL releases/latest/download/cui-x86_64-linux` → 200,
   `cui --version` → `2.12.0`, no `rust-dev` strings, floor ≤ target.
3. **Only then** push the R1 bridge commit to `main` (bumps `VERSION` → 2.12.0,
   ships switch-run.sh + migrate.py + watchdog + `.gitignore` + `migration.json`
   at `percent:0`). This fires ntfy once; clients pull the safe Python bridge.
4. Dwell. Then ramp `migration.json.percent` over days (no VERSION change).

Reversing 2↔3 strands the fleet on a tree whose self-update would target a
non-existent asset.

---

## 9. Verification with no telemetry + go/no-go

- **Pre-release matrix** (the dev `[rust]` build is already running here in
  parallel — keep it): GNOME-X11, GNOME-Wayland **with and without** the
  AppIndicator extension, KDE/Plasma (the colleague's KWallet box — test cold login
  / locked wallet / kwalletd5 vs 6), an Ubuntu 20.04 box (glibc floor edge), and an
  **excluded** class (headless/no-D-Bus, no-user-systemd, aarch64) to confirm those
  stay safely on Python.
- **Passive health signals** (no server): GitHub issues, the maintainer's own
  machines, and an **opt-in** heartbeat at most. Crucially, also watch for the
  *silent non-event* — a machine that migrated and then went dark emits nothing;
  the watchdog's auto-restore-to-Python is what protects those.
- **Canary gates** (illustrative — real numbers are your call, §11): `1% → soak →
  5% → 20% → 60% → 100%`, each stage dwelling long enough to surface issues, with
  an explicit abort threshold (e.g. ≥1 credible "tray vanished / not updating"
  report at any gate ⇒ set `percent` back + `rollback_all` + ntfy ping).

---

## 10. Permanent coexistence / the long tail (accepted, by design)

These populations **stay on Python forever** and that is fine — the goal is "no
regression", not "100% Rust":
- never-click-update users with ntfy unreachable;
- dirty-tree / non-git installs (self-update aborts);
- excluded arch / glibc / no-D-Bus / no-user-systemd machines;
- multi-user hosts and root/system installs (each `--user` install decides
  independently; **out of scope for auto-migration unless decided otherwise**).

Therefore: **`main` must remain a runnable Python app indefinitely** (never delete
`claude_usage_indicator.py` / `cui/` / a venv-installable `requirements.txt`), and
the `~/.local/bin/claude-usage-indicator` wrapper keeps working because it execs
`run.sh` (the switch). The Python venv standby on migrated machines is deleted only
by a **local** decision (this machine is currently healthy-on-Rust), never by a
fleet-global cleanup commit.

---

## 11. Decisions only you can make (these gate implementation)

1. **glibc floor + CI build base** — `ubuntu-20.04` (~2.31, simplest) vs
   `cargo-zigbuild`/manylinux (~2.28/2.27, widest reach, more CI work) vs accept
   the high `ubuntu-latest` floor (excludes most older distros).
2. **arch scope** — add `aarch64` before cutover, or leave arm64 on Python.
3. **binary integrity** — publish + verify a SHA256 (and/or minisign/cosign
   signature), or ship with only the current 1 MB size floor.
4. **disclose the swap?** — README/privacy currently say "update checks only fetch
   VERSION"; a Rust binary pulling a ~16 MB ELF from the GitHub release CDN is a new
   network destination. Keep fully silent, or add a one-line changelog/privacy note.
5. **rollout % schedule + dwell + abort criteria** — concrete numbers, given no
   telemetry (what observable trips an abort?).
6. **version-sync mechanism** — `build.rs include_str!("../../VERSION")`
   (recommended) vs a CI sed step.
7. **fail-open vs fail-closed** when `migration.json` is unreachable — recommended:
   **fail-closed for forward migration** (don't migrate) + **fail-safe for
   rollback** (still allow restore), so the kill-switch can't be defeated by a fetch
   failure.
8. **Python-standby retention window** + the concrete condition for a future
   cleanup release to delete the venv.
9. **multi-user / root / non-Debian posture** — in scope or explicitly excluded.

Recommended defaults if you want to move fast: #1 zigbuild→glibc 2.28; #2 x86_64
only (arm64 stays Python); #3 add SHA256 now, signature later; #4 one-line privacy
note (matches the project's transparency ethos); #6 build.rs; #7 as stated.

---

## 12. Net assessment

The architecture (run.sh switch + sibling-dir binary + detached preflight-gated
swap + bash watchdog + permanent Python fallback) makes "strand a user with no
tray" impossible by construction, keeps the cutover imperceptible, and degrades to
"stays on Python" for every machine it can't safely migrate. The remaining real
work is mechanical: the §3 identity edits + dev feature flag, the §4 CI glibc pin +
floor gate, and the new code (`migrate.py`, switch-`run.sh`, the bash watchdog +
timer, the Rust heartbeat, `migration.json` + its gate, the `Uninstall…` menu item).
None of it touches `main` or ships anything until you approve — per the standing
rule, no push to main / no VERSION bump / no Release without your go-ahead.
