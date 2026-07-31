#!/usr/bin/env bash
# Rootless, upgrade-safe installer for Kwispr.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${KWISPR_INSTALL_PREFIX:-$HOME/.local}"
XDG_CONFIG_HOME_VALUE="${XDG_CONFIG_HOME:-$HOME/.config}"
XDG_DATA_HOME_VALUE="${XDG_DATA_HOME:-$HOME/.local/share}"

WITH_LOCAL_STT=""
TRAY_AUTOSTART=""
LOCAL_STT_AUTOSTART=""
LOCAL_STT_HOST_OPTION=""
LOCAL_STT_PORT_OPTION=""
LOCAL_STT_URL_OPTION=""
LOCAL_STT_MODEL_OPTION=""
OPEN_SETTINGS=""
ASSUME_YES=0
PLAIN=0
SKIP_BUILD=0
BUILD_BACKEND=""
KEEP_BUILD_DEPS=0
ALLOW_PACKAGE_INSTALL=0
NO_SYSTEMD_ACTIONS="${KWISPR_INSTALL_NO_SYSTEMD:-0}"
RUN_TESTS=0
UNINSTALL=0

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Application files install user-locally; optional Arch dependency provisioning may use sudo.

Options:
  --prefix PATH              Install prefix (default: ~/.local)
  --with-local-stt           Install the built offline STT runtime
  --without-local-stt        Install cloud/tray components only
  --autostart                Start the KDE tray now when possible and at login
  --no-autostart             Do not start the tray at login
  --local-stt-autostart      Enable and start local STT at login
  --no-local-stt-autostart   Install local STT without enabling it
  --local-stt-host ADDRESS   Local runtime listen address (default: 127.0.0.1)
  --local-stt-port PORT      Local runtime listen port (fresh default: 19650)
  --local-stt-url URL        Client transcription endpoint (independent of bind)
  --local-stt-model SLUG     Catalog model sent to a local or remote server
  --open-settings            Open the graphical settings after install
  --no-open-settings         Do not open settings after install
  --build-backend MODE       Build with auto, host, podman, or existing
  --skip-build               Alias for --build-backend existing
  --keep-build-deps          Keep native Arch build dependencies
  --allow-package-install    Authorize pacman in noninteractive mode
  --test                     Run developer tests after building
  --yes                      Accept recommended defaults; no prompts
  --plain                    Disable Gum and ANSI decoration
  --no-systemd-actions       Write units but do not call systemctl
  --uninstall                Remove installed application files
  -h, --help                 Show this help

Artifact overrides for packaging/tests:
  KWISPR_KDE_BINARY=/path/to/kde-whisper
  KWISPR_LOCAL_STT_RELEASE_DIR=/path/to/release
EOF
}

while (($#)); do
  case "$1" in
    --prefix) [[ $# -ge 2 ]] || { echo "--prefix needs a path" >&2; exit 2; }; PREFIX="$2"; shift 2 ;;
    --with-local-stt) WITH_LOCAL_STT=1; shift ;;
    --without-local-stt) WITH_LOCAL_STT=0; shift ;;
    --autostart) TRAY_AUTOSTART=1; shift ;;
    --no-autostart) TRAY_AUTOSTART=0; shift ;;
    --local-stt-autostart) LOCAL_STT_AUTOSTART=1; WITH_LOCAL_STT=1; shift ;;
    --no-local-stt-autostart) LOCAL_STT_AUTOSTART=0; shift ;;
    --local-stt-host) [[ $# -ge 2 ]] || { echo "--local-stt-host needs an address" >&2; exit 2; }; LOCAL_STT_HOST_OPTION="$2"; shift 2 ;;
    --local-stt-port) [[ $# -ge 2 ]] || { echo "--local-stt-port needs a port" >&2; exit 2; }; LOCAL_STT_PORT_OPTION="$2"; shift 2 ;;
    --local-stt-url) [[ $# -ge 2 ]] || { echo "--local-stt-url needs a URL" >&2; exit 2; }; LOCAL_STT_URL_OPTION="$2"; shift 2 ;;
    --local-stt-model) [[ $# -ge 2 ]] || { echo "--local-stt-model needs a slug" >&2; exit 2; }; LOCAL_STT_MODEL_OPTION="$2"; shift 2 ;;
    --open-settings) OPEN_SETTINGS=1; shift ;;
    --no-open-settings) OPEN_SETTINGS=0; shift ;;
    --build-backend) [[ $# -ge 2 ]] || { echo "--build-backend needs auto, host, podman, or existing" >&2; exit 2; }; BUILD_BACKEND="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --keep-build-deps) KEEP_BUILD_DEPS=1; shift ;;
    --allow-package-install) ALLOW_PACKAGE_INSTALL=1; shift ;;
    --test) RUN_TESTS=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    --plain) PLAIN=1; shift ;;
    --no-systemd-actions) NO_SYSTEMD_ACTIONS=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$BUILD_BACKEND" in
  ""|auto|host|podman|existing) ;;
  *) echo "Invalid build backend: $BUILD_BACKEND (expected auto, host, podman, or existing)" >&2; exit 2 ;;
esac
if [[ "$SKIP_BUILD" == 1 ]]; then
  if [[ -n "$BUILD_BACKEND" && "$BUILD_BACKEND" != existing ]]; then
    echo "--skip-build conflicts with --build-backend $BUILD_BACKEND" >&2
    exit 2
  fi
  BUILD_BACKEND=existing
fi

PREFIX="$(realpath -m "$PREFIX")"
BIN_DIR="$PREFIX/bin"
LIB_DIR="$PREFIX/lib/kwispr"
RUNTIME_ROOT="$PREFIX/share/kwispr/runtime"
APPLICATIONS_DIR="$PREFIX/share/applications"
METAINFO_DIR="$PREFIX/share/metainfo"
ICON_DIR="$PREFIX/share/icons/hicolor/scalable/apps"
CONFIG_DIR="$XDG_CONFIG_HOME_VALUE/kwispr"
CONFIG_FILE="$CONFIG_DIR/config.env"
AUTOSTART_DIR="$XDG_CONFIG_HOME_VALUE/autostart"
SYSTEMD_USER_DIR="$XDG_CONFIG_HOME_VALUE/systemd/user"
MODEL_DIR="$XDG_DATA_HOME_VALUE/kwispr/models"
LEGACY_CONFIG="${KWISPR_LEGACY_CONFIG:-$ROOT_DIR/.env}"
KDE_HOST_BUILD_DIR="$ROOT_DIR/kde-whisper/build-host"

USE_GUM=0
if [[ "$PLAIN" == 0 && "$ASSUME_YES" == 0 && -t 0 && -t 1 ]] && command -v gum >/dev/null 2>&1; then
  USE_GUM=1
fi
USE_COLOR=0
if [[ "$PLAIN" == 0 && -t 1 ]]; then
  USE_COLOR=1
fi

style() {
  local code="$1"; shift
  if [[ "$USE_COLOR" == 1 ]]; then printf '\033[%sm%s\033[0m\n' "$code" "$*"; else printf '%s\n' "$*"; fi
}

banner() {
  if [[ "$USE_GUM" == 1 ]]; then
    gum style --border rounded --padding "1 4" --margin "1 0" --foreground 212 \
      "KWISPR" "Voice dictation for KDE"
  else
    printf '\n'
    style '1;35' '╭────────────────────────────────╮'
    style '1;35' '│       KWISPR INSTALLER         │'
    style '0;35' '│   Voice dictation for KDE      │'
    style '1;35' '╰────────────────────────────────╯'
    printf '\n'
  fi
}

confirm() {
  local prompt="$1" default_yes="${2:-0}" answer
  if [[ "$ASSUME_YES" == 1 ]]; then
    [[ "$default_yes" == 1 ]]
    return
  fi
  if [[ "$USE_GUM" == 1 ]]; then
    if [[ "$default_yes" == 1 ]]; then
      gum confirm --default=true "$prompt"
    else
      gum confirm "$prompt"
    fi
    return
  fi
  if [[ "$default_yes" == 1 ]]; then
    read -r -p "$prompt [Y/n] " answer
    [[ -z "$answer" || "$answer" =~ ^[Yy]$ ]]
  else
    read -r -p "$prompt [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]]
  fi
}

ok() { style '1;32' "✓ $*"; }
info() { style '0;36' "• $*"; }
warn() { style '1;33' "! $*" >&2; }
fail() { style '1;31' "✗ $*" >&2; exit 1; }

run_step() {
  local title="$1"; shift
  local log
  log="$(mktemp)"
  if [[ "$USE_GUM" == 1 ]]; then
    if gum spin --spinner dot --title "$title" -- "$@" >"$log" 2>&1; then
      rm -f "$log"; ok "$title"; return
    fi
  else
    info "$title"
    if "$@" >"$log" 2>&1; then
      rm -f "$log"; ok "$title"; return
    fi
  fi
  style '1;31' "✗ $title" >&2
  tail -n 40 "$log" >&2 || true
  rm -f "$log"
  exit 1
}

PACMAN_BIN="${KWISPR_PACMAN_BIN:-pacman}"
SUDO_BIN="${KWISPR_SUDO_BIN:-sudo}"
ARCH_STATE_DIR=""
ARCH_BUILD_REFERENCE=""
ARCH_BUILD_PACKAGE_FILE=""
ARCH_BUILD_CLEANUP_PENDING=0
PACMAN_ROOT_COMMAND=()

print_package_list() {
  local title="$1"; shift
  if (($# == 0)); then
    info "$title: none"
  else
    info "$title: $*"
  fi
}

arch_missing_packages() {
  local package
  for package in "$@"; do
    "$PACMAN_BIN" -Qq "$package" >/dev/null 2>&1 || printf '%s\n' "$package"
  done
}

arch_package_plan() {
  local output package
  if output="$("$PACMAN_BIN" -Sp --needed --print-format '%n' -- "$@" 2>/dev/null)"; then
    while IFS= read -r package; do
      [[ -n "$package" ]] && printf '%s\n' "$package"
    done <<< "$output"
  else
    printf '%s\n' "$@"
  fi
}

subtract_package_lists() {
  local candidate excluded
  for candidate in "$@"; do
    excluded=0
    if [[ -n "${ARCH_RUNTIME_PLAN_FILE:-}" && -f "$ARCH_RUNTIME_PLAN_FILE" ]] \
      && grep -Fqx -- "$candidate" "$ARCH_RUNTIME_PLAN_FILE"; then
      excluded=1
    fi
    [[ "$excluded" == 1 ]] || printf '%s\n' "$candidate"
  done
}

snapshot_arch_packages() {
  local destination="$1"
  "$PACMAN_BIN" -Qq | LC_ALL=C sort -u > "$destination"
}

cleanup_arch_build_packages() {
  local current_file cleanup_failed=0
  local -a introduced=()
  [[ "$ARCH_BUILD_CLEANUP_PENDING" == 1 ]] || return 0
  [[ "$KEEP_BUILD_DEPS" == 0 ]] || return 0
  [[ -n "$ARCH_BUILD_REFERENCE" && -f "$ARCH_BUILD_REFERENCE" ]] || return 0

  current_file="$ARCH_STATE_DIR/current-packages"
  if ! snapshot_arch_packages "$current_file"; then
    warn "Could not inspect pacman state; temporary build packages were not removed"
    return 1
  fi
  if [[ -n "$ARCH_BUILD_PACKAGE_FILE" && -f "$ARCH_BUILD_PACKAGE_FILE" ]]; then
    while IFS= read -r package; do
      [[ -n "$package" ]] && grep -Fqx -- "$package" "$current_file" && introduced+=("$package")
    done < "$ARCH_BUILD_PACKAGE_FILE"
  else
    # Last-resort recovery for an interrupted transaction before its plan was persisted.
    mapfile -t introduced < <(LC_ALL=C comm -13 "$ARCH_BUILD_REFERENCE" "$current_file")
  fi
  if ((${#introduced[@]} == 0)); then
    ARCH_BUILD_CLEANUP_PENDING=0
    return 0
  fi

  print_package_list "Removing build-only packages introduced by this run" "${introduced[@]}"
  if ! "${PACMAN_ROOT_COMMAND[@]}" -R --noconfirm -- "${introduced[@]}"; then
    warn "Automatic build-dependency cleanup failed; no pre-existing package was selected"
    cleanup_failed=1
  else
    ARCH_BUILD_CLEANUP_PENDING=0
    ok "Removed temporary native build dependencies"
  fi
  return "$cleanup_failed"
}

installer_exit() {
  local status=$?
  trap - EXIT INT TERM
  if ! cleanup_arch_build_packages && [[ "$status" == 0 ]]; then
    status=1
  fi
  [[ -z "$ARCH_STATE_DIR" ]] || rm -rf "$ARCH_STATE_DIR"
  exit "$status"
}

trap installer_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

choose_build_backend() {
  local selection answer
  if [[ "$USE_GUM" == 1 ]]; then
    selection="$(gum choose \
      "Native Arch build (temporary build dependencies)" \
      "Podman" \
      "Existing build artifact")" || exit 1
    case "$selection" in
      Native*) BUILD_BACKEND=host ;;
      Podman) BUILD_BACKEND=podman ;;
      Existing*) BUILD_BACKEND=existing ;;
    esac
    return
  fi

  printf '\nBuild backend\n'
  printf '  1) Native Arch build (temporary build dependencies)\n'
  printf '  2) Podman\n'
  printf '  3) Existing build artifact\n'
  read -r -p "Select [1-3]: " answer
  case "$answer" in
    1) BUILD_BACKEND=host ;;
    2) BUILD_BACKEND=podman ;;
    3) BUILD_BACKEND=existing ;;
    *) fail "Invalid build backend selection" ;;
  esac
}

resolve_build_backend() {
  if [[ -z "$BUILD_BACKEND" ]]; then
    if [[ "$ASSUME_YES" == 1 ]]; then
      BUILD_BACKEND=auto
    else
      choose_build_backend
    fi
  fi
  if [[ "$BUILD_BACKEND" == auto ]]; then
    if command -v podman >/dev/null 2>&1; then
      BUILD_BACKEND=podman
    else
      BUILD_BACKEND=host
    fi
  fi
  info "Build backend: $BUILD_BACKEND"
}

prepare_native_arch_packages() {
  local package
  local -a runtime_packages build_packages missing_runtime_packages missing_build_packages
  local -a runtime_plan build_plan cleanup_plan

  command -v "$PACMAN_BIN" >/dev/null 2>&1 || return 0

  runtime_packages=(ffmpeg curl jq python wl-clipboard libnotify qt6-base kcoreaddons kstatusnotifieritem)
  command -v pactl >/dev/null 2>&1 || runtime_packages+=(pipewire-pulse)
  if [[ "$WITH_LOCAL_STT" == 1 ]]; then
    runtime_packages+=(vulkan-icd-loader)
  fi
  build_packages=()
  if [[ "$BUILD_BACKEND" == host ]]; then
    build_packages=(base-devel cmake ninja extra-cmake-modules qt6-tools git)
    if [[ "$WITH_LOCAL_STT" == 1 ]]; then
      build_packages+=(rust clang pkgconf vulkan-headers shaderc spirv-headers ccache)
    fi
  fi

  mapfile -t missing_runtime_packages < <(arch_missing_packages "${runtime_packages[@]}")
  if ((${#build_packages[@]} > 0)); then
    mapfile -t missing_build_packages < <(arch_missing_packages "${build_packages[@]}")
  else
    missing_build_packages=()
  fi
  if ((${#missing_runtime_packages[@]} == 0 && ${#missing_build_packages[@]} == 0)); then
    return 0
  fi
  if [[ -n "$("$PACMAN_BIN" -Qu 2>/dev/null || true)" ]]; then
    fail "Arch has pending package upgrades; run sudo pacman -Syu before native dependency provisioning"
  fi

  if ((${#missing_runtime_packages[@]} > 0)); then
    mapfile -t runtime_plan < <(arch_package_plan "${missing_runtime_packages[@]}" | LC_ALL=C sort -u)
  else
    runtime_plan=()
  fi
  ARCH_STATE_DIR="$(mktemp -d)"
  ARCH_RUNTIME_PLAN_FILE="$ARCH_STATE_DIR/runtime-plan"
  printf '%s\n' "${runtime_plan[@]}" | LC_ALL=C sort -u > "$ARCH_RUNTIME_PLAN_FILE"
  if ((${#missing_build_packages[@]} > 0)); then
    mapfile -t build_plan < <(arch_package_plan "${missing_build_packages[@]}" | LC_ALL=C sort -u)
    mapfile -t cleanup_plan < <(subtract_package_lists "${build_plan[@]}")
  else
    build_plan=()
    cleanup_plan=()
  fi

  print_package_list "Runtime packages that will remain installed" "${runtime_plan[@]}"
  print_package_list "Temporary native build packages" "${cleanup_plan[@]}"
  print_package_list "Packages scheduled for cleanup" "${cleanup_plan[@]}"
  if ((EUID == 0)); then
    PACMAN_ROOT_COMMAND=("$PACMAN_BIN")
    info "Privilege escalation: not needed (already running as root)"
  else
    command -v "$SUDO_BIN" >/dev/null 2>&1 || fail "sudo is required to install missing Arch packages"
    PACMAN_ROOT_COMMAND=("$SUDO_BIN" "$PACMAN_BIN")
    info "Privilege escalation: sudo will be used for pacman"
  fi

  if [[ "$ASSUME_YES" == 1 && "$ALLOW_PACKAGE_INSTALL" != 1 ]]; then
    fail "Missing Arch packages require explicit authorization; add --allow-package-install"
  fi
  if [[ "$ASSUME_YES" == 0 ]] && ! confirm "Install the listed Arch packages?" 0; then
    fail "Package installation was not authorized"
  fi

  snapshot_arch_packages "$ARCH_STATE_DIR/before-packages"
  "$PACMAN_BIN" -Q | LC_ALL=C sort -u > "$ARCH_STATE_DIR/before-package-versions"
  "$PACMAN_BIN" -Qqe | LC_ALL=C sort -u > "$ARCH_STATE_DIR/before-explicit"
  "$PACMAN_BIN" -Qqd | LC_ALL=C sort -u > "$ARCH_STATE_DIR/before-dependencies"

  if ((${#missing_runtime_packages[@]} > 0)); then
    mapfile -t missing_runtime_packages < <(arch_missing_packages "${missing_runtime_packages[@]}")
    if ((${#missing_runtime_packages[@]} > 0)); then
      run_step "Installing required runtime packages" \
        "${PACMAN_ROOT_COMMAND[@]}" -S --needed --noconfirm -- "${missing_runtime_packages[@]}"
    fi
  fi

  snapshot_arch_packages "$ARCH_STATE_DIR/after-runtime-packages"
  ARCH_BUILD_REFERENCE="$ARCH_STATE_DIR/after-runtime-packages"
  ARCH_BUILD_PACKAGE_FILE="$ARCH_STATE_DIR/build-packages-introduced"
  if ((${#missing_build_packages[@]} > 0)); then
    mapfile -t missing_build_packages < <(arch_missing_packages "${missing_build_packages[@]}")
    if ((${#missing_build_packages[@]} > 0)); then
      printf '%s\n' "${cleanup_plan[@]}" | LC_ALL=C sort -u > "$ARCH_BUILD_PACKAGE_FILE"
      ARCH_BUILD_CLEANUP_PENDING=1
      run_step "Installing temporary native build packages as dependencies" \
        "${PACMAN_ROOT_COMMAND[@]}" -S --asdeps --needed --noconfirm -- "${missing_build_packages[@]}"
      snapshot_arch_packages "$ARCH_STATE_DIR/after-build-packages"
      LC_ALL=C comm -13 "$ARCH_BUILD_REFERENCE" "$ARCH_STATE_DIR/after-build-packages" > "$ARCH_BUILD_PACKAGE_FILE"
      mapfile -t cleanup_plan < "$ARCH_BUILD_PACKAGE_FILE"
      print_package_list "Exact build-only package set introduced by this run" "${cleanup_plan[@]}"
      if [[ "$KEEP_BUILD_DEPS" == 1 ]]; then
        ARCH_BUILD_CLEANUP_PENDING=0
        info "Keeping native build dependencies by request"
      fi
    fi
  fi
}

build_on_host() {
  local -a cmake_args
  command -v cmake >/dev/null 2>&1 || fail "cmake is required for the host build"
  # Keep native caches separate from Podman's /work paths and always refresh
  # this installer-owned directory so source paths/generators cannot conflict.
  rm -rf "$KDE_HOST_BUILD_DIR"
  cmake_args=(-S "$ROOT_DIR/kde-whisper" -B "$KDE_HOST_BUILD_DIR" "-DBUILD_TESTING=$([[ "$RUN_TESTS" == 1 ]] && echo ON || echo OFF)")
  command -v ninja >/dev/null 2>&1 && cmake_args+=(-G Ninja)
  run_step "Configuring KDE Whisper on the host" cmake "${cmake_args[@]}"
  run_step "Building KDE Whisper on the host" cmake --build "$KDE_HOST_BUILD_DIR"
  if [[ "$RUN_TESTS" == 1 ]]; then
    run_step "Testing KDE Whisper on the host" ctest --test-dir "$KDE_HOST_BUILD_DIR" --output-on-failure
  fi
  if [[ "$WITH_LOCAL_STT" == 1 ]]; then
    command -v cargo >/dev/null 2>&1 || fail "cargo is required for the host local STT build"
    if [[ "$RUN_TESTS" == 1 ]]; then
      run_step "Testing local STT on the host" cargo test --locked --manifest-path "$ROOT_DIR/rust-local-stt/Cargo.toml"
    fi
    run_step "Building local STT on the host" cargo build --release --locked --manifest-path "$ROOT_DIR/rust-local-stt/Cargo.toml"
  fi
}

build_in_podman() {
  command -v podman >/dev/null 2>&1 || fail "Podman backend selected, but podman is not installed"
  if [[ "$RUN_TESTS" == 1 ]]; then
    run_step "Building and testing KDE Whisper in Podman" "$ROOT_DIR/kde-whisper/scripts/podman-test.sh"
  else
    run_step "Building KDE Whisper in Podman" "$ROOT_DIR/kde-whisper/scripts/podman-build.sh"
  fi
  if [[ "$WITH_LOCAL_STT" == 1 ]]; then
    run_step "Building local STT in Podman" "$ROOT_DIR/rust-local-stt/build-in-podman.sh"
  fi
}

systemd_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
}

validate_local_stt_options() {
  python3 - "$LOCAL_STT_HOST_OPTION" "$LOCAL_STT_PORT_OPTION" "$LOCAL_STT_URL_OPTION" "$LOCAL_STT_MODEL_OPTION" <<'PY'
import ipaddress
import re
import sys
from urllib.parse import urlsplit

host, port, url, model = sys.argv[1:]
if host:
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise SystemExit(f"invalid --local-stt-host: {exc}")
if port:
    if not re.fullmatch(r"[0-9]+", port):
        raise SystemExit("invalid --local-stt-port: expected ASCII decimal digits")
    number = int(port, 10)
    if not 1 <= number <= 65535:
        raise SystemExit("invalid --local-stt-port: expected 1..65535")
if url:
    try:
        parsed = urlsplit(url)
        parsed_port = parsed.port
    except ValueError as exc:
        raise SystemExit(f"invalid --local-stt-url: {exc}")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit("invalid --local-stt-url: expected an HTTP(S) URL with a host")
    if parsed.hostname in {"0.0.0.0", "::"}:
        raise SystemExit("invalid --local-stt-url: listen wildcard is not a client destination")
    if parsed_port is not None and not 1 <= parsed_port <= 65535:
        raise SystemExit("invalid --local-stt-url: port must be 1..65535")
if model != model.strip() or (model and any(ch.isspace() for ch in model)):
    raise SystemExit("invalid --local-stt-model: expected a catalog slug without whitespace")
PY
}

configure_local_stt_config() {
  KWISPR_INSTALL_CONFIG_FILE="$CONFIG_FILE" \
  KWISPR_INSTALL_WITH_LOCAL_STT="$WITH_LOCAL_STT" \
  KWISPR_INSTALL_LOCAL_STT_HOST="$LOCAL_STT_HOST_OPTION" \
  KWISPR_INSTALL_LOCAL_STT_PORT="$LOCAL_STT_PORT_OPTION" \
  KWISPR_INSTALL_LOCAL_STT_URL="$LOCAL_STT_URL_OPTION" \
  KWISPR_INSTALL_LOCAL_STT_MODEL="$LOCAL_STT_MODEL_OPTION" \
  python3 <<'PY'
import os
import re
import shlex
from pathlib import Path

path = Path(os.environ["KWISPR_INSTALL_CONFIG_FILE"])
with_runtime = os.environ["KWISPR_INSTALL_WITH_LOCAL_STT"] == "1"
host_option = os.environ["KWISPR_INSTALL_LOCAL_STT_HOST"]
port_option = os.environ["KWISPR_INSTALL_LOCAL_STT_PORT"]
url_option = os.environ["KWISPR_INSTALL_LOCAL_STT_URL"]
model_option = os.environ["KWISPR_INSTALL_LOCAL_STT_MODEL"]
existed = path.exists()
lines = path.read_text(encoding="utf-8").splitlines() if existed else []
assignment = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
indices = {}
values = {}

def decode(raw):
    raw = raw.strip()
    if not raw:
        return ""
    try:
        parsed = shlex.split(raw, posix=True)
    except ValueError:
        return raw
    return parsed[0] if len(parsed) == 1 else raw

for index, line in enumerate(lines):
    match = assignment.match(line)
    if match:
        indices[match.group(1)] = index
        values[match.group(1)] = decode(match.group(2))

safe = re.compile(r"^[A-Za-z0-9_./:@%+=,\[\]-]+$")
def encoded(value):
    if not value or safe.fullmatch(value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"

def set_value(key, value):
    rendered = f"{key}={encoded(str(value))}"
    if key in indices:
        lines[indices[key]] = rendered
    else:
        indices[key] = len(lines)
        lines.append(rendered)
    values[key] = str(value)

legacy_url = "http://127.0.0.1:9000/v1/audio/transcriptions"
old_generated = values.get("KWISPR_API_URL") == legacy_url and "KWISPR_LOCAL_STT_PORT" not in values
if old_generated:
    if "KWISPR_LOCAL_STT_HOST" not in values:
        set_value("KWISPR_LOCAL_STT_HOST", "127.0.0.1")
    set_value("KWISPR_LOCAL_STT_PORT", "9000")
    set_value("KWISPR_LOCAL_STT_CONFIGURED", "1")

fresh_local = with_runtime and not existed
if with_runtime:
    if "KWISPR_LOCAL_STT_HOST" not in values:
        set_value("KWISPR_LOCAL_STT_HOST", "127.0.0.1")
    if "KWISPR_LOCAL_STT_PORT" not in values:
        set_value("KWISPR_LOCAL_STT_PORT", "19650")
if host_option:
    set_value("KWISPR_LOCAL_STT_HOST", host_option)
if port_option:
    port_option = str(int(port_option, 10))
    set_value("KWISPR_LOCAL_STT_PORT", port_option)

if fresh_local:
    client_port = port_option or "19650"
    set_value("KWISPR_BACKEND", "openai-transcriptions")
    set_value("KWISPR_API_URL", f"http://127.0.0.1:{client_port}/v1/audio/transcriptions")
    set_value("KWISPR_API_KEY", "")
    set_value("KWISPR_MODEL", model_option or "whisper-large-v3-turbo")
    set_value("KWISPR_LOCAL_STT_CONFIGURED", "1")
if url_option:
    set_value("KWISPR_BACKEND", "openai-transcriptions")
    set_value("KWISPR_API_URL", url_option)
    set_value("KWISPR_API_KEY", "")
    set_value("KWISPR_LOCAL_STT_CONFIGURED", "1")
    if "KWISPR_MODEL" not in values:
        set_value("KWISPR_MODEL", model_option or "whisper-large-v3-turbo")
if model_option:
    set_value("KWISPR_MODEL", model_option)

if lines != (path.read_text(encoding="utf-8").splitlines() if existed else []):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
PY
}

write_wrappers() {
  mkdir -p "$BIN_DIR"
  cat > "$BIN_DIR/kde-whisper" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export KWISPR_RUNTIME_ROOT=$(printf '%q' "$RUNTIME_ROOT")
exec $(printf '%q' "$LIB_DIR/kde-whisper") "\$@"
EOF
  cat > "$BIN_DIR/kwispr" <<EOF
#!/usr/bin/env bash
set -euo pipefail
case "\${1:-}" in
  settings)
    shift
    exec $(printf '%q' "$BIN_DIR/kde-whisper") --settings "\$@"
    ;;
  models)
    shift
    exec $(printf '%q' "$RUNTIME_ROOT/kwispr-models.py") "\$@"
    ;;
esac
exec $(printf '%q' "$RUNTIME_ROOT/kwispr.sh") "\$@"
EOF
  chmod 0755 "$BIN_DIR/kde-whisper" "$BIN_DIR/kwispr"
}

write_desktop_file() {
  local destination="$1" exec_path="$BIN_DIR/kde-whisper"
  exec_path="${exec_path//\\/\\\\}"
  exec_path="${exec_path//\"/\\\"}"
  exec_path="${exec_path//\`/\\\`}"
  exec_path="${exec_path//\$/\\\$}"
  mkdir -p "$(dirname "$destination")"
  awk -v exec_line="Exec=\"$exec_path\"" '
    /^Exec=/ { print exec_line; next }
    { print }
  ' "$ROOT_DIR/kde-whisper/org.kwispr.KdeWhisper.desktop" > "$destination"
  chmod 0644 "$destination"
}

write_local_stt_service() {
  local binary="$RUNTIME_ROOT/rust-local-stt/target/release/kwispr-local-stt"
  local catalog="$RUNTIME_ROOT/models/local-stt-catalog.json"
  local launcher="$RUNTIME_ROOT/start-local-stt.sh"

  cat > "$launcher" <<EOF
#!/usr/bin/env bash
set -euo pipefail
config_file=$(printf '%q' "$CONFIG_FILE")
default_model_dir=$(printf '%q' "$MODEL_DIR")
if [[ -r "\$config_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "\$config_file"
  set +a
fi
: "\${KWISPR_MODEL_DIR:=\$default_model_dir}"
: "\${KWISPR_LOCAL_STT_HOST:=127.0.0.1}"
: "\${KWISPR_LOCAL_STT_PORT:=19650}"
export KWISPR_MODEL_DIR
unset KWISPR_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY
exec $(printf '%q' "$binary") --host "\$KWISPR_LOCAL_STT_HOST" --port "\$KWISPR_LOCAL_STT_PORT" --catalog $(printf '%q' "$catalog")
EOF
  chmod 0700 "$launcher"

  mkdir -p "$SYSTEMD_USER_DIR"
  cat > "$SYSTEMD_USER_DIR/kwispr-local-stt.service" <<EOF
[Unit]
Description=Kwispr local speech-to-text server
Documentation=https://github.com/blockedby/kwispr
After=graphical-session.target

[Service]
Type=simple
ExecStart="$(systemd_escape "$launcher")"
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
  chmod 0644 "$SYSTEMD_USER_DIR/kwispr-local-stt.service"
}

systemctl_user() {
  [[ "$NO_SYSTEMD_ACTIONS" == 0 ]] || return 0
  command -v systemctl >/dev/null 2>&1 || { warn "systemctl not found; service was installed but not enabled"; return 0; }
  systemctl --user "$@"
}

stop_tray_processes() {
  local stopped_count
  [[ -e "$LIB_DIR/kde-whisper" ]] || return 0
  command -v python3 >/dev/null 2>&1 \
    || fail "python3 is required to stop the installed tray safely"

  stopped_count="$(python3 - "$LIB_DIR/kde-whisper" <<'PY'
import os
import select
import signal
import sys
import time

expected = os.path.realpath(sys.argv[1])
current_uid = os.geteuid()
processes = []


def executable_path(pid):
    path = os.readlink(f"/proc/{pid}/exe")
    if path.endswith(" (deleted)"):
        path = path[:-10]
    return os.path.realpath(path)


for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    pid = int(name)
    try:
        if os.stat(f"/proc/{pid}").st_uid != current_uid or executable_path(pid) != expected:
            continue
        pidfd = os.pidfd_open(pid)
        # Revalidate after pidfd_open: the descriptor is now bound to this exact process,
        # so later PID reuse cannot redirect either signal to an unrelated process.
        if os.stat(f"/proc/{pid}").st_uid != current_uid or executable_path(pid) != expected:
            os.close(pidfd)
            continue
        processes.append((pid, pidfd))
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        continue

for _pid, pidfd in processes:
    try:
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
    except ProcessLookupError:
        pass

deadline = time.monotonic() + 5.0
remaining = list(processes)
while remaining and time.monotonic() < deadline:
    alive = []
    for pid, pidfd in remaining:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        if not poller.poll(0):
            alive.append((pid, pidfd))
    remaining = alive
    if remaining:
        time.sleep(0.1)

for _pid, pidfd in remaining:
    try:
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
    except ProcessLookupError:
        pass

for _pid, pidfd in processes:
    os.close(pidfd)

print(len(processes))
PY
)" || fail "Could not stop the installed Kwispr tray"

  if ((stopped_count > 0)); then
    ok "Stopped the running Kwispr tray"
  fi
}

start_tray_in_graphical_session() {
  [[ "$TRAY_AUTOSTART" == 1 ]] || return 0
  if [[ "$NO_SYSTEMD_ACTIONS" == 1 ]]; then
    info "Tray autostart enabled; current-session launch skipped (--no-systemd-actions)"
    return 0
  fi
  if ! command -v systemctl >/dev/null 2>&1 || ! command -v systemd-run >/dev/null 2>&1; then
    info "Tray autostart enabled; it will start at the next graphical login"
    return 0
  fi
  if ! systemctl --user --quiet is-active graphical-session.target; then
    info "Tray autostart enabled; no active graphical session was found"
    return 0
  fi

  if systemd-run --user --quiet --collect --service-type=exec \
      --unit="kwispr-tray-install-$BASHPID.service" \
      --property=PartOf=graphical-session.target \
      --property=After=graphical-session.target \
      "$BIN_DIR/kde-whisper"; then
    ok "Kwispr tray started in the current graphical session"
  else
    warn "Tray autostart is installed, but the current-session launch failed"
  fi
}

uninstall_kwispr() {
  banner
  stop_tray_processes
  info "Removing application files from $PREFIX"
  if [[ -f "$SYSTEMD_USER_DIR/kwispr-local-stt.service" ]]; then
    systemctl_user disable --now kwispr-local-stt.service >/dev/null 2>&1 || true
    rm -f "$SYSTEMD_USER_DIR/kwispr-local-stt.service"
    systemctl_user daemon-reload >/dev/null 2>&1 || true
  fi
  rm -f \
    "$BIN_DIR/kwispr" \
    "$BIN_DIR/kde-whisper" \
    "$APPLICATIONS_DIR/org.kwispr.KdeWhisper.desktop" \
    "$METAINFO_DIR/org.kwispr.KdeWhisper.metainfo.xml" \
    "$ICON_DIR/org.kwispr.KdeWhisper.svg"
  if [[ -e "$AUTOSTART_DIR/org.kwispr.KdeWhisper.desktop" || -L "$AUTOSTART_DIR/org.kwispr.KdeWhisper.desktop" ]]; then
    rm -f "$AUTOSTART_DIR/org.kwispr.KdeWhisper.desktop"
    ok "Kwispr tray autostart entry removed"
  fi
  rm -rf "$LIB_DIR" "$RUNTIME_ROOT"
  ok "Kwispr application files removed"
  info "Settings and downloaded models were preserved:"
  printf '  %s\n  %s\n' "$CONFIG_FILE" "$MODEL_DIR"
}

if [[ "$UNINSTALL" == 1 ]]; then
  uninstall_kwispr
  exit 0
fi

banner
info "Install prefix: $PREFIX"
info "Configuration: $CONFIG_FILE"

if [[ "$ASSUME_YES" == 0 && ! -t 0 ]]; then
  fail "Interactive input is unavailable; rerun with --yes and explicit feature flags."
fi
if [[ "$ASSUME_YES" == 1 ]]; then
  TRAY_AUTOSTART="${TRAY_AUTOSTART:-1}"
  WITH_LOCAL_STT="${WITH_LOCAL_STT:-0}"
  LOCAL_STT_AUTOSTART="${LOCAL_STT_AUTOSTART:-0}"
  OPEN_SETTINGS="${OPEN_SETTINGS:-0}"
fi

if [[ -z "$TRAY_AUTOSTART" ]]; then
  if confirm "Start the Kwispr tray automatically when you log in?" 1; then TRAY_AUTOSTART=1; else TRAY_AUTOSTART=0; fi
fi
if [[ -z "$WITH_LOCAL_STT" ]]; then
  if confirm "Install the local/offline STT runtime?" 0; then WITH_LOCAL_STT=1; else WITH_LOCAL_STT=0; fi
fi
if [[ "$WITH_LOCAL_STT" == 1 && -z "$LOCAL_STT_AUTOSTART" ]]; then
  if confirm "Keep local STT loaded after login? (models may use 1–2 GB RAM)" 0; then LOCAL_STT_AUTOSTART=1; else LOCAL_STT_AUTOSTART=0; fi
fi
LOCAL_STT_AUTOSTART="${LOCAL_STT_AUTOSTART:-0}"

resolve_build_backend
prepare_native_arch_packages
if [[ -n "$LOCAL_STT_HOST_OPTION$LOCAL_STT_PORT_OPTION$LOCAL_STT_URL_OPTION$LOCAL_STT_MODEL_OPTION" ]]; then
  command -v python3 >/dev/null 2>&1 || fail "python3 is required to validate Local STT options"
  validate_local_stt_options || fail "Invalid Local STT option"
fi

missing_runtime=()
for command_name in ffmpeg curl jq python3 notify-send wl-copy; do
  command -v "$command_name" >/dev/null 2>&1 || missing_runtime+=("$command_name")
done
if ((${#missing_runtime[@]} > 0)); then
  warn "Missing runtime commands after dependency setup: ${missing_runtime[*]}"
  warn "Run ./setup.sh or install equivalent runtime packages."
fi

case "$BUILD_BACKEND" in
  host) build_on_host ;;
  podman) build_in_podman ;;
  existing) info "Using existing build artifacts" ;;
esac

if [[ "$BUILD_BACKEND" == host ]]; then
  DEFAULT_KDE_BINARY="$KDE_HOST_BUILD_DIR/kde-whisper"
else
  DEFAULT_KDE_BINARY="$ROOT_DIR/kde-whisper/build/kde-whisper"
fi
KDE_BINARY="${KWISPR_KDE_BINARY:-$DEFAULT_KDE_BINARY}"
[[ -x "$KDE_BINARY" ]] || fail "KDE binary not found or executable: $KDE_BINARY"

if [[ "$WITH_LOCAL_STT" == 1 ]]; then
  LOCAL_STT_RELEASE_DIR="${KWISPR_LOCAL_STT_RELEASE_DIR:-$ROOT_DIR/rust-local-stt/target/release}"
  [[ -x "$LOCAL_STT_RELEASE_DIR/kwispr-local-stt" ]] \
    || fail "Local STT binary not found: $LOCAL_STT_RELEASE_DIR/kwispr-local-stt"
fi

stop_tray_processes
info "Installing application files"
mkdir -p "$LIB_DIR" "$RUNTIME_ROOT/models" "$RUNTIME_ROOT/sounds" \
  "$APPLICATIONS_DIR" "$METAINFO_DIR" "$ICON_DIR" "$CONFIG_DIR" "$MODEL_DIR"
install -m 0755 "$KDE_BINARY" "$LIB_DIR/kde-whisper"
install -m 0755 "$ROOT_DIR/kwispr.sh" "$RUNTIME_ROOT/kwispr.sh"
install -m 0755 "$ROOT_DIR/kwispr-models.py" "$RUNTIME_ROOT/kwispr-models.py"
install -m 0644 "$ROOT_DIR/models/local-stt-catalog.json" "$RUNTIME_ROOT/models/local-stt-catalog.json"
install -m 0644 "$ROOT_DIR"/sounds/*.wav "$RUNTIME_ROOT/sounds/"
install -m 0644 "$ROOT_DIR/kde-whisper/org.kwispr.KdeWhisper.metainfo.xml" "$METAINFO_DIR/"
install -m 0644 "$ROOT_DIR/kde-whisper/icons/org.kwispr.KdeWhisper.svg" "$ICON_DIR/"
write_wrappers
write_desktop_file "$APPLICATIONS_DIR/org.kwispr.KdeWhisper.desktop"

if [[ "$TRAY_AUTOSTART" == 1 ]]; then
  write_desktop_file "$AUTOSTART_DIR/org.kwispr.KdeWhisper.desktop"
  ok "Kwispr tray added to graphical-session autostart"
else
  rm -f "$AUTOSTART_DIR/org.kwispr.KdeWhisper.desktop"
  info "Kwispr tray autostart disabled"
fi

if [[ ! -f "$CONFIG_FILE" && -f "$LEGACY_CONFIG" ]]; then
  install -m 0600 "$LEGACY_CONFIG" "$CONFIG_FILE"
  ok "Migrated legacy .env to $CONFIG_FILE"
elif [[ -f "$CONFIG_FILE" ]]; then
  chmod 0600 "$CONFIG_FILE"
  ok "Preserved existing configuration"
else
  info "The settings UI will create configuration on first save"
fi

if [[ "$WITH_LOCAL_STT" == 1 || -f "$CONFIG_FILE" || -n "$LOCAL_STT_HOST_OPTION$LOCAL_STT_PORT_OPTION$LOCAL_STT_URL_OPTION$LOCAL_STT_MODEL_OPTION" ]]; then
  command -v python3 >/dev/null 2>&1 || fail "python3 is required to configure Local STT"
  configure_local_stt_config
fi

if [[ "$WITH_LOCAL_STT" == 1 ]]; then
  RELEASE_DEST="$RUNTIME_ROOT/rust-local-stt/target/release"
  rm -rf "$RELEASE_DEST"
  mkdir -p "$RELEASE_DEST"
  install -m 0755 "$LOCAL_STT_RELEASE_DIR/kwispr-local-stt" "$RELEASE_DEST/kwispr-local-stt"
  shopt -s nullglob
  runtime_libraries=("$LOCAL_STT_RELEASE_DIR"/libtranscribe.so* "$LOCAL_STT_RELEASE_DIR"/libggml*.so*)
  if ((${#runtime_libraries[@]} > 0)); then
    cp -a "${runtime_libraries[@]}" "$RELEASE_DEST/"
  else
    info "Local STT artifact is self-contained; no staged backend libraries to copy"
  fi
  shopt -u nullglob
  write_local_stt_service
  systemctl_user daemon-reload
  if [[ "$LOCAL_STT_AUTOSTART" == 1 ]]; then
    systemctl_user enable --now kwispr-local-stt.service
    ok "Local STT enabled for login"
  else
    systemctl_user disable --now kwispr-local-stt.service >/dev/null 2>&1 || true
    info "Local STT installed but not loaded at login"
  fi
else
  if [[ -f "$SYSTEMD_USER_DIR/kwispr-local-stt.service" || -d "$RUNTIME_ROOT/rust-local-stt" ]]; then
    systemctl_user disable --now kwispr-local-stt.service >/dev/null 2>&1 || true
    rm -f "$SYSTEMD_USER_DIR/kwispr-local-stt.service" "$RUNTIME_ROOT/start-local-stt.sh"
    rm -rf "$RUNTIME_ROOT/rust-local-stt"
    systemctl_user daemon-reload >/dev/null 2>&1 || true
    info "Removed the previously installed local STT runtime; models were preserved"
  fi
fi

command -v update-desktop-database >/dev/null 2>&1 \
  && update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true

[[ -x "$BIN_DIR/kwispr" ]] || fail "Installed CLI validation failed"
[[ -x "$BIN_DIR/kde-whisper" ]] || fail "Installed tray validation failed"
[[ -f "$RUNTIME_ROOT/models/local-stt-catalog.json" ]] || fail "Installed catalog validation failed"
start_tray_in_graphical_session
ok "Kwispr installed successfully"

printf '\n'
style '1;35' 'Installation summary'
printf '  %-18s %s\n' 'Command' "$BIN_DIR/kwispr"
printf '  %-18s %s\n' 'Settings' "$BIN_DIR/kwispr settings"
printf '  %-18s %s\n' 'Configuration' "$CONFIG_FILE"
printf '  %-18s %s\n' 'Models' "$MODEL_DIR"
printf '\nBind your KDE shortcut to:\n  %s toggle\n\n' "$BIN_DIR/kwispr"

if [[ -z "$OPEN_SETTINGS" ]]; then
  if confirm "Open Kwispr Settings now?" 1; then OPEN_SETTINGS=1; else OPEN_SETTINGS=0; fi
fi
if [[ "$OPEN_SETTINGS" == 1 ]]; then
  nohup "$BIN_DIR/kde-whisper" --settings >/dev/null 2>&1 &
  disown || true
  ok "Kwispr Settings opened"
fi
