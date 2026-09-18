"""Deciding whether a tool call is allowed to happen.

The rule is simple: reads are free, writes ask. What makes it usable rather
than infuriating is remembering the answer -- per tool, or per exact command,
for the rest of the session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .scope import Mode


class Decision(Enum):
    ALLOW = "allow"
    ALLOW_ALWAYS = "allow_always"
    DENY = "deny"
    ABORT = "abort"


SAFE_COMMANDS = {
    "ls", "dir", "pwd", "cat", "head", "tail", "wc", "file", "stat", "which",
    "where", "echo", "date", "whoami", "env", "printenv", "tree", "du", "df",
    "grep", "rg", "find", "fd", "diff", "sort", "uniq", "cut", "awk", "sed",
    "python --version", "node --version", "go version", "cargo --version",
}
SAFE_SUBCOMMANDS = {
    "git": {"status", "log", "diff", "show", "branch", "remote", "config",
            "rev-parse", "ls-files", "blame", "describe", "stash list"},
    "npm": {"ls", "list", "view", "outdated"},
    "pip": {"list", "show", "freeze"},
    "cargo": {"tree", "metadata"},
    "docker": {"ps", "images", "version"},
    "kubectl": {"get", "describe", "logs"},
}

ALWAYS_CONFIRM = re.compile(
    r"\b(git\s+push|git\s+reset\s+--hard|git\s+clean|git\s+rebase|"
    r"npm\s+publish|cargo\s+publish|twine\s+upload|"
    r"curl|wget|ssh|scp|rsync|nc|"
    r"sudo|doas|chmod\s+777|chown|"
    r"docker\s+(run|rm|rmi)|kubectl\s+(apply|delete)|terraform\s+apply)\b",
    re.IGNORECASE,
)


@dataclass
class PermissionStore:
    """Remembers what the user has already said yes to, for this session."""

    always_allowed_tools: set[str] = field(default_factory=set)
    always_allowed_commands: set[str] = field(default_factory=set)
    denied_this_session: list[str] = field(default_factory=list)
    mode: Mode = Mode.MANUAL

    @property
    def yolo(self) -> bool:
        return self.mode is Mode.YOLO

    @yolo.setter
    def yolo(self, value: bool) -> None:
        self.mode = Mode.YOLO if value else Mode.MANUAL

    def preapprove(self, names: list[str]) -> None:
        self.always_allowed_tools.update(names)

    def blocked(self, tool_name: str, mutating: bool, internal: bool = False) -> str | None:
        if self.mode is Mode.PLAN and mutating and not internal:
            return (
                f"{tool_name} would change something, and wynxo is in plan mode "
                "(read-only). Investigate and describe what you would do instead. "
                "The user can switch with /mode auto or /mode manual."
            )
        return None

    @staticmethod
    def _is_remote_mutation(tool_name: str, args: dict) -> bool:
        """Whether a mutating tool changes a remote service rather than the local workspace."""
        return tool_name in {"github_write"}

    def needs_prompt(self, tool_name: str, mutating: bool, args: dict,
                     internal: bool = False) -> bool:
        if self.mode is Mode.YOLO:
            return False
        if not mutating or internal:
            return False

        launching_a_command = (tool_name == "launch_application"
                               and str(args.get("command", "")).strip())
        if launching_a_command:
            tool_name, args = "shell", {"command": args["command"]}

        # Remote writes and desktop input reach beyond the local project.
        # AUTO/REVIEW may edit files freely, but they must not silently click,
        # type into another application, or mutate a remote repository.
        if self._is_remote_mutation(tool_name, args):
            return True
        if tool_name == "computer_control":
            return tool_name not in self.always_allowed_tools

        if self.mode in (Mode.AUTO, Mode.REVIEW):
            if tool_name != "shell":
                return False

        if tool_name == "shell":
            command = str(args.get("command", "")).strip()
            if ALWAYS_CONFIRM.search(command):
                return True
            if command in self.always_allowed_commands:
                return False
            if is_read_only_command(command):
                return False
            if "shell" in self.always_allowed_tools:
                return False
            return True

        return tool_name not in self.always_allowed_tools

    def remember(self, tool_name: str, args: dict) -> None:
        if self._is_remote_mutation(tool_name, args):
            return
        if tool_name == "shell":
            command = str(args.get("command", "")).strip()
            if command and not ALWAYS_CONFIRM.search(command):
                self.always_allowed_commands.add(command)
        else:
            self.always_allowed_tools.add(tool_name)

    def record_denial(self, tool_name: str, reason: str) -> None:
        self.denied_this_session.append(f"{tool_name}: {reason}")


def _sed_writes_in_place(args: list[str]) -> bool:
    return any(a.startswith("-") and not a.startswith("--") and "i" in a[1:]
              for a in args)


def _awk_writes_in_place(args: list[str]) -> bool:
    return "inplace" in args


def _find_mutates(args: list[str]) -> bool:
    return bool({"-delete", "-exec", "-execdir", "-ok", "-okdir",
                "-fprint", "-fprintf"} & set(args))


def _writes_to_o_flag(args: list[str]) -> bool:
    return any(a in ("-o", "--output") or a.startswith("--output=") for a in args)


_HIDDEN_WRITE_FLAGS = {
    "sed": _sed_writes_in_place,
    "awk": _awk_writes_in_place,
    "gawk": _awk_writes_in_place,
    "find": _find_mutates,
    "sort": _writes_to_o_flag,
    "tree": _writes_to_o_flag,
}


def _git_config_only_reads(args: list[str]) -> bool:
    if not args:
        return True
    reading = {"--get", "--get-all", "--get-regexp", "--list", "-l"}
    if any(a in reading or a.startswith("--get") for a in args):
        return True
    named = [a for a in args if not a.startswith("-")]
    return len(named) == 1 and len(args) == 1


def is_read_only_command(command: str) -> bool:
    """Whether a command only observes. Conservative: unsure means no."""
    text = command.strip()
    if not text:
        return False
    if any(token in text for token in ("&&", "||", ";", ">", "<", "`", "$(", "&", "\n", "\r")):
        return False

    stages = [part.strip() for part in text.split("|")]
    if len(stages) > 1:
        return bool(stages) and all(is_read_only_command(stage) for stage in stages)

    parts = text.split()
    head = parts[0].lower()
    if head in _HIDDEN_WRITE_FLAGS and _HIDDEN_WRITE_FLAGS[head](parts[1:]):
        return False
    if head in SAFE_COMMANDS:
        return True
    if head in SAFE_SUBCOMMANDS and len(parts) > 1:
        if head == "git" and parts[1].lower() == "config":
            return _git_config_only_reads(parts[2:])
        return parts[1].lower() in SAFE_SUBCOMMANDS[head]
    if len(parts) == 2 and parts[1] in ("--version", "-v", "--help", "-h"):
        return True
    return False


def summarise_call(tool_name: str, args: dict, workspace=None) -> str:
    if tool_name == "shell":
        return str(args.get("command", ""))
    if tool_name == "computer_control":
        action = str(args.get("action", "computer"))
        if action in {"move", "click"} and args.get("x") is not None:
            return f"{action} {args.get('x')},{args.get('y')}"
        if action == "key":
            return f"key {args.get('key', '')}".strip()
        if action == "type":
            return f"type {len(str(args.get('text', '')))} character(s)"
        return f"{action} {args.get('amount', '')}".strip()
    if pattern := args.get("pattern"):
        where = args.get("glob") or args.get("path")
        shown = shorten_path(where, workspace) if where else ""
        return f"{pattern}" + (f"  in {shown}" if shown and shown != "." else "")
    if path := args.get("path"):
        return shorten_path(path, workspace)
    bits = [f"{k}={v!r}" for k, v in list(args.items())[:3]
            if not isinstance(v, (dict, list))]
    return ", ".join(bits)


def shorten_path(raw, workspace=None) -> str:
    from pathlib import Path

    text = str(raw)
    if workspace is None:
        return text
    try:
        return str(Path(text).resolve().relative_to(Path(workspace).resolve()))
    except (ValueError, OSError):
        pass
    parts = Path(text).parts
    return str(Path(*parts[-2:])) if len(parts) > 2 else text
