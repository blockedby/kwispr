#!/usr/bin/env python3
"""Arch package-ownership contracts for installer build backends."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "install.sh"

RUNTIME_PACKAGES = {
    "ffmpeg",
    "curl",
    "jq",
    "python",
    "wl-clipboard",
    "libnotify",
    "qt6-base",
    "kcoreaddons",
    "kdbusaddons",
    "kglobalaccel",
    "kstatusnotifieritem",
    "pipewire-pulse",
    "vulkan-icd-loader",
}
KDE_BUILD_PACKAGES = {
    "base-devel",
    "cmake",
    "ninja",
    "extra-cmake-modules",
    "qt6-tools",
    "git",
}
RUST_BUILD_PACKAGES = {
    "rust",
    "clang",
    "pkgconf",
    "vulkan-headers",
    "shaderc",
    "spirv-headers",
    "ccache",
}


FAKE_PACMAN = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_PACMAN_STATE"])
log_path = Path(os.environ["FAKE_PACMAN_LOG"])
deps = json.loads(os.environ.get("FAKE_PACMAN_DEPS", "{}"))
state = json.loads(state_path.read_text())
args = sys.argv[1:]


def save():
    state_path.write_text(json.dumps(state, sort_keys=True))


def log(kind, packages, asdeps=False):
    with log_path.open("a") as handle:
        handle.write(json.dumps({"kind": kind, "packages": packages, "asdeps": asdeps}) + "\n")


def requested():
    if "--" in args:
        return args[args.index("--") + 1:]
    return [arg for arg in args if not arg.startswith("-") and arg != "%n"]


def closure(packages):
    result = []
    seen = set()
    def add(package):
        if package in seen or package in state:
            return
        seen.add(package)
        for dependency in deps.get(package, []):
            add(dependency)
        result.append(package)
    for package in packages:
        add(package)
    return result

if "-Qu" in args:
    print(os.environ.get("FAKE_PACMAN_UPGRADES", ""))
elif args == ["-Q"]:
    print("\n".join(f"{name} 1.0-1" for name in sorted(state)))
elif "-Qqe" in args:
    print("\n".join(sorted(name for name, reason in state.items() if reason == "explicit")))
elif "-Qqd" in args:
    print("\n".join(sorted(name for name, reason in state.items() if reason == "dependency")))
elif "-Qq" in args:
    index = args.index("-Qq")
    if index + 1 < len(args):
        package = args[index + 1]
        if package not in state:
            sys.exit(1)
        print(package)
    else:
        print("\n".join(sorted(state)))
elif "-Sp" in args:
    print("\n".join(closure(requested())))
elif "-S" in args:
    packages = requested()
    asdeps = "--asdeps" in args
    for package in packages:
        for dependency in closure(deps.get(package, [])):
            state.setdefault(dependency, "dependency")
        state.setdefault(package, "dependency" if asdeps else "explicit")
    save()
    log("install", packages, asdeps)
elif "-R" in args:
    packages = requested()
    for package in packages:
        state.pop(package, None)
    save()
    log("remove", packages)
else:
    print("unsupported fake pacman invocation: " + " ".join(args), file=sys.stderr)
    sys.exit(2)
'''

FAKE_SUDO = r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_SUDO_LOG"
exec "$@"
'''

FAKE_COMM = r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s|%s\n' "${LC_ALL:-}" "$*" >> "$FAKE_COMM_LOG"
if [[ "${LC_ALL:-}" != C ]]; then
  printf 'comm must run with LC_ALL=C (got %s)\n' "${LC_ALL:-<unset>}" >&2
  exit 64
fi
exec "$REAL_COMM_BIN" "$@"
'''

FAKE_CMAKE = r'''#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--build" ]]; then
  [[ -z "${FAKE_BUILD_MARKER:-}" ]] || : > "$FAKE_BUILD_MARKER"
  if [[ -n "${FAKE_EXTERNAL_PACKAGE:-}" ]]; then
    python3 - "$FAKE_PACMAN_STATE" "$FAKE_EXTERNAL_PACKAGE" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
state = json.loads(path.read_text())
state[sys.argv[2]] = "explicit"
path.write_text(json.dumps(state, sort_keys=True))
PY
  fi
  if [[ "${FAKE_DELETE_BUILD_PACKAGE_FILE:-0}" == 1 ]]; then
    find "${TMPDIR:?}" -type f -name build-packages-introduced -delete
  fi
  if [[ "${FAKE_CMAKE_SLEEP:-0}" == 1 ]]; then sleep 30; fi
  if [[ "${FAKE_CMAKE_FAIL_BUILD:-0}" == 1 ]]; then exit 42; fi
fi
exit 0
'''

FAKE_CARGO = r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_CARGO_LOG"
exit 0
'''


class ArchBuildBackendContractTest(unittest.TestCase):
    def test_cleanup_does_not_use_recursive_pacman_removal(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertNotIn("pacman -Rns", installer)
        self.assertNotIn("-Rns --noconfirm", installer)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.prefix = self.root / "prefix"
        self.config = self.root / "config"
        self.data = self.root / "data"
        self.bin = self.root / "bin"
        for directory in (self.home, self.bin):
            directory.mkdir(parents=True)

        self.fake_kde = self.root / "kde-whisper"
        self.host_build = REPO_ROOT / "kde-whisper" / "build-host"
        self._write_executable(self.fake_kde, "#!/usr/bin/env bash\nexit 0\n")
        self.release = self.root / "release"
        self.release.mkdir()
        self._write_executable(self.release / "kwispr-local-stt", "#!/usr/bin/env bash\nexit 0\n")

        self.state_path = self.root / "pacman-state.json"
        self.pacman_log = self.root / "pacman.log"
        self.sudo_log = self.root / "sudo.log"
        self.cargo_log = self.root / "cargo.log"
        self.comm_log = self.root / "comm.log"
        self.marker = self.root / "build-started"
        self.pacman_log.touch()
        self.sudo_log.touch()
        self.cargo_log.touch()
        self.comm_log.touch()

        real_comm = shutil.which("comm")
        if real_comm is None:
            self.fail("comm is required for the installer contract tests")
        self.real_comm = real_comm
        self._write_executable(self.bin / "pacman", FAKE_PACMAN)
        self._write_executable(self.bin / "sudo", FAKE_SUDO)
        self._write_executable(self.bin / "comm", FAKE_COMM)
        self._write_executable(self.bin / "cmake", FAKE_CMAKE)
        self._write_executable(self.bin / "ctest", "#!/usr/bin/env bash\nexit 0\n")
        self._write_executable(self.bin / "ninja", "#!/usr/bin/env bash\nexit 0\n")
        self._write_executable(self.bin / "cargo", FAKE_CARGO)
        self._write_executable(self.bin / "pactl", "#!/usr/bin/env bash\nexit 0\n")

        self.base_state = {
            package: "explicit"
            for package in RUNTIME_PACKAGES | KDE_BUILD_PACKAGES | RUST_BUILD_PACKAGES
        }
        self.base_state.update({"preexisting-explicit": "explicit", "preexisting-dependency": "dependency"})
        self.dependencies = {
            "cmake": ["cmake-helper"],
            "ninja": ["ninja-helper"],
            "ffmpeg": ["ffmpeg-runtime-lib"],
            "rust": ["rust-helper"],
            "clang": ["clang-helper"],
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_executable(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def _write_state(self, state: dict[str, str]) -> None:
        self.state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    def _read_state(self) -> dict[str, str]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _operations(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.pacman_log.read_text().splitlines() if line]

    def _comm_invocations(self) -> list[str]:
        return [line for line in self.comm_log.read_text().splitlines() if line]

    def _non_c_locale(self) -> str:
        for candidate in ("ru_RU.UTF-8", "ru_RU.utf8", "en_US.UTF-8", "C.UTF-8"):
            result = subprocess.run(
                ["locale", "charmap"],
                env={**os.environ, "LC_ALL": candidate},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                return candidate
        self.skipTest("no non-C UTF-8 locale is available")

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.config),
                "XDG_DATA_HOME": str(self.data),
                "PATH": f"{self.bin}:{env['PATH']}",
                "KWISPR_KDE_BINARY": str(self.fake_kde),
                "KWISPR_LOCAL_STT_RELEASE_DIR": str(self.release),
                "KWISPR_INSTALL_NO_SYSTEMD": "1",
                "KWISPR_PACMAN_BIN": str(self.bin / "pacman"),
                "KWISPR_SUDO_BIN": str(self.bin / "sudo"),
                "FAKE_PACMAN_STATE": str(self.state_path),
                "FAKE_PACMAN_LOG": str(self.pacman_log),
                "FAKE_PACMAN_DEPS": json.dumps(self.dependencies),
                "FAKE_SUDO_LOG": str(self.sudo_log),
                "FAKE_CARGO_LOG": str(self.cargo_log),
                "FAKE_COMM_LOG": str(self.comm_log),
                "FAKE_BUILD_MARKER": str(self.marker),
                "REAL_COMM_BIN": self.real_comm,
                "TMPDIR": str(self.root),
            }
        )
        return env

    def command(self, *extra: str) -> list[str]:
        return [
            str(INSTALLER),
            "--prefix",
            str(self.prefix),
            "--build-backend",
            "host",
            "--yes",
            "--plain",
            "--no-open-settings",
            "--no-autostart",
            "--no-systemd-actions",
            *extra,
        ]

    def run_installer(self, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*extra),
            cwd=REPO_ROOT,
            env=env or self.env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )

    def test_host_build_installs_only_missing_build_packages_and_restores_state(self) -> None:
        initial = dict(self.base_state)
        initial.pop("cmake")
        initial.pop("ninja")
        self._write_state(initial)
        shutil.rmtree(self.host_build, ignore_errors=True)
        self.host_build.mkdir()
        stale_cache = self.host_build / "CMakeCache.txt"
        stale_cache.write_text("CMAKE_HOME_DIRECTORY:INTERNAL=/work/kde-whisper\n")

        result = self.run_installer("--without-local-stt", "--allow-package-install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._read_state(), initial)

        operations = self._operations()
        installs = [operation for operation in operations if operation["kind"] == "install"]
        self.assertEqual(len(installs), 1)
        self.assertTrue(installs[0]["asdeps"])
        self.assertEqual(set(installs[0]["packages"]), {"cmake", "ninja"})
        removed = set(next(operation["packages"] for operation in operations if operation["kind"] == "remove"))
        self.assertEqual(removed, {"cmake", "cmake-helper", "ninja", "ninja-helper"})
        self.assertIn("Exact build-only package set introduced by this run", result.stdout)
        self.assertIn("Removed temporary native build dependencies", result.stdout)
        self.assertFalse(stale_cache.exists())

    def test_package_diff_pins_locale_and_completes_install_under_non_c_collation(self) -> None:
        initial = dict(self.base_state)
        initial.pop("cmake")
        self._write_state(initial)
        env = self.env()
        env["LC_ALL"] = self._non_c_locale()
        dependencies = dict(self.dependencies)
        dependencies["cmake"] = [
            "0-locale-helper",
            "@locale-helper",
            "_locale-helper",
            "a-locale-helper",
            "ä-locale-helper",
        ]
        env["FAKE_PACMAN_DEPS"] = json.dumps(dependencies)

        result = self.run_installer("--without-local-stt", "--allow-package-install", env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.prefix / "bin" / "kwispr").exists())
        self.assertEqual(self._read_state(), initial)
        invocations = self._comm_invocations()
        self.assertEqual(len(invocations), 1, invocations)
        self.assertTrue(invocations[0].startswith("C|-13 "), invocations)

    def test_fallback_cleanup_pins_locale_and_restores_package_state(self) -> None:
        initial = dict(self.base_state)
        initial.pop("cmake")
        self._write_state(initial)
        env = self.env()
        env["LC_ALL"] = self._non_c_locale()
        env["FAKE_DELETE_BUILD_PACKAGE_FILE"] = "1"
        env["FAKE_CMAKE_FAIL_BUILD"] = "1"

        result = self.run_installer("--without-local-stt", "--allow-package-install", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._read_state(), initial)
        invocations = self._comm_invocations()
        self.assertEqual(len(invocations), 2, invocations)
        self.assertTrue(all(line.startswith("C|-13 ") for line in invocations), invocations)

    def test_host_build_cannot_delete_an_environment_selected_directory(self) -> None:
        self._write_state(dict(self.base_state))
        valuable = self.root / "valuable"
        valuable.mkdir()
        marker = valuable / "keep"
        marker.write_text("user data", encoding="utf-8")
        env = self.env()
        env["KWISPR_KDE_HOST_BUILD_DIR"] = str(valuable)

        result = self.run_installer("--without-local-stt", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "user data")

    def test_new_runtime_packages_and_dependencies_remain_installed(self) -> None:
        initial = dict(self.base_state)
        initial.pop("ffmpeg")
        initial.pop("kdbusaddons")
        self._write_state(initial)

        result = self.run_installer("--without-local-stt", "--allow-package-install")
        self.assertEqual(result.returncode, 0, result.stderr)
        final = self._read_state()
        self.assertEqual(final["ffmpeg"], "explicit")
        self.assertEqual(final["ffmpeg-runtime-lib"], "dependency")
        self.assertEqual(final["kdbusaddons"], "explicit")
        self.assertEqual(final["preexisting-dependency"], "dependency")
        self.assertFalse(any(operation["kind"] == "remove" for operation in self._operations()))

    def test_noninteractive_package_install_requires_explicit_authorization(self) -> None:
        initial = dict(self.base_state)
        initial.pop("cmake")
        self._write_state(initial)

        result = self.run_installer("--without-local-stt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--allow-package-install", result.stderr)
        self.assertEqual(self._read_state(), initial)
        self.assertEqual(self._operations(), [])

    def test_pending_arch_upgrades_block_package_provisioning(self) -> None:
        initial = dict(self.base_state)
        initial.pop("cmake")
        self._write_state(initial)
        env = self.env()
        env["FAKE_PACMAN_UPGRADES"] = "glibc 1.0 -> 2.0"

        result = self.run_installer("--without-local-stt", "--allow-package-install", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pacman -Syu", result.stderr)
        self.assertEqual(self._read_state(), initial)
        self.assertEqual(self._operations(), [])

    def test_failed_build_removes_only_packages_introduced_by_run(self) -> None:
        initial = dict(self.base_state)
        initial.pop("cmake")
        self._write_state(initial)
        env = self.env()
        env["FAKE_CMAKE_FAIL_BUILD"] = "1"

        result = self.run_installer("--without-local-stt", "--allow-package-install", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._read_state(), initial)
        self.assertTrue(any(operation["kind"] == "remove" for operation in self._operations()))

    def test_interrupted_build_removes_only_packages_introduced_by_run(self) -> None:
        initial = dict(self.base_state)
        initial.pop("cmake")
        self._write_state(initial)
        env = self.env()
        env["FAKE_CMAKE_SLEEP"] = "1"

        process = subprocess.Popen(
            self.command("--without-local-stt", "--allow-package-install"),
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while not self.marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.marker.exists(), "native build did not start")
        os.killpg(process.pid, signal.SIGINT)
        stdout, stderr = process.communicate(timeout=15)
        self.assertNotEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(self._read_state(), initial)

    def test_keep_build_deps_preserves_new_packages_as_dependencies(self) -> None:
        initial = dict(self.base_state)
        initial.pop("cmake")
        self._write_state(initial)

        result = self.run_installer(
            "--without-local-stt", "--allow-package-install", "--keep-build-deps"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        final = self._read_state()
        self.assertEqual(final["cmake"], "dependency")
        self.assertEqual(final["cmake-helper"], "dependency")
        self.assertFalse(any(operation["kind"] == "remove" for operation in self._operations()))

    def test_cleanup_does_not_claim_packages_added_after_its_transaction(self) -> None:
        initial = dict(self.base_state)
        initial.pop("cmake")
        self._write_state(initial)
        env = self.env()
        env["FAKE_EXTERNAL_PACKAGE"] = "installed-by-another-process"

        result = self.run_installer("--without-local-stt", "--allow-package-install", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        final = self._read_state()
        self.assertEqual(final["installed-by-another-process"], "explicit")
        removed = set(next(operation["packages"] for operation in self._operations() if operation["kind"] == "remove"))
        self.assertNotIn("installed-by-another-process", removed)

    def test_local_stt_host_build_uses_cargo_and_cleans_its_build_packages(self) -> None:
        initial = dict(self.base_state)
        initial.pop("rust")
        initial.pop("clang")
        self._write_state(initial)

        result = self.run_installer(
            "--with-local-stt",
            "--no-local-stt-autostart",
            "--allow-package-install",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._read_state(), initial)
        cargo_commands = self.cargo_log.read_text()
        self.assertIn("build --release --locked", cargo_commands)
        self.assertNotIn("test --locked", cargo_commands)
        removed = set(next(operation["packages"] for operation in self._operations() if operation["kind"] == "remove"))
        self.assertTrue({"rust", "rust-helper", "clang", "clang-helper"}.issubset(removed))

    def test_test_flag_runs_native_local_stt_tests(self) -> None:
        self._write_state(dict(self.base_state))

        result = self.run_installer(
            "--with-local-stt",
            "--no-local-stt-autostart",
            "--test",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        cargo_commands = self.cargo_log.read_text()
        self.assertIn("test --locked", cargo_commands)
        self.assertIn("build --release --locked", cargo_commands)

    def test_auto_prefers_an_already_installed_podman(self) -> None:
        self._write_state(dict(self.base_state))
        podman_log = self.root / "podman.log"
        self._write_executable(
            self.bin / "podman",
            f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {podman_log!s}\nexit 0\n",
        )
        command = self.command("--without-local-stt")
        index = command.index("--build-backend")
        command[index + 1] = "auto"

        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=self.env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Build backend: podman", result.stdout)
        self.assertTrue(podman_log.read_text())

    def test_skip_build_remains_an_alias_for_existing_artifacts(self) -> None:
        self._write_state(dict(self.base_state))
        command = self.command("--without-local-stt")
        index = command.index("--build-backend")
        command[index : index + 2] = ["--skip-build"]
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=self.env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Build backend: existing", result.stdout)
        self.assertEqual(self._operations(), [])


if __name__ == "__main__":
    unittest.main()
