"""Configuration loading for arch-controller."""

from __future__ import annotations

import os
import secrets
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PORT = 8787


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "arch-controller"


@dataclass
class Command:
    """A whitelisted command the phone is allowed to trigger."""

    id: str
    label: str
    argv: list[str]
    confirm: bool = False
    timeout: int = 120


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    token: str = ""
    token_file: Path = field(default_factory=lambda: config_home() / "token")
    session_hours: int = 720
    allow_shell: bool = False
    shell_timeout: int = 30
    allow_sudo: bool = False
    sudo_timeout: int = 120
    sudo_cache_minutes: int = 5
    apps_enabled: bool = True
    files_enabled: bool = True
    files_root: Path | None = None
    files_show_hidden: bool = False
    public_url: str = ""
    commands: list[Command] = field(default_factory=list)
    path: Path | None = None

    @property
    def base_url(self) -> str:
        if self.public_url:
            return self.public_url.rstrip("/")
        host = self.host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    def login_url(self) -> str:
        return f"{self.base_url}/#t={self.token}"


def default_config_path() -> Path:
    return config_home() / "config.toml"


def load(path: Path | None = None) -> Config:
    """Load config from TOML, falling back to defaults when the file is absent."""
    path = path or default_config_path()
    data: dict = {}
    if path.exists():
        with open(path, "rb") as fh:
            data = tomllib.load(fh)

    cfg = Config(path=path if path.exists() else None)

    server = data.get("server", {})
    cfg.host = str(server.get("host", cfg.host))
    cfg.port = int(server.get("port", cfg.port))
    cfg.public_url = str(server.get("public_url", ""))

    auth = data.get("auth", {})
    if "token_file" in auth:
        cfg.token_file = Path(os.path.expanduser(str(auth["token_file"])))
    cfg.session_hours = int(auth.get("session_hours", cfg.session_hours))

    actions = data.get("actions", {})
    cfg.allow_shell = bool(actions.get("allow_shell", False))
    cfg.shell_timeout = int(actions.get("shell_timeout", cfg.shell_timeout))
    cfg.allow_sudo = bool(actions.get("allow_sudo", False))
    cfg.sudo_timeout = int(actions.get("sudo_timeout", cfg.sudo_timeout))
    cfg.sudo_cache_minutes = max(0, int(actions.get("sudo_cache_minutes", cfg.sudo_cache_minutes)))

    apps = data.get("apps", {})
    cfg.apps_enabled = bool(apps.get("enabled", True))

    files = data.get("files", {})
    cfg.files_enabled = bool(files.get("enabled", True))
    cfg.files_show_hidden = bool(files.get("show_hidden", False))
    if files.get("root"):
        cfg.files_root = Path(os.path.expanduser(str(files["root"]))).resolve()

    for raw in data.get("commands", []):
        try:
            argv = [str(a) for a in raw["argv"]]
            cmd_id = str(raw["id"])
        except (KeyError, TypeError) as exc:
            raise ValueError(f"invalid [[commands]] entry in {path}: {exc}") from exc
        if not argv:
            raise ValueError(f"command {cmd_id!r} has an empty argv")
        cfg.commands.append(
            Command(
                id=cmd_id,
                label=str(raw.get("label", cmd_id)),
                argv=argv,
                confirm=bool(raw.get("confirm", False)),
                timeout=int(raw.get("timeout", 120)),
            )
        )

    # Token: env wins, then config, then the token file.
    env_token = os.environ.get("ARCHCTL_TOKEN", "").strip()
    if env_token:
        cfg.token = env_token
    elif auth.get("token"):
        cfg.token = str(auth["token"]).strip()
    else:
        cfg.token = read_or_create_token(cfg.token_file)

    return cfg


def read_or_create_token(path: Path) -> str:
    """Read the shared secret, generating one with 0600 perms on first run."""
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.chmod(0o600)
        if token:
            return token
    return write_token(path, secrets.token_urlsafe(32))


def write_token(path: Path, token: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    path.chmod(0o600)
    return token
