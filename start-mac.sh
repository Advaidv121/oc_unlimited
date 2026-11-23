#!/bin/bash
set -euo pipefail
/usr/bin/warp-svc >/tmp/warp-service.log 2>&1 &
trap 'kill $(jobs -pr) 2>/dev/null || true' EXIT
for i in $(seq 1 40); do
  warp-cli --accept-tos status >/dev/null 2>&1 && break
  sleep 1
done
warp-cli --accept-tos registration show >/dev/null 2>&1 || warp-cli --accept-tos registration new
warp-cli --accept-tos tunnel protocol set MASQUE
warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port 40000
warp-cli --accept-tos connect
runuser -u tester -- env HOME=/home/tester OUC_BIND_HOST=0.0.0.0 OUC_STRICT_PROXY=1 python3 /opt/unlimited-v2/src/zen_proxy.py 41987 &
if [[ -d /host-opencode-log ]]; then
  python3 -u /opt/unlimited-v2/rotation_mac.py &
fi
wait -n
