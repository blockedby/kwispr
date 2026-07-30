#!/usr/bin/env bash
# Install dependencies for voice dictation with optional auto-paste via ydotool.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YDOTOOL_VERSION="v1.0.4"
YDOTOOL_ARCH="ubuntu-latest"

echo "==> Checking PipeWire/PulseAudio compatibility..."
if command -v pactl >/dev/null 2>&1 && ! pactl info 2>/dev/null | grep -q "PipeWire"; then
  echo "WARN: PipeWire not detected. Install/start pipewire-pulse if recording fails."
fi

install_runtime_packages() {
  if command -v pacman >/dev/null 2>&1; then
    echo "==> Installing pacman packages..."
    sudo pacman -S --needed ffmpeg curl jq wl-clipboard libnotify pipewire-pulse ydotool acl python gum
  elif command -v apt >/dev/null 2>&1; then
    echo "==> Installing APT packages..."
    sudo apt install -y ffmpeg curl jq wl-clipboard libnotify-bin acl python3
  else
    echo "WARN: No supported package manager found. Install ffmpeg curl jq wl-clipboard libnotify acl and optionally ydotool manually."
  fi
}

echo "==> Optional privileged runtime/integration setup"
echo "    This helper uses sudo for desktop packages and, if selected, /dev/uinput access."
echo "    It does not install Podman or compiler toolchains."
echo "    Application files installed by ./install.sh remain under the user prefix."
echo ""
install_runtime_packages

# -----------------------------------------------------------------------------
# Optional: ydotool for auto-paste (Ctrl+V simulation on Wayland)
# -----------------------------------------------------------------------------

install_ydotool() {
  if command -v ydotool >/dev/null 2>&1 && command -v ydotoold >/dev/null 2>&1; then
    echo "==> ydotool already installed: $(command -v ydotool)"
    return 0
  fi

  local target_dir="$HOME/.local/bin"
  mkdir -p "$target_dir"

  echo "==> Downloading ydotool ${YDOTOOL_VERSION} fallback binaries..."
  curl -sSL -o "$target_dir/ydotool" \
    "https://github.com/ReimuNotMoe/ydotool/releases/download/${YDOTOOL_VERSION}/ydotool-release-${YDOTOOL_ARCH}"
  curl -sSL -o "$target_dir/ydotoold" \
    "https://github.com/ReimuNotMoe/ydotool/releases/download/${YDOTOOL_VERSION}/ydotoold-release-${YDOTOOL_ARCH}"
  chmod +x "$target_dir/ydotool" "$target_dir/ydotoold"
  echo "    installed to $target_dir/"
}

setup_uinput_udev() {
  echo "==> Configuring /dev/uinput udev rule..."
  sudo tee /etc/udev/rules.d/80-uinput.rules > /dev/null <<'EOF'
KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"
EOF
  sudo udevadm control --reload-rules
  sudo udevadm trigger
  if ! groups "$USER" | grep -qw input; then
    echo "==> Adding $USER to 'input' group..."
    sudo usermod -aG input "$USER"
    NEED_RELOGIN=1
  fi
  # Reload module to apply new permissions to existing /dev/uinput node.
  sudo modprobe -r uinput 2>/dev/null || true
  sudo modprobe uinput
  # Make auto-paste work in the current login session too; group membership
  # from usermod only applies after re-login, but ACL is immediate.
  if command -v setfacl >/dev/null 2>&1 && [[ -e /dev/uinput ]]; then
    sudo setfacl -m "u:$USER:rw" /dev/uinput || true
  fi
}

install_ydotoold_service() {
  echo "==> Installing ydotoold user systemd service..."
  local ydotoold_bin user_unit_dir dropin_dir
  ydotoold_bin="$(command -v ydotoold || echo "$HOME/.local/bin/ydotoold")"
  user_unit_dir="$HOME/.config/systemd/user"
  dropin_dir="$user_unit_dir/ydotool.service.d"
  mkdir -p "$dropin_dir"

  # Some distros package /usr/lib/systemd/user/ydotool.service with plain
  # `ydotoold`, which creates the wrong/default socket for kwispr and can get
  # stuck in start-limit-hit after first boot. Use a user-service drop-in so the
  # socket is stable and scoped to the active login session (%t=/run/user/$UID).
  if ! systemctl --user cat ydotool.service >/dev/null 2>&1; then
    cat > "$user_unit_dir/ydotool.service" <<EOF
[Unit]
Description=ydotool daemon for Kwispr auto-paste
Documentation=https://github.com/ReimuNotMoe/ydotool
After=graphical-session.target

[Service]
Type=simple
Restart=on-failure
RestartSec=2
ExecStart=$ydotoold_bin --socket-path=%t/.ydotool_socket --socket-perm=0600

[Install]
WantedBy=default.target
EOF
  fi

  cat > "$dropin_dir/override.conf" <<EOF
[Unit]
After=graphical-session.target

[Service]
ExecStart=
ExecStart=$ydotoold_bin --socket-path=%t/.ydotool_socket --socket-perm=0600
Restart=on-failure
RestartSec=2
EOF

  systemctl --user daemon-reload
  systemctl --user reset-failed ydotool.service || true
  systemctl --user enable --now ydotool.service
  sleep 0.5
  if systemctl --user is-active --quiet ydotool.service; then
    echo "    ydotoold running (pid $(systemctl --user show -p MainPID --value ydotool.service))"
    echo "    socket: /run/user/$(id -u)/.ydotool_socket"
  else
    echo "    WARN: ydotool.service failed to start. Check: systemctl --user status ydotool.service"
  fi
}

NEED_RELOGIN=0

echo ""
read -r -p "==> Install ydotool for auto-paste (Ctrl+V simulation)? [Y/n] " YN
YN="${YN:-Y}"
if [[ "$YN" =~ ^[Yy]$ ]]; then
  install_ydotool
  setup_uinput_udev
  install_ydotoold_service
else
  echo "    Skipped. Auto-paste disabled — set KWISPR_AUTOPASTE=0 in .env"
fi

# -----------------------------------------------------------------------------
# Finalize
# -----------------------------------------------------------------------------

echo "==> Making kwispr.sh executable..."
chmod +x "$SCRIPT_DIR/kwispr.sh"

echo "==> Creating cache directory..."
mkdir -p "$HOME/.cache/kwispr"

echo "==> Generating sound cues..."
mkdir -p "$SCRIPT_DIR/sounds"
if [[ ! -f "$SCRIPT_DIR/sounds/start.wav" ]]; then
  ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "sine=frequency=1000:duration=0.06,apad=pad_dur=0.07,volume=0.25" \
    -filter_complex "[0:a][0:a][0:a]concat=n=3:v=0:a=1[out]" \
    -map "[out]" -ar 44100 -ac 1 "$SCRIPT_DIR/sounds/start.wav"
  echo "    start.wav (1000 Hz pip×3) generated"
fi
if [[ ! -f "$SCRIPT_DIR/sounds/stop.wav" ]]; then
  ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "sine=frequency=500:duration=0.06,apad=pad_dur=0.07,volume=0.25" \
    -filter_complex "[0:a][0:a][0:a]concat=n=3:v=0:a=1[out]" \
    -map "[out]" -ar 44100 -ac 1 "$SCRIPT_DIR/sounds/stop.wav"
  echo "    stop.wav (500 Hz pup×3) generated"
fi
if [[ ! -f "$SCRIPT_DIR/sounds/ready.wav" ]]; then
  ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "sine=frequency=800:duration=0.06,apad=pad_dur=0.05,volume=0.25" \
    -f lavfi -i "sine=frequency=1200:duration=0.06,apad=pad_dur=0.03,volume=0.25" \
    -filter_complex "[0:a][1:a]concat=n=2:v=0:a=1[out]" \
    -map "[out]" -ar 44100 -ac 1 "$SCRIPT_DIR/sounds/ready.wav"
  echo "    ready.wav (800→1200 Hz ding-dong) generated"
fi

echo ""
echo "==> Setup complete."
echo ""
echo "NEXT STEPS:"
echo "  1. Run: $SCRIPT_DIR/install.sh"
echo "  2. Open settings: $HOME/.local/bin/kwispr settings"
echo "  3. Bind your KDE shortcut to: $HOME/.local/bin/kwispr toggle"
echo "     System Settings → Shortcuts → Custom Shortcuts → Add New → Command"
echo ""
echo "Kwispr stores UI-managed settings in ${XDG_CONFIG_HOME:-$HOME/.config}/kwispr/config.env."
echo "A repository .env is supported only as a legacy migration source."

if [[ "$NEED_RELOGIN" == "1" ]]; then
  echo ""
  echo "⚠  You were added to the 'input' group. Re-login (or reboot) for the"
  echo "   group membership to take effect. Auto-paste won't work until then."
fi
