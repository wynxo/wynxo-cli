"""Small local model-observation store.

Nothing here chooses a model. It only records what this machine actually
observed so /model can show useful local evidence instead of pretending every
installed model performs the same.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..config import atomic_write, data_dir

MAX_SAMPLES = 12


def _path() -> Path:
    return data_dir() / "model-health.json"


def _load() -> dict:
    path = _path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save(value: dict) -> None:
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass


def record(model: str, *, tps: float = 0.0, ttft_ms: float = 0.0,
           tool_failures: int = 0) -> None:
    if not model:
        return
    data = _load()
    rows = data.get(model)
    if not isinstance(rows, list):
        rows = []
    rows.append({
        "at": int(time.time()),
        "tps": max(0.0, float(tps or 0.0)),
        "ttft_ms": max(0.0, float(ttft_ms or 0.0)),
        "tool_failures": max(0, int(tool_failures or 0)),
    })
    data[model] = rows[-MAX_SAMPLES:]
    _save(data)


def summary(model: str) -> dict:
    rows = _load().get(model, [])
    if not isinstance(rows, list) or not rows:
        return {}
    good = [row for row in rows if isinstance(row, dict)]
    if not good:
        return {}
    speeds = [float(row.get("tps", 0) or 0) for row in good if row.get("tps")]
    ttfts = [float(row.get("ttft_ms", 0) or 0) for row in good if row.get("ttft_ms")]
    failures = sum(int(row.get("tool_failures", 0) or 0) for row in good)
    return {
        "samples": len(good),
        "tps": (sum(speeds) / len(speeds)) if speeds else 0.0,
        "ttft_ms": (sum(ttfts) / len(ttfts)) if ttfts else 0.0,
        "tool_failures": failures,
    }


def observation_text(model: str) -> str:
    info = summary(model)
    if not info:
        return ""
    parts = []
    if info["tps"]:
        parts.append(f'{info["tps"]:.1f} tok/s')
    if info["ttft_ms"]:
        parts.append(f'{info["ttft_ms"]:.0f} ms TTFT')
    if info["tool_failures"]:
        parts.append(f'{info["tool_failures"]} tool failure(s)')
    return " · ".join(parts)
