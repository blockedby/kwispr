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
OPEN_SETTINGS=""
ASSUME_YES=0
PLAIN=0
SKIP_BUILD=0
NO_SYSTEMD_ACTIONS="${KWISPR_INSTALL_NO_SYSTEMD:-0}"
RUN_TESTS=0
UNINSTALL=0

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

A polished user-local install is the default; no sudo is required.

Options:
  --prefix PATH              Install prefix (default: ~/.local)
  --with-local-stt           Install the built offline STT runtime
  --without-local-stt        Install cloud/tray components only
  --autostart                Start the KDE tray at login
  --no-autostart             Do not start the tray at login
  --local-stt-autostart      Enable and start local STT at login
  --no-local-stt-autostart   Install local STT without enabling it
  --open-settings            Open the graphical settings after install
  --no-open-settings         Do not open settings after install
  --skip-build               Use existing/override build artifacts
  --test                     Run the KDE test suite after building
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
    --open-settings) OPEN_SETTINGS=1; shift ;;
    --no-open-settings) OPEN_SETTINGS=0; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --test) RUN_TESTS=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    --plain) PLAIN=1; shift ;;
    --no-systemd-actions) NO_SYSTEMD_ACTIONS=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

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

systemd_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
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
export KWISPR_MODEL_DIR
unset KWISPR_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY
exec $(printf '%q' "$binary") --host 127.0.0.1 --port 9000 --catalog $(printf '%q' "$catalog")
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

uninstall_kwispr() {
  banner
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
    "$ICON_DIR/org.kwispr.KdeWhisper.svg" \
    "$AUTOSTART_DIR/org.kwispr.KdeWhisper.desktop"
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

missing_runtime=()
for command_name in ffmpeg curl jq python3 notify-send wl-copy; do
  command -v "$command_name" >/dev/null 2>&1 || missing_runtime+=("$command_name")
done
if ((${#missing_runtime[@]} > 0)); then
  warn "Missing runtime commands: ${missing_runtime[*]}"
  warn "Run ./setup.sh to install desktop/recording dependencies."
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

if [[ "$SKIP_BUILD" == 0 ]]; then
  if command -v podman >/dev/null 2>&1; then
    if [[ "$RUN_TESTS" == 1 ]]; then
      run_step "Building and testing KDE Whisper in Podman" "$ROOT_DIR/kde-whisper/scripts/podman-test.sh"
    else
      run_step "Building KDE Whisper in Podman" "$ROOT_DIR/kde-whisper/scripts/podman-build.sh"
    fi
  else
    command -v cmake >/dev/null 2>&1 \
      || fail "Podman or a host KDE development environment with cmake is required"
    cmake_args=(-S "$ROOT_DIR/kde-whisper" -B "$ROOT_DIR/kde-whisper/build" "-DBUILD_TESTING=$([[ "$RUN_TESTS" == 1 ]] && echo ON || echo OFF)")
    command -v ninja >/dev/null 2>&1 && cmake_args+=(-G Ninja)
    run_step "Configuring KDE Whisper on the host" cmake "${cmake_args[@]}"
    run_step "Building KDE Whisper on the host" cmake --build "$ROOT_DIR/kde-whisper/build"
    if [[ "$RUN_TESTS" == 1 ]]; then
      run_step "Testing KDE Whisper on the host" ctest --test-dir "$ROOT_DIR/kde-whisper/build" --output-on-failure
    fi
  fi
fi

KDE_BINARY="${KWISPR_KDE_BINARY:-$ROOT_DIR/kde-whisper/build/kde-whisper}"
[[ -x "$KDE_BINARY" ]] || fail "KDE binary not found or executable: $KDE_BINARY"

if [[ "$WITH_LOCAL_STT" == 1 ]]; then
  LOCAL_STT_RELEASE_DIR="${KWISPR_LOCAL_STT_RELEASE_DIR:-$ROOT_DIR/rust-local-stt/target/release}"
  if [[ ! -x "$LOCAL_STT_RELEASE_DIR/kwispr-local-stt" && "$SKIP_BUILD" == 0 ]]; then
    command -v podman >/dev/null 2>&1 || fail "Podman is required to build local STT"
    run_step "Building local STT in Podman" "$ROOT_DIR/rust-local-stt/build-in-podman.sh"
  fi
  [[ -x "$LOCAL_STT_RELEASE_DIR/kwispr-local-stt" ]] \
    || fail "Local STT binary not found: $LOCAL_STT_RELEASE_DIR/kwispr-local-stt"
fi

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
else
  rm -f "$AUTOSTART_DIR/org.kwispr.KdeWhisper.desktop"
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
