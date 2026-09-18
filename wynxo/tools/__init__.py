"""Tool registry."""

from __future__ import annotations

from pathlib import Path

from ..memory import Memory
from ..scope import Boundary
from .base import Tool
from .files import EditFile, ListDir, MultiEdit, ReadFile, WriteFile
from .dev import Git, RunTests
from ..secrets import Shield
from .memory_tool import Remember
from .search import Glob, Grep
from .shell import BackgroundPoll, Shell
from .todo import TodoWrite
from .apps import LaunchApplication
from .computer import ComputerControl, ComputerInfo
from .appcatalog import ApplicationCatalog
from .navigation_tool import NavigateSymbols
from .references_tool import FindReferences
from .web import WebSearch

__all__ = ["Tool", "ToolResult", "Registry", "build_registry"]


class Registry:
    """The tools the agent may call, and the ones it may not.

    Held apart rather than dropped: the agent is offered only what can
    work, so no request pays for a schema the model would be refused for
    using -- but /tools still lists what is missing and why, because "the
    GitHub tools are not there" is a confusing thing to discover from a
    model saying it cannot do something.
    """

    def __init__(self, tools: list[Tool]):
        self._tools = {}
        self.withheld: dict[str, str] = {}
        for tool in tools:
            if (reason := tool.unavailable()):
                self.withheld[tool.name] = reason
            else:
                self._tools[tool.name] = tool

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def ollama_schemas(self) -> list[dict]:
        return [t.ollama_schema() for t in self._tools.values()]

    def suggest(self, name: str) -> str | None:
        """A model that invents a tool name usually invents a near miss."""
        import difflib

        close = difflib.get_close_matches(name, self.names(), n=1, cutoff=0.6)
        return close[0] if close else None

    def describe(self) -> str:
        return "\n".join(f"  {t.signature()}\n      {t.description}" for t in self._tools.values())


def build_registry(
    workspace: Path,
    allow_shell: bool = True,
    boundary: Boundary | None = None,
    memory: Memory | None = None,
    shield: Shield | None = None,
    app_catalog: ApplicationCatalog | None = None,
    shell_max_output: int | None = None,
    include_github: bool = True,
) -> Registry:
    tools: list[Tool] = [
        ReadFile(workspace, boundary, shield),
        WriteFile(workspace, boundary, shield),
        EditFile(workspace, boundary, shield),
        MultiEdit(workspace, boundary, shield),
        ListDir(workspace, boundary, shield),
        Glob(workspace, boundary, shield),
        Grep(workspace, boundary, shield),
        TodoWrite(workspace, boundary, shield),
        LaunchApplication(workspace, boundary, shield, catalog=app_catalog),
        ComputerInfo(workspace, boundary, shield),
        ComputerControl(workspace, boundary, shield),
        NavigateSymbols(workspace, boundary, shield),
        FindReferences(workspace, boundary, shield),
        # GitHub is a workspace integration, not a default startup dependency.
        Remember(workspace, boundary, memory, shield),
        Git(workspace, boundary, shield),
        RunTests(workspace, boundary, shield),
        WebSearch(workspace, boundary, shield),
    ]
    if allow_shell:
        tools.append(BackgroundPoll(workspace, boundary, shield))
        kwargs = {}
        if shell_max_output:
            kwargs["max_output"] = shell_max_output
        tools.append(Shell(workspace, boundary, shield, **kwargs))
    if include_github:
        from .github_tool import GitHubRead, GitHubWrite
        tools.extend((GitHubRead(workspace, boundary, shield),
                      GitHubWrite(workspace, boundary, shield)))
    return Registry(tools)
