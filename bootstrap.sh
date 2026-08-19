#!/usr/bin/env bash
# Take a fresh Arch box from nothing to phone-controllable, in one command.
#
# Installs Tailscale so the machine is reachable from any network, installs the
# desktop helpers arch-controller uses, then hands over to ./install.sh for the
# service itself. Safe to re-run: everything here checks before it acts.
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info() { printf '\033[1;34m::\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m::\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m::\033[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1;36m==>\033[0m \033[1m%s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] && die "run this as your normal user — it will use sudo where it needs to"
command -v pacman >/dev/null || die "this bootstrap is for Arch (no pacman found); see the README for other distros"

# Ask once up front, so the rest can run unattended.
step "What this will do"
cat <<'PLAN'
  1. install Tailscale, so your phone can reach this machine from any network
  2. install the desktop helpers arch-controller uses (media keys, brightness,
     clipboard, screenshots, notifications) — skippable
  3. bring Tailscale up, which opens a browser for you to sign in
  4. run ./install.sh to set up the service, token and systemd unit

Everything is optional and re-runnable. Nothing is exposed to the internet:
Tailscale is a private mesh, and the service binds to your tailnet address only.
PLAN
read -rp $'\n:: continue? [Y/n] ' reply
[[ "$reply" =~ ^[Nn] ]] && { info "nothing done"; exit 0; }

# -- 1. Tailscale ---------------------------------------------------------
step "Tailscale"
if command -v tailscale >/dev/null; then
  info "already installed"
else
  info "installing tailscale"
  sudo pacman -S --needed --noconfirm tailscale
fi

if ! systemctl is-enabled --quiet tailscaled 2>/dev/null; then
  info "enabling tailscaled"
  sudo systemctl enable --now tailscaled
else
  sudo systemctl start tailscaled 2>/dev/null || true
fi

# -- 2. desktop helpers ---------------------------------------------------
step "Desktop helpers"

# grim is Wayland-only and maim is X11-only, so pick by session type rather
# than installing both and letting one sit unused.
SESSION="${XDG_SESSION_TYPE:-unknown}"
if [[ "$SESSION" == "wayland" ]]; then
  SHOT_PKG="grim"
elif [[ "$SESSION" == "x11" ]]; then
  SHOT_PKG="maim"
else
  warn "could not tell Wayland from X11 (XDG_SESSION_TYPE=$SESSION), defaulting to grim"
  SHOT_PKG="grim"
fi

CLIP_PKG="wl-clipboard"
[[ "$SESSION" == "x11" ]] && CLIP_PKG="xclip"

HELPERS=(playerctl brightnessctl libnotify xdg-utils "$SHOT_PKG" "$CLIP_PKG")
MISSING=()
for pkg in "${HELPERS[@]}"; do
  pacman -Qi "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
done

if [[ ${#MISSING[@]} -eq 0 ]]; then
  info "all helpers already installed"
else
  info "missing: ${MISSING[*]}"
  read -rp ":: install them? (media keys, brightness, clipboard, screenshots) [Y/n] " reply
  if [[ ! "$reply" =~ ^[Nn] ]]; then
    sudo pacman -S --needed --noconfirm "${MISSING[@]}"
  else
    warn "skipped — arch-controller will hide the controls it cannot serve"
  fi
fi

# -- 3. join the tailnet --------------------------------------------------
step "Joining your tailnet"
if tailscale status >/dev/null 2>&1; then
  info "already signed in as $(tailscale status --json 2>/dev/null | grep -o '"DNSName":"[^"]*"' | head -n1 | cut -d'"' -f4 || echo "this machine")"
else
  info "a browser will open — sign in with the same account you will use on your phone"
  sudo tailscale up
fi

TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
[[ -n "$TS_IP" ]] || die "tailscale did not report an address; run 'sudo tailscale up' and re-run this script"
info "this machine is $TS_IP on your tailnet"

# -- 4. the service itself ------------------------------------------------
step "arch-controller"
"$INSTALL_DIR/install.sh"

step "On your phone"
cat <<PHONE
  1. install Tailscale from the App Store and sign in with the same account
  2. open the login link printed above in Safari
  3. Share -> Add to Home Screen

Then it works from anywhere — cellular, someone else's wifi, a hotel.

To reach a full Claude Code session on this machine from your phone, install an
SSH client (Blink or Termius), connect to $TS_IP, and run: claude
PHONE
