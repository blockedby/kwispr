#!/usr/bin/env python3
"""Regression tests for reproducible setup.sh desktop integration."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_SH = REPO_ROOT / "setup.sh"


class SetupContractTest(unittest.TestCase):
    def test_ydotool_uses_user_service_with_kwispr_socket(self) -> None:
        setup = SETUP_SH.read_text(encoding="utf-8")

        self.assertIn("systemctl --user enable --now ydotool.service", setup)
        self.assertIn("systemctl --user reset-failed ydotool.service", setup)
        self.assertIn("--socket-path=%t/.ydotool_socket --socket-perm=0600", setup)
        self.assertIn("$HOME/.config/systemd/user", setup)
        self.assertIn("ydotool.service.d", setup)
        self.assertNotIn("/etc/systemd/system/ydotoold.service", setup)
        self.assertNotRegex(setup, re.compile(r"systemctl\s+enable\s+--now\s+ydotoold\.service"))

    def test_ydotool_grants_current_session_uinput_access(self) -> None:
        setup = SETUP_SH.read_text(encoding="utf-8")

        self.assertIn("setfacl -m \"u:$USER:rw\" /dev/uinput", setup)
        self.assertIn("udevadm trigger", setup)

    def test_setup_does_not_install_podman_implicitly(self) -> None:
        setup = SETUP_SH.read_text(encoding="utf-8")

        package_lines = "\n".join(
            line for line in setup.splitlines() if "pacman -S" in line or "apt install" in line
        )
        self.assertNotIn("podman", package_lines)
        self.assertIn("ffmpeg", package_lines)


if __name__ == "__main__":
    unittest.main()
