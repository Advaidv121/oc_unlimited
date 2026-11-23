"""Rotate only on new OpenCode server free-limit events. Never replay chats."""
import ipaddress
import json
import subprocess
import time
from pathlib import Path

LOG = Path('/host-opencode-log/opencode.log')
LANE = Path('/home/tester/.local/share/opencode-unlimited')
STATE = Path('/rotation-state/state.json')
PAUSE = LANE / 'keeper.pause'
QUOTA = b'free usage limit reached'


def scan(cursor):
    try:
        with LOG.open('rb') as log:
            import os
            info = os.fstat(log.fileno())
            previous, offset = cursor or (info.st_ino, info.st_size)
            if previous != info.st_ino or offset > info.st_size:
                offset = 0
            log.seek(max(offset, info.st_size - 262144))
            data = log.read(262144)
            offset = log.tell()
    except OSError:
        return cursor, False
    lines = data.split(bytes([10]))
    offset -= len(lines.pop())
    wall = any(b' level=ERROR ' in line and b' role=server ' in line
               and b'message="Failed to drain Session"' in line
               and QUOTA in line.lower() for line in lines)
    return (info.st_ino, offset), wall


def idle():
    try:
        status = json.loads((LANE / 'lane/proxy_status.json').read_text())
        return (status['active_tunnels'] == 0 and
                time.time() - status['active_tunnels_ts'] < 5 and
                time.time() - LOG.stat().st_mtime >= 6)
    except (OSError, ValueError, KeyError):
        return False


def route():
    try:
        result = subprocess.check_output([
            'curl', '-fsS', '--max-time', '4', '--proxy', 'socks5h://127.0.0.1:40000',
            'https://cloudflare.com/cdn-cgi/trace'], stderr=subprocess.DEVNULL, text=True)
        fields = dict(line.split('=', 1) for line in result.splitlines() if '=' in line)
        if fields.get('warp') in ('on', 'plus'):
            return ipaddress.IPv6Network(fields['ip'] + '/64', strict=False)
    except (subprocess.SubprocessError, OSError, ValueError, KeyError):
        pass
    return None


def rotate():
    before = route()
    if before is None or not idle():
        return False
    temporary = PAUSE.with_suffix('.tmp')
    temporary.write_text(json.dumps({'until': time.time() + 30, 'reason': 'mac quota guard'}))
    temporary.replace(PAUSE)
    try:
        if not idle():
            return False
        subprocess.run(['warp-cli', '--accept-tos', 'tunnel', 'rotate-keys'],
                       check=True, timeout=12, stdout=subprocess.DEVNULL)
        end = time.monotonic() + 12
        while time.monotonic() < end:
            time.sleep(1)
            after = route()
            if after is not None and after != before:
                return True
        return False
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        PAUSE.unlink(missing_ok=True)


def main():
    try:
        attempts = [float(x) for x in json.loads(STATE.read_text())]
    except (OSError, ValueError, TypeError):
        attempts = []
    cursor, _ = scan(None)
    pending = 0
    print('Quota guard ready; new server errors only; manual chat retry')
    while True:
        time.sleep(2)
        now = time.time()
        cursor, wall = scan(cursor)
        if wall:
            pending = now
        if not pending or now - pending > 300:
            continue
        attempts = [stamp for stamp in attempts if now - stamp < 3600]
        if len(attempts) >= 2:
            print('Quota guard: reached two attempts in one hour; waiting for a new error')
            pending = 0
            continue
        if (attempts and now - max(attempts) < 90) or not idle():
            continue
        pending = 0
        attempts.append(now)
        temp = STATE.with_suffix('.tmp')
        temp.write_text(json.dumps(attempts))
        temp.replace(STATE)
        changed = rotate()
        print('Quota guard: new WARP IPv6 block confirmed; retry manually' if changed else
              'Quota guard: no new WARP IPv6 block confirmed')


if __name__ == '__main__':
    main()
