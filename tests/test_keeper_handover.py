"""Keeper hand-over: a dead WARP tunnel must not leave the lane rotating nothing."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import proxy_chain
import tor_manager
import warp_keeper


class HandOverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.lane = Path(self._tmp.name)
        self.log = self.lane / "keeper.log"
        self._patches = [
            mock.patch.object(warp_keeper, "LANE_DIR", self.lane),
            mock.patch.object(warp_keeper, "KEEPER_LOG_FILE", self.log),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_a_dead_warp_tunnel_moves_the_lane_to_tor(self) -> None:
        with mock.patch.object(
            tor_manager, "get_active_tor_target", return_value=("127.0.0.1", 9050)
        ), mock.patch.object(
            proxy_chain,
            "get_egress_config",
            return_value={
                "mode": "warp",
                "warp_scope": "tun",
                "custom_proxy": "127.0.0.1:10808",
                "custom_type": "socks5",
            },
        ), mock.patch.object(proxy_chain, "set_egress_config") as save:
            self.assertTrue(warp_keeper._hand_over_to_tor("warp"))

        self.assertEqual(save.call_args.args[0], "tor")
        self.assertIn("switched to Tor", self.log.read_text(encoding="utf-8"))

    def test_a_strict_proxy_is_never_switched_behind_the_users_back(self) -> None:
        (self.lane / "strict_proxy.flag").write_text("1", encoding="utf-8")

        with mock.patch.object(proxy_chain, "set_egress_config") as save:
            self.assertFalse(warp_keeper._hand_over_to_tor("warp"))

        save.assert_not_called()

    def test_the_lane_is_left_alone_when_it_already_runs_on_tor(self) -> None:
        with mock.patch.object(proxy_chain, "set_egress_config") as save:
            self.assertFalse(warp_keeper._hand_over_to_tor("tor"))

        save.assert_not_called()

    def test_no_hand_over_when_tor_cannot_start(self) -> None:
        with mock.patch.object(
            tor_manager, "get_active_tor_target", return_value=None
        ), mock.patch.object(
            tor_manager, "start_portable_tor", return_value=(False, "no binary")
        ), mock.patch.object(proxy_chain, "set_egress_config") as save:
            self.assertFalse(warp_keeper._hand_over_to_tor("warp"))

        save.assert_not_called()
