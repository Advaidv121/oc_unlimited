"""Tor lane: the SOCKS handshake has to match what Tor actually accepts."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import tor_manager


class _FakeSocks:
    """Records what the client sent and replays a scripted server reply."""

    def __init__(self, replies: list[bytes]) -> None:
        self.sent: list[bytes] = []
        self._replies = list(replies)

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, size: int) -> bytes:
        return self._replies.pop(0)[:size] if self._replies else b""

    def settimeout(self, _value) -> None:
        pass

    def close(self) -> None:
        pass


class SocksGreetingTests(unittest.TestCase):
    def test_tor_is_not_offered_credentials_that_were_never_supplied(self) -> None:
        # Tor prefers method 2 whenever it is offered, so advertising it
        # without credentials makes every tunnel fail while the proxy itself
        # is perfectly healthy.
        server = _FakeSocks([b"\x05\x00"])

        with mock.patch.object(
            tor_manager.socket, "create_connection", return_value=server
        ):
            try:
                tor_manager.open_socks5_tunnel("example.com", 443, timeout=1.0)
            except Exception:
                pass

        self.assertEqual(server.sent[0], b"\x05\x01\x00")

    def test_credentials_are_offered_only_when_present(self) -> None:
        server = _FakeSocks([b"\x05\x02"])

        with mock.patch.object(
            tor_manager.socket, "create_connection", return_value=server
        ):
            try:
                tor_manager.open_socks5_tunnel(
                    "example.com", 443, timeout=1.0, username="u", password="p"
                )
            except Exception:
                pass

        self.assertEqual(server.sent[0], b"\x05\x02\x00\x02")
