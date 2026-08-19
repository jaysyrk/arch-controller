"""Read-only system telemetry, straight from /proc and /sys."""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time
from pathlib import Path

_cpu_lock = threading.Lock()
_last_cpu: tuple[int, int] | None = None  # (idle, total)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def uptime_seconds() -> float:
    raw = _read("/proc/uptime").split()
    return float(raw[0]) if raw else 0.0


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def loadavg() -> list[float]:
    raw = _read("/proc/loadavg").split()
    return [float(x) for x in raw[:3]] if len(raw) >= 3 else [0.0, 0.0, 0.0]


def parse_cpu_line(line: str) -> tuple[int, int]:
    """Return (idle, total) jiffies from a /proc/stat 'cpu' line."""
    parts = [int(x) for x in line.split()[1:]]
    if len(parts) < 5:
        return (0, 0)
    idle = parts[3] + parts[4]  # idle + iowait
    return (idle, sum(parts))


def cpu_percent() -> float:
    """Usage since the previous call; 0.0 on the very first sample."""
    global _last_cpu
    for line in _read("/proc/stat").splitlines():
        if line.startswith("cpu "):
            idle, total = parse_cpu_line(line)
            break
    else:
        return 0.0

    with _cpu_lock:
        previous = _last_cpu
        _last_cpu = (idle, total)

    if previous is None:
        return 0.0
    d_idle = idle - previous[0]
    d_total = total - previous[1]
    if d_total <= 0:
        return 0.0
    return round(100.0 * (d_total - d_idle) / d_total, 1)


def parse_meminfo(text: str) -> dict[str, int]:
    """kB values from /proc/meminfo, converted to bytes."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            out[key] = int(fields[0]) * 1024
    return out


def memory() -> dict:
    info = parse_meminfo(_read("/proc/meminfo"))
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    used = max(total - available, 0)
    swap_total = info.get("SwapTotal", 0)
    swap_used = max(swap_total - info.get("SwapFree", 0), 0)
    return {
        "total": total,
        "used": used,
        "percent": round(100.0 * used / total, 1) if total else 0.0,
        "swap_total": swap_total,
        "swap_used": swap_used,
    }


def disk(path: str = "/") -> dict:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"total": 0, "used": 0, "percent": 0.0, "path": path}
    percent = round(100.0 * usage.used / usage.total, 1) if usage.total else 0.0
    return {"total": usage.total, "used": usage.used, "percent": percent, "path": path}


def temperature() -> float | None:
    """Warmest sensible thermal zone, in degrees C."""
    best: float | None = None
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        raw = _read(str(zone / "temp")).strip()
        if not raw.lstrip("-").isdigit():
            continue
        celsius = int(raw) / 1000.0
        if not 0 < celsius < 150:
            continue
        best = celsius if best is None else max(best, celsius)
    return round(best, 1) if best is not None else None


def battery() -> dict | None:
    for supply in sorted(Path("/sys/class/power_supply").glob("*")):
        if _read(str(supply / "type")).strip() != "Battery":
            continue
        capacity = _read(str(supply / "capacity")).strip()
        if not capacity.isdigit():
            continue
        return {
            "percent": int(capacity),
            "status": _read(str(supply / "status")).strip() or "Unknown",
        }
    return None


def parse_proc_net_dev(text: str) -> dict[str, tuple[int, int]]:
    """Interface -> (rx_bytes, tx_bytes), skipping loopback."""
    out: dict[str, tuple[int, int]] = {}
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        fields = rest.split()
        if name == "lo" or len(fields) < 9:
            continue
        out[name] = (int(fields[0]), int(fields[8]))
    return out


def network() -> dict:
    totals = parse_proc_net_dev(_read("/proc/net/dev"))
    rx = sum(v[0] for v in totals.values())
    tx = sum(v[1] for v in totals.values())
    return {"interfaces": sorted(totals), "rx_bytes": rx, "tx_bytes": tx}


def snapshot() -> dict:
    return {
        "hostname": socket.gethostname(),
        "kernel": os.uname().release,
        "time": time.time(),
        "uptime": uptime_seconds(),
        "uptime_human": format_duration(uptime_seconds()),
        "load": loadavg(),
        "cpu_percent": cpu_percent(),
        "cpu_count": os.cpu_count() or 1,
        "memory": memory(),
        "disk": disk(),
        "temperature": temperature(),
        "battery": battery(),
        "network": network(),
    }
