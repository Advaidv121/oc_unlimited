"""Windows WARP keeper: enforces TUN mode and auto-repairs WARP connections."""

from __future__ import annotations

import os
import sys
import time

from ouc_common import (
    KEEPER_LOG_FILE,
    SERVICE_PROXY_PORT,
    acquire_mutex,
    append_log,
    ensure_dirs,
    find_warp_cli,
    get_warp_local_v6,
    is_admin,
    is_keeper_paused,
    run_warp,
    warp_status,
)
from ouc_firewall import install_killswitch, is_killswitch_installed
from zen_proxy import start_proxy_server

TICK_SECONDS = 45


def ensure_connected(warp_bin: str | None = None) -> bool:
    """Verify WARP connection based on active scope (isolated proxy or full TUN)."""
    if not warp_bin:
        warp_bin = find_warp_cli()
    if not warp_bin:
        return False

    default_scope = "tun" if sys.platform == "win32" else "isolated"
    scope = default_scope
    try:
        from proxy_chain import get_egress_config
        scope = get_egress_config().get("warp_scope", default_scope)
    except Exception:
        pass

    connected, mode, raw = warp_status(warp_bin)

    # 1. If already connected, DO NOT TOUCH IT!
    # Re-setting protocol, ports or endpoints tears down the adapter and drops active sockets (causing ECONNRESET).
    if connected:
        return True

    raw_lower = (raw or "").lower()

    # 2. If WARP is currently establishing handshake (Connecting / Reconnecting),
    # give it a generous grace window (up to 8s) rather than declaring it dead.
    if "connecting" in raw_lower:
        for _ in range(8):
            time.sleep(1.0)
            connected, mode, raw = warp_status(warp_bin)
            if connected:
                return True
            if "connecting" not in (raw or "").lower():
                break

    # 3. Disconnected state: apply configuration once and connect
    if scope == "isolated":
        if not mode or "proxy" not in mode.lower():
            run_warp(["mode", "proxy"], warp_bin=warp_bin)
            run_warp(["proxy", "port", "40000"], warp_bin=warp_bin)

        run_warp(["connect"], warp_bin=warp_bin)
        for _ in range(8):
            time.sleep(1.0)
            connected, _, raw = warp_status(warp_bin)
            if connected:
                return True
            if "connecting" not in (raw or "").lower() and not connected:
                pass

        # If still not connected after grace window, rotate keys and reset endpoints (NO nuclear registration deletion!)
        append_log(KEEPER_LOG_FILE, "isolated tunnel disconnected — rotating keys & reconnecting")
        run_warp(["tunnel", "endpoint", "reset"], warp_bin=warp_bin)
        run_warp(["tunnel", "rotate-keys"], warp_bin=warp_bin)
        run_warp(["mode", "proxy"], warp_bin=warp_bin)
        run_warp(["proxy", "port", "40000"], warp_bin=warp_bin)
        run_warp(["connect"], warp_bin=warp_bin)

        for _ in range(8):
            time.sleep(1.0)
            connected, _, _ = warp_status(warp_bin)
            if connected:
                return True

        # Only register new if registration is explicitly missing or corrupt
        raw_check = (raw or "").lower()
        if "registration missing" in raw_check or "not registered" in raw_check:
            append_log(KEEPER_LOG_FILE, "registration missing — registering fresh WARP client")
            run_warp(["registration", "new"], warp_bin=warp_bin)
            run_warp(["mode", "proxy"], warp_bin=warp_bin)
            run_warp(["proxy", "port", "40000"], warp_bin=warp_bin)
            run_warp(["connect"], warp_bin=warp_bin)
            time.sleep(3)
            connected, _, _ = warp_status(warp_bin)

        return connected

    # Full System TUN mode (only when scope is explicitly set to full TUN)
    if not mode or "warp" not in mode.lower():
        run_warp(["mode", "warp"], warp_bin=warp_bin)

    run_warp(["connect"], warp_bin=warp_bin)
    for _ in range(8):
        time.sleep(1.0)
        connected, _, raw = warp_status(warp_bin)
        if connected:
            return True
        if "connecting" not in (raw or "").lower() and not connected:
            pass

    append_log(KEEPER_LOG_FILE, "TUN tunnel disconnected — rotating keys & reconnecting")
    run_warp(["tunnel", "endpoint", "reset"], warp_bin=warp_bin)
    run_warp(["tunnel", "rotate-keys"], warp_bin=warp_bin)
    run_warp(["mode", "warp"], warp_bin=warp_bin)
    run_warp(["connect"], warp_bin=warp_bin)

    for _ in range(8):
        time.sleep(1.0)
        connected, _, _ = warp_status(warp_bin)
        if connected:
            return True

    raw_check = (raw or "").lower()
    if "registration missing" in raw_check or "not registered" in raw_check:
        append_log(KEEPER_LOG_FILE, "registration missing — registering fresh TUN client")
        run_warp(["registration", "new"], warp_bin=warp_bin)
        run_warp(["mode", "warp"], warp_bin=warp_bin)
        run_warp(["connect"], warp_bin=warp_bin)
        time.sleep(4)
        connected, _, _ = warp_status(warp_bin)

    return connected


def main() -> int:
    ensure_dirs()
    mutex = acquire_mutex("OpencodeUnlimitedKeeper")
    if mutex is None:
        return 0

    warp_bin = find_warp_cli()
    append_log(KEEPER_LOG_FILE, f"keeper up (tick={TICK_SECONDS}s, warp={'found' if warp_bin else 'none'})")

    # 1. ALWAYS start the desktop/service proxy on port 41987 first so OpenCode can route in all modes
    proxy_srv = None
    try:
        proxy_srv, bound_port, edge = start_proxy_server(SERVICE_PROXY_PORT)
        append_log(KEEPER_LOG_FILE, f"desktop/service proxy listening on 127.0.0.1:{bound_port} (edge={edge})")
    except Exception as exc:
        append_log(KEEPER_LOG_FILE, f"service proxy bind failed on port {SERVICE_PROXY_PORT}: {exc}")

    # On startup, allow network interface initialization if booting and warp is installed
    if warp_bin:
        for _ in range(6):
            rc, _ = run_warp(["status"], warp_bin=warp_bin, timeout=8.0)
            if rc == 0:
                break
            time.sleep(8)

    while True:
        try:
            mode = "warp"
            try:
                from proxy_chain import get_egress_config
                mode = get_egress_config().get("mode", "warp")
            except Exception:
                pass

            if mode in ("warp", "clean"):
                if warp_bin and not is_keeper_paused():
                    ensure_connected(warp_bin)
                    if is_admin() and (
                        os.environ.get("OUC_FIREWALL_KILLSWITCH") == "1"
                        or is_killswitch_installed()
                    ):
                        if not is_killswitch_installed():
                            install_killswitch()
            elif mode == "tor":
                # Ensure Tor daemon is healthy if managed
                try:
                    from tor_manager import get_active_tor_target, is_portable_tor_running, start_portable_tor
                    if not get_active_tor_target() and is_portable_tor_running():
                        start_portable_tor()
                except Exception:
                    pass
        except Exception as exc:
            append_log(KEEPER_LOG_FILE, f"keeper error: {exc}")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
