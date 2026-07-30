#!/usr/bin/env python3
"""Functional contracts for the rootless Kwispr installer."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "install.sh"


class InstallerContractTest(unittest.TestCase):
    def test_default_install_builds_without_running_test_wrapper(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('run_step "Building KDE Whisper in Podman" "$ROOT_DIR/kde-whisper/scripts/podman-build.sh"', installer)
        self.assertIn('if [[ "$RUN_TESTS" == 1 ]]', installer)

    def test_tray_shutdown_uses_pidfds_instead_of_reusable_numeric_pids(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("os.pidfd_open(pid)", installer)
        self.assertIn("signal.pidfd_send_signal(pidfd, signal.SIGTERM)", installer)
        self.assertIn("signal.pidfd_send_signal(pidfd, signal.SIGKILL)", installer)

    def test_native_dependency_setup_precedes_python_option_validation(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        main_flow = installer[installer.index("resolve_build_backend\nprepare_native_arch_packages") :]
        self.assertLess(
            main_flow.index("prepare_native_arch_packages"),
            main_flow.index('validate_local_stt_options || fail "Invalid Local STT option"'),
        )

    def test_tray_uses_configured_health_endpoint_and_restarts_changed_service(self) -> None:
        tray_app = (REPO_ROOT / "kde-whisper" / "src" / "ui" / "TrayApp.cpp").read_text(encoding="utf-8")
        self.assertIn("settings.localSttHealthUrl()", tray_app)
        self.assertIn("savedSettings.localSttHost != previousLocalSttHost", tray_app)
        self.assertIn("savedSettings.localSttPort != previousLocalSttPort", tray_app)
        self.assertIn('QStringLiteral("restart")', tray_app)
        self.assertNotIn("127.0.0.1:19650", tray_app)

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

    def start_stale_installed_tray(self) -> subprocess.Popen[bytes]:
        installed_tray = self.prefix / "lib" / "kwispr" / "kde-whisper"
        sleep_binary = shutil.which("sleep")
        self.assertIsNotNone(sleep_binary)
        shutil.copy2(sleep_binary, installed_tray)
        tray_process = subprocess.Popen([str(installed_tray), "60"])
        time.sleep(0.1)
        self.assertIsNone(tray_process.poll())

        replacement = self.root / f"replacement-{tray_process.pid}"
        shutil.copy2(sleep_binary, replacement)
        os.replace(replacement, installed_tray)
        executable = os.readlink(f"/proc/{tray_process.pid}/exe")
        self.assertTrue(executable.endswith("kde-whisper (deleted)"), executable)
        return tray_process

    def test_cloud_install_is_self_contained_and_migrates_config(self) -> None:
        result = self.install("--without-local-stt", "--autostart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Kwispr tray added to graphical-session autostart", result.stdout)

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

    def test_autostart_install_starts_tray_in_active_graphical_session(self) -> None:
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        systemctl = fake_bin / "systemctl"
        systemctl.write_text(
            "#!/usr/bin/env bash\n"
            "[[ \"$*\" == *\"is-active graphical-session.target\"* ]]\n",
            encoding="utf-8",
        )
        systemctl.chmod(0o755)
        systemd_run_log = self.root / "systemd-run.log"
        systemd_run = fake_bin / "systemd-run"
        systemd_run.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$@\" > \"$KWISPR_SYSTEMD_RUN_LOG\"\n"
            "command=\"${!#}\"\n"
            "\"$command\"\n",
            encoding="utf-8",
        )
        systemd_run.chmod(0o755)

        fake_kde_log = self.root / "started-kde.log"
        env = self.env()
        env.update(
            {
                "KWISPR_INSTALL_NO_SYSTEMD": "0",
                "KWISPR_SYSTEMD_RUN_LOG": str(systemd_run_log),
                "KWISPR_FAKE_LOG": str(fake_kde_log),
                "PATH": f"{fake_bin}:{env['PATH']}",
            }
        )
        result = subprocess.run(
            [
                str(INSTALLER), "--prefix", str(self.prefix), "--skip-build", "--yes", "--plain",
                "--without-local-stt", "--autostart", "--no-open-settings",
            ],
            cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Kwispr tray started in the current graphical session", result.stdout)
        self.assertTrue(fake_kde_log.exists())
        run_arguments = systemd_run_log.read_text(encoding="utf-8")
        self.assertIn("--collect\n", run_arguments)
        self.assertIn("--service-type=exec\n", run_arguments)
        self.assertIn("PartOf=graphical-session.target", run_arguments)
        self.assertIn(str(self.prefix / "bin" / "kde-whisper"), run_arguments)
        self.assertTrue((self.config_home / "autostart" / "org.kwispr.KdeWhisper.desktop").exists())

    def test_autostart_install_defers_without_active_graphical_session(self) -> None:
        fake_bin = self.root / "inactive-session-bin"
        fake_bin.mkdir()
        systemctl = fake_bin / "systemctl"
        systemctl.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        systemctl.chmod(0o755)
        unexpected_run = self.root / "unexpected-systemd-run"
        systemd_run = fake_bin / "systemd-run"
        systemd_run.write_text(
            f"#!/usr/bin/env bash\ntouch {unexpected_run}\n",
            encoding="utf-8",
        )
        systemd_run.chmod(0o755)

        env = self.env()
        env.update({"KWISPR_INSTALL_NO_SYSTEMD": "0", "PATH": f"{fake_bin}:{env['PATH']}"})
        result = subprocess.run(
            [
                str(INSTALLER), "--prefix", str(self.prefix), "--skip-build", "--yes", "--plain",
                "--without-local-stt", "--autostart", "--no-open-settings",
            ],
            cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no active graphical session was found", result.stdout)
        self.assertFalse(unexpected_run.exists())
        self.assertTrue((self.config_home / "autostart" / "org.kwispr.KdeWhisper.desktop").exists())

    def test_local_install_copies_runtime_and_writes_portable_service(self) -> None:
        release = self.root / "release"
        release.mkdir()
        binary = release / "kwispr-local-stt"
        binary.write_text(
            "#!/usr/bin/env bash\n"
            "env | grep '^KWISPR_' > \"${KWISPR_CAPTURE_PATH:-/dev/null}\"\n"
            "printf '%s\\n' \"$@\" > \"${KWISPR_CAPTURE_PATH:-/dev/null}.args\"\n",
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
        launcher_args = Path(f"{captured_env}.args").read_text(encoding="utf-8").splitlines()
        self.assertEqual(launcher_args[launcher_args.index("--host") + 1], "127.0.0.1")
        self.assertEqual(launcher_args[launcher_args.index("--port") + 1], "19650")

        if shutil.which("systemd-analyze"):
            verified = subprocess.run(
                ["systemd-analyze", "--user", "verify", str(service)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_fresh_local_install_and_lan_options_use_19650_without_wildcard_client(self) -> None:
        release = self.root / "fresh-release"
        release.mkdir()
        binary = release / "kwispr-local-stt"
        binary.write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"${KWISPR_CAPTURE_PATH:-/dev/null}.args\"\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        env = self.env()
        env["KWISPR_LOCAL_STT_RELEASE_DIR"] = str(release)
        env["KWISPR_LEGACY_CONFIG"] = str(self.root / "missing-legacy.env")
        result = subprocess.run(
            [
                str(INSTALLER), "--prefix", str(self.prefix), "--skip-build", "--yes", "--plain",
                "--with-local-stt", "--local-stt-host", "0.0.0.0", "--local-stt-port", "19650",
                "--no-local-stt-autostart", "--no-autostart", "--no-open-settings", "--no-systemd-actions",
            ],
            cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.config_home / "kwispr" / "config.env").read_text(encoding="utf-8")
        self.assertIn("KWISPR_LOCAL_STT_HOST=0.0.0.0\n", config)
        self.assertIn("KWISPR_LOCAL_STT_PORT=19650\n", config)
        self.assertIn("KWISPR_API_URL=http://127.0.0.1:19650/v1/audio/transcriptions\n", config)
        self.assertNotIn("KWISPR_API_URL=http://0.0.0.0", config)

        captured = self.root / "fresh-launch"
        launch_env = env.copy()
        launch_env["KWISPR_CAPTURE_PATH"] = str(captured)
        launcher = self.prefix / "share" / "kwispr" / "runtime" / "start-local-stt.sh"
        launched = subprocess.run([str(launcher)], env=launch_env, text=True, capture_output=True, timeout=10)
        self.assertEqual(launched.returncode, 0, launched.stderr)
        args = Path(f"{captured}.args").read_text(encoding="utf-8").splitlines()
        self.assertEqual(args[args.index("--host") + 1], "0.0.0.0")
        self.assertEqual(args[args.index("--port") + 1], "19650")

    def test_remote_only_client_needs_no_local_runtime_artifact(self) -> None:
        env = self.env()
        env["KWISPR_LEGACY_CONFIG"] = str(self.root / "missing-legacy.env")
        result = subprocess.run(
            [
                str(INSTALLER), "--prefix", str(self.prefix), "--skip-build", "--yes", "--plain",
                "--without-local-stt", "--local-stt-url",
                "http://inference-box.lan:19650/v1/audio/transcriptions",
                "--local-stt-model", "parakeet-tdt", "--no-autostart", "--no-open-settings",
                "--no-systemd-actions",
            ],
            cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.config_home / "kwispr" / "config.env").read_text(encoding="utf-8")
        self.assertIn("KWISPR_API_URL=http://inference-box.lan:19650/v1/audio/transcriptions\n", config)
        self.assertIn("KWISPR_MODEL=parakeet-tdt\n", config)
        self.assertIn("KWISPR_LOCAL_STT_CONFIGURED=1\n", config)
        self.assertFalse((self.prefix / "share" / "kwispr" / "runtime" / "rust-local-stt").exists())
        self.assertFalse((self.config_home / "systemd" / "user" / "kwispr-local-stt.service").exists())

    def test_legacy_generated_9000_config_keeps_matched_server_port(self) -> None:
        self.legacy_config.write_text(
            "KWISPR_BACKEND=openai-transcriptions\n"
            "KWISPR_API_URL=http://127.0.0.1:9000/v1/audio/transcriptions\n"
            "KWISPR_MODEL=whisper-large-v3-turbo\n",
            encoding="utf-8",
        )
        release = self.root / "legacy-release"
        release.mkdir()
        binary = release / "kwispr-local-stt"
        binary.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        env = self.env()
        env["KWISPR_LOCAL_STT_RELEASE_DIR"] = str(release)
        result = subprocess.run(
            [
                str(INSTALLER), "--prefix", str(self.prefix), "--skip-build", "--yes", "--plain",
                "--with-local-stt", "--no-local-stt-autostart", "--no-autostart",
                "--no-open-settings", "--no-systemd-actions",
            ],
            cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config_path = self.config_home / "kwispr" / "config.env"
        config = config_path.read_text(encoding="utf-8")
        self.assertIn("KWISPR_API_URL=http://127.0.0.1:9000/v1/audio/transcriptions\n", config)
        self.assertIn("KWISPR_LOCAL_STT_PORT=9000\n", config)

        rerun = subprocess.run(
            [
                str(INSTALLER), "--prefix", str(self.prefix), "--skip-build", "--yes", "--plain",
                "--with-local-stt", "--no-local-stt-autostart", "--no-autostart",
                "--no-open-settings", "--no-systemd-actions",
            ],
            cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertEqual(config_path.read_text(encoding="utf-8"), config)

    def test_installer_rerun_preserves_custom_client_endpoint_and_bind_port(self) -> None:
        env = self.env()
        env["KWISPR_LEGACY_CONFIG"] = str(self.root / "missing-legacy.env")
        first = subprocess.run(
            [
                str(INSTALLER), "--prefix", str(self.prefix), "--skip-build", "--yes", "--plain",
                "--without-local-stt", "--local-stt-url", "http://speech.lan:32100/v1/audio/transcriptions",
                "--local-stt-port", "32101", "--no-autostart", "--no-open-settings", "--no-systemd-actions",
            ],
            cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        config_path = self.config_home / "kwispr" / "config.env"
        expected = config_path.read_text(encoding="utf-8")
        second = subprocess.run(
            [
                str(INSTALLER), "--prefix", str(self.prefix), "--skip-build", "--yes", "--plain",
                "--without-local-stt", "--no-autostart", "--no-open-settings", "--no-systemd-actions",
            ],
            cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(config_path.read_text(encoding="utf-8"), expected)

    def test_invalid_local_stt_options_do_not_corrupt_existing_config(self) -> None:
        config_path = self.config_home / "kwispr" / "config.env"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("KWISPR_API_URL=http://custom.lan:23456/v1/audio/transcriptions\n", encoding="utf-8")
        before = config_path.read_bytes()
        invalid_options = (
            ("--local-stt-port", "70000"),
            ("--local-stt-port", " 19650"),
            ("--local-stt-port", "1_000"),
            ("--local-stt-url", "http://0.0.0.0:19650/v1/audio/transcriptions"),
        )
        for option, value in invalid_options:
            with self.subTest(option=option, value=value):
                result = self.install("--without-local-stt", option, value, "--no-autostart")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(config_path.read_bytes(), before)

    def test_local_stt_port_option_is_persisted_as_canonical_decimal(self) -> None:
        result = self.install(
            "--without-local-stt", "--local-stt-port", "019650", "--no-autostart"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.config_home / "kwispr" / "config.env").read_text(encoding="utf-8")
        self.assertIn("KWISPR_LOCAL_STT_PORT=19650\n", config)

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

    def test_no_autostart_upgrade_stops_stale_tray_and_preserves_config(self) -> None:
        installed = self.install("--without-local-stt", "--autostart")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        config = self.config_home / "kwispr" / "config.env"
        original_config = config.read_bytes()
        autostart = self.config_home / "autostart" / "org.kwispr.KdeWhisper.desktop"
        tray_process = self.start_stale_installed_tray()

        try:
            upgraded = self.install("--without-local-stt", "--no-autostart")
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            tray_process.wait(timeout=5)
            self.assertIn("Stopped the running Kwispr tray", upgraded.stdout)
            self.assertFalse(autostart.exists())
            self.assertEqual(config.read_bytes(), original_config)
        finally:
            if tray_process.poll() is None:
                tray_process.kill()
                tray_process.wait(timeout=5)

    def test_uninstall_stops_tray_removes_autostart_and_preserves_user_data(self) -> None:
        installed = self.install("--without-local-stt", "--autostart")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        model = self.data_home / "kwispr" / "models" / "keep.gguf"
        model.write_bytes(b"model")
        autostart = self.config_home / "autostart" / "org.kwispr.KdeWhisper.desktop"
        self.assertTrue(autostart.exists())

        tray_process = self.start_stale_installed_tray()

        try:
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
            tray_process.wait(timeout=5)
            self.assertIn("Stopped the running Kwispr tray", removed.stdout)
            self.assertIn("Kwispr tray autostart entry removed", removed.stdout)
            self.assertFalse((self.prefix / "bin" / "kwispr").exists())
            self.assertFalse(autostart.exists())
            self.assertTrue((self.config_home / "kwispr" / "config.env").exists())
            self.assertTrue(model.exists())
        finally:
            if tray_process.poll() is None:
                tray_process.kill()
                tray_process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
