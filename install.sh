#!/usr/bin/env bash
# Install arch-controller as a systemd user service on this machine.
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/arch-controller"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_NAME="arch-controller.service"

info() { printf '\033[1;34m::\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m::\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m::\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && die "run this as your normal user, not root — it installs a *user* service"

PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || die "python3 not found: pacman -S python"
"$PYTHON" - <<'PY' || die "arch-controller needs Python 3.11 or newer (for tomllib)"
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

info "installing from $INSTALL_DIR"
mkdir -p "$CONFIG_DIR" "$UNIT_DIR"

if [[ ! -f "$CONFIG_DIR/config.toml" ]]; then
  cp "$INSTALL_DIR/config.example.toml" "$CONFIG_DIR/config.toml"
  chmod 600 "$CONFIG_DIR/config.toml"
  info "wrote $CONFIG_DIR/config.toml"
else
  info "keeping existing $CONFIG_DIR/config.toml"
fi

# Offer the Tailscale address as the bind host — that is the setup that works
# from another network without opening a single port.
if command -v tailscale >/dev/null 2>&1; then
  TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  if [[ -n "$TS_IP" ]] && grep -q '^host = "127.0.0.1"' "$CONFIG_DIR/config.toml"; then
    read -rp ":: bind to your Tailscale address $TS_IP so your phone can reach it? [Y/n] " reply
    if [[ ! "$reply" =~ ^[Nn] ]]; then
      sed -i "s/^host = \"127.0.0.1\"/host = \"$TS_IP\"/" "$CONFIG_DIR/config.toml"
      info "bound to $TS_IP"
    fi
  fi
else
  warn "tailscale not installed — see the README for how your phone will reach this box"
fi

sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" -e "s|__PYTHON__|$PYTHON|g" \
  "$INSTALL_DIR/systemd/$UNIT_NAME" > "$UNIT_DIR/$UNIT_NAME"
info "wrote $UNIT_DIR/$UNIT_NAME"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

# Keep the service alive when you are not logged in at the console.
if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "$USER" >/dev/null 2>&1 || warn "could not enable linger (needs polkit auth)"
fi

sleep 1
if ! systemctl --user is-active --quiet "$UNIT_NAME"; then
  warn "service is not running. Logs:"
  journalctl --user -u "$UNIT_NAME" -n 20 --no-pager || true
  exit 1
fi

info "arch-controller is running"
echo
"$PYTHON" -c "import sys; sys.path.insert(0, '$INSTALL_DIR')" 2>/dev/null || true
(cd "$INSTALL_DIR" && "$PYTHON" -m archctl check)
echo
info "open this on your iPhone (it logs you straight in — treat it like a password):"
(cd "$INSTALL_DIR" && "$PYTHON" -m archctl url)
echo
info "then Share -> Add to Home Screen for a full-screen app icon"
