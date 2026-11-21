"""Egress instances borrowed from a WarpGate host.

WarpGate runs many isolated Cloudflare WARP registrations behind one HTTP
control plane and exposes each of them as a SOCKS5 port.  Every registration
carries its own IPv6 exit, so a single WarpGate host behaves like a pool of
daily free-tier blocks.  This module owns the conversation with that control
plane: hand a client an instance, keep the assignment sticky, take it back, and
report a dead tunnel so WarpGate can rebuild it.

The SOCKS5 side lives here too, because dialing through an instance is what
makes it useful and the handshake only has one caller.

Nothing in this module binds a source address.  The relay's routed-prefix lane
does that; this lane lets Cloudflare pick the exit and reads it back.
"""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import socket
import ssl
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

# WarpGate answers every endpoint with {"code": "00000", "msg": "", "data": ...}.
OK_CODE = "00000"
# Codes the caller can act on differently from a generic transport failure.
CODE_UNAUTHORIZED = "A0001"
CODE_RATE_LIMITED = "A0002"
CODE_NO_PROXY = "P0001"
CODE_NO_SESSION = "S0001"

TRACE_REQUEST = (
    b"GET /cdn-cgi/trace HTTP/1.1\r\n"
    b"Host: www.cloudflare.com\r\n"
    b"User-Agent: blockrelay\r\n"
    b"Connection: close\r\n\r\n"
)
# Cloudflare's own trace endpoint is the only place that reports the address the
# world sees.  Third-party "what is my ip" services hosted on Cloudflare return
# the WARP ingress address instead, which is useless for quota accounting.
TRACE_HOST = "www.cloudflare.com"


class WarpGateError(RuntimeError):
    """A control-plane call failed, with the provider's code when there is one."""

    def __init__(self, message: str, code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status

    @property
    def rate_limited(self) -> bool:
        return self.code == CODE_RATE_LIMITED or self.status == 429

    @property
    def unauthorized(self) -> bool:
        return self.code == CODE_UNAUTHORIZED or self.status == 401


@dataclass(frozen=True)
class Instance:
    """One SOCKS5 endpoint backed by a single WARP registration."""

    name: str
    host: str
    port: int
    username: str = ""
    password: str = ""
    # The address WarpGate reports for the registration.  It is the tunnel
    # address, not the public exit, so it is a label and never a quota key.
    advertised_ip: str = ""
    session_id: str = ""

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def proxy_url(self) -> str:
        auth = ""
        if self.username:
            auth = (
                urllib.parse.quote(self.username, safe="")
                + ":"
                + urllib.parse.quote(self.password, safe="")
                + "@"
            )
        return f"socks5h://{auth}{self.host}:{self.port}"


@dataclass
class PoolStats:
    instances: int = 0
    available: int = 0
    in_use: int = 0
    sessions: int = 0
    reachable: bool = False
    detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InstanceHealth:
    """What the control plane says about one instance right now.

    ``cf_403_rate`` is the share of checks that Cloudflare answered 403 on, in
    percent, or ``None`` when the gate has never had a report for that exit.
    ``dedicated`` marks an instance an operator has pinned to its exit IP.
    """

    name: str
    health: str = "unknown"
    dedicated: bool = False
    cf_403_rate: float | None = None
    cf_checks: int = 0
    port: int = 0


def _split_credentials(value: str) -> tuple[str, str, str]:
    """Return ``(user, password, url)`` with any userinfo removed from ``url``."""
    from urllib.parse import unquote, urlsplit, urlunsplit

    text = (value or "").strip()
    if "://" not in text:
        return "", "", text
    parts = urlsplit(text)
    if "@" not in parts.netloc:
        return "", "", text
    userinfo, hostport = parts.netloc.rsplit("@", 1)
    user, _, password = userinfo.partition(":")
    clean = urlunsplit((parts.scheme, hostport, parts.path, parts.query, parts.fragment))
    return unquote(user), unquote(password), clean


def _split_proxy_url(value: str, fallback_host: str) -> tuple[str, int]:
    text = (value or "").strip()
    if not text:
        raise WarpGateError("WarpGate returned an empty proxy address")
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.rsplit("@", 1)[-1].rstrip("/")
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise WarpGateError(f"WarpGate returned an unparsable proxy address: {value!r}")
    return host.strip("[]") or fallback_host, int(port)


def index_from_name(name: str) -> int | None:
    """``warp-3`` -> ``3``.  ``None`` when the name is not WarpGate's shape."""
    prefix, _, tail = (name or "").partition("-")
    if prefix != "warp" or not tail.isdigit():
        return None
    return int(tail)


class WarpGatePool:
    """Client for one WarpGate control plane.

    The pool is intentionally thin: WarpGate already does health checks, IP
    uniqueness and session expiry.  Duplicating any of that here would mean two
    sources of truth that drift apart, so this class only translates between
    the relay's world (one client token, one block) and WarpGate's (a session
    id that maps to one instance).
    """

    def __init__(
        self,
        url: str,
        username: str = "",
        password: str = "",
        *,
        proxy_host: str = "",
        api_key: str = "",
        sticky_minutes: int = 1440,
        timeout: float = 10.0,
        verify_tls: bool = True,
    ) -> None:
        parsed = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
        if not parsed.hostname:
            raise WarpGateError(f"WarpGate url has no host: {url!r}")
        self.scheme = (parsed.scheme or "http").lower()
        self.host = parsed.hostname
        self.port = parsed.port or (443 if self.scheme == "https" else 4433)
        self.base_path = parsed.path.rstrip("/")
        self.username = username
        self.password = password
        # A relay usually shares the box with WarpGate but not always; letting
        # the SOCKS host differ from the API host covers a private network hop
        # without giving WarpGate a public bind address.
        self.proxy_host = (proxy_host or self.host).strip()
        self.api_key = api_key
        self.sticky_minutes = max(1, int(sticky_minutes))
        self.timeout = float(timeout)
        self.verify_tls = bool(verify_tls)

    # -- control plane ------------------------------------------------------ #

    def _connection(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            context = ssl.create_default_context()
            if not self.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=self.timeout, context=context
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def request(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Call one control-plane endpoint and return its ``data`` object."""
        target = f"{self.base_path}{path}"
        if query:
            target += "?" + urllib.parse.urlencode(query)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["X-API-Key"] = self.api_key
            headers["Authorization"] = f"Bearer {self.api_key}"

        conn = self._connection()
        if timeout:
            conn.timeout = float(timeout)
        try:
            conn.request(method.upper(), target, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read(1 << 20)
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise WarpGateError(
                f"WarpGate {self.host}:{self.port} unreachable: {exc}"
            ) from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

        try:
            envelope = json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:
            raise WarpGateError(
                f"WarpGate returned a non-JSON body for {path} (HTTP {status})",
                status=status,
            ) from exc
        if not isinstance(envelope, dict):
            raise WarpGateError(f"WarpGate returned an unexpected body for {path}")

        code = str(envelope.get("code", "")) if "code" in envelope else ""
        if code and code != OK_CODE:
            message = str(envelope.get("msg") or f"WarpGate error {code}")
            raise WarpGateError(message, code=code, status=status)
        if status >= 400:
            raise WarpGateError(
                str(envelope.get("msg") or f"WarpGate answered HTTP {status}"),
                code=code,
                status=status,
            )

        data = envelope.get("data", envelope)
        return data if isinstance(data, dict) else {"items": data}

    # -- instances ---------------------------------------------------------- #

    def acquire(self, session_id: str, minutes: int | None = None) -> Instance:
        """Lease one instance for ``session_id``.

        Session ids are sticky, so calling this twice for the same client
        returns the same exit until the lease expires.  That is exactly the
        behaviour the free tier needs: one block per client per day.
        """
        query = {"session_id": session_id}
        ttl = int(minutes or self.sticky_minutes)
        if ttl > 0:
            # The Rust build calls it ``time``; the older Node build calls it
            # ``minutes``. Sending both keeps the lease length on either.
            query["time"] = str(ttl)
            query["minutes"] = str(ttl)
        data = self.request("GET", "/api/pool/acquire", query=query)
        proxy = str(data.get("proxy") or data.get("url") or "")
        username = str(data.get("username") or self.username)
        password = str(data.get("password") or self.password)
        if not proxy:
            # ``proxy_auth`` is the ready-to-use URL newer builds return. It
            # carries the credentials inline, so use it for both parts.
            proxy = str(data.get("proxy_auth") or "")
            url_user, url_password, proxy = _split_credentials(proxy)
            username = username or url_user
            password = password or url_password
        host, port = _split_proxy_url(proxy, self.proxy_host)
        if self.proxy_host and host in ("127.0.0.1", "localhost", "::1"):
            host = self.proxy_host
        return Instance(
            name=str(data.get("name") or data.get("instance") or f"warp-{port}"),
            host=host,
            port=port,
            username=username,
            password=password,
            advertised_ip=str(data.get("ip") or ""),
            session_id=str(data.get("session_id") or session_id),
        )

    def release(self, session_id: str) -> bool:
        """Drop a lease so the next acquire can return a different exit."""
        try:
            self.request(
                "POST", "/api/pool/release", payload={"session_id": session_id}
            )
            return True
        except WarpGateError as exc:
            if exc.code == CODE_NO_SESSION:
                return False
            raise

    def rotate_instance(self, instance: Instance, timeout: float = 180.0) -> bool:
        """Delete an instance's WARP account and register a new one.

        This is the only call that changes the block an instance exits from.
        ``report`` is not a substitute: WarpGate restarts the tunnel on the same
        account, so the exit stays where it was.  Registration is two Cloudflare
        calls plus a tunnel start, which is why the timeout is minutes and not
        seconds.
        """
        payload: dict[str, Any] = {"name": instance.name}
        index = index_from_name(instance.name)
        if index is not None:
            payload["index"] = index
        try:
            self.request(
                "POST", "/api/pool/rotate", payload=payload, timeout=timeout
            )
            return True
        except WarpGateError:
            return False

    def report(
        self, session_id: str, status: int = 0, failure_type: str = ""
    ) -> bool:
        """Tell WarpGate a tunnel misbehaved so it can rebuild the instance."""
        payload: dict[str, Any] = {"session_id": session_id}
        code = int(status or 0)
        if code:
            # WarpGate meters ``result`` (the HTTP status it should count).
            # ``status`` is kept for older builds that used that name.
            payload["result"] = code
            payload["status"] = code
        else:
            payload["failure_type"] = failure_type or "proxy_dead"
        if failure_type:
            payload["failure_type"] = failure_type
        try:
            self.request("POST", "/api/pool/report", payload=payload)
            return True
        except WarpGateError:
            return False

    def stats(self, timeout: float = 4.0) -> PoolStats:
        """Best-effort capacity read.  Never raises."""
        try:
            data = self.request("GET", "/api/stats", timeout=timeout)
        except WarpGateError as exc:
            return PoolStats(reachable=False, detail=str(exc))
        result = PoolStats(reachable=True, raw=data)
        for key, attribute in (
            # `total_instances` is what the Rust control plane reports; the
            # others are older spellings kept so a version bump cannot blind
            # the capacity read.
            ("total_instances", "instances"),
            ("total", "instances"),
            ("instances", "instances"),
            ("count", "instances"),
            ("available", "available"),
            ("healthy", "available"),
            ("in_use", "in_use"),
            ("busy", "in_use"),
            ("sessions", "sessions"),
            ("active_sessions", "sessions"),
            ("sticky_sessions", "sessions"),
        ):
            value = data.get(key)
            if isinstance(value, (int, float)) and not getattr(result, attribute):
                setattr(result, attribute, int(value))
        if not result.instances:
            # Builds without a count in /api/stats only expose the per-instance
            # list, and that endpoint answers with the array itself -- not an
            # object wrapping `items`.
            try:
                listing = self.request("GET", "/api/containers", timeout=timeout)
                if isinstance(listing, dict):
                    listing = (
                        listing.get("items") or listing.get("containers") or []
                    )
                if isinstance(listing, list):
                    result.instances = len(listing)
                    result.available = sum(
                        1
                        for item in listing
                        if isinstance(item, dict)
                        and str(item.get("health", "")).lower() != "unhealthy"
                    )
            except WarpGateError:
                pass
        return result

    def instances(self, timeout: float = 6.0) -> list[InstanceHealth]:
        """Per-instance view: health, dedupe flag, and Cloudflare 403 rate.

        ``/api/containers`` answers with the array itself on the Rust build and
        with an ``items`` wrapper on the Node build; both are accepted. This is
        the only endpoint that carries the two facts the relay must respect:
        an instance an operator has pinned, and one the gate already knows is
        returning 403.
        """
        data = self.request("GET", "/api/containers", timeout=timeout)
        if isinstance(data, dict):
            data = data.get("items") or data.get("containers") or []
        if not isinstance(data, list):
            return []
        out: list[InstanceHealth] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("container") or item.get("name") or "")
            if not name:
                continue
            rate = item.get("cf_403_rate")
            if not isinstance(rate, (int, float)):
                rate = None
            checks = item.get("cf_check_count")
            port = item.get("host_port")
            out.append(
                InstanceHealth(
                    name=name,
                    health=str(item.get("health") or "unknown").lower(),
                    dedicated=bool(item.get("dedicated")),
                    cf_403_rate=float(rate) if rate is not None else None,
                    cf_checks=int(checks) if isinstance(checks, int) else 0,
                    port=int(port) if isinstance(port, int) else 0,
                )
            )
        return out

    def proxies(self, count: int = 1, ip_mode: str = "ip6") -> list[Instance]:
        """Bulk extract, used by ``doctor`` to survey the pool."""
        data = self.request(
            "GET",
            "/api/proxies",
            query={
                "num": str(max(1, int(count))),
                "type": "json",
                "ip_mode": ip_mode,
            },
        )
        items = data.get("items") or data.get("list") or data.get("proxies") or []
        if isinstance(items, dict):
            items = [items]
        out: list[Instance] = []
        for item in items:
            if isinstance(item, str):
                user, password, raw = _split_credentials(item)
                host, port = _split_proxy_url(raw, self.proxy_host)
                out.append(
                    Instance(
                        name=f"warp-{port}",
                        host=host,
                        port=port,
                        username=user or self.username,
                        password=password or self.password,
                    )
                )
                continue
            if not isinstance(item, dict):
                continue
            raw = str(item.get("proxy") or item.get("url") or "")
            username = str(item.get("username") or self.username)
            password = str(item.get("password") or self.password)
            if not raw:
                raw = str(item.get("proxy_auth") or "")
                url_user, url_password, raw = _split_credentials(raw)
                username = username or url_user
                password = password or url_password
            host, port = _split_proxy_url(raw, self.proxy_host)
            out.append(
                Instance(
                    name=str(item.get("name") or f"warp-{port}"),
                    host=host,
                    port=port,
                    username=username,
                    password=password,
                    advertised_ip=str(item.get("ip") or ""),
                )
            )
        return out

    def session_id_for(self, client_id: str) -> str:
        """Stable lease name for a relay client.

        The prefix keeps WarpGate leases from colliding with anything else an
        operator runs against the same pool.
        """
        return f"blockrelay-{client_id}"


# --------------------------------------------------------------------------- #
# SOCKS5
# --------------------------------------------------------------------------- #


async def _read_exact(
    loop: asyncio.AbstractEventLoop, sock: socket.socket, count: int, timeout: float
) -> bytes:
    buffer = b""
    deadline = loop.time() + timeout
    while len(buffer) < count:
        left = deadline - loop.time()
        if left <= 0:
            raise TimeoutError("SOCKS5 handshake timed out")
        chunk = await asyncio.wait_for(loop.sock_recv(sock, count - len(buffer)), left)
        if not chunk:
            raise ConnectionError("SOCKS5 peer closed during handshake")
        buffer += chunk
    return buffer


def _connect_request(address: str, port: int) -> bytes:
    """Build a SOCKS5 CONNECT for a hostname or an address literal."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raw = address.encode("idna")
        if len(raw) > 255:
            raise ValueError("hostname is too long for SOCKS5")
        return b"\x05\x01\x00\x03" + bytes([len(raw)]) + raw + port.to_bytes(2, "big")
    if ip.version == 4:
        return b"\x05\x01\x00\x01" + ip.packed + port.to_bytes(2, "big")
    return b"\x05\x01\x00\x04" + ip.packed + port.to_bytes(2, "big")


async def open_socks5(
    host: str,
    port: int,
    socks_host: str,
    socks_port: int,
    *,
    username: str = "",
    password: str = "",
    timeout: float = 15.0,
) -> socket.socket:
    """Connect ``host:port`` through a SOCKS5 proxy, returning a live socket.

    The destination is handed to the proxy as-is.  When the caller has already
    resolved an address, passing the literal keeps the exit family predictable;
    that matters here because the whole point of the lane is which address the
    other end sees.
    """
    loop = asyncio.get_running_loop()
    family = socket.AF_INET
    try:
        proxy_ip = ipaddress.ip_address(socks_host)
        family = socket.AF_INET6 if proxy_ip.version == 6 else socket.AF_INET
    except ValueError:
        pass
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(
            loop.sock_connect(sock, (socks_host, socks_port)), timeout
        )
        if username:
            await asyncio.wait_for(
                loop.sock_sendall(sock, b"\x05\x02\x00\x02"), timeout
            )
        else:
            await asyncio.wait_for(loop.sock_sendall(sock, b"\x05\x01\x00"), timeout)
        version, method = await _read_exact(loop, sock, 2, timeout)
        if version != 5:
            raise ConnectionError("SOCKS5 proxy answered with the wrong version")
        if method == 2:
            user = username.encode("utf-8")[:255]
            secret = password.encode("utf-8")[:255]
            auth = (
                b"\x01"
                + bytes([len(user)])
                + user
                + bytes([len(secret)])
                + secret
            )
            await asyncio.wait_for(loop.sock_sendall(sock, auth), timeout)
            _version, status = await _read_exact(loop, sock, 2, timeout)
            if status != 0:
                raise ConnectionError("SOCKS5 proxy rejected the credentials")
        elif method != 0:
            raise ConnectionError("SOCKS5 proxy requires an unsupported auth method")

        await asyncio.wait_for(
            loop.sock_sendall(sock, _connect_request(host, port)), timeout
        )
        _version, reply, _reserved, atyp = await _read_exact(loop, sock, 4, timeout)
        if reply != 0:
            raise ConnectionError(f"SOCKS5 proxy refused the target (code {reply})")
        if atyp == 1:
            await _read_exact(loop, sock, 4 + 2, timeout)
        elif atyp == 4:
            await _read_exact(loop, sock, 16 + 2, timeout)
        elif atyp == 3:
            length = (await _read_exact(loop, sock, 1, timeout))[0]
            await _read_exact(loop, sock, length + 2, timeout)
        else:
            raise ConnectionError("SOCKS5 proxy sent a malformed reply")
        return sock
    except BaseException:
        try:
            sock.close()
        except Exception:
            pass
        raise


# --------------------------------------------------------------------------- #
# Egress probe
# --------------------------------------------------------------------------- #


def _trace_over_socket(sock: socket.socket, timeout: float) -> str:
    context = ssl.create_default_context()
    sock.settimeout(timeout)
    with context.wrap_socket(sock, server_hostname=TRACE_HOST) as tls:
        tls.sendall(TRACE_REQUEST)
        data = b""
        while len(data) < 65536:
            chunk = tls.recv(8192)
            if not chunk:
                break
            data += chunk
    for line in data.decode("utf-8", "replace").splitlines():
        if line.startswith("ip="):
            return line.split("=", 1)[1].strip()
    return ""


async def _probe_async(instance: Instance, timeout: float) -> str:
    addresses = await asyncio.to_thread(_trace_addresses)
    last: Exception | None = None
    for address in addresses:
        try:
            sock = await open_socks5(
                address,
                443,
                instance.host,
                instance.port,
                username=instance.username,
                password=instance.password,
                timeout=timeout,
            )
        except Exception as exc:
            last = exc
            continue
        try:
            return await asyncio.to_thread(_trace_over_socket, sock, timeout)
        except Exception as exc:
            last = exc
            try:
                sock.close()
            except Exception:
                pass
    raise ConnectionError(f"no trace through {instance.endpoint}: {last}")


def _trace_addresses() -> list[str]:
    """IPv6 first so the exit is the per-registration address, not the NAT pool."""
    try:
        infos = socket.getaddrinfo(TRACE_HOST, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    v6: list[str] = []
    v4: list[str] = []
    for family, _type, _proto, _canon, address in infos:
        bucket = v6 if family == socket.AF_INET6 else v4
        if address[0] not in bucket:
            bucket.append(address[0])
    return v6 + v4


def probe_egress(instance: Instance, timeout: float = 10.0) -> str:
    """Return the public address a SOCKS5 instance exits from, or ``""``.

    Runs its own event loop, so call it from a worker thread when the caller is
    already inside one.
    """
    try:
        return asyncio.run(_probe_async(instance, timeout))
    except Exception:
        return ""


def block_of(address: str) -> str:
    """The /64 the upstream limiter meters, or ``""`` for an unusable address."""
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return ""
    if ip.version != 6:
        return ""
    return str(ipaddress.IPv6Network((ip, 64), strict=False))


def probe_block(instance: Instance, timeout: float = 10.0) -> tuple[str, str]:
    """``(egress, block)`` for an instance, both empty when the probe fails."""
    egress = probe_egress(instance, timeout)
    return egress, block_of(egress)
