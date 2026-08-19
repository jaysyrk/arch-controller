"""Command line entry point: python -m archctl <command>."""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

from . import __version__, actions, config as config_module, server


def cmd_serve(cfg, args) -> int:
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    return server.serve(cfg)


def cmd_token(cfg, args) -> int:
    if args.rotate:
        cfg.token = config_module.write_token(cfg.token_file, secrets.token_urlsafe(32))
        print(f"new token written to {cfg.token_file}", file=sys.stderr)
        print("restart arch-controller and log in again on your phone", file=sys.stderr)
    print(cfg.token)
    return 0


def cmd_url(cfg, args) -> int:
    print(cfg.login_url())
    return 0


def cmd_check(cfg, args) -> int:
    caps = actions.capabilities()
    print(f"arch-controller {__version__}")
    print(f"config      {cfg.path or '(defaults, no config file)'}")
    print(f"token file  {cfg.token_file}")
    print(f"listening   {cfg.host}:{cfg.port}")
    print(f"base url    {cfg.base_url}")
    print(f"shell       {'enabled' if cfg.allow_shell else 'disabled'}")
    print(f"commands    {len(cfg.commands)} configured")
    print("\nfeature support on this machine:")
    hints = {
        "media": "playerctl",
        "volume": "wireplumber (wpctl) or pulseaudio (pactl)",
        "brightness": "brightnessctl",
        "power": "systemd",
        "lock": "systemd-logind (loginctl)",
        "notify": "libnotify (notify-send)",
        "clipboard": "wl-clipboard or xclip",
        "open": "xdg-utils",
        "screenshot": "grim (Wayland) or maim/scrot (X11)",
    }
    for name, ok in sorted(caps.items()):
        mark = "yes" if ok else "no "
        suffix = "" if ok else f"  — install {hints[name]}"
        print(f"  [{mark}] {name}{suffix}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archctl",
        description="Control this Arch Linux machine from a phone, over any network.",
    )
    parser.add_argument("--version", action="version", version=f"arch-controller {__version__}")
    parser.add_argument("-c", "--config", type=Path, help="path to config.toml")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="run the control server")
    serve_parser.add_argument("--host", help="override the bind address")
    serve_parser.add_argument("--port", type=int, help="override the port")
    serve_parser.set_defaults(func=cmd_serve)

    token_parser = subparsers.add_parser("token", help="print or rotate the access token")
    token_parser.add_argument("--rotate", action="store_true", help="generate a fresh token")
    token_parser.set_defaults(func=cmd_token)

    url_parser = subparsers.add_parser("url", help="print a one-tap login URL for your phone")
    url_parser.set_defaults(func=cmd_url)

    check_parser = subparsers.add_parser("check", help="report config and feature support")
    check_parser.set_defaults(func=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args((argv or []) + ["serve"])

    try:
        cfg = config_module.load(args.config)
    except (ValueError, OSError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
