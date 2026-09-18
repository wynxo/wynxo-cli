"""One calm presentation layer for the installed product."""

from __future__ import annotations

import re

from rich.console import Group
from rich.table import Table
from rich.text import Text

from ..cli import TerminalCallbacks as CoreCallbacks
from ..ui import UI as CoreUI, plan_steps, sanitise
from .events import EventBuffer, StageEvent, ToolFinished, ToolStarted

_TEST_COUNTS = re.compile(r"(?:(\d+)\s+passed)?(?:.*?(\d+)\s+failed)?", re.IGNORECASE)


class ProductUI(CoreUI):
    """Flat terminal transcript. Permanent surfaces stay small and quiet."""

    def home(self, model: str, workspace: str, *, mode: str = "CODE",
             companion: str = "ready", version: str = "",
             show_companion: bool = False, show_art: bool = False,
             show_static_controls: bool = False) -> None:
        self.refresh_size()
        palette = self.palette
        self.console.print()
        head = Text()
        head.append("WYNXO", style=f"bold {palette.accent}")
        if version:
            head.append(f"  {version}", style=palette.faint)
        head.append("  local AI", style=palette.muted)
        self.console.print(head)

        context = Text()
        context.append(f"{mode.upper()}  ", style=f"bold {palette.text}")
        context.append(self.shorten_model(model, max(14, self.width // 3)), style=palette.muted)
        context.append(f"  {self.g.dot}  ", style=palette.faint)
        context.append(self.shorten_path(workspace), style=palette.muted)
        self.console.print(context)
        self.console.print()
        self.console.print(Text(
            "Chat normally. Give Wynxo a project when you want it to work.",
            style=palette.text,
        ))
        self.console.print(Text(
            "/chat conversation   /code local project   /github repository   / for commands",
            style=palette.faint,
        ))
        self.console.print()

    def user_line(self, text: str, note: str = "") -> None:
        self.console.boundary()
        line = Text()
        line.append(f"{self.g.caret} ", style=f"bold {self.palette.accent}")
        line.append(sanitise(text), style=self.palette.text)
        if note:
            line.append(f"  {sanitise(note)}", style=self.palette.faint)
        self.console.print(line)

    def tool_call(self, name: str, target: str, detail: str = "", ok: bool = True) -> None:
        from ..ui import verb

        line = Text()
        line.append(self.g.tick if ok else self.g.cross,
                    style=self.palette.good if ok else self.palette.bad)
        line.append("  ")
        line.append(verb(name), style=self.palette.accent_dim if ok else self.palette.bad)
        if target:
            line.append("  " + sanitise(target)[:180], style=self.palette.text)
        if detail:
            line.append("  " + sanitise(detail)[:220], style=self.palette.faint)
        self.console.boundary()
        self.console.print(line)

    def todos(self, rendered: str) -> None:
        steps = plan_steps(sanitise(rendered))
        if not steps:
            return
        done = sum(1 for state, _ in steps if state == "done")
        lines = Text()
        lines.append(f"plan  {done}/{len(steps)}", style=self.palette.muted)
        for state, label in steps:
            marker = self.g.step_done if state == "done" else (
                self.g.step_now if state == "now" else self.g.step_todo)
            tone = self.palette.good if state == "done" else (
                self.palette.accent if state == "now" else self.palette.faint)
            lines.append("\n")
            lines.append(f"{marker} ", style=tone)
            lines.append(label, style=self.palette.text if state == "now" else self.palette.muted)
        self.console.boundary()
        self.console.print(Group(lines))


class ProductCallbacks(CoreCallbacks):
    """Core callbacks plus typed events and per-turn verification counters."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = EventBuffer()
        self.tests_passed = 0
        self.tests_failed = 0
        self.tool_failures = 0

    def begin_product_turn(self) -> None:
        self.tests_passed = 0
        self.tests_failed = 0
        self.tool_failures = 0

    async def on_stage(self, name: str, detail: str = "") -> None:
        self.events.emit(StageEvent(name=name, detail=detail))
        await super().on_stage(name, detail)

    async def on_tool_start(self, name: str, summary: str, event=None) -> None:
        self.events.emit(ToolStarted(name=name, summary=summary))
        await super().on_tool_start(name, summary, event)

    async def on_tool_result(self, name: str, ok: bool, display: str,
                             output: str, event=None) -> None:
        self.events.emit(ToolFinished(name=name, ok=ok, display=display))
        if not ok:
            self.tool_failures += 1
        if name in {"run_tests", "tests"}:
            text = f"{display}\n{output}"
            passed = re.search(r"(\d+)\s+passed", text, re.IGNORECASE)
            failed = re.search(r"(\d+)\s+failed", text, re.IGNORECASE)
            if passed:
                self.tests_passed = max(self.tests_passed, int(passed.group(1)))
            elif ok:
                self.tests_passed = max(self.tests_passed, 1)
            if failed:
                self.tests_failed = max(self.tests_failed, int(failed.group(1)))
            elif not ok:
                self.tests_failed = max(self.tests_failed, 1)
        await super().on_tool_result(name, ok, display, output, event)
