# blockrelay: a stable egress path for the OpenCode free tier

The free tier is metered **per egress block**: one IPv6 `/64` (or one IPv4 `/32`)
gets a daily request allowance, reset at 00:00 UTC. Tor exits are refused
outright, and consumer WARP hands out `/64`s from a small shared pool that is
already mostly spent. That is why the older rotation tricks look "patched":
there is nothing fresh left to rotate to.

`blockrelay` manufactures fresh blocks instead. It runs two lanes and the
operator picks one, or both:

| Lane | Where the block comes from | Needs | Capacity |
|---|---|---|---|
| `prefix` | a routed IPv6 prefix sliced into `/64`s, each bound as a source address | Linux, a routed prefix, `ip_nonlocal_bind` | 2^(64-prefixlen), effectively unlimited |
| `warpgate` | [WarpGate](https://github.com/zhoushu44/WarpGate) instances, each a separate Cloudflare WARP registration with its own IPv6 exit | one extra host running WarpGate | 1 block per registration |
| `hybrid` | prefix first, WarpGate when the prefix is spent | both | sum of the two |

Whichever lane is active, the client sees the same thing:

- each user token is assigned **one sticky `/64`**, so a whole working day shares
  one budget exactly the way the upstream limiter counts it;
- the outbound connection leaves from that block;
- when the user hits the wall, the client calls `POST /api/rotate` and gets a
  different block, and the old one is burnt until midnight;
- the tunnel is a **blind CONNECT proxy**. It never terminates TLS, never
  rewrites headers, never touches the body, so the OpenCode client keeps its own
  TLS session and the fingerprint the gateway expects. That part is
  non-negotiable: a MITM proxy gets `403 FreeTierError` because the fingerprint
  changes.

## Requirements

| Lane | Item | Detail |
|---|---|---|
| both | Host | Linux, Python 3.10+ (stdlib only), one TCP port (default `4443`) for tunnel and API |
| `prefix` | Prefix | **routed** to the host, not just on-link. Check with `ip -6 route show` |
| `prefix` | Kernel | `net.ipv6.ip_nonlocal_bind=1`, so the relay can bind addresses it does not own |
| `warpgate` | Host | a WarpGate instance reachable from the relay over HTTP for the API and SOCKS5 for the dial |
| `warpgate` | Prefix | none. Any Linux box will do, including the relay host itself |

A `/64` routed prefix is exactly **1** block: one user-day. A `/48` is the
practical minimum for a real deployment. Most providers hand out a `/64` by
default and route a `/48` on request. Hurricane Electric tunnels are free and
come with a `/48` if you have a public IPv4 and protocol 41 is allowed through.

## Install

```bash
sudo useradd --system --home /var/lib/blockrelay --shell /usr/sbin/nologin blockrelay
sudo mkdir -p /opt/blockrelay /etc/blockrelay /var/lib/blockrelay
sudo cp blockrelay.py warpgate_pool.py /opt/blockrelay/
sudo chown -R blockrelay:blockrelay /var/lib/blockrelay

# Bind addresses that are routed here but not configured on an interface.
echo 'net.ipv6.ip_nonlocal_bind = 1' | sudo tee /etc/sysctl.d/90-blockrelay.conf
sudo sysctl --system

sudo cp config.example.json /etc/blockrelay/config.json
sudo nano /etc/blockrelay/config.json          # prefixes, admin_token, enroll_secret
sudo chmod 600 /etc/blockrelay/config.json
```

The `prefix` key can be left out entirely on a WarpGate-only deployment.

### TLS

The client hop should be TLS. A plaintext hop leaks the client token to anyone
on the path.

```bash
# Real certificate (recommended): point an A/AAAA record at the relay.
sudo certbot certonly --standalone -d relay.example.com
# then in config.json:
#   "tls": {"cert": "/etc/letsencrypt/live/relay.example.com/fullchain.pem",
#           "key":  "/etc/letsencrypt/live/relay.example.com/privkey.pem"}

# Self-signed (fine for a private relay):
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout /etc/blockrelay/relay.key -out /etc/blockrelay/relay.crt \
  -subj "/CN=relay.example.com"
sudo chmod 600 /etc/blockrelay/relay.key
```

With a self-signed certificate the client must opt in: set `OUC_RELAY_INSECURE=1`
or create `lane/relay_insecure.flag` in the app's data directory. `curl` probes
need `--proxy-insecure` too, which the app adds automatically under the same
flag.

### systemd

```bash
sudo cp blockrelay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now blockrelay
sudo journalctl -u blockrelay -f
```

## The WarpGate lane

WarpGate registers N Cloudflare WARP accounts on one host and exposes each as a
SOCKS5 port. Every registration gets its own IPv6 exit, so N registrations are N
daily blocks. On the tested `h2` data plane 500 instances need about 85 MB of RSS
and only outbound TCP/443, which fits a free-tier VPS.

Deploy it from its own repository (Docker is the short path):

```bash
git clone https://github.com/zhoushu44/WarpGate
cd WarpGate
docker compose up -d
# management UI on :4433, SOCKS5 on 10010..10010+N-1
```

Then point the relay at it:

```json
"upstream": {
  "mode": "hybrid",
  "warpgate": {
    "url": "http://10.0.0.5:4433",
    "proxy_host": "10.0.0.5",
    "username": "socks-user",
    "password": "socks-pass",
    "sticky_minutes": 1440
  }
}
```

Notes that cost time if you learn them the hard way:

- `proxy_host` is where the SOCKS5 ports live, which is not always the API host.
  Leave it empty when they are the same box.
- WarpGate's API is IP-allowlisted. Add the relay's address with
  `POST /api/whitelist`, or run both on the same host.
- `sticky_minutes` should cover a working day. The relay keeps its own ledger of
  which `/64` belongs to which client, so a WarpGate lease that expires does not
  move the client by itself.
- WarpGate's IPv4 exits are a shared NAT pool of about four addresses per edge
  colo. Only the IPv6 exit is unique per registration, and the relay always asks
  for IPv6 first, so nothing else can be relied on.
- Registration is rate limited. Six parallel registrations trip
  `429 error 1015`; serial with a 2 second gap works until a few hundred
  accounts, then Cloudflare wants a cooldown of 30 minutes to a few hours. Scale
  in batches and let the pool sit.

> **Operational risk.** WarpGate's capacity comes from many separate Cloudflare
> WARP registrations. Mass registration is rate-limited (`error 1015`) and can be
> stopped by Cloudflare, so treat the pool as something that can disappear and
> keep the prefix lane available. Only the IPv6 exit is unique per registration;
> the IPv4 exits are a shared NAT pool, so a destination that resolves or falls
> back to IPv4 collapses every instance into the same bucket. `opencode.ai`
> publishes AAAA records today, which is what makes this lane work.

### WarpGate capacity

```
blocks = number of healthy registrations
```

One registration is one user-day. Two hundred instances serve about 200 users at
300 requests each, or about 600 users at 100 requests each, before anyone needs a
second block. When the pool runs dry the relay says so (`503 no unspent exit`)
instead of silently spending a burnt address.

### Retiring a spent exit

WarpGate has two different repair calls and they mean very different things:

| call | what it does | exit block |
|---|---|---|
| `POST /api/pool/report` | after two connect failures, restarts the tunnel on the **same** account | unchanged |
| `POST /api/pool/rotate` | deletes the WARP account and registers a fresh one | **new** |

Rotation therefore uses `rotate`, not `report`. Reporting a spent block would
restart a tunnel that is working perfectly and hand the same exhausted `/64`
straight back, so the relay would burn blocks against an account that is already
at its daily limit and never recover the capacity.

The order is:

1. burn the old `/64` for the rest of the UTC day,
2. `POST /api/pool/rotate {"name": "warp-N", "index": N}` — delete that account,
   register a replacement,
3. drop the cached exit for the instance, drop the lease, lease again.

Step 2 is a real re-registration: two Cloudflare calls plus a tunnel start. It
is serialized inside WarpGate by `max_concurrent_create`, so the request can take
tens of seconds and the relay allows three minutes for it.

Step 3's cache drop matters because a rebuilt instance keeps its name and SOCKS5
port but exits from a brand new block. The stale row would point the router at an
address that no longer belongs to it.

A tunnel that fails its egress probe during assignment is handed back with
`report(failure_type="proxy_dead")` instead: the account is fine, only the
tunnel is wedged, and WarpGate's engine restart fixes that without spending a
registration.

Assignment recycles spent exits on its own. Round-robin pools wrap, so a pool
with nothing fresh left offers the same burnt `/64` again and again. When the
relay fails to claim a block that nobody owns and that is burnt until midnight,
it re-registers that instance rather than handing the dead exit to the next
client — the same `rotate` call, no operator action. A block that belongs to
another client is only released; replacing that account would cut off whoever is
using it.

Re-registration is capped at `REISSUE_ATTEMPTS` (2) per assignment so an
exhausted pool cannot spend a dozen Cloudflare registrations in one request. Past
the cap the exit is released and another is tried; if none is left, the client
gets an honest `503 no unspent exit` and the next request tries again.

### Respecting the pool's own judgement

WarpGate already health-checks every instance, counts Cloudflare 403s per exit and
lets an operator pin an instance to its address. The relay reads those facts from
`GET /api/containers` instead of guessing them:

- **pinned** (`dedicated`) — never re-registered. Replacing that account would
  break the one promise a pin makes, so the lease is handed back and another exit
  is tried. This is checked against a fresh read before any rotation, because a
  pin added in the last half minute must still stop it; assignment may use the
  30-second cached view.
- **unhealthy** — passed over without being reported. The gate's own
  `auto_rebuild_on_unhealthy` loop repairs it; reporting it as a dead tunnel
  would only restart something already being restarted.
- **`cf_403_rate` at or above `CF_403_THRESHOLD` with at least
  `CF_403_MIN_CHECKS` reports** — passed over. A single blocked check is a
  sample, not a verdict, so thin evidence is ignored.
- **`unknown` health** — accepted. An instance rebuilt seconds ago has no probe
  yet, and excluding it would shrink the pool exactly when it is needed.

A missing `/api/containers` (older build, transport error) yields an empty view
and changes nothing: the relay fails open rather than refusing to assign because
an advisory endpoint is gone.

The gate also rate-limits control calls per source address. `acquire` answers
`A0002` on refusal; the relay pauses `RATE_LIMIT_BACKOFF` per retry up to
`RATE_LIMIT_BUDGET` instead of surfacing a burst of enrolments as "no capacity".
If the relay shares an address with a busy console, raise
`rate_limit_per_minute` in WarpGate's config rather than lowering the relay's
retries.

If rotation then fails for lack of capacity, the client is left unassigned rather
than restored to an instance whose account has just been replaced. The next
request re-assigns from the pool once the replacement is up.

## Verify before handing out tokens

```bash
sudo -u blockrelay python3 /opt/blockrelay/blockrelay.py \
  -c /etc/blockrelay/config.json doctor

# a deeper look at the WarpGate lane: capacity, then N instances dialed end to end
sudo -u blockrelay python3 /opt/blockrelay/blockrelay.py \
  -c /etc/blockrelay/config.json pool --probe 5
```

Expected prefix line:

```
2001:db8:1234::/64  source=2001:db8:1234::1  egress=2001:db8:1234:...  OK
```

Expected WarpGate lines:

```
warpgate: http://10.0.0.5:4433 reachable=True instances=500 available=480
  10.0.0.5:10011   egress=2a09:bac1:...  block=2a09:bac1:...::/64  OK
  10.0.0.5:10012   egress=2a09:bac1:...  block=2a09:bac1:...::/64  OK
```

`FAIL` on a prefix means it is not routed to the host, or `ip_nonlocal_bind` is
off. `FAIL` on a WarpGate instance means its tunnel is dead; `pool --probe`
reports each one so you can rebuild the bad ones from WarpGate's UI.

## Mint a client

```bash
# CLI
sudo -u blockrelay python3 /opt/blockrelay/blockrelay.py \
  -c /etc/blockrelay/config.json client-new --label alice
# -> relay://block:<token>@relay.example.com:4443

# Or over the API, for self-service enrollment
curl -sS -X POST https://relay.example.com:4443/api/enroll \
  -H "X-Enroll-Secret: $ENROLL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"label":"alice"}'
```

The returned string is the whole client configuration. Treat it as a credential:
whoever holds it spends that user's blocks.

```bash
# Confirm a token works end to end
curl -sS --proxy-insecure -x 'https://block:TOKEN@relay.example.com:4443' \
  https://www.cloudflare.com/cdn-cgi/trace | grep '^ip='
```

## Point the app at it

Egress mode **Custom**, and paste the `relay://` string as the proxy. That is the
only change on the client: no new mode, no new tab.

```
mode         custom
custom_proxy relay://block:TOKEN@relay.example.com:4443
custom_type  relay
```

From then on, when the guard sees the wall it calls `rotate_custom_proxy`, which
posts `/api/rotate` and reports the block and the exit address it got back. The
guard log line looks like:

```
Custom proxy rotated #4: relay relay.example.com:4443 -> 2001:db8:1234:5a::/64 (exit 2001:db8:1234:5a::1)
```

A relay is **not** a privacy lane, so a dead relay never falls back to the local
WARP address: traffic fails loudly instead of quietly burning the one address the
relay exists to replace. The local WARP tunnel is left alone.

## Capacity

```
prefix lane:  blocks = 2 ** (64 - prefix_len)      # /48 -> 65,536
warpgate:     blocks = healthy registrations       # 500 instances -> 500
requests/day ≈ blocks * 200                        # /48 -> ~13.1M
users/day    ≈ total_blocks * 200 / requests_per_user_per_day
```

A busy coding day is roughly 200 to 500 upstream requests per user (a turn is
several requests: title, summary, tool calls, the answer). So:

| Source | Blocks | Users/day at 300 req | Notes |
|---|---|---|---|
| `/64` prefix | 1 | 0 (one user-day) | dev only |
| `/56` prefix | 256 | ~170 | small pilot |
| `/48` prefix | 65,536 | ~43,000 | real deployment |
| 100 WarpGate instances | 100 | ~65 | one small VPS |
| 500 WarpGate instances | 500 | ~330 | one medium VPS |

Concurrency is a separate axis: one block happily serves a user's parallel
tunnels (`max_tunnels_per_client`, default 8). What runs out is **blocks per day**,
because a burnt block is parked until 00:00 UTC.

## Scaling past one host

The client only ever talks to one relay URL. To add capacity:

1. Stand up a second relay with its own prefix and its own database.
2. Give the client a **pool**: the custom proxy field accepts a list:
   `relay://block:TOKEN_A@relay-a.example.com:4443, relay://block:TOKEN_B@relay-b.example.com:4443`
3. The client rotates its block on the current node first, and only advances to
   the next node when that relay has no capacity left.

Proxy index is persisted in `lane/custom_proxy_index.txt`, so a restart resumes on
the node that was working.

Adding WarpGate instances grows a single relay instead of adding a host. Grow the
prefix first, the pool second, and only add hosts when both are spent.

## API

| Route | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | liveness, lane, capacity, burnt count |
| `POST /api/enroll` | `X-Enroll-Secret` | mint a client (disable by clearing `enroll_secret`) |
| `POST /api/rotate` | client token | burn the current block, return a new one (verified) |
| `GET /api/state` | client or admin token | this client's lane, block, requests, reset time |
| `POST /api/clients` | admin token | mint a client |
| `GET /api/clients` | admin token | list clients |

Rotation returns:

```json
{
  "ok": true,
  "client_id": "cl_9f2c...",
  "lane": "warpgate",
  "old_block": "2a09:bac1:1000:1::/64",
  "block": "2a09:bac1:1000:2::/64",
  "source": "10.0.0.5:10012",
  "instance": "warp-2",
  "endpoint": "10.0.0.5:10012",
  "egress_ip": "2a09:bac1:1000:2::1",
  "verified": true,
  "resets_at": 1794960000
}
```

`verified` is the relay dialing `www.cloudflare.com/cdn-cgi/trace` through the new
block and checking the address it sees is inside it. An unverified block is
reported as a failure by the client, never as success.

`/healthz` carries the lane split:

```json
{"ok": true, "stats": {
  "lane": "hybrid",
  "capacity": 66036,
  "prefix_blocks": 65536, "prefix_free": 65500,
  "warpgate_instances": 500, "warpgate_available": 480, "warpgate_reachable": true,
  "clients": 42, "clients_prefix": 40, "clients_warpgate": 2,
  "burnt_blocks": 7, "resets_at": 1794960000
}}
```

## Operations

```bash
# capacity, both lanes, plus an end-to-end dial
blockrelay.py -c config.json doctor

# WarpGate capacity and N live probes
blockrelay.py -c config.json pool --probe 5

# who is on what
blockrelay.py -c config.json client-list

# move one client to a fresh block
blockrelay.py -c config.json client-rotate --id cl_9f2c...

# cut a leaked token off
blockrelay.py -c config.json client-revoke --id cl_9f2c...
```

## Security notes

- The tunnel is authenticated per client; there is no unauthenticated path. The
  default host allowlist is `*` because a narrow list has to name every domain
  OpenCode touches, and a miss shows up as a hard `403`. Narrow it if you know
  the full set; keep `*.opencode.ai`, `models.dev` and the update hosts at
  minimum.
- Keep `admin_token` and `enroll_secret` out of the client distribution. Clients
  only ever need their own token.
- The relay sees the destination host and the client token, never the payload.
- One token per user. Sharing a token means sharing one block, which is the same
  as sharing one daily allowance.
- `database` is the source of truth for assignments. Back it up or you will
  re-hand blocks that are already burnt today.
- The WarpGate credentials live in the relay config. Keep it `600`; anyone who
  reads it can spend the pool.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `doctor` prints FAIL on a prefix | prefix not routed, or `ip_nonlocal_bind` off | `ip -6 route show`, sysctl |
| Client: `no egress could be confirmed` | relay cannot dial out from the block | `doctor` per prefix, check provider filters |
| Client: `outside that block` | traffic is leaving another address | provider NAT or shared prefix; you need a real routed prefix |
| `503 no unspent exit` | every WarpGate block is burnt or held | add instances, add a prefix, switch to `hybrid`, or wait for 00:00 UTC |
| `warpgate reachable=False` | relay cannot reach the control plane | allowlist the relay IP in WarpGate, check the URL and port |
| WarpGate instance probes FAIL | tunnel is dead, or the port is firewalled | rebuild it in the UI; open the whole `10010..10010+N-1` range |
| `403` on CONNECT | host not in `allowed_hosts` | widen the list |
| `503 no free blocks left` | every prefix block is burnt for today | add a prefix or a WarpGate lane; resets at 00:00 UTC |
| `407` on CONNECT | missing or wrong token | re-copy the `relay://` string |
| Rotation works, app still limited | client kept the old connection | guard restarts the lane; confirm `state.json` shows the new block |
