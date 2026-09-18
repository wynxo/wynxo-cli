"""Canonical product commands for Wynxo 0.2.

The legacy CLI remains the execution engine. This module gives it one product
surface: CHAT/CODE/GITHUB describes the kind of work, while
PLAN/ASK/AUTO/REVIEW/YOLO describes autonomy.
"""

from __future__ import annotations

import difflib
import time
from pathlib import Path

from prompt_toolkit.completion import Completion

from .. import cli
from ..scope import Mode, Scope
from ..session import Session
from ..workspace import Workspace
from . import health, worktrees
from .events import TurnFinished

BaseRepl = cli.Repl
BaseCompleter = cli.CommandCompleter

PRIMARY = (
    "/chat", "/code", "/github", "/mode", "/model", "/context",
    "/resume", "/new", "/clear", "/diff", "/test", "/commit",
    "/undo", "/checkpoint", "/restore", "/worktree", "/voice",
    "/stats", "/doctor", "/help", "/quit",
)

# Kept as dispatcher compatibility, not as competing entries in discovery.
HIDDEN_COMPAT = {
    "/ctx", "/repo", "/gh", "/todo", "/sessions", "/yolo", "/mommy",
}

TERMS = {
    "/chat": ("talk", "conversation", "chatter", "companion"),
    "/code": ("code", "coding", "project", "local"),
    "/github": ("git", "repo", "repository", "remote", "pr"),
    "/mode": ("permission", "autonomy", "approval", "review"),
    "/model": ("llm", "ollama", "model"),
    "/context": ("ctx", "tokens", "window", "compact"),
    "/resume": ("session", "history", "continue", "previous"),
    "/new": ("fresh", "conversation"),
    "/clear": ("screen", "terminal"),
    "/diff": ("changes", "patch"),
    "/test": ("tests", "verify", "check"),
    "/undo": ("revert", "rollback"),
    "/checkpoint": ("snapshot", "savepoint"),
    "/restore": ("rollback", "checkpoint"),
    "/worktree": ("branch", "isolate", "sandbox"),
    "/voice": ("persona", "tone", "style"),
    "/doctor": ("health", "diagnose", "slow"),
}

MODE_CHOICES = ("plan", "ask", "auto", "review", "yolo")
MODE_DESCRIPTIONS = {
    "plan": "read-only; investigate and propose",
    "ask": "ask before writes and commands",
    "auto": "edit freely in scope; ask before commands",
    "review": "edit freely; review the whole turn as one patch",
    "yolo": "never ask for approval",
}


def autonomy_label(mode: Mode) -> str:
    return "ASK" if mode is Mode.MANUAL else mode.value.upper()


def _canonical_rows(cli_mod) -> list:
    return [row for row in cli_mod.COMMAND_LIST if row.name not in HIDDEN_COMPAT]


def install_commands(cli_mod) -> None:
    """Replace discovery metadata without deleting legacy handler methods."""
    rows = _canonical_rows(cli_mod)
    by_name = {row.name: row for row in rows}

    def replace(name: str, does: str, values=()):
        old = by_name.get(name)
        if old is None:
            return
        by_name[name] = cli_mod.Command(name, does, old.handler, tuple(values))

    replace("/mode", "autonomy: plan | ask | auto | review | yolo", MODE_CHOICES)
    replace("/context", "context: status | window [tokens] | compact",
            ("status", "window", "compact"))
    replace("/clear", "clear the terminal; keep this conversation")
    replace("/github", "GitHub: status | open | clone | local | api")
    replace("/resume", "find and continue a saved conversation")
    replace("/undo", "revert the last agent turn as one transaction")
    replace("/diff", "summarise changes; add full or staged for detail",
            ("full", "staged"))
    replace("/test", "run affected tests first, or full",
            ("affected", "full"))

    additions = (
        cli_mod.Command("/checkpoint", "name the current undo position",
                        "cmd_checkpoint"),
        cli_mod.Command("/restore", "restore a named checkpoint",
                        "cmd_restore"),
        cli_mod.Command("/worktree", "isolated Git worktrees: list | new | open | remove",
                        "cmd_worktree", ("list", "new", "open", "remove")),
        cli_mod.Command("/voice", "text persona: plain | warm | mentor | blunt | kawaii | mommy",
                        "cmd_voice", ("plain", "warm", "mentor", "blunt", "kawaii", "mommy")),
    )
    for row in additions:
        by_name[row.name] = row

    # Preserve the original order, then append genuinely new concepts.
    ordered = []
    seen = set()
    for row in rows:
        current = by_name[row.name]
        if current.name not in seen:
            ordered.append(current)
            seen.add(current.name)
    for row in additions:
        if row.name not in seen:
            ordered.append(row)
            seen.add(row.name)

    cli_mod.COMMAND_LIST = tuple(ordered)
    cli_mod.REGISTRY = {row.name: row for row in cli_mod.COMMAND_LIST}
    cli_mod.COMMANDS = {row.name: row.does for row in cli_mod.COMMAND_LIST}
    cli_mod._SUBCOMMAND_VALUES = {
        row.name: row.values for row in cli_mod.COMMAND_LIST if row.values
    }

    # Old short forms still resolve, but never point at a removed duplicate.
    aliases = dict(cli_mod.ALIASES)
    aliases["/se"] = "/resume"
    cli_mod.ALIASES = aliases


def _ranked(cli_mod, text: str, limit: int = 8) -> list[str]:
    raw = (text or "").strip().lower()
    if raw == "/":
        return [name for name in PRIMARY if name in cli_mod.COMMANDS][:limit]
    stem = raw.lstrip("/")
    scored = []
    for name, description in cli_mod.COMMANDS.items():
        nstem = name.lstrip("/")
        score = 99
        if name.startswith(raw):
            score = 0
        elif stem and nstem.startswith(stem):
            score = 1
        elif len(stem) >= 2 and any(term.startswith(stem) for term in TERMS.get(name, ())):
            score = 2
        elif len(stem) >= 3 and stem in (name + " " + description).lower():
            score = 3
        if score < 99:
            primary = PRIMARY.index(name) if name in PRIMARY else 999
            scored.append((score, primary, name))
    scored.sort()
    return [name for _, _, name in scored[:limit]]


class ProductCommandCompleter(BaseCompleter):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            for name in _ranked(cli, text):
                yield Completion(
                    name,
                    start_position=-len(text),
                    display=name,
                    display_meta=cli.COMMANDS.get(name, ""),
                )
            return
        yield from super().get_completions(document, complete_event)


class ProductRepl(BaseRepl):
    """The coherent product surface over the mature CLI execution core."""

    def __init__(self, *args, **kwargs):
        self._turn_marks: list[int] = []
        self._last_changed_paths: list[Path] = []
        self._named_checkpoints: dict[str, int] = {}
        self._branch = ""
        super().__init__(*args, **kwargs)
        self._refresh_branch()

    # -- product model -------------------------------------------------

    def _product_mode(self) -> str:
        if getattr(self.agent, "working_mode", "code") == "chat":
            return "CHAT"
        info = getattr(self.agent, "workspace_info", None)
        if info is not None and getattr(info, "provider", "local") == "github":
            return "GITHUB"
        return "CODE"

    def _autonomy(self) -> str:
        mode = getattr(getattr(self.agent, "permissions", None), "mode", Mode.MANUAL)
        return autonomy_label(mode)

    def _agent_mode(self) -> str:
        return f"{self._product_mode()} · {self._autonomy()}"

    def _refresh_branch(self) -> None:
        try:
            from .. import repo
            self._branch = repo.status(self.workspace) or ""
        except Exception:
            self._branch = ""

    def _status_line(self) -> str:
        used = self.agent.session.token_estimate()
        limit, _ = self._context_limit()
        model = self.ui.shorten_model(self.config.model, max(12, self.ui.width // 4))
        info = getattr(self.agent, "workspace_info", None)
        if info is not None and getattr(info, "provider", "") == "github":
            location = info.label
        else:
            location = self.ui.shorten_path(str(self.workspace))
        pieces = [
            self._product_mode(),
            self._autonomy(),
            model,
            f"ctx {100 * used / max(1, limit):.0f}%",
        ]
        if self._branch:
            pieces.append(self._branch)
        if location:
            pieces.append(location)
        out = f" {self.ui.g.vbar} ".join(pieces)
        if len(out) > max(30, self.ui.width - 2):
            out = out[:max(27, self.ui.width - 5)] + self.ui.g.ellipsis
        return out

    def cmd_chat(self, args: list[str]) -> bool:
        self.agent.set_working_mode("chat")
        self.config.working_mode = "chat"
        self.config.save()
        self.ui.success("CHAT · project tools off")
        return True

    def cmd_code(self, args: list[str]) -> bool:
        # /code is the local-project front door. A GitHub API workspace is a
        # different work context and must never silently survive this switch.
        self.gh_ws = None
        self.agent.set_workspace(Workspace(provider="local", root=self.workspace))
        self.agent.set_working_mode("code")
        self.config.working_mode = "code"
        self.config.save()
        if self.project_info is None:
            self._refresh_map()
        self.ui.success(
            "GITHUB · repository workspace active"
            if getattr(self.agent.workspace_info, "provider", "local") == "github"
            else "CODE · local project tools on"
        )
        return True

    async def cmd_mode(self, args: list[str]) -> bool:
        if not args:
            options = [(name, MODE_DESCRIPTIONS[name]) for name in MODE_CHOICES]
            chosen = await self._pick(
                "autonomy", options,
                "ask" if self.agent.permissions.mode is Mode.MANUAL
                else self.agent.permissions.mode.value,
            )
            if chosen is None:
                return True
            if chosen is cli.NO_PICKER:
                self.ui.table(
                    ["autonomy", "behaviour"],
                    [(name, MODE_DESCRIPTIONS[name]) for name in MODE_CHOICES],
                    title="autonomy",
                )
                return True
            args = [chosen]

        key = args[0].lower()
        if key == "ask":
            key = "manual"
        try:
            mode = Mode.parse(key)
        except KeyError as exc:
            self.ui.warn(str(exc).replace("manual", "ask"))
            return True
        self.mode = mode
        self.agent.permissions.mode = mode
        self.agent.refresh_system_prompt()
        self.ui.success(f"{autonomy_label(mode)} · {mode.describe()}")
        if mode is Mode.YOLO:
            self.ui.warn("Approval prompts are disabled for this session.")
        return True

    # -- compatibility without duplicate commands ---------------------

    def _compat(self, text: str) -> str:
        parts = text.split()
        if not parts:
            return text
        name = parts[0].lower()
        args = parts[1:]
        if name == "/ctx":
            return "/context" + (f" window {' '.join(args)}" if args else " status")
        if name == "/repo":
            return "/github clone" + (f" {' '.join(args)}" if args else "")
        if name == "/gh":
            return "/github api" + (f" {' '.join(args)}" if args else " status")
        if name == "/sessions":
            return "/resume" + (f" {' '.join(args)}" if args else "")
        if name == "/todo":
            return "/plan" + (f" {' '.join(args)}" if args else "")
        if name == "/yolo":
            target = "ask" if self.agent.permissions.mode is Mode.YOLO else "yolo"
            return f"/mode {target}"
        if name == "/mommy":
            if not args:
                target = "plain" if self.config.voice == "mommy" else "mommy"
            elif args[0].lower() in {"off", "plain", "default"}:
                target = "plain"
            else:
                target = "mommy"
            return f"/voice {target}"
        return text

    async def command(self, text: str) -> bool:
        return await super().command(self._compat(text))

    def _leave_conversation(self) -> None:
        """Reset product state that belongs to the conversation being left."""
        super()._leave_conversation()
        self._turn_marks.clear()
        self._last_changed_paths.clear()
        self._named_checkpoints.clear()

    # -- conversation and context -------------------------------------

    def cmd_clear(self, args: list[str]) -> bool:
        """Clear pixels, not memory or conversation state."""
        self.ui.clear()
        self.ui.home(
            self.config.model,
            str(self.workspace),
            mode=self._agent_mode(),
            companion=self._companion_state(),
            show_companion=self.config.pet,
        )
        return True

    async def cmd_context(self, args: list[str]) -> bool:
        action = args[0].lower() if args else "status"
        if action in {"status", "show"}:
            return super().cmd_context(args[1:])
        if action in {"window", "size", "set"}:
            return await super().cmd_ctx(args[1:])
        if action == "compact":
            return await super().cmd_compact(args[1:])
        self.ui.warn("usage: /context [status | window [tokens] | compact]")
        return True

    async def cmd_resume(self, args: list[str]) -> bool:
        if not args:
            return await super().cmd_resume(args)
        query = " ".join(args).strip()
        rows = Session.recent(exclude=self.agent.session.session_id)
        exact = next((r for r in rows if r["session_id"].startswith(query.lstrip("#"))), None)
        if exact:
            return self._load_session(exact["session_id"])
        needle = query.lower()
        matches = [
            row for row in rows
            if needle in (row.get("title") or row.get("preview") or "").lower()
            or needle in (row.get("preview") or "").lower()
            or needle in (row.get("workspace") or "").lower()
        ]
        if len(matches) == 1:
            return self._load_session(matches[0]["session_id"])
        if matches:
            self.ui.table(
                ["id", "conversation", "workspace"],
                [(
                    row["session_id"][:8],
                    row.get("title") or row.get("preview") or "(untitled)",
                    self.ui.shorten_path(row.get("workspace") or ""),
                ) for row in matches[:20]],
                title=f"resume: {query}",
            )
            self.ui.hint("/resume <id> to continue one")
            return True
        self.ui.warn(f"no saved conversation matches {query!r}")
        return True

    # -- GitHub and isolation -----------------------------------------

    async def cmd_github(self, args: list[str]) -> bool:
        if not args:
            return await super().cmd_github(["status"])
        action = args[0].lower()
        rest = args[1:]
        if action == "clone":
            return await super().cmd_repo(rest)
        if action == "api":
            return await super().cmd_gh(rest or ["status"])
        if action in {"login", "ls", "cat", "edit", "branch", "pr"}:
            return await super().cmd_gh([action, *rest])
        if action in {"local", "close", "status", "open", "repo"} or "/" in args[0]:
            result = await super().cmd_github(args)
            self._refresh_branch()
            return result
        self.ui.warn("usage: /github [status | open owner/repo | clone owner/repo | local | api ...]")
        return True

    def cmd_cd(self, args: list[str]) -> bool:
        result = super().cmd_cd(args)
        self._refresh_branch()
        return result

    def cmd_worktree(self, args: list[str]) -> bool:
        action = args[0].lower() if args else "list"
        if action == "list":
            rows = worktrees.list_worktrees(self.workspace)
            if not rows:
                self.ui.info("no Git worktrees found")
                return True
            self.ui.table(
                ["branch", "path"],
                [(row.branch or "(detached)", self.ui.shorten_path(str(row.path)))
                 for row in rows],
                title="worktrees",
            )
            return True
        if action == "new":
            if len(args) < 2:
                self.ui.warn("usage: /worktree new <name> [base]")
                return True
            ok, path, message = worktrees.create(
                self.workspace, args[1], args[2] if len(args) > 2 else "HEAD")
            (self.ui.success if ok else self.ui.error)(message)
            if ok and path:
                self.ui.hint(f"/worktree open {path}")
            return True
        if action == "open":
            if len(args) < 2:
                self.ui.warn("usage: /worktree open <path-or-name>")
                return True
            target = args[1]
            rows = worktrees.list_worktrees(self.workspace)
            hit = next((row.path for row in rows
                        if str(row.path) == target or row.path.name == target
                        or row.branch == target or row.branch == f"wynxo/{target}"), None)
            if hit is None:
                hit = Path(target).expanduser()
            previous = self.workspace
            super().cmd_cd([str(hit)])
            if self.workspace != previous:
                self._apply_scope(Scope.REPO)
                self._refresh_branch()
            return True
        if action == "remove":
            if len(args) < 2:
                self.ui.warn("usage: /worktree remove <path-or-name>")
                return True
            ok, message = worktrees.remove(self.workspace, args[1])
            (self.ui.success if ok else self.ui.error)(message)
            return True
        self.ui.warn("usage: /worktree [list | new <name> [base] | open <name> | remove <name>]")
        return True

    # -- diffs, verification and transactional undo ------------------

    def cmd_diff(self, args: list[str]) -> bool:
        from ..repo import run_git

        staged = "staged" in {arg.lower() for arg in args}
        full = "full" in {arg.lower() for arg in args}
        unknown = [arg for arg in args if arg.lower() not in {"staged", "full"}]
        if unknown:
            self.ui.warn("usage: /diff [staged] [full]")
            return True
        ok, _ = run_git(["rev-parse", "--git-dir"], cwd=self.workspace, timeout=10)
        if not ok:
            self.ui.warn("not a Git repository")
            return True
        command = ["diff", "--staged"] if staged else ["diff"]
        ok, diff = run_git(command, cwd=self.workspace, timeout=60)
        if not ok:
            self.ui.warn("could not read the diff")
            return True
        if not diff.strip():
            self.ui.info("no changes")
            return True
        files, additions, deletions = [], 0, 0
        for line in diff.splitlines():
            if line.startswith("diff --git "):
                files.append(line.rsplit(" b/", 1)[-1])
            elif line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
        self.ui.info(f"{len(files)} files · +{additions} -{deletions}")
        for name in files[:80]:
            self.ui.info(f"  M  {name}")
        if full:
            self.ui.diff(diff[:120_000])
            if len(diff) > 120_000:
                self.ui.info("diff truncated visually; git diff has the complete patch")
        else:
            self.ui.hint("/diff full to inspect the patch")
        return True

    async def cmd_test(self, args: list[str]) -> bool:
        from .. import testing

        action = args[0].lower() if args else "smart"
        if action not in {"smart", "affected", "full"}:
            self.ui.warn("usage: /test [affected | full]")
            return True
        if action != "full" and self._last_changed_paths:
            focused = testing.focused_command(self.workspace, self._last_changed_paths)
            if focused:
                shell = self.agent.tools.get("shell")
                if shell is None:
                    self.ui.warn("shell tool is disabled")
                    return True
                await self.callbacks.on_stage("testing", focused)
                result = await shell.invoke(
                    {"command": focused, "timeout": testing.DEFAULT_TIMEOUT},
                    timeout=testing.DEFAULT_TIMEOUT + 30,
                )
                if result.ok:
                    self.ui.success(f"affected tests passed: {focused}")
                else:
                    self.ui.error(f"affected tests failed: {focused}")
                    if result.output.strip():
                        self.ui.code(result.output, language="text")
                return True
            if action == "affected":
                self.ui.info("no focused test set could be identified")
                return True
        return await super().cmd_test([])

    def cmd_checkpoint(self, args: list[str]) -> bool:
        if not args:
            if not self._named_checkpoints:
                self.ui.info("no named checkpoints")
            else:
                self.ui.table(
                    ["name", "undo position"],
                    [(name, str(mark)) for name, mark in self._named_checkpoints.items()],
                    title="checkpoints",
                )
            return True
        name = "-".join(args).strip()[:48]
        self._named_checkpoints[name] = self.agent.checkpoints.mark()
        self.ui.success(f"checkpoint: {name}")
        return True

    def cmd_restore(self, args: list[str]) -> bool:
        if not args:
            self.ui.warn("usage: /restore <checkpoint>")
            return True
        name = "-".join(args).strip()
        mark = self._named_checkpoints.get(name)
        if mark is None:
            self.ui.warn(f"unknown checkpoint {name!r}")
            return True
        if mark > len(self.agent.checkpoints):
            self.ui.warn("that checkpoint is older than the current undo history")
            return True
        reverted, problems = self.agent.checkpoints.revert_since(mark)
        for problem in problems:
            self.ui.warn(problem)
        self._turn_marks = [item for item in self._turn_marks if item < mark]
        self.ui.success(f"restored {name}: {reverted} change(s) reverted")
        return True

    def cmd_undo(self, args: list[str]):
        if args or not self._turn_marks:
            return super().cmd_undo(args)
        mark = self._turn_marks.pop()
        if mark > len(self.agent.checkpoints):
            return super().cmd_undo(args)
        reverted, problems = self.agent.checkpoints.revert_since(mark)
        for problem in problems:
            self.ui.warn(problem)
        if reverted:
            self.ui.success(f"reverted the last agent turn ({reverted} change(s))")
        else:
            self.ui.info("the last agent turn has no remaining file changes")
        return True

    # -- persona, models and diagnostics ------------------------------

    def cmd_voice(self, args: list[str]) -> bool:
        from ..prompts import VOICES

        if not args:
            self.ui.table(
                ["voice", "style"],
                [(name, cli._voice_summary(name)) for name in VOICES],
                title="text persona",
            )
            self.ui.hint("/voice <name> · speech voices stay under /speak voice")
            return True
        choice = args[0].lower()
        if choice not in VOICES:
            self.ui.warn("unknown voice; choose " + " | ".join(VOICES))
            return True
        self._set_voice(choice)
        return True

    async def cmd_model(self, args: list[str]) -> bool:
        if args:
            return await super().cmd_model(args)
        from ..provider import inspect_all, same_model

        try:
            models = await self.client.list_models()
            models = await inspect_all(self.client, models)
            loaded = await self.client.running()
        except Exception as exc:
            self.ui.error(str(exc))
            return True
        rows = []
        for model in models:
            resident = next((item for item in loaded if same_model(item.name, model.name)), None)
            gpu = ""
            if resident is not None and resident.size:
                gpu = f"{resident.on_gpu * 100:.0f}% GPU"
            observed = health.observation_text(model.name)
            rows.append((
                model.name + ("  ←" if same_model(model.name, self.config.model) else ""),
                model.human_size(),
                ("tools" if model.supports_tools else "chat")
                if model.capabilities_known else "?",
                ("think" if model.supports_thinking else "")
                if model.capabilities_known else "?",
                f"{model.context_length // 1024}k" if model.context_length else "?",
                gpu,
                observed,
            ))
        self.ui.table(
            ["model", "size", "cap", "reason", "ctx", "placement", "observed here"],
            rows,
            title="local models",
        )
        self.ui.hint("/model <name> switches; Wynxo never switches models silently")
        return True


    # -- help ----------------------------------------------------------

    def cmd_help(self, args: list[str]) -> bool:
        query = args[0].lower() if args else ""
        if query in {"all", "advanced"}:
            rows = [row for row in cli.COMMAND_LIST if row.name not in PRIMARY]
            self.ui.table(
                ["command", "what it does"],
                [(row.name, row.does) for row in rows],
                title="advanced commands",
            )
            return True
        if query in {"aliases", "compat"}:
            self.ui.table(
                ["old command", "now"],
                [
                    ("/ctx", "/context window"),
                    ("/repo", "/github clone"),
                    ("/gh", "/github api"),
                    ("/sessions", "/resume"),
                    ("/todo", "/plan"),
                    ("/yolo", "/mode yolo"),
                    ("/mommy", "/voice mommy"),
                ],
                title="compatibility aliases",
            )
            return True
        if query:
            name = query if query.startswith("/") else "/" + query
            row = cli.REGISTRY.get(cli.resolve_command(name) or name)
            if row is not None:
                self.ui.table(["command", "what it does"], [(row.name, row.does)])
                if row.values:
                    self.ui.hint("Options: " + " | ".join(row.values))
                return True
            self.ui.warn(f"no command matches {query!r}")
            return True
        self.ui.table(
            ["command", "what it does"],
            [(name, cli.COMMANDS[name]) for name in PRIMARY if name in cli.COMMANDS],
            title="commands",
        )
        self.ui.hint("/help advanced · /help aliases · Tab completes commands")
        return True

    # -- turn accounting ----------------------------------------------

    @staticmethod
    def _change_stats(changes) -> tuple[int, int]:
        additions = deletions = 0
        for snapshot in changes:
            before = (snapshot.content or "").splitlines()
            try:
                after = (
                    snapshot.path.read_text(encoding="utf-8", errors="surrogateescape")
                    if snapshot.path.exists() else ""
                ).splitlines()
            except OSError:
                continue
            for line in difflib.ndiff(before, after):
                if line.startswith("+ "):
                    additions += 1
                elif line.startswith("- "):
                    deletions += 1
        return additions, deletions

    async def turn(self, text: str) -> bool:
        mark = self.agent.checkpoints.mark()
        tools_before = self.agent.session.usage.tool_calls
        started = time.monotonic()
        if hasattr(self.callbacks, "begin_product_turn"):
            self.callbacks.begin_product_turn()
        outcome = await super().turn(text)
        changes = self.agent.checkpoints.changes_since(mark)
        if changes:
            self._turn_marks.append(mark)
            self._turn_marks = self._turn_marks[-40:]
            self._last_changed_paths = [snapshot.path for snapshot in changes]
        else:
            self._last_changed_paths = []
        additions, deletions = self._change_stats(changes)
        tool_calls = max(0, self.agent.session.usage.tool_calls - tools_before)
        elapsed = max(0.0, time.monotonic() - started)
        tests_passed = int(getattr(self.callbacks, "tests_passed", 0) or 0)
        tests_failed = int(getattr(self.callbacks, "tests_failed", 0) or 0)

        if hasattr(self.callbacks, "events"):
            self.callbacks.events.emit(TurnFinished(
                files=len(changes), additions=additions, deletions=deletions,
                tool_calls=tool_calls, tests_passed=tests_passed,
                tests_failed=tests_failed, elapsed=elapsed,
            ))

        if self._product_mode() != "CHAT" and (
            changes or tool_calls or tests_passed or tests_failed
        ):
            bits = []
            if changes:
                bits.append(f"{len(changes)} file{'s' if len(changes) != 1 else ''} · +{additions} -{deletions}")
            if tests_passed or tests_failed:
                bits.append(f"tests {tests_passed} passed"
                            + (f", {tests_failed} failed" if tests_failed else ""))
            if tool_calls:
                bits.append(f"{tool_calls} tool{'s' if tool_calls != 1 else ''}")
            bits.append(f"{elapsed:.1f}s")
            self.ui.info(" · ".join(bits))

        metrics = getattr(self.client, "metrics", None)
        last = getattr(metrics, "last", None) if metrics is not None else None
        if last is not None:
            health.record(
                getattr(last, "model", "") or self.config.model,
                tps=float(getattr(last, "tokens_per_second", 0) or 0),
                ttft_ms=1000.0 * float(getattr(last, "time_to_first_token", 0) or 0),
                tool_failures=int(getattr(self.callbacks, "tool_failures", 0) or 0),
            )
        self._refresh_branch()
        return outcome
