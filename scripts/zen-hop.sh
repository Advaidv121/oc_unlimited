#!/bin/bash
# zen hop — manual identity rotation. renews the WireGuard session keys and
# reconnects to obtain a new IPv6 /64. the device registration is kept.
#
# the keeper is paused during the hop so its tick cannot reconnect the tunnel
# in the middle of the rotation. The kill-switch chain stays installed the
# whole time, and the keeper resumes when the hop exits.
set -u

# warp binary: env → PATH → common fallbacks. probe with/without --accept-tos
# (shimmed boxes pass it twice = dead CLI).
WARP="${OUC_WARP_BIN:-}"
if [ -z "$WARP" ]; then
  if command -v warp-cli >/dev/null 2>&1; then WARP="$(command -v warp-cli)"
  elif [ -x /usr/bin/warp-cli ]; then WARP=/usr/bin/warp-cli
  elif [ -x "$HOME/.local/bin/warp-cli" ]; then WARP="$HOME/.local/bin/warp-cli"
  fi
fi
[ -n "$WARP" ] || { echo "warp-cli not found"; exit 1; }

KEEPER="${OUC_KEEPER_UNIT:-opencode-unlimited-keeper}"
w(){  sudo -n "$WARP" --accept-tos "$@" >/dev/null 2>&1 && return 0; sudo -n "$WARP" "$@" >/dev/null 2>&1; }
wo(){ sudo -n "$WARP" --accept-tos "$@" 2>/dev/null && return 0; sudo -n "$WARP" "$@" 2>/dev/null; }

restore(){ sudo -n systemctl start "$KEEPER" >/dev/null 2>&1 || sudo systemctl start "$KEEPER" >/dev/null 2>&1; }
trap restore EXIT

IS_VPS=0
if [ -n "${SSH_CLIENT:-}" ] || [ -n "${SSH_TTY:-}" ] || [ -n "${SSH_CONNECTION:-}" ] || [ -f /etc/cloud/cloud.cfg ] || [ -d /var/lib/cloud ] || wo settings 2>/dev/null | grep -qi "proxy"; then
  IS_VPS=1
fi

cur_v6(){
  local v ep
  if [ "$IS_VPS" = "1" ]; then
    for ep in https://api6.ipify.org https://api64.ipify.org; do
      v=$(timeout 10 curl -x socks5h://127.0.0.1:40000 -s -m 8 "$ep" 2>/dev/null)
      case "$v" in *:*) echo "$v"; return;; esac
    done
    v=$(timeout 10 curl -x http://127.0.0.1:41987 -s -m 8 https://api6.ipify.org 2>/dev/null)
    case "$v" in *:*) echo "$v"; return;; esac
  else
    for ep in https://api6.ipify.org https://api64.ipify.org; do
      v=$(timeout 10 curl -6 -s -m 8 "$ep" 2>/dev/null)
      case "$v" in *:*) echo "$v"; return;; esac
    done
  fi
  echo ""
}

m64(){ echo "$1" | cut -d: -f1-4; }

OLD_V6=$(cur_v6)
echo "old /64: ${OLD_V6:-?}"

sudo systemctl stop "$KEEPER" >/dev/null 2>&1
echo "-- rotating (keeper paused) --"
w disconnect

# Recreate a registration only when it is actually missing.
if ! wo registration show >/dev/null 2>&1; then
  for i in 1 2 3; do
    w registration new && break
    echo "  registration attempt $i failed"; sleep 6
  done
fi

w tunnel rotate-keys
if [ "$IS_VPS" = "1" ]; then
  w mode proxy
  w proxy port 40000
else
  w mode warp
fi
w connect

NEW_V6=""
for i in $(seq 1 20); do
  sleep 3
  wo status | head -1 | grep -q Connected || continue
  V=$(cur_v6)
  case "$V" in
    *:*)
      if [ -z "$OLD_V6" ] || [ "$(m64 "$V")" != "$(m64 "$OLD_V6")" ]; then
        NEW_V6="$V"
        break
      fi
      ;;
  esac
done

echo "new /64: ${NEW_V6:-?}"
if [ -n "$NEW_V6" ]; then
  # Clear client-side rate limit lockouts from drafts.sqlite
  for db in "$HOME/.config/ai.opencode.desktop/drafts.sqlite" "$HOME/.local/share/opencode/drafts.sqlite"; do
    [ -f "$db" ] && sqlite3 "$db" "DELETE FROM state WHERE key IN ('free-limit', 'go-upsell', 'free_usage_limit', 'usage_limit', 'quota_exceeded');" >/dev/null 2>&1 || true
  done
  echo "new /64 prefix accepted; re-send your message."
  exit 0
fi
echo "Cloudflare returned the same /64 block; run the command again."
exit 1
