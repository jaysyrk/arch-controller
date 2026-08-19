"""Everything the phone can actually make the machine do.

Each action shells out to a well-known Arch tool with a fixed argv (never a
shell string), and every tool is probed at startup so the UI can hide controls
the machine cannot support.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SINK = "@DEFAULT_AUDIO_SINK@"


class ActionError(Exception):
    """A requested action failed or is unsupported on this machine."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Result:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    code: int = 0

    def as_dict(self) -> dict:
        return {"ok": self.ok, "stdout": self.stdout, "stderr": self.stderr, "code": self.code}


def which(name: str) -> str | None:
    return shutil.which(name)


def run(argv: list[str], timeout: int = 15, check: bool = True) -> Result:
    """Run argv with no shell. Raises ActionError when check and it fails."""
    if not which(argv[0]):
        raise ActionError(f"{argv[0]} is not installed on this machine", status=501)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ActionError(f"{argv[0]} timed out after {timeout}s", status=504) from None
    except OSError as exc:
        raise ActionError(f"failed to run {argv[0]}: {exc}", status=500) from exc

    result = Result(
        ok=proc.returncode == 0,
        stdout=proc.stdout.strip(),
        stderr=proc.stderr.strip(),
        code=proc.returncode,
    )
    if check and not result.ok:
        raise ActionError(result.stderr or f"{argv[0]} exited {proc.returncode}")
    return result


def capabilities() -> dict:
    """Which feature groups this machine can serve."""
    volume = bool(which("wpctl") or which("pactl") or which("amixer"))
    clipboard = bool((which("wl-copy") and which("wl-paste")) or which("xclip"))
    screenshot = bool(which("grim") or which("maim") or which("scrot"))
    return {
        "media": bool(which("playerctl")),
        "volume": volume,
        "brightness": bool(which("brightnessctl")),
        "power": bool(which("systemctl")),
        "lock": bool(which("loginctl")),
        "notify": bool(which("notify-send")),
        "clipboard": clipboard,
        "open": bool(which("xdg-open")),
        "screenshot": screenshot,
    }


# -- media ----------------------------------------------------------------

MEDIA_ACTIONS = {"play-pause", "next", "previous", "stop"}


def media(action: str) -> dict:
    if action not in MEDIA_ACTIONS:
        raise ActionError(f"unknown media action {action!r}")
    run(["playerctl", action])
    return media_status()


def media_status() -> dict:
    if not which("playerctl"):
        return {"available": False}
    status = run(["playerctl", "status"], check=False)
    if not status.ok:
        return {"available": True, "status": "stopped", "now_playing": ""}
    meta = run(
        ["playerctl", "metadata", "--format", "{{artist}} — {{title}}"],
        check=False,
    )
    return {
        "available": True,
        "status": status.stdout.lower(),
        "now_playing": meta.stdout if meta.ok else "",
    }


# -- volume ---------------------------------------------------------------


def parse_wpctl_volume(text: str) -> dict:
    """'Volume: 0.45 [MUTED]' -> {'percent': 45, 'muted': True}."""
    parts = text.split()
    percent = 0
    for part in parts[1:]:
        try:
            percent = round(float(part) * 100)
            break
        except ValueError:
            continue
    return {"percent": percent, "muted": "[MUTED]" in text.upper()}


def parse_pactl_volume(text: str) -> int:
    """First percentage in a `pactl get-sink-volume` line."""
    for token in text.replace("/", " ").split():
        if token.endswith("%") and token[:-1].strip().isdigit():
            return int(token[:-1])
    return 0


def volume_status() -> dict:
    if which("wpctl"):
        res = run(["wpctl", "get-volume", SINK], check=False)
        if res.ok:
            return {"available": True, **parse_wpctl_volume(res.stdout)}
    if which("pactl"):
        vol = run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], check=False)
        mute = run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"], check=False)
        if vol.ok:
            return {
                "available": True,
                "percent": parse_pactl_volume(vol.stdout),
                "muted": "yes" in mute.stdout.lower(),
            }
    return {"available": False, "percent": 0, "muted": False}


def _clamp_step(step: int) -> int:
    return max(1, min(int(step), 50))


def volume(action: str, value: int | None = None) -> dict:
    step = _clamp_step(value if value is not None else 5)
    if which("wpctl"):
        if action == "up":
            run(["wpctl", "set-volume", "--limit", "1.0", SINK, f"{step}%+"])
        elif action == "down":
            run(["wpctl", "set-volume", SINK, f"{step}%-"])
        elif action == "mute":
            run(["wpctl", "set-mute", SINK, "toggle"])
        elif action == "set":
            level = max(0, min(int(value or 0), 100))
            run(["wpctl", "set-volume", SINK, f"{level}%"])
        else:
            raise ActionError(f"unknown volume action {action!r}")
        return volume_status()

    if which("pactl"):
        sink = "@DEFAULT_SINK@"
        if action == "up":
            run(["pactl", "set-sink-volume", sink, f"+{step}%"])
        elif action == "down":
            run(["pactl", "set-sink-volume", sink, f"-{step}%"])
        elif action == "mute":
            run(["pactl", "set-sink-mute", sink, "toggle"])
        elif action == "set":
            level = max(0, min(int(value or 0), 100))
            run(["pactl", "set-sink-volume", sink, f"{level}%"])
        else:
            raise ActionError(f"unknown volume action {action!r}")
        return volume_status()

    raise ActionError("no volume control found (install wireplumber or pulseaudio)", status=501)


# -- brightness -----------------------------------------------------------


def parse_brightnessctl(text: str) -> int:
    """'device,class,current,45%,max' -> 45."""
    fields = text.strip().split(",")
    for field in fields:
        if field.endswith("%") and field[:-1].isdigit():
            return int(field[:-1])
    return 0


def brightness_status() -> dict:
    if not which("brightnessctl"):
        return {"available": False, "percent": 0}
    res = run(["brightnessctl", "-m"], check=False)
    if not res.ok or not res.stdout:
        return {"available": False, "percent": 0}
    return {"available": True, "percent": parse_brightnessctl(res.stdout.splitlines()[0])}


def brightness(action: str, value: int | None = None) -> dict:
    step = _clamp_step(value if value is not None else 10)
    if action == "up":
        run(["brightnessctl", "set", f"{step}%+"])
    elif action == "down":
        run(["brightnessctl", "set", f"{step}%-"])
    elif action == "set":
        level = max(1, min(int(value or 1), 100))
        run(["brightnessctl", "set", f"{level}%"])
    else:
        raise ActionError(f"unknown brightness action {action!r}")
    return brightness_status()


# -- power ----------------------------------------------------------------

POWER_ACTIONS = {
    "lock": ["loginctl", "lock-session"],
    "suspend": ["systemctl", "suspend"],
    "hibernate": ["systemctl", "hibernate"],
    "reboot": ["systemctl", "reboot"],
    "poweroff": ["systemctl", "poweroff"],
}


def power(action: str) -> dict:
    argv = POWER_ACTIONS.get(action)
    if argv is None:
        raise ActionError(f"unknown power action {action!r}")
    # reboot/poweroff kill the connection before systemd replies; fire and forget.
    detached = action in ("reboot", "poweroff", "suspend", "hibernate")
    if detached:
        if not which(argv[0]):
            raise ActionError(f"{argv[0]} is not installed on this machine", status=501)
        subprocess.Popen(argv, start_new_session=True)
        return {"ok": True, "action": action, "detached": True}
    run(argv)
    return {"ok": True, "action": action, "detached": False}


# -- desktop odds and ends ------------------------------------------------


def notify(title: str, body: str = "") -> dict:
    title = (title or "arch-controller").strip()[:200]
    run(["notify-send", "--app-name=arch-controller", title, body.strip()[:1000]])
    return {"ok": True}


def clipboard_get() -> dict:
    if which("wl-paste"):
        res = run(["wl-paste", "--no-newline"], check=False)
    elif which("xclip"):
        res = run(["xclip", "-selection", "clipboard", "-o"], check=False)
    else:
        raise ActionError("no clipboard tool found (install wl-clipboard or xclip)", status=501)
    return {"text": res.stdout if res.ok else ""}


def clipboard_set(text: str) -> dict:
    text = text[:100_000]
    if which("wl-copy"):
        argv = ["wl-copy", "--"]
    elif which("xclip"):
        argv = ["xclip", "-selection", "clipboard"]
    else:
        raise ActionError("no clipboard tool found (install wl-clipboard or xclip)", status=501)
    try:
        subprocess.run(argv, input=text, text=True, timeout=10, check=True)
    except (subprocess.SubprocessError, OSError) as exc:
        raise ActionError(f"clipboard write failed: {exc}") from exc
    return {"ok": True, "length": len(text)}


ALLOWED_URL_SCHEMES = ("http://", "https://")


def open_url(url: str) -> dict:
    url = (url or "").strip()
    if not url.startswith(ALLOWED_URL_SCHEMES):
        raise ActionError("only http:// and https:// URLs can be opened")
    if any(ch in url for ch in "\n\r\x00"):
        raise ActionError("invalid URL")
    subprocess.Popen(["xdg-open", url], start_new_session=True)
    return {"ok": True, "url": url}


def screenshot(dest: Path) -> Path:
    if which("grim"):
        run(["grim", str(dest)], timeout=20)
    elif which("maim"):
        run(["maim", str(dest)], timeout=20)
    elif which("scrot"):
        run(["scrot", "--overwrite", str(dest)], timeout=20)
    else:
        raise ActionError("no screenshot tool found (install grim, maim or scrot)", status=501)
    if not dest.exists() or dest.stat().st_size == 0:
        raise ActionError("screenshot tool produced no image", status=500)
    return dest


# -- processes ------------------------------------------------------------


def processes(limit: int = 15) -> list[dict]:
    """Top processes by resident memory, read straight from /proc."""
    limit = max(1, min(int(limit), 100))
    found: list[dict] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8")
        except OSError:
            continue
        name = ""
        rss = 0
        for line in status.splitlines():
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("VmRSS:"):
                fields = line.split()
                if len(fields) >= 2 and fields[1].isdigit():
                    rss = int(fields[1]) * 1024
            if name and rss:
                break
        if not name:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", "replace"
            ).strip()
        except OSError:
            cmdline = ""
        found.append(
            {"pid": int(entry.name), "name": name, "rss": rss, "cmdline": cmdline[:160]}
        )
    found.sort(key=lambda p: p["rss"], reverse=True)
    return found[:limit]


def kill_process(pid: int, signal_name: str = "TERM") -> dict:
    import signal as signal_module

    pid = int(pid)
    if pid <= 1:
        raise ActionError("refusing to signal pid <= 1")
    if pid == os.getpid():
        raise ActionError("refusing to signal arch-controller itself")
    sig = getattr(signal_module, f"SIG{signal_name.upper()}", None)
    if sig not in (signal_module.SIGTERM, signal_module.SIGKILL, signal_module.SIGINT):
        raise ActionError("only TERM, KILL and INT are allowed")
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        raise ActionError(f"no such process {pid}", status=404) from None
    except PermissionError:
        raise ActionError(f"not permitted to signal {pid}", status=403) from None
    return {"ok": True, "pid": pid, "signal": signal_name.upper()}


# -- configured commands --------------------------------------------------


def run_command(command, timeout: int | None = None) -> dict:
    result = run(list(command.argv), timeout=timeout or command.timeout, check=False)
    return {"id": command.id, "label": command.label, **result.as_dict()}


def run_shell(script: str, timeout: int = 30) -> dict:
    """Only reachable when actions.allow_shell is explicitly enabled."""
    script = (script or "").strip()
    if not script:
        raise ActionError("empty command")
    shell = os.environ.get("SHELL") or "/bin/bash"
    if not Path(shell).exists():
        shell = "/bin/sh"
    try:
        proc = subprocess.run(
            [shell, "-lc", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ActionError(f"command timed out after {timeout}s", status=504) from None
    return Result(
        ok=proc.returncode == 0,
        stdout=proc.stdout[-20_000:],
        stderr=proc.stderr[-20_000:],
        code=proc.returncode,
    ).as_dict()
