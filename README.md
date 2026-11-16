# PLEASE GIVE PROPER CREDITS. JUST DONT REBRAND AND LAUNCH IT AS YOURS.
# NEW VERSION-2.4.2 (Fixed:503,ECONNRESET, keeper flapping, quota resets, drafts.sqlite unlock, WireGuard enforcement, auto-resume and some other BUGS)
# Opencode-Unlimited-Limit-Bypass

[![Version](https://img.shields.io/badge/Version-v2.4.2-22c55e?style=for-the-badge&logo=semver)](https://github.com/ShadowySphinX/Opencode-Unlimited-Limit-Bypass-)
[![Author](https://img.shields.io/badge/Author-Avatar%20%28ShadowySphinX%29-22d3ee?style=for-the-badge&logo=github)](https://github.com/ShadowySphinX)
[![Repository](https://img.shields.io/badge/GitHub-Opencode--Unlimited-blue?style=for-the-badge&logo=github)](https://github.com/ShadowySphinX/Opencode-Unlimited-Limit-Bypass-)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)

Automatic rate-limit rotator, multi-egress proxy router, and autonomous session auto-resume for **OpenCode Desktop** and **OpenCode CLI**.

![OpenCode Unlimited Dashboard](assets/ui-preview.png)

OpenCode's free-tier limits are applied to the inbound public IP address (`/64` prefix on IPv6, `/32` on IPv4). The rotator changes that address and clears the matching client-side lockout.

This tool monitors OpenCode logs for quota and rate-limit errors, cycles the active identity (fresh IPv6 `/64`, Tor circuit, or proxy pool node), and tries to continue the running session without starting a new chat.

---

## Table of Contents
1. [What's New in v2.4.2](#whats-new-in-v242)
2. [Prerequisites & Downloads](#prerequisites--downloads-what-you-need)
3. [Quick Start (Windows)](#quick-start-windows)
4. [Recommended Settings for Every Method](#recommended-settings-for-every-method)
5. [How Limit Reset Works on Existing Chats](#how-limit-reset-works-on-existing-chats)
6. [Restricted Regions Guide (Russia, Iran, China)](#restricted-regions-guide-russia-iran-china)
7. [Troubleshooting Playbook ("What to do when something happens")](#troubleshooting-playbook-what-to-do-when-something-happens)
8. [Routing Architecture](#routing-architecture)
9. [Linux Setup (Headless & VPS)](#linux-setup-headless--vps)
10. [Building from Source](#building-from-source)

---

## What's New in v2.4.2

* **Connection Recovery & Diagnostics (current build):**
  * The local proxy now fails over to another Cloudflare edge in seconds instead of stalling on one dead address.
  * A dropped WARP tunnel or proxy route is recorded, repaired, and surfaced in the dashboard instead of returning an empty `503`.
  * **Auto-Repair** is on by default: it restarts a stopped proxy, reconnects WARP, or changes edge without changing your selected mode, clearing chats, or restarting OpenCode. It respects workers you deliberately turned off.
  * A new **Proxy** log tab shows edge failover, dropped streams, direct fallbacks, and tunnel repairs for real troubleshooting.
  * A direct fallback is still allowed in Standard mode so OpenCode stays usable, but the health panel now says **Using a direct connection** and offers a one-click repair.
* **WARP Keeper Stability:**
  * A connected tunnel is left alone unless the selected WARP scope changed; the keeper repairs disconnected or error states.
  * Routine repairs renew the WireGuard session keys instead of deleting the device registration.
  * In-flight `Connecting` / `Reconnecting` states get a longer grace period before a repair starts.
  * Non-destructive key rotation (`tunnel rotate-keys`) is used for quota rotation.
* **Existing Chat Auto-Unlock & History Preservation:**
  * Removes client-side rate limit lockouts (`free-limit`, `go-upsell`) from `%APPDATA%\ai.opencode.desktop\drafts.sqlite` after a successful rotation.
  * Leaves the chat history and session tables untouched.
  * Reloads the Desktop window and sends `continue` when the window can be focused safely; otherwise it falls back to a CLI resume.
* **Streaming Proxy:**
  * TCP keepalive (`SIO_KEEPALIVE_VALS` on Windows, `TCP_KEEPIDLE` on Linux) and a 15-minute idle ceiling keep long model responses from stalling.
* **Detached Desktop Launching:**
  * OpenCode Desktop now launches as an independent detached process (`os.startfile`), so it stays open even if the launcher or terminal is closed.

---

## Prerequisites & Downloads (What You Need)

Install these components before setup:

| Component | Purpose | Requirement | Official Link |
| :--- | :--- | :---: | :--- |
| **OpenCode (Desktop or CLI)** | The AI coding assistant | **Required** | [Download OpenCode](https://opencode.ai/) or [GitHub Releases](https://github.com/opencode-ai/opencode) |
| **Cloudflare WARP** | Egress IPv6 `/64` provider | **Required** (for WARP modes) | [Download Cloudflare WARP (1.1.1.1)](https://one.one.one.one/) |
| **Microsoft WebView2 Runtime** | Powers the modern dashboard UI | **Pre-installed** on Win 10/11 | [WebView2 Evergreen Bootstrapper](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) |
| **Python 3.10+** | Only needed if running from source | Optional (binary is standalone) | [Download Python](https://www.python.org/downloads/) |
| **Tor Expert Bundle** | Decentralized IP circuit rotation | Optional (for Tor mode) | [Tor Project Downloads](https://www.torproject.org/download/tor/) *(or use in-app 1-click download)* |
| **xdotool** | Linux window focus & reload | Optional (Linux GUI users) | `sudo apt install xdotool` / `sudo dnf install xdotool` |

---

## Quick Start (Windows)

> You do **not** need Python installed. Everything is bundled into a single standalone program.

1. **Download & Run:**
   * Download **`OpencodeUnlimited-Multi.exe`** from the repository root, or clone the source and build it.
   * Double-click to launch the dashboard.
2. **Verify Protection Controls (Left Panel):**
   * Ensure these switches are **ON** (they are on by default):
     * **WARP Keeper:** ON
     * **Zen Guard:** ON
     * **Desktop App Proxy:** ON
     * **Auto-Resume on Bypass:** ON (continues the session after a rotation)
3. **Use OpenCode Normally:**
   * Open OpenCode Desktop or run `opencode` in your command prompt/terminal.
   * Work normally. When you hit a free-tier rate limit, the app rotates the egress address and tries to resume the session, which avoids waiting for the quota timer.

---

## Recommended Settings for Every Method

Select the method that best matches your network environment:

### Method 1: Cloudflare WARP (Default)
* **Best for:** General use with IPv6 `/64` rotation.
* **Recommended Settings:**
  * **Scope (Windows):** `Full System TUN` (keeps the WARP adapter active; recommended if App-Only mode shows no adapter).
  * **Scope (Linux / VPS):** `App Only (Isolated)` on port `40000` (leaves the system's normal routing and SSH access in place).
  * **Protocol:** `WireGuard` (MASQUE uses UDP 443, which some ISPs throttle or block).
  * **Switches:** Turn ON **WARP Keeper**, **Zen Guard**, **Auto-Resume**, and **Auto-Repair**.
  * **Strict mode:** Leave it OFF for normal use. It is only for users who prefer a failed request over a direct fallback.

### Method 2: Clean WireGuard Endpoints (Best for ISP Throttling / Restricted Countries)
* **Best for:** Users in Russia, Iran, China, or university/corporate networks where Cloudflare's default Anycast IPs are throttled or blocked (`CF_HAPPY_EYEBALLS_MITM_FAILURE`).
* **Recommended Settings:**
  1. Open the dashboard and click the **Clean Endpoint** tab.
  2. Click **Ping Test** (benchmarks the built-in Anycast endpoint list).
  3. Select the node with the lowest ping (e.g. `162.159.192.1:2408` or port `500` / `4500`).
  4. Click **Apply Endpoint**. WireGuard connects to the selected endpoint.

### Method 3: Tor Network (Best for Strict Censorship & Maximum Privacy)
* **Best for:** Users whose networks completely block UDP / WireGuard traffic, or who require decentralized exit nodes.
* **Recommended Settings:**
  1. Click the **Tor Network** tab on the dashboard.
  2. Click **Start Tor Daemon** (launches an isolated portable Tor daemon on `127.0.0.1:9050` with control port `9051`).
  3. If you already run **Tor Browser**, the app automatically hooks into port `9150`.
  4. On rate limits, a `SIGNAL NEWNYM` command requests a new circuit and waits for a different IPv4 exit address.

### Method 4: Custom Proxy Pool (Clash / V2Ray / Xray / sing-box / Residential)
* **Best for:** Users with established proxy subscriptions or local proxy clients.
* **Recommended Settings:**
  1. Click the **Custom Proxy** tab.
  2. Paste your local proxy address (e.g. `127.0.0.1:7890` or `127.0.0.1:10808`).
  3. Format supported: Single address or multi-line pool (separated by newlines, commas, or semicolons).
  4. Choose protocol: **SOCKS5** or **HTTP**.
  5. On a rate limit, the guard switches to the next pool node and verifies its exit IP before reporting success.

---

## How Limit Reset Works on Existing Chats

A common concern is whether rotating limits requires starting a brand-new chat. It does not. The reset clears the lockout and continues the existing chat.

```
[ User Active Chat / Session ]
          │
          ▼
┌──────────────────────────────┐
│  Layer 1: Client SQLite Lock │  ──> Stored locally in %APPDATA%\ai.opencode.desktop\drafts.sqlite
│       ('free-limit' key)     │      Disables the UI prompt bar & displays the red upsell banner.
└──────────────┬───────────────┘
               │ (Automatically purged by Zen Guard / Deep Reset)
               ▼
┌──────────────────────────────┐
│  Layer 2: Upstream Cloudflare│  ──> Evaluated per egress IP (CF-Connecting-IP).
│       Server Rate Limit      │      Applied to the egress IP, not to the conversation ID.
└──────────────────────────────┘
```

1. **Server-Side Quota (IP-Based):** OpenCode's backend applies free-tier limits to the incoming public IP address. Rotating the IPv6 `/64` presents a different address, which is the behavior the rotator relies on.
2. **Client-Side Lockout Removal:** When OpenCode Desktop hits a rate limit, the local Electron app writes a lockout timestamp into `%APPDATA%\ai.opencode.desktop\drafts.sqlite` (`state` table). Zen Guard deletes this key (`DELETE FROM state WHERE key IN ('free-limit', 'go-upsell')`).
3. **History Preservation:** The reset only touches those lockout keys. Chat history, system prompts, code blocks, and context in the `document` and `session_v2` tables are left unchanged.
4. **Window Reload & Resume:** The app reloads OpenCode Desktop, which refreshes the view and clears the banner, then sends `continue{ENTER}`. If the window cannot be focused safely, it falls back to a CLI resume.

---

## Troubleshooting Playbook ("What to do when something happens")

### 1. What to do when you hit a rate limit wall
* **Automatic Behavior:** With Zen Guard and Auto-Resume on, the guard rotates the egress address, clears the client lockout, and attempts to resume the session. A rotation usually completes within a minute.
* **Manual Override:** To rotate now, click the hop button on the dashboard. Its label follows the active mode: **Rotate /64 Now (Hop)** for WARP, **Rotate Circuit (Hop)** for Tor, and **Rotate Proxy (Hop)** for a custom pool.

### 2. What to do if OpenCode shows "You've reached your free usage limit" banner in an existing chat
* **Cause:** The client-side Electron app is still holding the lockout timestamp in `drafts.sqlite`.
* **Fix:** Click **Deep Reset** on the dashboard, or press `Ctrl+R` in OpenCode Desktop. This usually clears the banner and unlocks the input bar.

### 3. What to do if you get `Connection lost while reading the response: ECONNRESET`
* **Cause:** Earlier 2.4.x keepers could change the WARP protocol on every 45-second tick, which tore down the virtual network adapter mid-stream while reasoning models were generating.
* **Fix in v2.4.2:** 
  1. Update to **v2.4.2** (the keeper no longer changes protocol or mode on every tick; it leaves a connected tunnel alone unless the selected scope changed).
  2. Ensure you are not running third-party VPNs (NordVPN, ExpressVPN) alongside WARP, as they compete for the Windows Wintun driver.
  3. Ensure **Desktop App Proxy** is on (15-minute idle ceiling with TCP keepalive).
  4. If it still happens, open the **Proxy** log tab. Repeated `reset` or `edge_fail` lines mean the pinned edge is unhealthy; press **Re-roll** or let Auto-Repair change it.

### 3.1 What to do if OpenCode returns `503 Service Unavailable`
* **Cause:** The selected Cloudflare edge or WARP tunnel stopped answering. A stale edge, ISP UDP drop, or a proxy that is not running can all produce a 503.
* **Fix:**
  1. Open the **Proxy** log tab and check the last event.
  2. Press **Fix WARP (Reset Keys)** if the tunnel is down, or **Re-roll** if only the edge is failing.
  3. Leave **Auto-Repair** on. It retries safe fixes with cooldowns instead of looping.
  4. If the health panel says **Using a direct connection**, the app kept OpenCode online through a fallback; press **Repair route** to put it back on your selected egress.

### 4. What to do if WARP keeps reconnecting or looping (`Connecting...`)
* **Cause:** ISP UDP packet drops or corrupt local WireGuard keys.
* **Fix:**
  1. Click **Fix WARP (Reset Keys)** on the dashboard. This rotates cryptographic keys without deleting your device identity.
  2. Switch to the **Clean Endpoint** tab, run a **Ping Test**, and click **Apply Endpoint** to bind to a reliable IP/port (e.g. port `2408` or `500`).

### 5. What to do if WARP shows "Network: unstable"
* **Notice:** Cloudflare WARP can report `Network: unstable` when latency is high or during initial packet bursts. It does not necessarily mean your internet is down.
* In v2.4.2, the app treats "unstable" as healthy traffic. Do not disconnect or delete your registration.

### 6. What to do if you get `CF_HAPPY_EYEBALLS_MITM_FAILURE`
* **Cause:** MASQUE protocol (UDP 443) blocked by ISP or stale Anycast routing.
* **Fix:** Click **Fix WARP (Reset Keys)** on the dashboard. The app automatically locks the tunnel into WireGuard mode, which bypasses MASQUE MITM checks.

### 7. What to do if port 42914 or 41987 is already in use
* **Cause:** OpenCode's managed service (42914) or this proxy (41987) did not release its socket, usually after a forced shutdown.
* **Fix:** Click **Fix Port Conflict** on the dashboard. The app stops and restarts the OpenCode service through its own CLI, then re-wires and verifies the local proxy.

### 8. Windows SmartScreen or antivirus warning
* **Cause:** PyInstaller one-file executables are occasionally flagged by heuristics or reputation checks. This is more common for unsigned binaries.
* **What the build does:** The build uses `--noupx` and embeds a version resource to reduce avoidable heuristic triggers.
* **Options:** Build from source, verify the downloaded file's SHA-256 hash, or sign the executable with your own code-signing certificate (`OUC_SIGN_PFX` + `OUC_SIGN_PASSWORD`, or `OUC_SIGN_THUMBPRINT`). A trusted signature is the reliable way to clear SmartScreen reputation warnings.

---

## Routing Architecture

```
[ OpenCode Desktop / CLI ]
            │
            ▼ (HTTP / HTTPS via service.json)
   [ 127.0.0.1:41987 (Zen Proxy with TCP KeepAlive) ]
            │
    ────────┴───────────────────────────────────────────────────────
    │ Active Egress Mode Selection                                  │
    ─────────────────────────────────────────────────────────────────
    ├── [1] WARP (Full TUN Mode)    ──> Virtual Adapter (Wintun)    ──> System-wide Cloudflare VPN
    ├── [2] WARP (Isolated Mode)    ──> 127.0.0.1:40000 (SOCKS5)    ──> Cloudflare Edge [Fresh /64 IPv6]
    ├── [3] Clean Endpoint          ──> Clean WireGuard IP:2408     ──> Selected Anycast Edge
    ├── [4] Tor Network             ──> 127.0.0.1:9050 / 9150       ──> Tor 3-Hop Circuit [Fresh IPv4]
    └── [5] Custom Proxy Pool       ──> Node #1 -> Node #2 -> ...   ──> Verified Upstream Nodes
```

---

## Linux Setup (Headless & VPS)

Run the automated one-shot installer:
```bash
bash install.sh
```

* **Compatibility:** Debian, Ubuntu, Fedora, Arch Linux, NixOS, and openSUSE.
* **Systemd Daemons:** Installs `opencode-unlimited-keeper.service` (root) and `opencode-unlimited-guard.service` (user).
* **Cloud VPS Safe:** Adds firewall rules that allow SSH (port 22 and custom ports) and keeps WARP in isolated proxy mode so the system's normal routing stays in place.
* **Existing Chat Unlock:** Clears the lockout keys from `~/.config/ai.opencode.desktop/drafts.sqlite` and runs `nohup opencode run --continue "continue" &` after a successful rotation.

---

## Building from Source

```bash
git clone https://github.com/ShadowySphinX/Opencode-Unlimited-Limit-Bypass-.git
cd Opencode-Unlimited-Limit-Bypass-
pip install -r requirements.txt
```

To compile the standalone Windows executable:
```powershell
powershell -ExecutionPolicy Bypass -File .\build_multi_exe.ps1
```

The build disables UPX, embeds file version metadata, and publishes the result with a retrying file swap so an open executable produces a clear error instead of a Windows `WinError 5`. To sign the result, set `OUC_SIGN_PFX` and `OUC_SIGN_PASSWORD` (or `OUC_SIGN_THUMBPRINT`) before building; `signtool.exe` must be on `PATH`.

---

## Author & Credits

- Built & Maintained by **[Avatar (ShadowySphinX)](https://github.com/ShadowySphinX)**.
- Official Project Repository: [ShadowySphinX/Opencode-Unlimited-Limit-Bypass-](https://github.com/ShadowySphinX/Opencode-Unlimited-Limit-Bypass-)

---

## License

MIT License. Copyright (c) 2026 Avatar (ShadowySphinX).
