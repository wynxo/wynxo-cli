from __future__ import annotations

import asyncio

from wynxo.permissions import PermissionStore
from wynxo.scope import Mode
from wynxo.tools import build_registry
from wynxo.tools.computer import ComputerControl, _linux_control_driver, _ydotool_key_sequence


def test_work_registry_exposes_computer_tools_when_backend_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "wynxo.tools.computer.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    registry = build_registry(tmp_path)
    assert "computer_info" in registry
    assert "computer_control" in registry


def test_computer_control_validates_before_touching_desktop(tmp_path):
    tool = ComputerControl(tmp_path)
    result = asyncio.run(tool.invoke({"action": "move"}))
    assert not result.ok
    assert "requires both x and y" in result.output


def test_auto_still_prompts_for_desktop_input():
    store = PermissionStore(mode=Mode.AUTO)
    args = {"action": "key", "key": "ctrl+l"}
    assert store.needs_prompt("computer_control", True, args)
    store.remember("computer_control", args)
    assert not store.needs_prompt("computer_control", True, args)


def test_review_still_prompts_for_desktop_input():
    store = PermissionStore(mode=Mode.REVIEW)
    assert store.needs_prompt(
        "computer_control", True, {"action": "click", "x": 10, "y": 10}
    )


def test_yolo_is_explicit_no_prompt_for_desktop_input():
    store = PermissionStore(mode=Mode.YOLO)
    assert not store.needs_prompt(
        "computer_control", True, {"action": "click", "x": 10, "y": 10}
    )


def test_wayland_prefers_wayland_native_backend(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(
        "wynxo.tools.computer.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"wdotool", "ydotool", "xdotool"} else None,
    )
    assert _linux_control_driver() == "wdotool"


def test_wayland_falls_back_to_ydotool(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(
        "wynxo.tools.computer.shutil.which",
        lambda name: "/usr/bin/ydotool" if name == "ydotool" else None,
    )
    assert _linux_control_driver() == "ydotool"


def test_ydotool_shortcut_uses_linux_keycodes():
    assert _ydotool_key_sequence("ctrl+l") == ["29:1", "38:1", "38:0", "29:0"]
