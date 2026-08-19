"""Filesystem navigation: list directories, open files on the desktop."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .actions import ActionError, which

MAX_ENTRIES = 500


def resolve(raw: str, root: Path | None = None) -> Path:
    """Expand and normalise a requested path, keeping it inside root."""
    raw = (raw or "~").strip() or "~"
    path = Path(os.path.expanduser(raw))
    try:
        path = path.resolve()
    except OSError as exc:
        raise ActionError(f"cannot resolve path: {exc}") from exc
    if root is not None:
        root = root.resolve()
        if path != root and root not in path.parents:
            raise ActionError(f"{path} is outside the allowed root {root}", status=403)
    return path


def places() -> list[dict]:
    """Shortcut chips for the usual destinations that actually exist."""
    home = Path.home()
    candidates = [
        ("Home", home),
        ("Downloads", home / "Downloads"),
        ("Documents", home / "Documents"),
        ("Pictures", home / "Pictures"),
        ("Config", home / ".config"),
        ("Root", Path("/")),
        ("Etc", Path("/etc")),
        ("Logs", Path("/var/log")),
    ]
    return [{"label": label, "path": str(p)} for label, p in candidates if p.is_dir()]


def entry_info(path: Path) -> dict:
    """One row, tolerant of broken symlinks and unreadable metadata."""
    info = {
        "name": path.name,
        "path": str(path),
        "is_dir": False,
        "size": 0,
        "mtime": 0.0,
        "symlink": path.is_symlink(),
        "readable": False,
    }
    try:
        stat = path.stat()  # follows symlinks
        info["is_dir"] = os.path.isdir(path)
        info["size"] = 0 if info["is_dir"] else stat.st_size
        info["mtime"] = stat.st_mtime
        info["readable"] = os.access(path, os.R_OK)
    except OSError:
        # Dangling symlink or no permission to stat the target.
        try:
            info["is_dir"] = path.is_dir()
        except OSError:
            pass
    return info


def listdir(raw_path: str, show_hidden: bool = False, root: Path | None = None) -> dict:
    path = resolve(raw_path, root)
    if not path.exists():
        raise ActionError(f"{path} does not exist", status=404)
    if not path.is_dir():
        raise ActionError(f"{path} is not a directory")

    try:
        children = sorted(path.iterdir(), key=lambda p: p.name.lower())
    except PermissionError:
        raise ActionError(f"not permitted to read {path}", status=403) from None
    except OSError as exc:
        raise ActionError(f"cannot read {path}: {exc}") from exc

    if not show_hidden:
        children = [c for c in children if not c.name.startswith(".")]

    truncated = len(children) > MAX_ENTRIES
    entries = [entry_info(c) for c in children[:MAX_ENTRIES]]
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))

    parent = str(path.parent) if path != path.parent else None
    if root is not None and parent is not None:
        root = root.resolve()
        if path == root:
            parent = None

    return {
        "path": str(path),
        "parent": parent,
        "entries": entries,
        "truncated": truncated,
        "places": places(),
    }


def open_on_desktop(raw_path: str, root: Path | None = None) -> dict:
    """Hand a file to whatever the desktop uses for it."""
    path = resolve(raw_path, root)
    if not path.exists():
        raise ActionError(f"{path} does not exist", status=404)
    if not which("xdg-open"):
        raise ActionError("xdg-open not found (install xdg-utils)", status=501)
    subprocess.Popen(
        ["xdg-open", str(path)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"ok": True, "path": str(path)}


def download_target(raw_path: str, root: Path | None = None) -> Path:
    """Validate a path is a readable regular file before streaming it."""
    path = resolve(raw_path, root)
    if not path.is_file():
        raise ActionError(f"{path} is not a file", status=404)
    if not os.access(path, os.R_OK):
        raise ActionError(f"not permitted to read {path}", status=403)
    return path
