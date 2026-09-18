from __future__ import annotations

import asyncio

from wynxo.permissions import PermissionStore
from wynxo.scope import Mode
from wynxo.tools import build_registry
from wynxo.tools.computer import ComputerControl


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
