"""macOS quota guard using the local OpenCode service's active-session API."""
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
LOG = HOME / '.local/share/opencode/log/opencode.log'
STATE = HOME / 'Library/Application Support/OpenCodeProxyQuotaGuard/attempts.json'
CONTAINER = 'opencode-unlimited-proxy-v4'
DOCKER = shutil.which('docker') or '/usr/local/bin/docker'
PAUSE = '/home/tester/.local/share/opencode-unlimited/keeper.pause'


def read_output(args, timeout=6):
    return subprocess.check_output(args, timeout=timeout, stderr=subprocess.DEVNULL, text=True)


def idle():
    executables = (HOME / 'Library/Application Support/ai.opencode.desktop/cli').glob('*-arm64/opencode-cli')
    try:
        cli = max(executables, key=lambda p: tuple(map(int, re.findall(r'\d+', p.parent.name.split('-')[0]))))
        result = json.loads(read_output([str(cli), 'api', 'GET', '/api/session/active']))['data']
        return isinstance(result, dict) and not result
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False


def route(while_paused=False):
    import ipaddress
    try:
        # The CONNECT proxy deliberately holds requests while paused. Probe
        # the very same WARP SOCKS listener directly during this window.
        prefix = [DOCKER, 'exec', CONTAINER, 'curl'] if while_paused else ['/usr/bin/curl']
        proxy = 'socks5h://127.0.0.1:40000' if while_paused else 'http://127.0.0.1:41988'
        body = read_output(prefix + ['-fsS', '--max-time', '4', '--noproxy', '', '-x',
                                     proxy, 'https://cloudflare.com/cdn-cgi/trace'])
        data = dict(x.split('=', 1) for x in body.splitlines() if '=' in x)
        if data.get('warp') in ('on', 'plus'):
            return ipaddress.IPv6Network(data['ip'] + '/64', strict=False)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    return None


def pause_proxy():
    # The proxy holds new connections during this lease. Expiry also covers
    # the guard being killed before it can remove the marker.
    code = ('import json,sys,time; from pathlib import Path; '
            'p=Path(sys.argv[1]); t=p.with_suffix(".host-tmp"); '
            't.write_text(json.dumps({"until":time.time()+30})); t.replace(p)')
    read_output([DOCKER, 'exec', '-u', 'tester', CONTAINER, 'python3', '-c', code, PAUSE])


def scan(cursor):
    try:
        with LOG.open('rb') as log:
            st = os.fstat(log.fileno())
            offset = cursor[1] if cursor and cursor[0] == st.st_ino and cursor[1] <= st.st_size else 0
            log.seek(max(offset, st.st_size - 262144))
            lines = log.read(262144).split(b'\n')
            offset = log.tell() - len(lines.pop())
    except OSError:
        return cursor, 0
    newest = 0
    for line in lines:
        if not all(s in line for s in (b' level=ERROR ', b' role=server ', b'message="Failed to drain Session"', b'Free usage limit reached')):
            continue
        try:
            stamp = datetime.fromisoformat(line.split()[0][10:].decode().replace('Z', '+00:00')).timestamp()
            newest = max(newest, stamp)
        except (ValueError, IndexError):
            pass
    return (st.st_ino, offset), newest


def main():
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        attempts = [float(x) for x in json.loads(STATE.read_text())]
    except (OSError, ValueError, TypeError):
        attempts = []
    cursor, pending = None, 0
    print('Mac quota guard ready; checks OpenCode active sessions; retries remain manual')
    while True:
        cursor, found = scan(cursor)
        now = time.time()
        if found > pending and 0 <= now - found <= 300:
            print('Quota guard: upstream quota rejection detected; waiting for idle '
                  'sessions before a route check. A route change does not confirm recovery.')
        pending = max(found, pending)
        attempts = [x for x in attempts if now - x < 3600]
        if pending and now - pending > 300:
            print('Quota guard: quota event expired without recovery verification; '
                  'no task was resumed. Run scripts/quota-diagnostics.py for upstream '
                  'status and retry delay.')
            pending = 0
        if 6 <= now - pending <= 300 and (not attempts or pending > max(attempts)):
            if len(attempts) >= 2:
                print('Quota guard: two rotation attempts this hour; waiting for a new error')
                pending = 0
            elif (not attempts or now - max(attempts) >= 90) and idle():
                before = route()
                if before is not None and idle():
                    paused = False
                    try:
                        pause_proxy()
                        paused = True
                        if not idle():
                            time.sleep(3)
                            continue
                        attempts.append(time.time())
                        tmp = STATE.with_suffix('.tmp')
                        tmp.write_text(json.dumps(attempts))
                        tmp.replace(STATE)
                        read_output([DOCKER,'exec',CONTAINER,'warp-cli','--accept-tos','tunnel','rotate-keys'], 15)
                        for _ in range(12):
                            time.sleep(1)
                            after = route(while_paused=True)
                            if after is not None and after != before:
                                print('Quota guard: new public IPv6 block verified; '
                                      'upstream recovery unverified; retry manually')
                                break
                        else:
                            print('Quota guard: no new public IPv6 block verified')
                    except (OSError, subprocess.SubprocessError):
                        print('Quota guard: WARP rotation command failed')
                    finally:
                        if paused:
                            try:
                                read_output([DOCKER, 'exec', '-u', 'tester', CONTAINER, 'rm', '-f', PAUSE])
                            except (OSError, subprocess.SubprocessError):
                                pass
                    pending = 0
        time.sleep(3)


if __name__ == '__main__':
    main()
