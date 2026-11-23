#!/bin/bash
set -euo pipefail
[[ $(uname -s) == Darwin ]] || { echo 'This installer is for macOS.' >&2; exit 1; }
repo=$(cd "$(dirname "$0")/.." && pwd)
command -v docker >/dev/null
/usr/bin/python3 - "$repo" "$(command -v docker)" <<'PY'
import os
import plistlib
import shutil
import sys
from pathlib import Path

home = Path.home()
root = home / 'Library/Application Support/OpenCodeProxyQuotaGuard'
root.mkdir(mode=0o700, parents=True, exist_ok=True)
script = root / 'rotation_host.py'
shutil.copyfile(Path(sys.argv[1]) / 'rotation_host.py', script)
os.chmod(script, 0o600)
label = 'local.opencode-proxy-quota-guard'
plist = home / 'Library/LaunchAgents' / (label + '.plist')
plist.parent.mkdir(parents=True, exist_ok=True)
with plist.open('wb') as output:
    plistlib.dump(dict(Label=label, ProgramArguments=['/usr/bin/python3', '-u', str(script)],
                       RunAtLoad=True, KeepAlive=True, ThrottleInterval=15,
                       StandardOutPath=str(root / 'guard.log'), StandardErrorPath=str(root / 'guard.log'),
                       EnvironmentVariables={'PATH': str(Path(sys.argv[2]).parent) + ':/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin'}), output)
PY
label=local.opencode-proxy-quota-guard
domain="gui/$(id -u)"
plist="$HOME/Library/LaunchAgents/$label.plist"
plutil -lint "$plist"
if launchctl print "$domain/$label" >/dev/null 2>&1; then
  launchctl bootout "$domain/$label"
fi
launchctl bootstrap "$domain" "$plist"
