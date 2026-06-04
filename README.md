# Claude Usage Indicator

**中文** · [English](#english)

在 Linux 顶栏（系统托盘）实时显示 **claude.ai 用量**：当前会话（5 小时窗口）与本周限额，以及各自的重置时间。一个后台 Python 进程，开机自启，自动从浏览器读取登录态。

```
Cur 39% 2h56m | All 5% Mon 7am
```

## 托盘菜单

顶栏显示上面那行；点开菜单大致如下：

```
Current session | Resets in 1 hr 27 min
▕████████████░░░░░░░░░░░░▏  49%
All models | Resets Mon 7:00 AM
▕███████████░░░░░░░░░░░░░▏  45%
Status: ok | Last updated: 0s ago
──────────
More ▸
   Refresh now
   Update now                  （仅有新版时出现）
   Check for updates
   Open Claude Usage page
   Send feedback / report issue
   Notification language: English
   About (GitHub)  vX.Y.Z
   ──────────
   Uninstall…
```

- **用量行**：每个限额占两行——`名称 | 重置时间`，下面是进度条 + 百分比。`Sonnet only` / `Opus only` 用过后才出现。
- **Status**：正常显示 `ok` + 上次更新时间；出故障时变 ⚠️，并在顶层多出 **Show error details**（点开把故障详情弹成通知，可一键复制）。
- **More ▸**：其余所有操作都收在这里（鼠标悬停展开）。
- 命令行：`claude-usage-indicator --doctor`（自检登录态/凭证，不泄露密钥）、`--once`（拉取一次并打印，调试用）。

## 安装

**前置**：Debian / Ubuntu 桌面；已在 **Chrome / Chromium / Brave / Edge** 任一里登录过 `https://claude.ai`（无需常开标签页）。

一行命令即可（交互式：选语言 → 检查环境 → 装依赖 → 验证登录态 → 启动服务）：

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/install.sh | bash
```

几秒后顶栏出现用量。想先看代码再装：`git clone` 仓库后运行 `./install.sh`。

> **GNOME 默认不显示托盘图标**，需要 AppIndicator 扩展（Ubuntu 通常已默认启用）。若看不到图标：
> ```bash
> sudo apt-get install -y gnome-shell-extension-appindicator   # 然后注销重登一次
> ```

## 更新

托盘菜单或「发现新版本」通知里点 **Update now** 一键更新（后台 git + pip + 重启，无需 sudo）。有新版会通过桌面通知即时提示。也可在终端：

```bash
claude-usage-indicator --self-update   # 同 Update now（无需 sudo）
claude-usage-indicator --update        # 连系统库一起更新（需 sudo，极少需要）
claude-usage-indicator --check         # 只检查有没有新版
```

## 卸载

托盘菜单点 **Uninstall**，或：

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/uninstall.sh | bash               # 保留配置
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/uninstall.sh | bash -s -- --purge  # 连配置一起删
```

## 故障排查

顶栏出现 ⚠️ 时，看托盘 **Status** 行或桌面通知：

- **登录已过期**：在浏览器重新登录 claude.ai，自动恢复。
- **读不到登录态**（钥匙环锁着，KDE 等常见）：解锁系统钥匙环，或在 `~/.config/claude-usage-indicator/config.json` 手动填凭证兜底，然后 `systemctl --user restart claude-usage-indicator.service`：
  ```json
  {"session_key": "sk-ant-sid02-…", "org_id": "<lastActiveOrg 的值>"}
  ```
- 看日志：`journalctl --user -u claude-usage-indicator.service -f`

## 工作原理

1. 用 [`browser_cookie3`](https://github.com/borisbabic/browser_cookie3) 从浏览器 cookie 自动读取 `sessionKey`（会扫描所有浏览器 profile）。
2. 用 [`curl_cffi`](https://github.com/lexiforest/curl_cffi) 伪装 Chrome 的 TLS 指纹，直连 claude.ai 的内部用量 **JSON 接口**（普通 `requests`/`curl` 会被 Cloudflare 在指纹层拦截）。
3. 解析 JSON、更新 GTK 顶栏。**自适应轮询**：用量在变时快轮询（约 5s）≈ 准实时；长时间无变化自动退避（最高 90s）。每次轮询兼做健康检查，出问题就弹通知并标 ⚠️。

因为读的是 JSON 接口而非抓网页 DOM，claude.ai 改版网页不影响本工具。`sessionKey` 只在内存中使用、**不写入任何文件**；不上报任何遥测，请求只发往 `claude.ai`，更新检查只拉本仓库的 `VERSION`。

## License

[MIT](LICENSE)

<br>

---

<a id="english"></a>

# Claude Usage Indicator — English

[中文](#claude-usage-indicator) · **English**

Show your **claude.ai usage** live in the Linux top bar (system tray): the current session (5‑hour window) and weekly limits, with each one's reset time. A single background Python process, auto‑starts on login, reads your login from the browser automatically.

```
Cur 39% 2h56m | All 5% Mon 7am
```

## Tray menu

The top bar shows the line above; opening the menu looks roughly like:

```
Current session | Resets in 1 hr 27 min
▕████████████░░░░░░░░░░░░▏  49%
All models | Resets Mon 7:00 AM
▕███████████░░░░░░░░░░░░░▏  45%
Status: ok | Last updated: 0s ago
──────────
More ▸
   Refresh now
   Update now                  (only when an update exists)
   Check for updates
   Open Claude Usage page
   Send feedback / report issue
   Notification language: English
   About (GitHub)  vX.Y.Z
   ──────────
   Uninstall…
```

- **Usage rows:** two lines per limit — `name | reset time`, then a progress bar + percentage. `Sonnet only` / `Opus only` appear once you've used them.
- **Status:** shows `ok` + last-updated time normally; on failure it turns ⚠️ and a **Show error details** item appears at the top level (pops the details as a notification you can copy).
- **More ▸:** everything else lives here (hover to expand).
- Command line: `claude-usage-indicator --doctor` (self-check login/credentials, no secrets leaked), `--once` (fetch once and print, for debugging).

## Install

**Requirements:** a Debian / Ubuntu desktop; logged into `https://claude.ai` in **Chrome / Chromium / Brave / Edge** (no need to keep a tab open).

One line (interactive: pick language → check environment → install deps → verify login → start):

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/install.sh | bash
```

Your usage shows up in the top bar within seconds. To read the code first: `git clone` the repo and run `./install.sh`.

> **GNOME doesn't show tray icons by default** — it needs the AppIndicator extension (usually on in Ubuntu). If you don't see the icon:
> ```bash
> sudo apt-get install -y gnome-shell-extension-appindicator   # then log out and back in
> ```

## Update

Click **Update now** in the tray menu or the "Update available" notification (background git + pip + restart, no sudo). New versions are announced via a desktop notification. Or in a terminal:

```bash
claude-usage-indicator --self-update   # same as Update now (no sudo)
claude-usage-indicator --update        # also refresh system libs (needs sudo; rarely needed)
claude-usage-indicator --check         # just check for a new version
```

## Uninstall

Click **Uninstall** in the tray menu, or:

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/uninstall.sh | bash               # keep config
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/uninstall.sh | bash -s -- --purge  # also remove config
```

## Troubleshooting

When the top bar shows ⚠️, check the **Status** row or the notification:

- **Login expired:** re-login to claude.ai in your browser; it recovers automatically.
- **Can't read login** (keyring locked, common on KDE): unlock your system keyring, or put credentials in `~/.config/claude-usage-indicator/config.json` as a fallback and `systemctl --user restart claude-usage-indicator.service`:
  ```json
  {"session_key": "sk-ant-sid02-…", "org_id": "<the lastActiveOrg value>"}
  ```
- Logs: `journalctl --user -u claude-usage-indicator.service -f`

## How it works

1. [`browser_cookie3`](https://github.com/borisbabic/browser_cookie3) reads `sessionKey` from the browser's cookie store automatically (scans every browser profile).
2. [`curl_cffi`](https://github.com/lexiforest/curl_cffi) impersonates Chrome's TLS fingerprint and calls claude.ai's internal usage **JSON API** directly (plain `requests`/`curl` get blocked by Cloudflare at the fingerprint layer).
3. Parse the JSON and update the GTK top bar. **Adaptive polling:** fast (~5s) while the numbers change ≈ near‑real‑time, backing off (up to 90s) when idle. Every poll doubles as a health check and pops a notification with a ⚠️ on problems.

Because it reads a JSON API rather than scraping the page, claude.ai redesigning its web UI doesn't affect this tool. `sessionKey` is used in memory only and **never written to any file**; no telemetry, requests only go to `claude.ai`, and update checks only fetch this repo's `VERSION`.

## License

[MIT](LICENSE)
