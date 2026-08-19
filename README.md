# arch-controller

Control your Arch Linux machine from your iPhone, from any network.
**Built with Claude Code!! Not a portfolio project just a useful tool for me**

## Short answer: yes, this is very doable

Your phone can't reach your desktop's LAN address (`192.168.x.x`) from cellular or a
café's wifi, because that address only means something inside your home network, and
your router won't accept unsolicited connections from outside. That's the only real
obstacle, and there are three standard ways past it:

| Approach | How it works | Trade-off |
|---|---|---|
| **Tailscale / WireGuard** (recommended) | Both devices join a private encrypted mesh and get stable `100.x` addresses. Both dial *out*, so it works behind CGNAT and hotel wifi with no router config. | Install an app on both ends. |
| **Cloudflare Tunnel** | A daemon on the Arch box holds an outbound tunnel; you get an HTTPS hostname. | Traffic passes through a third party; needs a domain. |
| **Port forwarding** | Open a port on your router to the machine. | Exposes the service to the whole internet. Don't, unless you must. |

Any of them gets packets to the machine. What you do once they arrive is the other
half, and that's what this repo is: a small control server that runs on the Arch box
and serves a phone-shaped web app.

If all you want is a terminal, you can skip this repo entirely — install Tailscale on
both devices and use [Blink](https://blink.sh) or [Termius](https://termius.com) to SSH
in over the tailnet. This exists for the things a terminal is bad at on a phone: pausing
music, nudging the volume, firing off a suspend, checking whether the build machine is
melting — one tap, no typing.

## What you get

- **A home-screen web app.** Add to Home Screen and it opens full-screen like a native app.
- **Live vitals** — CPU, memory, disk, temperature, battery, uptime, load.
- **Media keys** — play/pause, skip, with the current track, via `playerctl`.
- **Volume and brightness** sliders, via `wpctl`/`pactl` and `brightnessctl`.
- **Power** — lock, suspend, reboot, shut down (destructive ones need a confirmation).
- **Desktop reach-through** — take a screenshot, read/write the clipboard, open a URL,
  push a desktop notification.
- **Your own buttons** — whitelisted commands from the config file, each a fixed argv.
- **Top processes**, with the ability to kill one.
- **A plain JSON API**, so iOS Shortcuts, Siri, or `curl` can drive the same actions.

Pure Python standard library. No pip install, no dependencies, no build step.

## Setup

On the Arch machine:

```bash
sudo pacman -S --needed tailscale playerctl brightnessctl wl-clipboard grim libnotify
sudo systemctl enable --now tailscaled
sudo tailscale up

git clone https://github.com/jaysyrk/arch-controller.git
cd arch-controller
./install.sh
```

The installer writes a config, generates an access token, offers to bind the server to
your Tailscale address, installs a systemd **user** service, enables lingering so it
survives logout, and prints a one-tap login URL.

On the iPhone: install Tailscale from the App Store, sign in to the same account, open
that login URL in Safari, then **Share → Add to Home Screen**.

That's it. It now works from any network, on cellular, anywhere.

### Naming it something memorable

With [MagicDNS](https://tailscale.com/kb/1081/magicdns) on, the box is reachable at
`http://your-hostname:8787`. For real HTTPS (which unlocks the clipboard-to-phone copy
and quiets Safari's warnings) let Tailscale terminate TLS for you:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8787
```

Then set `host = "127.0.0.1"` and `public_url = "https://your-hostname.your-tailnet.ts.net"`
in the config, and `archctl url` prints the right link.

### Without Tailscale

- **Cloudflare Tunnel:** `cloudflared tunnel --url http://127.0.0.1:8787` — keep
  `host = "127.0.0.1"` and put Cloudflare Access in front of it.
- **SSH tunnel:** if you already SSH in from Blink or Termius, forward the port
  (`-L 8787:127.0.0.1:8787`) and open `http://127.0.0.1:8787` in the app's browser.
- **Port forwarding:** if you really must, put it behind a reverse proxy with a real
  TLS certificate. The token is the only thing standing between the internet and your
  machine, and plain HTTP sends it in the clear.

## Using it

```bash
archctl check          # what this machine supports, and what to install for the rest
archctl token          # print the access token
archctl token --rotate # invalidate every logged-in phone
archctl url            # one-tap login link
archctl serve          # run in the foreground
```

(Those are `python3 -m archctl …` if you didn't `pip install .`.)

Service management is ordinary systemd:

```bash
systemctl --user status arch-controller
systemctl --user restart arch-controller
journalctl --user -u arch-controller -f
```

## The API

Everything the web app does is a JSON call with a bearer token, which makes iOS
Shortcuts a first-class client — build a "Suspend the desktop" shortcut, put it on your
home screen, or ask Siri to run it.

```bash
TOKEN=$(archctl token)
HOST=http://your-hostname:8787

curl -H "Authorization: Bearer $TOKEN" $HOST/api/status
curl -H "Authorization: Bearer $TOKEN" -d '{"action":"play-pause"}' $HOST/api/media
curl -H "Authorization: Bearer $TOKEN" -d '{"action":"set","value":40}' $HOST/api/volume
curl -H "Authorization: Bearer $TOKEN" -d '{"action":"suspend"}' $HOST/api/power
curl -H "Authorization: Bearer $TOKEN" -d '{"id":"update-check"}' $HOST/api/command
curl -H "Authorization: Bearer $TOKEN" $HOST/api/screenshot -o screen.png
```

| Method | Path | Body |
|---|---|---|
| `GET` | `/api/ping` | — (public health check) |
| `POST` | `/api/login` | `{"token": "…"}` → session cookie |
| `POST` | `/api/logout` | — |
| `GET` | `/api/status` | — → system, capabilities, media, volume, brightness, commands |
| `POST` | `/api/media` | `{"action": "play-pause\|next\|previous\|stop"}` |
| `POST` | `/api/volume` | `{"action": "up\|down\|mute\|set", "value": 0-100}` |
| `POST` | `/api/brightness` | `{"action": "up\|down\|set", "value": 1-100}` |
| `POST` | `/api/power` | `{"action": "lock\|suspend\|hibernate\|reboot\|poweroff", "confirm": true}` |
| `POST` | `/api/notify` | `{"title": "…", "body": "…"}` |
| `GET`/`POST` | `/api/clipboard` | `{"text": "…"}` to set |
| `POST` | `/api/open` | `{"url": "https://…"}` |
| `GET` | `/api/screenshot` | — → `image/png` |
| `GET` | `/api/processes` | `?limit=15` |
| `POST` | `/api/kill` | `{"pid": 1234, "signal": "TERM"}` |
| `POST` | `/api/command` | `{"id": "…"}` — a command from your config |
| `POST` | `/api/shell` | `{"cmd": "…"}` — only when `allow_shell = true` |

## Security

This is a remote control for your computer, so the design leans conservative:

- **Bearer token or session cookie on every endpoint** except `/api/ping`. The token is
  generated with `secrets`, stored `0600`, and compared in constant time.
- **Brute force throttling** — five bad tokens from an address and the lockout starts
  doubling, up to fifteen minutes.
- **Cookie sessions are `HttpOnly`, `SameSite=Strict`**, and every mutating request also
  needs an `X-Archctl: 1` header that no cross-site form can set. A malicious page you
  open on the same phone cannot reboot your desktop.
- **No shell by default.** Buttons run fixed argv lists you wrote in the config —
  nothing is interpolated into a shell string. `allow_shell` exists, it's off, and
  turning it on means the token is a shell login.
- **Bound to loopback by default.** You choose to widen it.
- **Sharp edges are gated** — reboot, shut down and hibernate need an explicit
  `confirm`, and `kill` refuses PID 1 and the server itself.

The token is the whole security boundary, so treat the login URL like a password, prefer
HTTPS (`tailscale serve`) over plain HTTP on any network you don't own, and run
`archctl token --rotate` if a phone goes missing.

## Development

```bash
python3 -m unittest discover -s tests -v
```

45 tests, standard library only: config parsing and token file permissions, session and
lockout logic, every `/proc` and CLI-output parser, and end-to-end HTTP tests against a
real server covering auth, CSRF, error codes and the action endpoints.

Layout:

```
archctl/
  __main__.py   CLI: serve, token, url, check
  config.py     TOML config, token generation
  auth.py       tokens, sessions, throttling
  system.py     /proc and /sys telemetry
  actions.py    everything that touches the desktop
  server.py     HTTP routing and the JSON API
  web/          the phone app (HTML, CSS, JS, service worker)
```

Feature support is probed at runtime, so the UI only shows controls this machine can
actually serve — `archctl check` lists what's missing and which package provides it.
