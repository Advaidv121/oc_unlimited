"""Watches OpenCode's log for quota walls and rotates the active egress."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Ensure local script and bundled directories are resolvable
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
_SCRIPTS = Path(os.environ.get("LOCALAPPDATA", "")) / "opencode-unlimited" / "scripts"
if _SCRIPTS.is_dir() and str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from ouc_common import (
    GUARD_LOG_FILE,
    GUARD_STATE_FILE,
    OPENCODE_LOG_FILE,
    acquire_mutex,
    append_log,
    ensure_dirs,
    find_warp_cli,
    get_egress_v6,
    get_warp_local_v6,
    hop_once,
    keeper_pause_ctx,
    normalize_v6_64,
)

POLL_SECONDS = 10
COOLDOWN_SECONDS = 75
MAX_HOP_TRIES = 4
BLACKLIST_MAX = 200
MAX_READ_BYTES = 4_000_000


def _default_state(offset: int = 0) -> dict:
    return {
        "blacklist": [],
        "offset": offset,
        "last_hop": 0,
        "hops": 0,
        "current": "",
        "wall_pending": False,
        "wall_seen": 0,
        "last_ok_hop": 0,
        "last_wait_log": 0,
    }


def _load_state() -> dict:
    if not GUARD_STATE_FILE.is_file():
        offset = OPENCODE_LOG_FILE.stat().st_size if OPENCODE_LOG_FILE.is_file() else 0
        state = _default_state(offset)
        _save_state(state)
        return state
    try:
        data = json.loads(GUARD_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {**_default_state(), **data}
    except Exception:
        pass
    return _default_state()


def _save_state(state: dict) -> None:
    try:
        GUARD_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _dispatch_auto_resume() -> None:
    """Dispatches 'continue' to the active OpenCode session once the limit is bypassed."""
    try:
        from auto_resume import resume_active_session, is_auto_resume_enabled, clear_opencode_free_limit

        # Always clear client-side free-limit locks immediately upon rotation
        clear_opencode_free_limit()

        if is_auto_resume_enabled():
            time.sleep(1.5)
            ok, res_msg = resume_active_session()
            append_log(GUARD_LOG_FILE, f"[auto-resume] Limit bypassed -> {res_msg}")
    except Exception as exc:
        append_log(GUARD_LOG_FILE, f"[auto-resume] note: {exc}")


def _blacklist_add(state: dict, addr: str) -> None:
    prefix = normalize_v6_64(addr)
    if not prefix:
        return
    bl = [x for x in state.get("blacklist", []) if x and normalize_v6_64(x) != prefix]
    bl.append(prefix)
    state["blacklist"] = bl[-BLACKLIST_MAX:]


def _is_burnt(state: dict, addr: str) -> bool:
    prefix = normalize_v6_64(addr)
    if not prefix:
        return False
    return any(x and normalize_v6_64(x) == prefix for x in state.get("blacklist", []))


def _read_new_log_bytes(state: dict) -> bool:
    """Return True if a quota wall appeared since the last offset."""
    if not OPENCODE_LOG_FILE.is_file():
        return False
    try:
        size = OPENCODE_LOG_FILE.stat().st_size
    except OSError:
        return False

    offset = int(state.get("offset", 0) or 0)
    if size < offset:
        offset = 0

    start = max(offset, size - MAX_READ_BYTES)
    delta = size - start
    wall = False
    if delta > 0:
        try:
            with OPENCODE_LOG_FILE.open("rb") as fh:
                fh.seek(start)
                chunk = fh.read(delta).decode("utf-8", errors="replace")
            chunk_lower = chunk.lower()
            wall_patterns = (
                "quotaexceeded",
                "quota exceeded",
                "quota_exceeded",
                "free usage limit",
                "free usage exceeded",
                "free-tier usage limit",
                "usage limit reached",
                "exceeded your current quota",
                "credits exhausted",
                "rate limit exceeded",
                "rate_limit_exceeded",
                "rate limited",
                "ratelimited",
                "resource_exhausted",
                "429 too many requests",
                "status=429",
                "status: 429",
                "status 429",
                "code=429",
            )
            if any(p in chunk_lower for p in wall_patterns):
                wall = True
        except OSError:
            pass

    state["offset"] = size
    if wall:
        state["wall_pending"] = True
        state["wall_seen"] = int(time.time())
    _save_state(state)
    return wall


def _rotate(state: dict, warp_bin: str | None) -> bool:
    now = int(time.time())

    # Detect active egress mode
    mode = "warp"
    try:
        from proxy_chain import get_egress_config
        mode = get_egress_config().get("mode", "warp")
    except Exception:
        pass

    # 1. Tor Network mode: rotate Tor circuit to get a new exit node IP
    if mode == "tor":
        append_log(GUARD_LOG_FILE, "wall detected in Tor mode — rotating circuit for fresh exit identity")
        try:
            from tor_manager import rotate_tor_circuit
            ok, msg = rotate_tor_circuit()
            state["last_hop"] = now
            state["hops"] = int(state.get("hops", 0) or 0) + 1
            if ok:
                state["last_ok_hop"] = now
            _save_state(state)
            append_log(GUARD_LOG_FILE, f"Tor rotated #{state['hops']}: {msg}")
            if ok:
                _dispatch_auto_resume()
            return ok
        except Exception as exc:
            append_log(GUARD_LOG_FILE, f"Tor rotation error: {exc}")
            return False

    # 2. Custom Proxy mode: cycle proxy node in pool
    if mode == "custom":
        append_log(GUARD_LOG_FILE, "wall detected in Custom Proxy mode — cycling proxy node")
        try:
            from proxy_chain import rotate_custom_proxy
            ok, msg = rotate_custom_proxy()
            state["last_hop"] = now
            state["hops"] = int(state.get("hops", 0) or 0) + 1
            if ok:
                state["last_ok_hop"] = now
            _save_state(state)
            append_log(GUARD_LOG_FILE, f"Custom proxy rotated #{state['hops']}: {msg}")
            if ok:
                _dispatch_auto_resume()
            return ok
        except Exception as exc:
            append_log(GUARD_LOG_FILE, f"Custom proxy rotation error: {exc}")
            state["last_hop"] = now
            state["hops"] = int(state.get("hops", 0) or 0) + 1
            _save_state(state)
            return False

    # 3. WARP / Clean Endpoint mode: rotate IPv6 /64 subnet
    if not warp_bin:
        append_log(GUARD_LOG_FILE, "wall detected but warp-cli is not installed for WARP rotation")
        return False

    old_v6 = (
        get_egress_v6(timeout=8.0)
        or (get_warp_local_v6() or "")
        or str(state.get("current", ""))
    )
    append_log(GUARD_LOG_FILE, f"wall detected — rotating WARP /64 (was: {old_v6 or '?'})")
    _blacklist_add(state, old_v6)
    _save_state(state)

    new_v6 = ""
    with keeper_pause_ctx("zen-guard", seconds=600):
        avoid = {str(x) for x in state.get("blacklist", []) if x}
        for attempt in range(1, MAX_HOP_TRIES + 1):
            cand = hop_once(
                warp_bin,
                avoid_prefixes=avoid,
                previous=old_v6,
            )
            if not cand:
                append_log(GUARD_LOG_FILE, f"try {attempt}: rotation produced no fresh /64")
                continue
            if _is_burnt(state, cand):
                append_log(
                    GUARD_LOG_FILE,
                    f"try {attempt}: cloudflare returned a blacklisted /64 ({cand}) — again",
                )
                continue
            new_v6 = cand
            break

        if not new_v6:
            append_log(GUARD_LOG_FILE, "everything blacklisted — recycling oldest block")
            cand = hop_once(
                warp_bin,
                avoid_prefixes=set(),
                previous=old_v6,
            )
            if cand:
                new_v6 = cand

    state["last_hop"] = now
    if new_v6:
        state["current"] = new_v6
        state["hops"] = int(state.get("hops", 0) or 0) + 1
        state["last_ok_hop"] = now
        _save_state(state)
        append_log(GUARD_LOG_FILE, f"rotated #{state['hops']}: {old_v6} -> {new_v6}")
        try:
            from ouc_common import LANE_DIR
            (LANE_DIR / "edge_reroll").write_text(str(time.time()), encoding="utf-8")
        except Exception:
            pass
        _dispatch_auto_resume()
        return True

    _save_state(state)
    append_log(
        GUARD_LOG_FILE,
        "rotation failed — keeper keeps the tunnel alive; retrying after cooldown",
    )
    return False


def main() -> int:
    ensure_dirs()
    mutex = acquire_mutex("OpencodeUnlimitedGuard")
    if mutex is None:
        return 0

    warp_bin = find_warp_cli()
    state = _load_state()
    append_log(
        GUARD_LOG_FILE,
        f"guard up (poll {POLL_SECONDS}s, cooldown {COOLDOWN_SECONDS}s, warp={'found' if warp_bin else 'none'})",
    )

    while True:
        time.sleep(POLL_SECONDS)
        try:
            state = _load_state()
            wall = _read_new_log_bytes(state)
            if not wall and not state.get("wall_pending"):
                continue

            now = int(time.time())
            last_hop = int(state.get("last_hop", 0) or 0)
            last_ok = int(state.get("last_ok_hop", 0) or 0)
            seen = int(state.get("wall_seen", 0) or 0)
            if seen and seen <= last_ok:
                # The wall came from the session that was already rotated.
                state["wall_pending"] = False
                _save_state(state)
                continue

            age = now - last_hop
            if age >= COOLDOWN_SECONDS:
                state["wall_pending"] = False
                _save_state(state)
                if not _rotate(state, warp_bin):
                    state["wall_pending"] = True
                    _save_state(state)
            else:
                last_log = int(state.get("last_wait_log", 0) or 0)
                if now - last_log >= 60:
                    append_log(
                        GUARD_LOG_FILE,
                        f"wall queued — waiting out cooldown ({age}s < {COOLDOWN_SECONDS}s)",
                    )
                    state["last_wait_log"] = now
                    _save_state(state)
        except Exception as exc:
            append_log(GUARD_LOG_FILE, f"guard error: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
