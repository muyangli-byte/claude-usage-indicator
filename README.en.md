# Claude Usage Indicator

[中文](README.md) | **English**

Show your **claude.ai usage** live in the Linux top bar (system tray): current session (5‑hour window), weekly limits (All models / Sonnet / Opus), and each one's reset time.

```
Cur 39% 5:10pm | All 5% Mon 7am
```

- ✅ **No open web page, no Tampermonkey, no browser extension**
- ✅ A single background Python process, auto‑starts on login
- ✅ Reads your login from Chrome automatically — virtually zero maintenance

---

## How it works

1. Use [`browser_cookie3`](https://github.com/borisbabic/browser_cookie3) to read `sessionKey` from Chrome's cookie store automatically.
2. Use [`curl_cffi`](https://github.com/lexiforest/curl_cffi) to impersonate Chrome's TLS fingerprint and call claude.ai's internal usage API directly
   (plain `requests`/`curl` get blocked by Cloudflare at the TLS‑fingerprint layer, so impersonation is required).
3. Parse the returned JSON and update the GTK AppIndicator in the top bar.

Because it reads a **JSON API** rather than scraping the page DOM, claude.ai redesigning its web UI doesn't affect this tool.

### Refresh rate (adaptive)

claude.ai has no push channel, so we must poll. The tool **adapts its frequency**:

- While you're **using Claude and the numbers change**, it polls fast (~every **5s**) ≈ near‑real‑time.
- After a long period with **no change**, it **backs off** (10→20→…→90s cap) to spare the API.
- On any change it snaps back to fast polling; the **Refresh now** menu item forces an immediate poll.
- The reset time shown is the **raw reset timestamp from the API** (`resets_at`, only converted to your local timezone) — **no "time remaining" countdown is computed**.

### Health monitoring (heartbeat)

Every poll is a health check. On a problem it pops a **desktop notification** and marks the top bar with `⚠`:

- **API schema change** (`schema`): a missing/retyped field → "API schema changed, needs an update", and the raw response is saved to `diagnostics/`.
- **Blocked by Cloudflare / login expired / network error**: each has its own message.
- Notifies **immediately** on entering a bad state; while a problem persists it re‑notifies **every 30 minutes** (so you don't miss it).

---

## Prerequisites

- **Debian / Ubuntu** (system deps installed via apt)
- Logged into `https://claude.ai` in **Chrome** (no need to keep a tab open; Chrome doesn't even need to be running, as long as the cookie hasn't expired)
- A desktop with a system tray. **GNOME doesn't show tray icons by default** and needs the AppIndicator extension:

  ```bash
  sudo apt-get install gnome-shell-extension-appindicator
  gnome-extensions enable ubuntu-appindicators@ubuntu.com \
    || gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
  ```

  Log out and back in afterwards. **Ubuntu usually has it enabled already — no action needed.**

---

## Install

### Option 1: one‑liner (fastest)

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/install.sh | bash
```

### Option 2: git clone, then install (use this if you want to read the code first — also more robust)

```bash
git clone https://github.com/muyangli-byte/claude-usage-indicator.git
cd claude-usage-indicator
less install.sh        # optional: inspect the script before running it
./install.sh           # same as `bash install.sh`; prompts for sudo password once
```

> Running from a **local file** is more robust than `curl | bash`: it can't hit the "apt reads the piped script's stdin and truncates it" problem.

Either way, `install.sh` will: install system deps (needs sudo) → deploy the **latest `main`** to `~/.local/share/claude-usage-indicator` → create an isolated venv and install Python deps → register and start a systemd user service → install the `claude-usage-indicator` command.

> `install.sh` always deploys the latest `main` from GitHub, so the clone is mainly for reading the code; the actual running copy lives in `~/.local/share/...`, independent of your clone.

After installing, **log into `claude.ai` in Chrome** (no need to keep a tab open). The top bar shows usage within seconds. Verify a fetch immediately with `claude-usage-indicator --once`.

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/uninstall.sh | bash
```

To wipe everything (including the config dir `config.json`; note piping args needs `bash -s --`):

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/uninstall.sh | bash -s -- --purge
```

If you installed via git clone, you can also run `./uninstall.sh` (or `./uninstall.sh --purge`) from the clone.

Diagnostics live inside the install dir and are removed by default; system libraries are kept by default (they may be used by other programs).

## Update

**Easiest: click "Update now" in the tray menu** (pulls the latest code + deps in the background and restarts automatically — no terminal, no sudo).

Or in a terminal:

```bash
claude-usage-indicator --self-update   # lightweight: git+pip+restart, no sudo (same as "Update now")
claude-usage-indicator --update        # re-run the installer, updating system libs too (needs sudo)
claude-usage-indicator --check         # just check whether a new version exists
```

The tool compares versions against the repo daily; when a new version exists it's shown in the tray and a desktop notification — just click "Update now". Use `--self-update`/"Update now" normally; only use `--update` when an update needs new system libraries.

---

## Tray menu

| Item | Description |
|---|---|
| Current session / All models / Sonnet / Opus | Usage percentage and reset time for each |
| Status | Current status (ok / login expired / schema changed …) |
| Refresh now | Fetch once immediately |
| Check for updates | Check for a new version now |
| Update now | **Only appears after Check for updates finds a newer version**; click it to update and auto‑restart (no sudo) |
| Open usage page | Open the claude.ai usage page |
| Notification language | Switch the desktop **notification** language (中文 / English); the menu itself stays English |

## Command line

```bash
claude-usage-indicator --once     # fetch once and print (debug)
claude-usage-indicator --version
```

---

## Troubleshooting

When the top bar shows `⚠`, check the **Status** line in the tray or the desktop notification:

| Status | Meaning | What to do |
|---|---|---|
| login expired (`auth`) | sessionKey invalid | Re‑login to claude.ai in Chrome; recovers automatically |
| Cloudflare blocked (`cloudflare`) | TLS impersonation broke | Usually needs a tool update: `--update`; see the diagnostics below |
| schema changed (`schema`) | usage API fields changed | Needs a tool update; the raw response is saved so the parser can be fixed |
| cookie read failed (`cookie`) | can't decrypt Chrome cookies | Make sure you're logged in and the keyring is unlocked |
| network / HTTP error | transient | Retries automatically |

Failed responses are saved to `~/.local/share/claude-usage-indicator/diagnostics/` (last 20 only) to help diagnose or file an issue.

View logs:

```bash
journalctl --user -u claude-usage-indicator.service -f
```

---

## Privacy & security

- `sessionKey` is only used in memory; **the tool never writes it to any file**.
- No telemetry. Usage requests go only to `claude.ai`; the daily update check fetches this repo's `VERSION`; `claude-usage-indicator --update` re‑downloads the installer from GitHub and installs deps from PyPI.

## Limitations

- claude.ai's "Daily included routine runs" and plan name (e.g. Team) aren't in the usage API, so this tool doesn't show them.
- Currently Debian/Ubuntu only.

## License

[MIT](LICENSE)
