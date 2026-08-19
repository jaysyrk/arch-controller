"""Find and launch desktop applications from their .desktop entries."""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

from .actions import ActionError, which

# Field codes the spec says a launcher must strip when it has no file to pass.
FIELD_CODES = {"%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N", "%i", "%c", "%k", "%v", "%m"}

TERMINALS = [
    ["foot"], ["alacritty", "-e"], ["kitty"], ["wezterm", "start", "--"],
    ["konsole", "-e"], ["gnome-terminal", "--"], ["xfce4-terminal", "-e"],
    ["st", "-e"], ["urxvt", "-e"], ["xterm", "-e"],
]

_cache_lock = threading.Lock()
_cache: tuple[float, list[dict]] | None = None
CACHE_SECONDS = 60


def data_dirs() -> list[Path]:
    """XDG application directories, most specific first."""
    home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    system = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    roots = [home, *system.split(":")]
    seen: list[Path] = []
    for root in roots:
        if not root:
            continue
        path = Path(root) / "applications"
        if path.is_dir() and path not in seen:
            seen.append(path)
    return seen


def parse_desktop_entry(text: str) -> dict | None:
    """Pull the fields we care about out of the [Desktop Entry] group."""
    entry: dict[str, str] = {}
    in_group = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_group = line == "[Desktop Entry]"
            continue
        if not in_group or not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        # Skip localised variants like Name[de]; the plain key is what we show.
        if "[" in key:
            continue
        entry[key] = value.strip()

    if entry.get("Type", "Application") != "Application":
        return None
    if entry.get("NoDisplay", "").lower() == "true" or entry.get("Hidden", "").lower() == "true":
        return None
    if not entry.get("Name") or not entry.get("Exec"):
        return None
    try_exec = entry.get("TryExec")
    if try_exec and not which(try_exec) and not Path(try_exec).exists():
        return None

    return {
        "name": entry["Name"],
        "exec": entry["Exec"],
        "comment": entry.get("Comment", ""),
        "terminal": entry.get("Terminal", "").lower() == "true",
        "categories": [c for c in entry.get("Categories", "").split(";") if c],
    }


def clean_exec(exec_line: str) -> list[str]:
    """Turn a desktop Exec= line into an argv list, dropping field codes."""
    try:
        parts = shlex.split(exec_line)
    except ValueError:
        parts = exec_line.split()
    argv = [p for p in parts if p not in FIELD_CODES]
    # A code glued to another argument (--file=%f) loses just the code.
    argv = [p for p in (strip_field_codes(p) for p in argv) if p]
    return argv


def strip_field_codes(arg: str) -> str:
    for code in FIELD_CODES:
        arg = arg.replace(code, "")
    return arg.strip()


def list_apps(force: bool = False) -> list[dict]:
    """Every launchable application on the machine, sorted by name."""
    global _cache
    now = time.time()
    with _cache_lock:
        if _cache and not force and now - _cache[0] < CACHE_SECONDS:
            return _cache[1]

    found: dict[str, dict] = {}
    for directory in data_dirs():
        for path in sorted(directory.rglob("*.desktop")):
            app_id = path.name
            if app_id in found:  # earlier directories win, per XDG precedence
                continue
            try:
                entry = parse_desktop_entry(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if entry is None:
                continue
            found[app_id] = {
                "id": app_id,
                "name": entry["name"],
                "comment": entry["comment"],
                "terminal": entry["terminal"],
                "categories": entry["categories"],
                "path": str(path),
            }

    apps = sorted(found.values(), key=lambda a: a["name"].lower())
    with _cache_lock:
        _cache = (now, apps)
    return apps


def find_app(app_id: str) -> dict:
    for app in list_apps():
        if app["id"] == app_id:
            return app
    raise ActionError(f"no application named {app_id!r}", status=404)


def terminal_argv() -> list[str] | None:
    for candidate in TERMINALS:
        if which(candidate[0]):
            return candidate
    return None


def launch(app_id: str) -> dict:
    """Start an app detached, so it outlives the request."""
    app = find_app(app_id)

    # gtk-launch and gio understand desktop files properly (actions, D-Bus
    # activation, the lot), so prefer them and only hand-roll as a fallback.
    if which("gtk-launch"):
        argv = ["gtk-launch", app_id]
    elif which("gio"):
        argv = ["gio", "launch", app["path"]]
    else:
        entry = parse_desktop_entry(Path(app["path"]).read_text(encoding="utf-8", errors="replace"))
        if entry is None:
            raise ActionError(f"{app_id} is no longer launchable")
        argv = clean_exec(entry["exec"])
        if not argv:
            raise ActionError(f"{app_id} has no runnable Exec line")
        if entry["terminal"]:
            term = terminal_argv()
            if term is None:
                raise ActionError(f"{app['name']} needs a terminal emulator, none found", status=501)
            argv = term + argv

    try:
        subprocess.Popen(
            argv,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ActionError(f"could not launch {app['name']}: {exc}") from exc
    return {"ok": True, "id": app_id, "name": app["name"]}
