#!/usr/bin/env python3
"""Functional contracts for the rootless Kwispr installer."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "install.sh"


class InstallerContractTest(unittest.TestCase):
    def test_default_install_builds_without_running_test_wrapper(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('run_step "Building KDE Whisper in Podman" "$ROOT_DIR/kde-whisper/scripts/podman-build.sh"', installer)
        self.assertIn('if [[ "$RUN_TESTS" == 1 ]]', installer)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.prefix = self.root / "prefix"
        self.config_home = self.root / "config"
        self.data_home = self.root / "data"
        self.home.mkdir()
        self.fake_kde = self.root / "kde-whisper"
        self.fake_kde.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"${KWISPR_RUNTIME_ROOT:-}\" \"$*\" > \"${KWISPR_FAKE_LOG:-/dev/null}\"\n",
            encoding="utf-8",
        )
        self.fake_kde.chmod(0o755)
        self.legacy_config = self.root / "legacy.env"
        self.legacy_config.write_text(
            "KWISPR_BACKEND=openrouter-chat\n"
            "KWISPR_API_KEY='not-a-real-secret'\n"
            "KWISPR_MODEL_DIR=~/.local/share/kwispr/models\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.config_home),
                "XDG_DATA_HOME": str(self.data_home),
                "KWISPR_KDE_BINARY": str(self.fake_kde),
                "KWISPR_LEGACY_CONFIG": str(self.legacy_config),
                "KWISPR_INSTALL_NO_SYSTEMD": "1",
            }
        )
        return env

    def install(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(INSTALLER),
                "--prefix",
                str(self.prefix),
                "--skip-build",
                "--yes",
                "--plain",
                "--no-open-settings",
                "--no-systemd-actions",
                *extra,
            ],
            cwd=REPO_ROOT,
            env=self.env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )

    def test_cloud_install_is_self_contained_and_migrates_config(self) -> None:
        result = self.install("--without-local-stt", "--autostart")
        self.assertEqual(result.returncode, 0, result.stderr)

        runtime = self.prefix / "share" / "kwispr" / "runtime"
        cli = self.prefix / "bin" / "kwispr"
        tray = self.prefix / "bin" / "kde-whisper"
        config = self.config_home / "kwispr" / "config.env"
        desktop = self.prefix / "share" / "applications" / "org.kwispr.KdeWhisper.desktop"
        autostart = self.config_home / "autostart" / "org.kwispr.KdeWhisper.desktop"

        for path in (
            cli,
            tray,
            runtime / "kwispr.sh",
            runtime / "kwispr-models.py",
            runtime / "models" / "local-stt-catalog.json",
            desktop,
            autostart,
            config,
        ):
            self.assertTrue(path.exists(), path)

        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(config.read_text(encoding="utf-8"), self.legacy_config.read_text(encoding="utf-8"))
        self.assertIn(f'Exec="{tray}"', desktop.read_text(encoding="utf-8"))
        self.assertIn(f'Exec="{tray}"', autostart.read_text(encoding="utf-8"))
        if shutil.which("desktop-file-validate"):
            validated = subprocess.run(
                ["desktop-file-validate", str(desktop)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)

        fake_log = self.root / "fake-kde.log"
        run_env = self.env()
        run_env["KWISPR_FAKE_LOG"] = str(fake_log)
        settings = subprocess.run(
            [str(cli), "settings"], env=run_env, text=True, capture_output=True, timeout=10
        )
        self.assertEqual(settings.returncode, 0, settings.stderr)
        lines = fake_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], str(runtime))
        self.assertEqual(lines[1], "--settings")

        models = subprocess.run(
            [str(cli), "models", "list"], env=run_env, text=True, capture_output=True, timeout=10
        )
        self.assertEqual(models.returncode, 0, models.stderr)
        self.assertIn("whisper-large-v3-turbo", models.stdout)

    def test_local_install_copies_runtime_and_writes_portable_service(self) -> None:
        release = self.root / "release"
        release.mkdir()
        binary = release / "kwispr-local-stt"
        binary.write_text(
            "#!/usr/bin/env bash\nenv | grep '^KWISPR_' > \"${KWISPR_CAPTURE_PATH:-/dev/null}\"\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        (release / "libtranscribe.so.0").write_bytes(b"transcribe")
        (release / "libggml.so.0").write_bytes(b"ggml")

        env = self.env()
        env["KWISPR_LOCAL_STT_RELEASE_DIR"] = str(release)
        result = subprocess.run(
            [
                str(INSTALLER),
                "--prefix",
                str(self.prefix),
                "--skip-build",
                "--yes",
                "--plain",
                "--with-local-stt",
                "--no-local-stt-autostart",
                "--no-autostart",
                "--no-open-settings",
                "--no-systemd-actions",
            ],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        release_dest = self.prefix / "share" / "kwispr" / "runtime" / "rust-local-stt" / "target" / "release"
        self.assertTrue((release_dest / "kwispr-local-stt").exists())
        self.assertTrue((release_dest / "libtranscribe.so.0").exists())
        self.assertTrue((release_dest / "libggml.so.0").exists())

        service = self.config_home / "systemd" / "user" / "kwispr-local-stt.service"
        service_text = service.read_text(encoding="utf-8")
        launcher = self.prefix / "share" / "kwispr" / "runtime" / "start-local-stt.sh"
        launcher_text = launcher.read_text(encoding="utf-8")
        self.assertIn(str(launcher), service_text)
        self.assertNotIn("EnvironmentFile", service_text)
        self.assertIn(str(release_dest / "kwispr-local-stt"), launcher_text)
        self.assertIn(str(self.config_home / "kwispr" / "config.env"), launcher_text)
        self.assertIn(str(self.data_home / "kwispr" / "models"), launcher_text)
        self.assertIn("unset KWISPR_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY", launcher_text)
        self.assertNotIn(str(REPO_ROOT), service_text + launcher_text)

        captured_env = self.root / "local-stt.env"
        launcher_env = self.env()
        launcher_env["KWISPR_CAPTURE_PATH"] = str(captured_env)
        launched = subprocess.run(
            [str(launcher)],
            env=launcher_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        self.assertEqual(launched.returncode, 0, launched.stderr)
        child_environment = captured_env.read_text(encoding="utf-8")
        self.assertNotIn("KWISPR_API_KEY=", child_environment)
        self.assertIn(f"KWISPR_MODEL_DIR={self.home / '.local' / 'share' / 'kwispr' / 'models'}", child_environment)

        if shutil.which("systemd-analyze"):
            verified = subprocess.run(
                ["systemd-analyze", "--user", "verify", str(service)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_local_install_accepts_self_contained_binary(self) -> None:
        release = self.root / "static-release"
        release.mkdir()
        binary = release / "kwispr-local-stt"
        binary.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)

        env = self.env()
        env["KWISPR_LOCAL_STT_RELEASE_DIR"] = str(release)
        result = subprocess.run(
            [
                str(INSTALLER),
                "--prefix",
                str(self.prefix),
                "--skip-build",
                "--yes",
                "--plain",
                "--with-local-stt",
                "--no-local-stt-autostart",
                "--no-autostart",
                "--no-open-settings",
                "--no-systemd-actions",
            ],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = self.prefix / "share" / "kwispr" / "runtime" / "rust-local-stt" / "target" / "release" / "kwispr-local-stt"
        self.assertTrue(installed.exists())
        self.assertIn("self-contained", result.stdout)

    def test_cloud_upgrade_removes_local_runtime_but_preserves_user_data(self) -> None:
        release = self.root / "upgrade-release"
        release.mkdir()
        binary = release / "kwispr-local-stt"
        binary.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        env = self.env()
        env["KWISPR_LOCAL_STT_RELEASE_DIR"] = str(release)

        local_install = subprocess.run(
            [
                str(INSTALLER), "--prefix", str(self.prefix), "--skip-build", "--yes", "--plain",
                "--with-local-stt", "--no-local-stt-autostart", "--no-autostart",
                "--no-open-settings", "--no-systemd-actions",
            ],
            cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(local_install.returncode, 0, local_install.stderr)
        model = self.data_home / "kwispr" / "models" / "keep.gguf"
        model.write_bytes(b"model")

        cloud_install = self.install("--without-local-stt", "--no-autostart")
        self.assertEqual(cloud_install.returncode, 0, cloud_install.stderr)
        self.assertFalse((self.config_home / "systemd" / "user" / "kwispr-local-stt.service").exists())
        self.assertFalse((self.prefix / "share" / "kwispr" / "runtime" / "rust-local-stt").exists())
        self.assertFalse((self.prefix / "share" / "kwispr" / "runtime" / "start-local-stt.sh").exists())
        self.assertTrue((self.config_home / "kwispr" / "config.env").exists())
        self.assertTrue(model.exists())

    def test_uninstall_preserves_user_config_and_models(self) -> None:
        installed = self.install("--without-local-stt", "--no-autostart")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        model = self.data_home / "kwispr" / "models" / "keep.gguf"
        model.write_bytes(b"model")

        removed = subprocess.run(
            [
                str(INSTALLER),
                "--prefix",
                str(self.prefix),
                "--plain",
                "--yes",
                "--no-systemd-actions",
                "--uninstall",
            ],
            cwd=REPO_ROOT,
            env=self.env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse((self.prefix / "bin" / "kwispr").exists())
        self.assertTrue((self.config_home / "kwispr" / "config.env").exists())
        self.assertTrue(model.exists())


if __name__ == "__main__":
    unittest.main()
