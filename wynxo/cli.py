"""Entry point and REPL."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import os
import select
import signal
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.formatted_text.html import html_escape as escape
from prompt_toolkit.history import FileHistory
from prompt_toolkit.filters import Always, Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, Window
from prompt_toolkit.patch_stdout import patch_stdout

from . import __version__
from . import config as config_module
from . import stt_devices
from .agent import Agent, Callbacks, Interrupted
from .events import ToolEvent
from .discovery import Discovery
from .config import (Config, Endpoint, MIN_USABLE_CONTEXT, data_dir,
                     is_configured, load, normalise_url)
from . import companion
from . import livediff
from .doctor import run_doctor
from .effort import ORDER, resolve
from .permissions import Decision
from .provider import (ProviderError, check_context, gpu_share,
                       make_client, same_model)
from .queue import Pending
from .session import Session
from .keys import KeyWatcher
from .journal import Journal, recent as recent_logs
from .memory import Memory
from .workspace import Workspace
from .pacing import Typewriter
from .pet import Pet
from .select import (
    HINT, HINT_ASCII, Choice, choose, silence_cpr_warning,
    supported as arrows_supported)
from .scope import Mode, Scope, resolve as resolve_scope
from .status import Status, WARN
from .tools import Registry, build_registry
from .tools.appcatalog import ApplicationCatalog
from rich.cells import cell_len
from rich.text import Text

from .ui import (ACCENT, BAR_ACCENT, FAINT, MUTED, ActivityBar,
                 CodeStreamer, ThoughtStreamer, UI, _ansi_of, plan_steps, sanitise)

def _first_sentence(text: str) -> str:
    """The opening sentence of a tool's description.

    A tool description is written for the model and runs to a paragraph --
    when to use it, when not to, what the arguments mean. /tools is a
    person asking what exists, and the first sentence is the answer to
    that. It used to be cut at seventy characters instead, which landed
    mid-word about half the time.
    """
    head = " ".join((text or "").split())
    stop = head.find(". ")
    return head[:stop + 1] if stop > 0 else head


def _is_a_sentence(text: str, tool: str = "") -> bool:
    """Whether a tool's "target" is really prose.

    A target is a thing: a path, a pattern, a count. Three or more words
    with no path separator in them is a tool describing itself, which
    belongs under the head rather than on it.

    Never for shell, whose target is a command by construction. ``ls wynxo
    | head -4`` is five words with no slash in it, and it is not a tool
    describing itself -- it is the single most important thing on the
    block. Judged prose, it was moved off the head line and printed in the
    row the command's own output should have been in, so the block said
    what was run twice and what came of it not at all.
    """
    if tool == "shell":
        return False
    words = text.split()
    return len(words) >= 3 and "/" not in text and "\\" not in text


def _trim_echo(detail: str, tool: str, target: str) -> str:
    """``detail`` with the words the head line already said taken out.

    The block's head is "read_file  calc.py" and the tool's own summary of
    what it did is "read calc.py (5 lines)", so the pair spent two lines to
    say "read" twice, "calc.py" twice, and "5 lines" once -- and the one new
    fact was the part in brackets. Only the leading verb is considered, and
    only when the tool's name is built from it: "read" leads "read_file", so
    it goes; "updated" does not lead "write_file", so it stays, because
    "updated" is news about what happened to the file.

    Never empties the line. If everything in it was an echo there is nothing
    to report and the caller drops the row rather than printing a bracket.
    """
    out = detail.strip()
    head = out.split(" ", 1)[0].strip(":").lower()
    stem = (tool or "").split("_", 1)[0].lower()
    if head and stem and (head == stem or head.startswith(stem)
                          or stem.startswith(head)):
        out = out[len(out.split(" ", 1)[0]):].lstrip()
    if target and target in out:
        out = out.replace(target, "", 1)
    out = " ".join(out.split()).strip(" ·-,")
    if out.startswith("(") and out.endswith(")"):
        out = out[1:-1].strip()
    return out


# What the activity bar says while each tool runs.
_ACTIVITY = {
    "read_file": "reading", "write_file": "writing file", "edit_file": "editing",
    "list_dir": "listing", "glob": "finding", "grep": "searching",
    "shell": "running", "todo_write": "planning", "launch_application": "launching",
    "computer_info": "inspecting desktop",
    "computer_control": "controlling desktop",
    "run_tests": "testing",
}
_LANGUAGE = {"read_file": "python", "shell": "text"}
"""How to highlight a tool's output, where it is known.

shell was "console", which is pygments' *session* lexer: it expects a
transcript with prompts and commands in it, and what a shell tool returns
is the output on its own. Beyond that, a command prints whatever it prints
-- a file listing, JSON, a stack trace, a progress bar -- and "text" is the
honest answer to what language that is."""

# Keys that work *while the agent is running*, not just at the prompt.
LIVE_KEYS = {"ctrl+o": "thinking", "ctrl+t": "detail",
             "ctrl+d": "diff", "ctrl+c": "stop"}
from .platforms import (
    is_dumb_terminal, ollama_server_help as server_help,
    suspicious_workspace)
from .wizard import probe, run_wizard


def live_note(note):
    """(text to show, note to keep) for a transient prompt note.

    A function rather than a method because both strips that can show one --
    the classic prompt's bottom border and the chat layout's footer -- need
    identical expiry, and the second one grew its own copy that never ran.
    """
    if note is None:
        return "", None
    message, until = note
    if time.monotonic() < until:
        return message, note
    return "", None


# Short forms for the prefixes that are genuinely ambiguous. An exact command
# is matched before any of these, so /mode still means /mode.
ALIASES = {
    "/exit": "/quit", "/q": "/quit", "/?": "/help", "/h": "/help",
    "/m": "/model", "/mo": "/model", "/mod": "/model",
    "/e": "/effort", "/eff": "/effort",
    "/t": "/theme", "/th": "/theme",
    "/mem": "/memory", "/sc": "/scope", "/st": "/stats", "/se": "/sessions",
    "/c": "/clear", "/co": "/compact",
    # The dispatcher has always answered /status, but the resolver never let
    # it through: /status is not a command name, not a plural of one, and no
    # command begins with it, so it came back "unknown command" while the
    # branch handling it sat there unreachable.
    "/status": "/session",
}


class _NoPicker:
    """Sentinel: this terminal cannot draw an arrow picker."""

    def __repr__(self) -> str:
        return "NO_PICKER"


NO_PICKER = _NoPicker()


def _first(text: str) -> str:
    return text.strip().splitlines()[0].strip() if text.strip() else ""


def _plural(count: int, noun: str, plural: str = "") -> str:
    """"1 tool call", "2 tool calls". Said properly, or not said at all."""
    return f"{count:,} {noun if count == 1 else (plural or noun + 's')}"


def _ago(stamp: float) -> str:
    """How long ago, in the coarsest unit that still says something.

    A conversation is picked out of a list by when it happened, not by its
    id, so this is the column the eye actually uses.
    """
    import time as _time

    if not stamp:
        return "?"
    seconds = max(0.0, _time.time() - stamp)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def _clean_commit_message(text: str) -> str:
    """Take the message out of whatever the model wrapped it in.

    Small models fence things, label them, and add "Here is the commit
    message:" no matter how firmly the prompt says not to.
    """
    import re as _re

    out = _re.sub(r"<(think|thinking|reasoning)>.*?</\1>", "", text,
                  flags=_re.DOTALL | _re.IGNORECASE).strip()
    fenced = _re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", out, _re.DOTALL)
    if fenced:
        out = fenced.group(1)
    out = _re.sub(r"^\s*(here'?s?\s+(is\s+)?)?(the\s+)?commit\s+message\s*:?\s*\n+",
                  "", out, flags=_re.IGNORECASE)
    lines = [ln.rstrip() for ln in out.strip().splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).strip()


def resolve_command(name: str) -> str | None:
    """Expand an abbreviation to a command, when it is unambiguous.

    Typing /mo for /model is the sort of thing people do without thinking, and
    refusing it is friction for no safety benefit -- but /m matching both
    /model and /memory must not silently pick one, so an ambiguous prefix is
    resolved only by an explicit alias.
    """
    if name in ALIASES:
        return ALIASES[name]
    if name in COMMANDS:
        return name
    # A trailing "s" is the other thing people type without thinking: the
    # command lists things, so /themes and /models are the natural plurals.
    # Prefix matching alone cannot catch them -- "/theme" does not start with
    # "/themes" -- so they used to come back as unknown commands.
    if name.endswith("s") and name[:-1] in COMMANDS:
        return name[:-1]
    matches = [c for c in COMMANDS if c.startswith(name)]
    return matches[0] if len(matches) == 1 else None


def suggest_commands(name: str, limit: int = 6) -> list[str]:
    """Commands that ``name`` might have meant, best guess first.

    Prefixes come first and in full: typing /mo is far more often an
    unfinished /mode or /model than a misspelling of anything. Only when
    nothing starts with it does this fall back to spelling, which is the
    case that catches /mdoe. Aliases are resolved to what they expand to,
    so /se does not offer itself.
    """
    name = name.strip().lower()
    if not name or not name.startswith("/"):
        return []

    ordered: list[str] = []

    def add(command: str) -> None:
        if command in COMMANDS and command not in ordered:
            ordered.append(command)

    if (alias := ALIASES.get(name)):
        add(alias)
    for command in COMMANDS:
        if command.startswith(name):
            add(command)
    # An alias whose own spelling starts with what was typed -- /q for
    # /quit. Prefix matching on the command names alone never finds these.
    for alias, target in sorted(ALIASES.items()):
        if alias.startswith(name):
            add(target)

    if not ordered:
        import difflib

        for match in difflib.get_close_matches(name, list(COMMANDS),
                                               n=limit, cutoff=0.6):
            add(match)
        # A word that is right but for the slash, or one buried inside a
        # command name: /sesion, /tool, /colour.
        stem = name.lstrip("/")
        if len(stem) >= 3:
            for command in COMMANDS:
                if stem in command:
                    add(command)

    return ordered[:limit]


def command_hints(buffer: str) -> list[str]:
    """Commands matching the half-typed one at the prompt, if any.

    Empty for everything else, which is almost all of the time: ordinary
    prose must not put a menu under the composer, and neither must a command
    that is already complete or has moved on to its arguments.
    """
    buffer = buffer.strip().lower()
    if not buffer.startswith("/") or " " in buffer or len(buffer) < 2:
        return []
    matches = suggest_commands(buffer, limit=5)
    # Nothing to say when the only match is what is already typed.
    return [] if matches == [buffer] else matches


def _composer_text(repl) -> str:
    """What is in the composer right now, or "" if there is no composer.

    Reaching into prompt_toolkit's application state, so it is wrapped: the
    toolbar is redrawn on every keystroke, and one that raises takes the
    prompt down with it.
    """
    try:
        return (repl.prompt_session.app.current_buffer.text or "").strip()
    except Exception:                                  # noqa: BLE001
        return ""


def _theme_summary(name: str) -> str:
    return {
        "purple": "deep violet (default)",
        "sakura": "pink and violet, turned up",
        "kawaii": "soft candy pink with sparkles",
        "midnight": "cool blue",
        "ember": "warm orange",
        "catboy": "soft pastel blue and pink",
        "plain": "your terminal's own 16 colours",
        "minimal": "plain grey, no animation (reduced motion)",
    }.get(name, "")


def _voice_summary(voice: str) -> str:
    return {
        "plain": "direct and human, no support-bot filler",
        "warm": "friendly, still honest about failures",
        "mentor": "explains the reasoning behind decisions",
        "blunt": "the fewest words that say what happened",
        "kawaii": "cheerful and affectionate, same engineering underneath",
        "mommy": "warm, playful, doting -- your goodboy, her mommy -- same engineering underneath",
    }.get(voice, "")


def _escape(text: str) -> str:
    """prompt_toolkit's HTML helper parses its input, so a model tag with an
    angle bracket in it would raise rather than render."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


@dataclass(frozen=True)
class Command:
    """One slash command: its name, what it does, and what runs it.

    All three in one row, because keeping them in three places is how the
    dispatcher and the help drifted apart. /status had a branch no input
    could reach for months -- the resolver did not know the name, so the
    code handling it was dead and nothing said so. Every abbreviation in the
    alias table was dead for the same shape of reason. A row cannot be
    half-present: `test_command_table` walks this list and checks each
    handler exists, and the dispatcher looks a command up here rather than
    comparing it against fifty string literals.
    """

    name: str
    does: str
    """What /help prints. A sentence, lower case, no full stop."""
    handler: str
    """The Repl method that runs it. Named rather than referenced because
    these are unbound at import time; the dispatcher resolves it on self,
    awaits it if it is a coroutine, and treats anything but False as
    "stay"."""
    values: tuple[str, ...] = ()
    """What may follow the space, for completion. Empty means the argument
    is free text -- a path, a model tag, a number."""


COMMAND_LIST: tuple[Command, ...] = (
    Command("/help", "show this", "cmd_help"),
    Command("/chat", "conversation only; no tools or project access", "cmd_chat"),
    Command("/code", "local coding agent; files, commands and tests", "cmd_code"),
    Command("/context", "show mode, workspace, request and latency diagnostics",
            "cmd_context"),
    Command("/effort", "how hard it works: low | medium | high | xhigh | max "
                       "| ultra", "cmd_effort",
            ("low", "medium", "high", "xhigh", "max", "ultra")),
    Command("/model", "switch model, or list what the server has", "cmd_model"),
    Command("/endpoint", "where Ollama serves: list | use | add | test",
            "cmd_endpoint", ("list", "use", "add", "test")),
    Command("/ctx", "show or set the context window (num_ctx)", "cmd_ctx"),
    Command("/tools", "list the tools the agent can call", "cmd_tools"),
    Command("/apps", "applications on this machine: <word> | refresh",
            "cmd_apps", ("refresh",)),
    Command("/pet", "the companion: on | off | still | name | voice | show",
            "cmd_pet", ("on", "off", "still", "name", "voice", "show")),
    Command("/mommy", "mommy-style talking: on | off",
            "cmd_mommy", ("on", "off")),
    Command("/animate", "the companion's states: list | on | off | <state>",
            "cmd_animate", ("list", "on", "off")),
    Command("/todo", "show the current plan", "cmd_todo"),
    Command("/queue", "what you typed while it was working: show | run | clear",
            "cmd_queue", ("show", "run", "clear")),
    Command("/dictate", "record one spoken message onto the prompt (Ctrl-R)",
            "cmd_dictate"),
    Command("/theme",
            "colour palette: purple | sakura | kawaii | midnight | ember | "
            "catboy | plain | minimal", "cmd_theme",
            ("purple", "sakura", "kawaii", "midnight", "ember", "catboy",
             "plain", "minimal")),
    Command("/secrets", "credential protection: on | off | allow",
            "cmd_secrets", ("on", "off", "allow")),
    Command("/speak", "read answers out loud: on | off | test | engine | voice",
            "cmd_speak", ("on", "off", "test", "engine", "voice")),
    Command("/talker", "small model that does the talking: off | <model>",
            "cmd_talker", ("off",)),
    Command("/log", "the session recording: tail | list | off", "cmd_log",
            ("tail", "list", "off")),
    Command("/mode", "how much it asks first: plan | manual | auto | yolo",
            "cmd_mode", ("plan", "manual", "auto", "yolo")),
    Command("/scope", "where it may work: folder | repo | machine | <path>",
            "cmd_scope", ("folder", "repo", "machine")),
    Command("/cd", "work in another directory", "cmd_cd"),
    Command("/repo", "clone a GitHub repo and work in it", "cmd_repo"),
    Command("/undo", "revert the last file change", "cmd_undo"),
    Command("/copy", "copy the conversation to the clipboard (/copy last = "
                     "last answer)", "cmd_copy", ("last",)),
    Command("/memory", "show, add to, or forget long-term memory", "cmd_memory"),
    Command("/thinking", "show or hide the model's reasoning", "cmd_thinking"),
    Command("/stream",
            "watch tool calls being written: on | off",
            "cmd_stream", ("on", "off")),
    Command("/plan", "show the current plan", "cmd_plan"),
    Command("/new", "start a new chat: fresh history, screen and log", "cmd_new"),
    Command("/resume",
            "pick up an earlier conversation, from this project or another",
            "cmd_resume"),
    Command("/github", "select a GitHub workspace, or return to local", "cmd_github"),
    Command("/gh",
            "GitHub API workspace: status | login | open | ls | cat | edit | "
            "branch | pr | close", "cmd_gh",
            ("status", "login", "open", "ls", "cat", "edit", "branch", "pr",
             "close")),
    Command("/commit", "write a commit message from the staged diff, then commit",
            "cmd_commit"),
    Command("/review", "ask the model to review the working-tree changes",
            "cmd_review"),
    Command("/diff", "show uncommitted changes (or /diff staged)", "cmd_diff",
            ("staged",)),
    Command("/test", "run the project's detected test command", "cmd_test"),
    Command("/clear", "start a fresh conversation", "cmd_clear"),
    Command("/compact", "summarise the conversation to reclaim context",
            "cmd_compact"),
    Command("/stats", "tokens, speed, context use", "cmd_stats"),
    Command("/session",
            "this conversation; `list` for the others, `<id>` to pick one up",
            "cmd_session", ("list", "resume")),
    Command("/doctor", "check the server and model for problems", "cmd_doctor"),
    Command("/yolo", "stop asking permission for this session", "cmd_yolo"),
    Command("/sessions", "the conversations you can pick up (same as /session "
                         "list)", "cmd_sessions"),
    Command("/init", "write a WYNXO.md describing this project", "cmd_init"),
    Command("/map", "the project layout the model is given, or rebuild it",
            "cmd_map"),
    Command("/pull", "download a model from Ollama, then switch to it", "cmd_pull"),
    Command("/quit", "exit", "cmd_quit"),
)

REGISTRY: dict[str, Command] = {c.name: c for c in COMMAND_LIST}

COMMANDS: dict[str, str] = {c.name: c.does for c in COMMAND_LIST}
"""Name to description. Derived, so it cannot describe a command that does
not exist or miss one that does -- which it did, both ways."""

_SUBCOMMAND_VALUES: dict[str, tuple[str, ...]] = {
    c.name: c.values for c in COMMAND_LIST if c.values}
"""Values offered after a slash command's space, e.g. ``/effort h``."""


class CommandCompleter(Completer):
    """Suggests slash commands, their subcommand values, and files after
    an "@".

    Nothing else is completed. A menu opening over ordinary prose -- which
    is what you are typing almost all of the time -- would be in the way
    rather than helpful.
    """

    def __init__(self, workspace_getter=None, model_names_getter=None):
        self._workspace = workspace_getter
        self._model_names = []
        self._model_names_getter = model_names_getter

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor

        # @path, anywhere in the line: it is a reference inside a sentence.
        word = text.rsplit(" ", 1)[-1]
        if word.startswith("@") and self._workspace is not None:
            from .mentions import candidates

            prefix = word[1:]
            for path in candidates(self._workspace(), prefix):
                yield Completion(
                    "@" + path, start_position=-len(word),
                    display=path,
                    display_meta="directory" if path.endswith("/") else "file")
            return

        if not text.startswith("/"):
            return
        if text.startswith("/model "):
            prefix = text.split(None, 1)[1]
            names = self._model_names_getter() if self._model_names_getter else self._model_names
            for model in names:
                if model.startswith(prefix):
                    yield Completion(model, start_position=-len(prefix), display=model,
                                     display_meta="installed model")
            return
        command, _, arg = text.partition(" ")
        if command in _SUBCOMMAND_VALUES and arg:
            prefix = arg
            for value in _SUBCOMMAND_VALUES[command]:
                if value.startswith(prefix):
                    yield Completion(value, start_position=-len(prefix),
                                     display=value)
            return
        if " " in text:
            return

        seen: set[str] = set()
        for name, description in COMMANDS.items():
            if name.startswith(text):
                seen.add(name)
                yield Completion(
                    name, start_position=-len(text),
                    display=name, display_meta=description)

        # Aliases that are not prefixes of what they expand to -- /q for
        # /quit, /? for /help. Prefix matching alone would never find them.
        for alias, target in sorted(ALIASES.items()):
            if alias.startswith(text) and target not in seen:
                seen.add(target)
                yield Completion(
                    target, start_position=-len(text),
                    display=f"{target}  ({alias})",
                    display_meta=COMMANDS.get(target, ""))


STATE_GAP = "   "
"""Between two states in the gallery /animate list and /pet draw."""

STATE_INDENT = 2


def gallery_columns(width: int) -> int:
    """How many companion states fit across a terminal this wide.

    From the terminal, not from a constant. Four was hard-coded, and four
    of anything is only ever right at one width: even at the old sprite
    size the gallery needed seventy columns and was drawn on any terminal
    at all, so at sixty-two every row wrapped -- the cats came apart into
    bands of pixels with their labels adrift underneath. The command exists
    to show what the character looks like, and a version of it that mangles
    the character is worse than not having it.
    """
    from . import sprite

    room = width - STATE_INDENT
    return max(1, room // (sprite.WIDTH + len(STATE_GAP)))


def _gigabytes(size: int) -> str:
    """A byte count as a person would say it."""
    return f"{size / 1e9:.1f} GB" if size else "an unknown amount"


class TerminalCallbacks(Callbacks):
    """Wires the agent's events to the terminal."""

    def __init__(self, ui: UI, prompt_session: PromptSession | None = None):
        self.ui = ui
        # None in non-interactive mode, where nothing can be asked anyway.
        self.prompt_session = prompt_session
        self._streaming = False
        self._thinker: ThoughtStreamer | None = None
        self.bar: ActivityBar | None = None
        self.journal: Journal | None = None
        self.watcher: KeyWatcher | None = None
        """Set while a turn runs. It holds the terminal in cbreak mode, so it
        must be stopped before prompt_toolkit is asked to read a line --
        otherwise both read stdin and keystrokes go to whichever wins."""
        self.thinking_asked_for: "Callable[[], bool] | None" = None
        """Whether the effort level actually asks the model to think. Set by
        Repl.

        Showing reasoning and *having* reasoning are two settings, and only
        one of them is on this keypress. Below "high" the policy sends no
        ``think`` at all, so turning the display on produced a session that
        said "thinking shown" and then never showed a word -- indefinitely,
        with nothing on screen to say why. The toggle has to be able to say
        there is nothing to reveal."""
        self.rearm_interrupt: "Callable[[], None] | None" = None
        """Put the session's SIGINT handler back. Set by Repl.

        Every prompt_toolkit read removes it: the Application installs its
        own for the length of the read and calls
        ``loop.remove_signal_handler(SIGINT)`` in its finally, and there is
        only one handler per signal -- so it removes ours rather than
        restoring it. A permission prompt in the middle of a turn therefore
        left the rest of that turn with no handler at all, and Ctrl-C did
        nothing while the agent went on working. The key watcher is no help
        there: cbreak leaves ISIG set, so the driver turns ^C into a signal
        and the byte never reaches a reader."""
        self._card: livediff.DiffCard | None = None
        """The edit being streamed right now, or the last one finished. Fed
        by on_code with the provider's real fragments. Assigned through the
        property below, which keeps the live region in step -- the card is
        drawn there, and a second place to remember is a place to forget."""
        self.detail_diffs = False
        """Ctrl-D. Whether a finished edit prints its whole diff into the
        transcript instead of one summary line."""
        self.workspace = None
        """Set by Repl, so a card can read the file it is about to replace
        and diff against it."""
        self.boundary = None
        """The same wall the tools use. The card reads independently of the
        tool, so without it a path the tool would refuse to write was still
        read and drawn."""
        self.active_tool = ""
        """The tool running right now, or "". The companion's scene is chosen
        from this and the task state together: "executing" is equally true of
        reading a file and of writing one, and those must not look alike."""
        self.task_state = None
        """The agent's task state machine, handed over by the Repl.

        Read, never written. The companion is a view of work the agent is
        already tracking -- giving the presentation layer its own idea of
        what is happening is how a character ends up animating through a
        turn that finished a minute ago."""
        self.streamer: CodeStreamer | None = None
        self.verbose_tools = False
        self._tool_started = 0.0
        """When the running tool began, for deciding whether anybody is
        waiting on its output."""
        self._pending_call: tuple[str, str] | None = None
        """The call the live region is currently narrating. Held so the block
        printed when it finishes can name what it acted on."""
        self._held_output: list[str] = []
        """Lines from a command too young to be worth showing yet."""
        """Ctrl-T: show full tool output instead of a one-line summary."""
        self.tokens = 0
        self._coder: CodeStreamer | None = None
        self._code_tool = ""
        """The tool whose argument is currently streaming, once the
        half-written call has named itself."""
        self._code_path = ""
        """What that call is about to act on, once it says."""
        """The file currently being written, while a tool call streams."""
        self._thinking_buffer: list[str] = []
        """Every thought of the whole session, shown or not.

        Capturing never stops. Hiding is a display state, not a decision to
        throw the reasoning away, so the record survives both the toggle and
        the turn boundary -- ``/thinking all`` and a mid-session Ctrl-O can
        both go back over everything the model has thought."""
        self._thinking_unsent: list[str] = []
        """Chunks that arrived while the panel was collapsed and have not
        been fed to a thinker yet. This is the un-shown suffix: opening the
        panel drains it, so a reopen never replays what already printed, and
        no full-buffer re-join is needed to find the boundary -- collapsed
        thinking stays O(1) per chunk instead of O(n) per chunk."""
        self._thinking_turns: list[str] = []
        """Completed turns' thinking, oldest first, so a replay can show them
        with a divider rather than as one undifferentiated wall."""
        self._thinking_total = 0
        """len of the concatenated buffer, kept incrementally so a live panel
        never re-joins the whole scratchpad per token."""
        self._thinking_words = 0
        """Running space count for the collapse note, so the note is O(1)
        per chunk rather than a recount of the whole buffer."""
        self._status_message = ""
        self._status_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self.typed = Typewriter(self._status_lock)
        """The answer's pacer. Started for the length of a turn where there
        is a live region, and a straight pass-through everywhere else."""

    @property
    def card(self) -> "livediff.DiffCard | None":
        return self._card

    @card.setter
    def card(self, value: "livediff.DiffCard | None") -> None:
        self._card = value
        if self.bar is not None:
            self.bar.card = value

    # -- live toggles, called from the key watcher thread -------------------

    def thinking_is_off_at_this_effort(self) -> bool:
        """Whether the model is being asked to think at all right now."""
        if self.thinking_asked_for is None:
            return False
        try:
            return not self.thinking_asked_for()
        except Exception:
            return False

    NOTHING_TO_THINK = ("no reasoning at this effort "
                        "\u00b7 /effort high to turn it on")
    """Shown when the display is switched on at a level that sends no
    ``think``. Without it the display is on, nothing ever appears, and that
    reads as a broken feature rather than as a level that does not think."""

    def toggle_thinking(self) -> None:
        self.ui.show_thinking = not self.ui.show_thinking
        nothing_to_show = (self.ui.show_thinking
                           and self.thinking_is_off_at_this_effort())
        # Announced before the panel opens, not after: the note is a line of
        # its own, and printing it once the backlog is streaming drops it
        # into the middle of a sentence.
        #
        # And mid-turn it is not printed at all. This runs from the key
        # watcher while an answer is streaming a word at a time, so anything
        # written to the terminal here lands between two words of a
        # sentence. The live region is where a transient note belongs: the
        # status bar already reflects the state, so the note rides in its
        # detail slot and disappears with it.
        if nothing_to_show:
            self._status_message = self.NOTHING_TO_THINK
        else:
            self._status_message = "Thinking..." if self.ui.show_thinking else ""
        if self.bar is not None:
            self.bar.update(detail=self._status_message)
        else:
            self.ui.info("thinking shown" if self.ui.show_thinking
                         else "thinking hidden")
            if nothing_to_show:
                self.ui.info("this effort level does not ask the model to "
                             "think -- /effort high or above to give it "
                             "reasoning to show")
        if self.ui.show_thinking:
            self._open_thinking()
        else:
            # Collapse what is already on screen's worth of buffer, so hiding
            # takes effect now rather than after the current block finishes.
            self._end_thinking()

    THINKING_TURNS_KEPT = 12
    """How many finished turns of reasoning stay replayable. Enough to look
    back over a session; bounded so a long one cannot grow without limit."""

    def _open_thinking(self, whole_session: bool = False) -> None:
        """Show the reasoning, then let the rest stream in.

        Showing means showing all of it. Opening part-way through prints
        everything that has arrived and the live stream picks up from there,
        so the panel reads the same whether it was opened at the start of the
        turn or in the middle of it.

        ``whole_session`` also replays the turns already finished, under a
        divider each. That is the answer to "show me everything it has
        thought": hiding never discarded any of it, so there is always a full
        record to go back to.

        The common case -- a mid-turn Ctrl-O -- still costs only the
        collapsed backlog, because the chunks already fed to a thinker were
        printed live and are not in ``_thinking_unsent``.
        """
        if self._streaming:
            self._end_stream()
        history = self._thinking_turns if whole_session else []
        # Only the leading space is trimmed. Stripping the tail would glue
        # the backlog to whichever word streams in next.
        backlog = "".join(self._thinking_unsent).lstrip()
        if not backlog and not history:
            return

        if history:
            self.ui.console.print()
            self.ui.console.print(ThoughtStreamer.head(
                self.ui,
                f"everything thought this session "
                f"({len(history)} earlier turn"
                f"{'s' if len(history) != 1 else ''})"))
            for index, thought in enumerate(history, start=1):
                if not thought.strip():
                    continue
                self.ui.console.print(Text(f"  turn {index}", style=FAINT))
                past = ThoughtStreamer(self.ui)
                past.feed(thought.lstrip())
                past.finish()

        if not backlog:
            return
        self.ui.console.print()
        self.ui.console.print(ThoughtStreamer.head(self.ui))
        if self._thinker is None:
            self._thinker = ThoughtStreamer(self.ui)
        self._thinker.feed(backlog)
        self._thinking_unsent.clear()

    def _thinking_note(self) -> str:
        """The collapsed form: how much thinking there is, and how to read it.

        Collapsed does not mean invisible, and it does not mean lost. The
        count keeps rising while hidden because the reasoning is still being
        captured -- hiding only stops it being drawn -- so the note doubles
        as the promise that Ctrl-O will still have something to show.
        """
        words = self._thinking_words
        if words < 3:
            return ""
        if self.ui.show_thinking:
            return ""
        return f"{words} words thought  ^O to read"

    def toggle_diff_detail(self) -> None:
        """Ctrl-D. Show the whole diff, or just the summary line.

        Works during a stream and after it: while an edit is live the card
        in the overlay grows to full height, and a finished edit prints its
        diff into the transcript on the spot rather than making the user
        cause another one to see it.
        """
        self.detail_diffs = not self.detail_diffs
        self._note("Diffs: full" if self.detail_diffs else "")
        card = self.card
        if self.detail_diffs and card is not None and not card.live:
            self._commit_diff(card)

    OUTPUT_AFTER_SECONDS = 1.5
    """How long a command must run before its output goes to the screen.

    Under this it reads as instant and its transcript is noise; over it,
    somebody is watching a progress bar that does not exist yet."""
    HELD_OUTPUT_LINES = 400
    """How much of a young command's output to keep for the flush. A bound,
    so a chatty command cannot grow this without limit before the threshold."""

    def _streaming_output(self) -> bool:
        """Whether a running command's output belongs on the screen yet."""
        if self.verbose_tools:
            return True          # asked for explicitly; show everything
        if not self._tool_started:
            return True          # no start time: never hold on a guess
        return time.monotonic() - self._tool_started >= self.OUTPUT_AFTER_SECONDS

    def toggle_verbose(self) -> None:
        self.verbose_tools = not self.verbose_tools
        self._status_message = "Tool output: full" if self.verbose_tools else ""
        if self.bar is not None:
            self.bar.update(detail=self._status_message)

    def _note(self, message: str) -> None:
        """Update the transient status without adding transcript noise."""
        self._status_message = message
        if self.bar is not None:
            self.bar.update(detail=message)

    def _write_thought(self, text: str) -> None:
        """One paced piece of the model's reasoning."""
        if self._thinker is not None:
            self._thinker.feed(text)

    def _write_code(self, text: str) -> None:
        """One paced piece of a file being written, in the transcript."""
        if self._coder is not None:
            self._coder.feed(text)

    def _write_card(self, text: str) -> None:
        """One paced piece of a file being written, into its live card."""
        if self.card is not None and self.card.live:
            self.card.feed(text)
            if self.bar is not None:
                self.bar.refresh()

    def _write_content(self, text: str) -> None:
        """One paced piece of the answer, on its way to the screen.

        The fallback is not decoration: text held by the pacer has already
        been generated, and dropping it because the block it belonged to was
        closed underneath would lose the end of an answer with nothing on
        screen to say so.
        """
        if self.streamer is not None:
            self.streamer.feed(text)
        else:
            self.ui.console.print(text, end="", soft_wrap=True)

    def _end_stream(self) -> None:
        """Close whichever transient block is open, so the next starts clean."""
        # Before anything else writes. Whatever the pacer is still holding
        # was generated *before* the line that is about to go out, and a
        # tool line landing in the middle of a held sentence is the one
        # thing the streamer's own ordering rules exist to prevent.
        self.typed.flush()
        self._end_thinking()
        if self._streaming:
            if self.streamer is not None:
                self.streamer.finish()
                self.streamer = None
            self._streaming = False

    async def on_thinking(self, text: str) -> None:
        if not text:
            return
        async with self._status_lock:
            self.tokens += 1
            # Kept whether or not it is being shown. Collapsed is a display
            # state, not a decision to throw the reasoning away -- without this
            # buffer, opening the panel part-way through could only ever show
            # what came after the keypress, and the thought you wanted to read
            # was the one that had already gone by.
            self._thinking_buffer.append(text)
            self._thinking_total += len(text)
            self._thinking_words += text.count(" ")
            if self.bar is not None:
                self.bar.update(activity="thinking", tokens=self.tokens,
                                detail=self._thinking_note(),
                                state=self._companion_state())

            if not self.ui.show_thinking:
                # Held for when the panel opens. Nothing is fed to a thinker
                # while collapsed, so the reopen can show exactly what was
                # missed without re-printing what already went out.
                self._thinking_unsent.append(text)
                return
            if self._streaming:
                self._end_stream()
            if self._thinker is None:
                self.ui.console.print()
                self.ui.console.print(ThoughtStreamer.head(self.ui))
                self._thinker = ThoughtStreamer(self.ui)
            self.typed.feed(self._write_thought, text)

    def _end_thinking(self) -> None:
        if self._thinker is not None:
            self._thinker.finish()
            self._thinker = None

    async def on_content(self, text: str) -> None:
        if not text:
            return
        async with self._status_lock:
            # Ollama streams roughly one token per chunk, so counting chunks gives
            # a live figure that tracks generation instead of a character estimate
            # that lurches. The exact count arrives with the final chunk.
            self.tokens += 1
            if not self._streaming:
                self._end_stream()
                # Indented to the same column the user's own line, the tool
                # lines and the greeting all sit at. The answer -- the thing
                # the screen exists for -- was the one kind of content
                # starting hard against column zero, so it read as overflow
                # rather than as the reply to the line above it.
                self.streamer = CodeStreamer(self.ui, indent="")
                self._streaming = True
            if self.bar is not None:
                self.bar.update(activity="writing", detail="",
                                tokens=self.tokens,
                                state=self._writing_state())
            # Through the pacer, not straight at the streamer: a chunk is
            # whatever the model happened to emit in one go, and shown whole
            # it reads as text being pasted rather than written.
            self.typed.feed(self._write_content, text)

    async def on_stage(self, name: str, detail: str = "") -> None:
        async with self._status_lock:
            await self._on_stage_locked(name, detail)

    async def _on_stage_locked(self, name: str, detail: str = "") -> None:
        if self.journal is not None:
            self.journal.stage(name, detail)
        self._end_stream()
        if self.bar is not None:
            # The live strip owns this. A stage is a state, not an event: it
            # is true for a while and then stops being true, which is what a
            # status region shows and what a transcript line cannot. Printing
            # it as well was tolerable while stages were rare, but there is
            # one before every model request now -- so a five-step coding
            # turn wrote "thinking" into the conversation five times, between
            # the tool results the user actually wanted to read.
            self.bar.update(activity=name, detail=detail)
            self._last_stage = name
            return
        # No live region -- a pipe, -p, a dumb terminal. A printed line is
        # the only way to show it there, so it is printed, but never twice
        # in a row for the same state.
        if name == getattr(self, "_last_stage", ""):
            return
        self._last_stage = name
        # The transcript's own shape -- head at column zero, detail under
        # it -- rather than an arrow at column two. Piped output was the one
        # place a stage line sat one indent in from the tool lines around
        # it, which is the margin this design took out everywhere else.
        self.ui.tool_call(name, detail, ok=True)

    async def on_tool_start(self, name: str, summary: str, event: ToolEvent | None = None) -> None:
        async with self._status_lock:
            await self._on_tool_start_locked(name, summary, event)

    async def _on_tool_start_locked(self, name: str, summary: str, event: ToolEvent | None = None) -> None:
        self.active_tool = name
        self._last_stage = ""      # a tool ends the stage it followed
        self._tool_started = time.monotonic()
        self._held_output.clear()
        self._end_code()
        if livediff.is_edit(name):
            # The card may already exist: the arguments stream arrives before
            # the call has been parsed, so on_code opens one as soon as the
            # first fragment lands and this fills in what only becomes known
            # now -- which tool it was, which file, and the file's contents
            # before the tool replaces them.
            path = ""
            if event is not None:
                path = str(getattr(event, "target", "") or "")
            if not path:
                path = summary.strip().split()[-1] if summary.strip() else ""
            before = (livediff.read_before(self.workspace, path,
                                           self.boundary)
                      if self.workspace is not None else "")
            streamed = self.card.streamed if self.card is not None \
                and self.card.live else ""
            self.card = livediff.DiffCard(tool=name, path=path, before=before,
                                          streamed=streamed)
        elif self.card is not None and self.card.live:
            # A different tool ran while a card was open: whatever was
            # streaming was not an edit after all.
            self.card = None
        if self.journal is not None:
            self.journal.tool(name, {"summary": summary})
        self._end_stream()
        if self.bar is not None:
            self.bar.update(activity=_ACTIVITY.get(name, name), detail=summary,
                            state=self._companion_state(name))
        # Nothing is committed here. What is in flight is the live region's
        # job -- it says "reading calc.py" for as long as that is true -- and
        # the conversation gets one block when the call finishes. Printing
        # both is how the tool name, the file name and the outcome all ended
        # up on screen twice per call.
        self._pending_call = (name, summary)

    async def on_tool_result(self, name: str, ok: bool, display: str, output: str, event: ToolEvent | None = None) -> None:
        async with self._status_lock:
            await self._on_tool_result_locked(name, ok, display, output, event)

    async def _on_tool_result_locked(self, name: str, ok: bool, display: str, output: str, event: ToolEvent | None = None) -> None:
        self.active_tool = ""
        # Before the card is finished: anything the pacer is still holding
        # is part of the file that was being written, and finishing the card
        # around it would settle the diff against half its own content.
        self.typed.flush()
        if self.card is not None and self.card.tool == name and self.card.live:
            settled = ""
            if ok and not self.card.streamed and self.workspace is not None:
                # Nothing streamed: an atomic provider. The file itself is
                # the honest source for what changed.
                settled = livediff.read_before(self.workspace,
                                               self.card.path, self.boundary)
            self.card.finish(ok=ok, error="" if ok else (output or "")[:200],
                             settled=settled)
            self._commit_card()
            return
        # Normally on_tool_start has already closed whatever was streaming.
        # Not always: an unknown tool, one blocked by the mode, or one the
        # user declined never starts, and the result went out while the
        # sentence before it was still held in the streamer -- so "Let me
        # check that for you." appeared *after* the error it preceded, run
        # into the next turn's first words. In plan mode, where every write
        # is refused, that was every single tool call.
        self._end_code()
        self._end_stream()
        if self.journal is not None:
            self.journal.tool_result(name, ok, output)
        # The plan is a structure, not a sentence: it gets the plan block
        # rather than a tool line. The pinned copy in the live region is a
        # layer and disappears with the turn, so suppressing this entirely
        # -- which is what happened while the bar was up -- left the record
        # with no plan in it at all. Scrolling back past a finished session
        # showed "plan the work" answered by nothing.
        #
        # Only when the steps themselves changed. A todo_write that moves
        # one step from doing to done is the live region's news, and
        # reprinting four lines for it is how the plan ended up on screen
        # once per tool call.
        if name == "todo_write" and ok:
            # One line, like every other call. The full list used to be
            # committed here whenever the steps changed, which put a
            # five-row box into the scrollback for a four-step plan -- on a
            # turn that had not done anything yet. A plan is a state, and a
            # state belongs in the live region, where it is one line that
            # changes rather than a panel printed again each time it does.
            # /todo prints the list when the list is what you want.
            steps = plan_steps(sanitise(display or output))
            done = sum(1 for state, _ in steps if state == "done")
            if steps:
                self.ui.tool_call("todo_write", f"{done}/{len(steps)}",
                                  next((text for state, text in steps
                                        if state == "now"), ""))
            return
        # Whatever was held back because the command looked too quick to be
        # worth watching. It worked out is the only reason holding it was
        # safe; a failure is exactly when those lines are the whole point,
        # and the result line carries only the first of them.
        if not ok and self._held_output:
            for held in self._held_output:
                self.ui.tool_output(held)
        self._held_output.clear()
        target, detail = self._call_summary(name, ok, display, output)
        self.ui.tool_call(name, target, detail, ok=ok)
        # Ctrl-T: the whole output under the block, whenever it says more
        # than the block's one dim line already did.
        #
        # It used to require more than one line of it, which is the same
        # bug from the other end: `which firefox` answers in one line, the
        # detail toggle showed nothing for it, and that is exactly the
        # command somebody turns the toggle on for. What matters is
        # whether the block already carries the whole answer, not how many
        # lines the answer happens to have.
        whole = output.strip()
        # Compared the way the detail was made, or the two differ by the
        # "ERROR:" the detail had stripped off and a one-line failure is
        # printed twice -- once as the block's own reason, once as a
        # highlighted quote of the same sentence.
        spoken = whole if ok else whole.split("ERROR:", 1)[-1].strip()
        if self.verbose_tools and whole and spoken != detail.strip():
            # Whole lines. Slicing at four thousand characters cut the
            # last one wherever it happened to land -- usually mid-token,
            # sometimes mid-escape -- and said nothing about the rest.
            # code() has its own ceiling now and counts what it leaves out.
            self.ui.code(output, _LANGUAGE.get(name, "text"))

    async def on_code(self, text: str) -> None:
        if not text:
            return
        async with self._status_lock:
            await self._on_code_locked(text)

    WRITES_A_FILE = frozenset({"write_file", "edit_file", "multi_edit",
                               "github_write"})
    """Tools whose streaming argument is a file's contents.

    Everything else that streams -- shell, and whatever else puts a long
    value in a named argument -- is written into the transcript instead. A
    diff card is a picture of a file changing, and a command is not that."""

    async def on_code_target(self, name: str, path: str = "") -> None:
        """Which tool the code about to stream belongs to, and on what.

        Arrives before the first fragment, because the name and the path
        both come before the long argument in the call. Without it the
        display had to assume, and it assumed write_file -- so a shell
        command being composed opened a diff card headed with a filename
        it did not have and a verb it had not earned, and a file really
        being written was headed "(unnamed)" until the call finished.
        """
        async with self._status_lock:
            self._code_tool = name
            self._code_path = path or self._code_path
            if path and self.card is not None and self.card.live \
                    and not self.card.path:
                self.card.path = path
            if self.bar is not None:
                # The strip said "thinking" while a file was visibly being
                # written under it. No tool has *started* yet -- the call
                # is still being composed -- but what it is going to be is
                # already known, and that is what the region is for.
                self.bar.update(activity=_ACTIVITY.get(name, "writing"),
                                detail=path,
                                state=self._companion_state(name))
                self.bar.refresh(force=True)

    async def _on_code_locked(self, text: str) -> None:
        """An argument arriving inside a tool call, shown as it is written.

        Its own streamer rather than the prose one: this is a value out of
        a JSON object, so none of the fence-detection guesswork applies and
        every character can go straight out.
        """
        if self._streaming:
            self._end_stream()
        writing = self._code_tool in self.WRITES_A_FILE or not self._code_tool
        if writing and (self.card is None or not self.card.live):
            # The fragments arrive before the call has been parsed, so there
            # is no path yet. Opened blank and filled in by on_tool_start;
            # opening it there instead meant the card was created after the
            # stream it existed to catch.
            self.card = livediff.DiffCard(tool=self._code_tool or "write_file",
                                          path=self._code_path)
        if writing and self.card is not None and self.card.live:
            # An edit in flight: the fragments belong to its card, which
            # draws in the live region. Streaming them into the conversation
            # as well would put the whole file on screen twice.
            self.typed.feed(self._write_card, text)
            return
        if self._coder is None:
            self.ui.console.print()
            self.ui.console.print(
                Text("  " + _ACTIVITY.get(self._code_tool, "writing"),
                     style=f"bold {MUTED}"))
            self._coder = CodeStreamer(self.ui, indent="  ",
                                       style=MUTED, code=False, literal=True)
        self.typed.feed(self._write_code, text)

    def _companion_state(self, tool: str | None = None) -> str:
        """What the companion should be doing, from what the agent is doing.

        Read from the running tool and the task state machine rather than
        set by hand at each call site, so the character cannot drift out of
        step with the work: there is one answer to "what is happening", and
        the strip, the mark and the companion all read it.
        """
        task = getattr(self.task_state, "state", None)
        running = self.active_tool if tool is None else tool
        return companion.state_for(
            running or "", getattr(task, "value", "") or "").value

    def _writing_state(self) -> str:
        """The companion while the answer itself is streaming.

        The task state machine has no entry for this. Generating an answer
        is "thinking" to it from the first token to the last, so the strip
        said "writing" beside a character sitting with its paw against its
        chin, staring into the air -- the one place the two halves of the
        live region disagreed about what was happening, in the state they
        spend most of a turn in.

        A running tool still wins: a file being written during a turn is
        more specific than the fact that words are coming back, and it is
        what the companion is for.
        """
        settled = self._companion_state()
        if settled in (companion.State.THINKING.value,
                       companion.State.IDLE.value):
            return companion.State.CODING.value
        return settled

    def _call_summary(self, name: str, ok: bool, display: str,
                      output: str) -> tuple[str, str]:
        """(what it acted on, what came of it) for one finished tool call.

        The target comes from the call the agent made rather than from the
        result text, so the block reads the same whether the tool succeeded
        or not -- an error changes the mark and the colour, not the shape.
        """
        pending = getattr(self, "_pending_call", None)
        target = pending[1] if pending and pending[0] == name else ""
        self._pending_call = None
        body = sanitise(display or output).strip()
        if not body:
            return target, ""
        first = body.splitlines()[0]
        if not ok:
            # "ERROR: nope.py does not exist." beside a cross that already
            # says so. The mark carries the fact; the line carries the
            # reason.
            first = first.split("ERROR:", 1)[-1].strip() or first
        rest = len(body.splitlines()) - 1
        if not target:
            return "", first[:110] + (f"  (+{rest} lines)" if rest else "")
        if _is_a_sentence(target, name):
            # Some tools summarise themselves in prose -- run_tests reports
            # "syntax check passed (compileall)" -- and prose on the head
            # line reads as a filename that got out of hand. The head is
            # for what was acted on; anything that is really a sentence
            # belongs underneath with the rest of the outcome.
            return "", target[:110]
        detail = _trim_echo(first, name, target)
        if not detail.strip(" $"):
            # The display said nothing the head line had not already said,
            # so fall through to what the tool actually produced. shell's
            # display is "$ <command>" and the command is the head, so
            # trimming the echo left a bare "$" -- and what the command
            # printed appeared nowhere: not in the block, and not under it
            # either, because the expansion only ever fired on output with
            # more than one line in it. `which firefox` is one line, and it
            # is the whole answer.
            if spoken := sanitise(output).strip():
                lines = spoken.splitlines()
                detail, rest = lines[0], len(lines) - 1
                if not ok:
                    # Same rule as the display path above, which this
                    # falls through from: the cross already says it
                    # failed, so "ERROR:" in front of the reason is the
                    # mark repeated in words.
                    detail = detail.split("ERROR:", 1)[-1].strip() or detail
        return target, (detail[:110]
                        + (f"  (+{rest} lines)" if rest else ""))

    def _commit_card(self) -> None:
        """Put the finished edit into the transcript.

        One line by default. The live body was drawn in the live region,
        which is a layer rather than a record -- the conversation on screen
        is append-only, so anything written into it while streaming could
        never be taken back.

        The same block every other tool gets. An edit used to print its own
        shape -- "✓ write_file · demo.py · +0 -0" -- beside blocks that read
        "◈ read_file  calc.py", so which of the two a call rendered as came
        down to whether the tool happened to write a file. And "+0 -0" is
        not a fact about the edit: it is what the counter says when the
        provider wrote the file atomically and there was nothing to count.
        Counts are shown when there are counts.
        """
        card = self.card
        if card is None:
            return
        # Out of the live region first: the block about to be printed is
        # the committed record of this edit, and a card still drawn above it
        # would be the same edit twice, one of them saying "streaming".
        if self.bar is not None:
            self.bar.card = None
        ok = card.state == "done"
        if not ok:
            detail = (card.error.splitlines()[0][:110] if card.error
                      else "failed")
        else:
            added, removed = card.counts()
            detail = f"+{added} -{removed}" if (added or removed) else ""
        self.ui.tool_call(card.tool, card.path or "", detail, ok=ok)
        if self.detail_diffs:
            self._commit_diff(card)

    def _commit_diff(self, card) -> None:
        """Put an edit's diff into the transcript.

        Through ui.diff, which is what the transcript uses for every other
        diff. The card had its own renderer for this -- a framed box of
        plain strings -- so the same edit was drawn one way when it was
        committed and another when /diff showed it: no colour on the + and
        - rows, a border nothing else in the transcript has, and a repeat
        of the +4 -1 the tool line above had just given.
        """
        self.ui.diff("\n".join(card.diff_lines()))

    def close_card(self) -> None:
        """Close an edit left streaming, at the end of a turn.

        A cancelled turn never delivers a tool result, so the card stayed
        live and the live region went on saying "streaming..." into the next
        turn -- describing an edit that had already stopped. Deliberately not
        folded into _end_stream(): that runs *during* a turn as well, and
        would close the card moments after on_code opened it.
        """
        self.typed.flush()
        if self.card is not None and self.card.live:
            self.card.finish(ok=False, error="interrupted")
            self._commit_card()
        self.active_tool = ""

    def _end_code(self) -> None:
        self.typed.flush()
        self._code_tool = self._code_path = ""
        if self._coder is not None:
            self._coder.finish()
            self._coder = None

    async def on_tool_output(self, name: str, line: str) -> None:
        if not line:
            return
        async with self._status_lock:
            await self._on_tool_output_locked(name, line)

    async def _on_tool_output_locked(self, name: str, line: str) -> None:
        """A line from a command while it is still running.

        Only shell gets this. A build or a test run is the case where
        waiting in silence is worst: the output that explains what went
        wrong arrives long before the exit code does, and if the command
        times out it is the only output there will ever be.
        """
        if name != "shell":
            return
        # Held until somebody is actually waiting. The reason for showing a
        # running command's output is that silence is worst while you wait --
        # but a command that finishes in a moment was never waited on, and
        # dumping its whole transcript into an append-only conversation left
        # eleven rows of pytest preamble behind for a result of "1 passed",
        # twice per coding turn. Below the threshold nothing lands here; the
        # pinned bar below still shows the current line, so it is visible
        # that something is happening.
        #
        # Held rather than dropped, so a slow command still shows its output
        # from the beginning rather than from whenever the clock ran out.
        if not self._streaming_output():
            self._held_output.append(line)
            del self._held_output[:-self.HELD_OUTPUT_LINES]
            if self.bar is not None:
                self.bar.update(detail=line.strip()[:60])
            return
        self._end_stream()
        if self._held_output:
            for held in self._held_output:
                self.ui.tool_output(held)
            self._held_output.clear()
        self.ui.tool_output(line)
        if self.bar is not None:
            # Doubles as the keep-alive: the pinned bar now says what the
            # command is doing right now, so a slow build looks busy rather
            # than hung.
            self.bar.update(detail=line.strip()[:60])

    async def on_todos(self, rendered: str) -> None:
        """Pin the plan in the live region rather than printing it again.

        It used to print a fresh panel on every update, so a five-step plan
        left five panels in the scrollback and the current one was whichever
        had scrolled past last. Now there is one, it is redrawn in place, and
        it ticks itself off and leaves when the work is done.
        """
        if self.bar is None:
            self.ui.todos(rendered)
            return

        self.bar.set_plan(rendered)
        if self.bar.plan_is_complete():
            await self.bar.finish_plan()

    async def on_warning(self, message: str) -> None:
        self._end_stream()
        self.ui.warn(message)

    async def ask_permission(self, name: str, summary: str, preview: str) -> Decision:
        self._end_stream()
        if self.prompt_session is None:
            return Decision.ALLOW
        try:
            return await self._ask(name, summary, preview)
        finally:
            # The classic prompt temporarily releases its reader.
            self._resume_live()

    def _suspend_live(self) -> None:
        """Release the terminal before prompt_toolkit reads a line."""
        if self.watcher is not None:
            self.watcher.stop()
        if self.bar is not None:
            self.bar.stop()

    def _resume_live(self) -> None:
        # Before anything else: the read that just finished took the SIGINT
        # handler with it, and the rest of this turn is exactly when Ctrl-C
        # is wanted.
        if self.rearm_interrupt is not None:
            self.rearm_interrupt()
        if self.bar is not None:
            self.bar.start()
        # The watcher is the only reader of stdin during a turn, and it was
        # stopped by _suspend_live so prompt_toolkit could read the answer.
        # Restart it or the rest of the turn has no type-ahead and no
        # mid-turn keys.
        if self.watcher is not None:
            self.watcher.start()

    async def _ask(self, name: str, summary: str, preview: str) -> Decision:
        self._suspend_live()
        self.ui.console.print()
        verb = {
            "write_file": "write to",
            "edit_file": "edit",
            "shell": "run",
        }.get(name, name)
        # Flush with the transcript. The question is the one moment the
        # session stops and waits for you, and it was the one block still
        # set in two columns from the edge.
        self.ui.console.print(f"[bold {ACCENT}]{verb}[/] [bold]{summary}[/]")
        if preview:
            self.ui.diff(preview) if preview.lstrip().startswith(("---", "+", "-")) else self.ui.code(preview)

        question = "[y] yes  [a] always  [n] no  [q] stop:"
        while True:
            try:
                answer = (await self.prompt_session.prompt_async(
                    HTML(f'<style fg="{ACCENT}">{question} </style>')
                )).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return Decision.ABORT
            if answer in ("", "y", "yes"):
                return Decision.ALLOW
            if answer in ("a", "always"):
                return Decision.ALLOW_ALWAYS
            if answer in ("n", "no"):
                return Decision.DENY
            if answer in ("q", "quit", "stop"):
                return Decision.ABORT
            self.ui.warn("y, a, n or q.")


class _LazyPromptSession:
    """A PromptSession stand-in built on first use.

    Everything that touches ``prompt_session`` -- the classic loop, the
    free-text input, the permission question -- sees a normal session
    through attribute delegation; the expensive constructor (which opens
    the Windows console and can raise NoConsoleScreenBufferError in Git
    Bash and remote shells) runs only when the classic path actually
    prompts. Chat mode never does, so it never pays the cost.
    """

    def __init__(self, factory: Callable[[], PromptSession]):
        self._factory = factory
        self._session: PromptSession | None = None

    def _get(self) -> PromptSession:
        if self._session is None:
            self._session = self._factory()
        return self._session

    def __getattr__(self, name: str):
        return getattr(self._get(), name)


class Repl:
    def __init__(self, config: Config, workspace: Path, ui: UI,
                 scope: Scope = Scope.FOLDER, mode: Mode = Mode.MANUAL):
        self.config = config
        self.workspace = workspace
        self.ui = ui
        self.client = make_client(config)
        self.policy = resolve(config.effort)
        self.boundary = resolve_scope(workspace, scope)
        self.mode = mode
        self.memory = Memory(workspace)
        self._prompt_note: tuple[str, float] | None = None
        """A transient line for the bottom toolbar: (message, expiry).

        Key-bindings that change state (Ctrl-E effort) must not print into
        the live prompt -- prompt_toolkit would render the input below the
        stray line, wedging it inside the box. They park the change here
        instead, and the toolbar shows it until it expires."""
        self.discovery = Discovery(workspace)
        self.journal = Journal.open(
            self.agent_session_id(), enabled=config.log)
        self.pending = Pending()
        self.pet = Pet(
            name=config.pet_name,
            enabled=config.pet,
            animate=config.animations,
            unicode=ui.g.unicode,
        )
        self.pet.style_name = ("kawaii" if config.voice == "kawaii"
                                else "mommy" if config.voice == "mommy"
                                else "default")
        self._last_elapsed = 0.0
        self._warming: "asyncio.Future | None" = None
        self._placement_checked = 0
        """Attempts made to report the model's GPU placement.

        Once a session once it is known: the fact does not change while a
        model stays loaded, and a line about it every turn is noise on top
        of a slow turn. Counted rather than a flag because the first look
        can be too early -- a 30B still loading is not yet in /api/ps --
        and a check that gives up on the one answer it exists to give is
        worse than no check."""
        """The background model load started at connect. Held so it is not
        collected mid-flight, and so a turn can say what it is waiting
        behind."""

        # The talker speaks; the coder works. Constructed here so /talker can
        # turn it on and off mid-session without rebuilding the agent.
        from .duo import Talker
        from .prompts import VOICES
        from .speech import Speaker, pick as pick_engine

        self.talker: Talker | None = None
        if config.talker:
            self.talker = Talker(self.client, config.talker,
                                 voice_block=VOICES.get(config.voice, ""))

        # Speech is opt-in and degrades to silence: a missing synthesiser is
        # not a reason to refuse to start.
        engine = pick_engine(config.speech_engine) if config.speak else None
        self.speaker = Speaker(engine, voice=config.speech_voice,
                               rate=config.speech_rate, model=config.speech_model)

        history_file = data_dir() / "history"
        history_file.parent.mkdir(parents=True, exist_ok=True)

        bindings = KeyBindings()

        @bindings.add("escape", "enter")
        def _(event):
            """Alt-Enter inserts a newline; Enter submits."""
            event.current_buffer.insert_text("\n")

        @bindings.add("c-o")
        def _(event):
            """Ctrl-O toggles the thinking display, at the prompt or mid-turn."""
            self.callbacks.toggle_thinking()

        @bindings.add("c-t")
        def _(event):
            """Ctrl-T toggles full tool output."""
            self.callbacks.toggle_verbose()

        @bindings.add("c-d", filter=Condition(
            lambda: bool(get_app().current_buffer.text)))
        def _(event):
            """Ctrl-D expands or collapses the edit detail -- but only with
            something typed.

            On an empty composer it is left alone, so prompt_toolkit's own
            end-of-file binding runs and Ctrl-D leaves the session, the way
            it does in bash, python and psql. This used to be bound
            unconditionally, which took that away: the one universal way out
            of a terminal program silently toggled a diff instead, and the
            ``except EOFError`` in the prompt loop was unreachable from the
            keyboard.

            It was unconditional for a reason that has since gone. These
            bindings were also handed to the old full-screen layout, whose
            application ran for the whole *turn* -- so there an unfiltered
            Ctrl-D really would have quit mid-edit. The prompt only runs
            when nothing else does, and mid-turn the key watcher binds
            Ctrl-D to the same toggle, so both cases are covered without
            spending the convention.
            """
            self.callbacks.toggle_diff_detail()

        @bindings.add("c-e")
        def _(event):
            """Ctrl-E steps the effort level up; Ctrl-B steps it down."""
            self._shift_effort(1)
            event.app.invalidate()

        @bindings.add("c-b")
        def _(event):
            self._shift_effort(-1)
            event.app.invalidate()

        @bindings.add("c-r")
        def _(event):
            """Ctrl-R records one spoken message for the prompt."""
            self.start_dictation()
            event.app.invalidate()

        self._prompt_bindings = bindings
        # The prompt is built lazily, on its first real use. Its constructor
        # builds a whole prompt_toolkit application, and on Windows that
        # opens the console -- which raises NoConsoleScreenBufferError when
        # TERM is set but no console buffer exists (Git Bash, mintty, a
        # remote shell). A session that only ever runs `-p` never needs one,
        # and must not crash at start-up building one it will not use.
        self.prompt_session: PromptSession | None = _LazyPromptSession(
            self._make_prompt_session)
        self.callbacks = TerminalCallbacks(ui, self.prompt_session)
        self.callbacks.workspace = workspace
        self.callbacks.rearm_interrupt = self._arm_interrupt
        self.callbacks.thinking_asked_for = lambda: bool(self.policy.thinking)
        self.callbacks.boundary = self.boundary
        self.callbacks.journal = self.journal
        self.callbacks.pet = self.pet
        # Chat is deliberately close to a direct Ollama conversation: no
        # repository walk or project map is prepared before the first turn.
        self.project_info = (
            None if getattr(config, "working_mode", "code") == "chat"
            else self.discovery.scan()
        )
        self.agent = Agent(self.client, config, self.policy, workspace, self.callbacks,
                           boundary=self.boundary, memory=self.memory)
        # The companion is a view of the agent's own task state, so the
        # presentation layer reads it rather than keeping a second copy.
        # Assigned here rather than beside the other callback wiring above,
        # which runs before the agent exists.
        self.callbacks.task_state = self.agent.task_state
        launcher = self.agent.tools.get("launch_application")
        # Application discovery is shared by /apps and the launch tool, but
        # Chat never constructs it. It is created on the first /apps request
        # or when a local Code registry needs it.
        self._app_catalog = launcher.catalog if launcher else None
        """The one application scan this session shares: /apps and the
        launch tool must agree on what this machine has installed."""
        self._model_names: list[str] = []
        self.agent.permissions.mode = mode
        self.agent.refresh_system_prompt()
        self._task: asyncio.Task | None = None
        self._dictation_task: asyncio.Task | None = None
        self._interrupt_armed = 0.0
        """When a first Ctrl-C on an idle prompt stops meaning "quit"."""
        self._dictation_draft = ""
        """A transcription waiting on the next prompt line, for review."""
        self.gh = None
        """The lazy GitHubClient; created on the first /gh use."""
        self.gh_ws = None
        """The open cloud workspace: owner, repo, branch, default, tree."""

    def _make_prompt_session(self) -> PromptSession:
        """Build the prompt on demand.

        Constructing a PromptSession eagerly builds its whole application
        (and opens the Windows console), which crashed start-up under Git
        Bash and remote shells. Built here, on the first real prompt.
        """
        history_file = data_dir() / "history"
        session = PromptSession(
            history=FileHistory(str(history_file)),
            completer=CommandCompleter(
                lambda: self.workspace,
                model_names_getter=lambda: self._model_names),
            complete_while_typing=True,
            key_bindings=self._prompt_bindings,
            multiline=False,
            # Nothing is reserved for a completion dropdown, because a
            # dropdown needs rows *below* the input and the composer is
            # seated on the bottom rows of the screen. Reserving them put an
            # empty slab between the caret and the closing border -- the box
            # was three rows of frame around six rows of nothing, and it was
            # there on every prompt whether or not a completion was open.
            #
            # Readline-style completion needs none of it: the candidates are
            # printed above the composer like any other output, and scroll
            # away with the transcript.
            reserve_space_for_menu=0,
            complete_style=CompleteStyle.READLINE_LIKE,
            # The composer takes its rows back when it closes. Without this
            # every accepted turn left its whole frame behind, so the
            # scrollback filled with stranded top borders and half-boxes --
            # the transcript wants one line saying what was asked, which
            # _echo_prompt prints instead.
            erase_when_done=True,
        )
        # Prompt_toolkit's default input window is allowed to extend to every
        # unused row in the terminal. That is harmless for a plain one-line
        # prompt, but our two-line top edge plus bottom toolbar turns the
        # unused space into a giant empty composer between them. Keep the
        # window at its content height; it can still grow when the input
        # wraps, but it never becomes a floating panel.
        for window in session.app.layout.find_all_windows():
            content = getattr(window, "content", None)
            if getattr(content, "buffer", None) is session.default_buffer:
                window.dont_extend_height = Always()
        # Prompt_toolkit renders a non-full-screen application from the
        # current cursor downward. Its default leftover space is placed after
        # the input, which makes the composer float in the middle of a tall
        # terminal. Give that space an explicit weighted slot before the
        # input so the composer and toolbar are docked to the bottom edge.
        session.app.layout.container.children.insert(
            0, Window(height=Dimension(weight=1)))
        silence_cpr_warning(session.app)
        return session

    def agent_session_id(self) -> str:
        import uuid

        return uuid.uuid4().hex[:8]

    async def _connect(self) -> bool:
        """Reach the server, report what is loaded, adapt to the model."""
        self._arm_resize()
        if self.config.clear_on_start:
            self.ui.clear()
        status = Status()
        problems: list[tuple[str, str, str]] = []

        def note(state: str, message: str, detail: str = "") -> None:
            """Collect rather than print. A wall of green OK lines on every
            start is noise; the things that are wrong are the news."""
            problems.append((state, message, detail))

        try:
            # The reachability check, not a fact worth keeping. The server's
            # version went into the header once, where the header dropped it
            # unread -- and the endpoint is nearly always the same loopback
            # address, which makes it the definition of something that earns
            # nothing by being on screen. /endpoint still reports it, on the
            # occasions somebody is actually choosing one.
            await self.client.ping()
        except ProviderError as exc:
            status.fail("ollama", self.client.base_url)
            status.close()
            self.ui.error(str(exc))
            # The configured server is dead. Before giving up, discover
            # every Ollama that *is* answering -- loopback, this machine's
            # own LAN address, and the network -- and let the user pick.
            picked = await self._offer_endpoint_discovery()
            if picked is None:
                self.ui.help_block(server_help())
                return False
            self._adopt_endpoint(picked)
            try:
                await self.client.ping()
            except ProviderError as exc2:
                self.ui.error(str(exc2))
                self.ui.help_block(server_help())
                return False

        info = None
        try:
            info = await self.client.show(self.config.model)
        except ProviderError:
            pass
        if info is not None and info.capabilities_known and not info.supports_tools:
            note(WARN, self.config.model, "no native tool calling")

        if warning := await check_context(self.client, self.config, info):
            note(WARN, f"context {self.config.num_ctx}", warning.split(".")[0])

        if getattr(self.agent, "working_mode", "code") != "chat":
            self._refresh_map(note)
        await self.agent.detect_capabilities(info)
        # EffortPolicy is immutable: a capability downgrade inside the agent
        # produces a new object rather than mutating self.policy in place, so
        # without this the status bar and /effort table would keep showing
        # "thinking on" after the agent had silently turned it off.
        self.policy = self.agent.policy
        if not self.agent.native_tools:
            note(WARN, "tools", "hermes (prompted), not native")

        if reason := suspicious_workspace(self.workspace):
            note(WARN, f"scope {self.boundary.scope.value}", reason)

        # A settings file that could not be read is the reason the endpoint
        # list, the model and the theme are suddenly back to their defaults.
        # Falling back is right; doing it in silence is not.
        for problem in config_module.LOAD_PROBLEMS:
            note(WARN, "settings", problem)

        if problems:
            print("", file=status.stream)
            for state, message, detail in problems:
                status.line(state, message, detail)
        status.close()

        # The opening screen is a launchpad: header, workspace, welcome,
        # capabilities and quick-start commands. It is drawn once and then
        # scrolled past like any other output -- it is not a layout the
        # session lives inside, which keeps streaming, tool calls, resizing
        # and the prompt exactly as they were.
        await self.ui.boot_sequence(
            self.config.model, str(self.workspace),
            enabled=self.config.animations)
        self.ui.home(self.config.model, str(self.workspace),
                     mode=self._agent_mode(),
                     companion=self._companion_state(),
                     show_companion=self.config.pet,
                     show_art=True,
                     show_static_controls=False)
        self._start_warming()
        return True

    PLACEMENT_TRIES = 3
    """Turns to keep looking before giving up on the GPU-placement check."""

    async def _report_placement(self) -> None:
        """Say, once, when the model is not entirely on the GPU.

        This is the single biggest fact about local generation speed and
        the one nothing was saying. A model that runs at forty tokens a
        second on the GPU runs at five with a few layers pushed onto the
        CPU, and the only symptom is that everything feels slow for no
        stated reason -- which is exactly the shape of "wynxo is slower
        than `ollama run`".

        The cause is usually wynxo's own doing. `ollama run` uses the
        model's default window; wynxo asks for num_ctx, which defaults to
        32,768, and the KV cache grows with it -- so the same model on the
        same machine can be entirely on the GPU under one and spilled
        under the other.

        Once per session, and only when it is true. A line every turn
        would be noise, and a line on a machine with no GPU at all would be
        wrong: nothing has gone wrong there and there is nothing to fix.
        """
        if self._placement_checked >= self.PLACEMENT_TRIES:
            return
        try:
            loaded = await self.client.running()
        except Exception:                                   # noqa: BLE001
            self._placement_checked += 1
            return          # a diagnostic must never be able to fail a turn
        mine = next((m for m in loaded
                     if same_model(m.name, self.config.model)), None)
        if mine is None:
            # Nothing definite yet: an older server with no /api/ps, or a
            # model that had not finished loading. Counted rather than
            # settled, so a slow first load does not silently cost the
            # answer for the whole session -- and bounded, so an old server
            # is not asked once a turn forever.
            self._placement_checked += 1
            return
        self._placement_checked = self.PLACEMENT_TRIES
        if not mine.split:
            return

        installed: list = []
        weights = 0
        try:
            installed = await self.client.list_models()
        except Exception:                                   # noqa: BLE001
            pass
        for entry in installed:
            if same_model(entry.name, self.config.model):
                weights = entry.size
                break

        # One block, not three. It went out as a warn() followed by two
        # info()s, and info() has no marker -- so the warning wrapped under
        # its own "!" at column two while the explanation under it started
        # hard against column zero. Three ragged paragraphs for one fact.
        # Head and detail is the shape every other block here uses.
        fits = mine.context_that_fits(weights, self.config.num_ctx)
        vram = mine.size_vram
        # Every line that offers a command leads with it. Trailed at the
        # end of a sentence, "/model qwen2.5-coder:7b" wrapped between the
        # command and its argument -- so the one thing on screen meant to
        # be typed came out as two, and could not be copied.
        note = [f"{self.config.model} is {mine.on_gpu:.0%} on the GPU -- "
                f"most of why generation feels slow"]
        if fits >= MIN_USABLE_CONTEXT:
            note.append(f"/ctx {fits} would keep it entirely on the GPU: "
                        f"the KV cache grows with the context window, and "
                        f"num_ctx is {self.config.num_ctx:,}")
        elif fits:
            note.append(f"/ctx {fits} is all that would fit on the GPU, "
                        f"which is tight for an agent -- it trades context "
                        f"for speed")
        elif (paging := self._memory_squeeze(mine, weights)):
            # The card is not the only ceiling. A model needs its weights
            # and its cache resident somewhere, and when that comes to more
            # than the card and the machine's RAM together the rest is read
            # off disk on every token -- which is slow in a way no amount
            # of CPU makes up for, and is the one condition where a smaller
            # window helps a machine with no GPU room left to gain.
            window, budget = paging
            note.append(
                f"it needs {_gigabytes(mine.size)} at num_ctx="
                f"{self.config.num_ctx:,}, and this machine has about "
                f"{_gigabytes(budget)} of card and RAM to give it -- so "
                f"part of it is being read off disk on every token")
            if window:
                note.append(f"/ctx {window} brings it inside that, which is "
                            f"the difference between memory and disk")
        else:
            # The window is not the lever here, and saying "halve num_ctx"
            # would send somebody to change a setting for two points. The
            # weights are what does not fit on the card, and layers go
            # there whole with their slice of the cache, so shrinking the
            # cache moves a fraction of a fraction.
            floor = mine.share_at(weights, self.config.num_ctx, 2048)
            note.append(
                f"{_gigabytes(weights)} of weights against about "
                f"{_gigabytes(vram)} of card, so the context window is not "
                f"the lever: even /ctx 2048 would only reach {floor:.0%}")
        if (better := self._better_model(installed, vram, mine.on_gpu)) is not None:
            note.append(f"/model {better.name} -- installed already at "
                        f"{_gigabytes(better.size)}, and about "
                        f"{gpu_share(better.size, vram):.0%} of it would "
                        f"run on the GPU")
        note.append("/doctor explains it in full")
        self.ui.console.print()
        self.ui.warn("\n".join(note))

    SYSTEM_MEMORY = 2_000_000_000
    """RAM this machine needs for everything that is not the model.

    A desktop, a browser, the editor the agent is being pointed at. Left
    out of the budget, "it fits" would be true right up to the moment the
    machine started swapping, which is the condition being diagnosed."""

    def _memory_squeeze(self, loaded, weights: int):
        """(window that would fit, budget) when the model is paging, else ().

        The card is not the only ceiling, and on a small machine it is not
        the interesting one. A 20.1 GB model on a card with 3.6 GB and a
        machine with 16 GB is over the line once the system has its share,
        and the remainder is read off disk on every token.
        """
        from .platforms import total_memory

        ram = total_memory()
        if not ram or not loaded.size:
            return ()
        budget = loaded.size_vram + max(0, ram - self.SYSTEM_MEMORY)
        if loaded.size <= budget:
            return ()
        return (loaded.context_within(weights, self.config.num_ctx, budget),
                budget)

    def _better_model(self, installed: list, vram: int, share: float):
        """The model already here that would run most of itself on the GPU.

        "A smaller model is the fix" is advice, not help: it leaves the one
        question that matters -- which one -- to somebody who has just been
        told their setup is slow.

        Not a fits-or-does-not test. A 4.4 GB model on a 3.6 GB card runs
        about four fifths on the GPU, and four fifths is transformative
        next to the fifth a 17.7 GB model manages; ruling it out for not
        fitting whole would be withholding the answer for being imperfect.
        """
        from .provider import faster_on_gpu

        for candidate in faster_on_gpu(installed, vram, share):
            if not same_model(candidate.name, self.config.model):
                return candidate
        return None

    def _start_warming(self) -> None:
        """Load the model while the user is typing, not after.

        Most of why `ollama run` feels quicker than wynxo: it loads the
        model as the terminal opens, so it is resident by the time you have
        finished typing. wynxo asked the server for nothing until you
        pressed enter, so the first question of every session paid for a
        cold load -- tens of seconds for a 30B -- behind a status line that
        said "thinking".

        Fired and not awaited: start-up must not wait on it, and it must not
        be able to fail one. The reference is kept so the task is not
        collected mid-flight and so a turn can tell whether the load it is
        waiting behind is this one.
        """
        if not self.config.warm_start:
            return
        try:
            self._warming = asyncio.ensure_future(self._warm())
        except RuntimeError:
            self._warming = None            # no loop: nothing to warm into

    async def _warm(self) -> None:
        """Load the model, and read the prompt into it if that is cheap.

        Two steps, because the second one is only a good idea on a machine
        that can do it quickly.

        Reading the prompt in advance is worth a lot: the system prompt and
        the tool schemas come to somewhere north of five thousand tokens,
        and Ollama reuses a matching prefix, so a first question that
        arrives afterwards pays only for itself.

        But Ollama runs one request at a time per model. On a machine where
        most of the model is on the CPU, reading five thousand tokens takes
        longer than anybody will wait -- and the first question queues
        behind it, so the priming meant to save that turn *becomes* that
        turn's wait. A minute and a half of "loading the model" to answer
        "hi", against five seconds for `ollama run`, which sends two
        tokens and nothing else.

        So the weights are loaded first, the placement is read back, and
        the prompt goes in only when the model is sitting on the GPU where
        reading it is quick.
        """
        await self.client.warm(self.config.model)
        try:
            loaded = await self.client.running()
        except Exception:                                   # noqa: BLE001
            return
        mine = next((m for m in loaded
                     if same_model(m.name, self.config.model)), None)
        if mine is not None and mine.split:
            return
        try:
            wire = self.agent.session.wire()
            tools = (self.agent.tools.ollama_schemas()
                     if self.agent.native_tools else None)
        except Exception:                                   # noqa: BLE001
            return
        await self.client.warm(self.config.model, messages=wire, tools=tools)

    async def _offer_endpoint_discovery(self) -> str | None:
        """The configured server answered nothing: look on this machine and
        the LAN, show every hit, and let the user pick one (or type an
        address). Returns the chosen URL, or None to give up."""
        from .discovery import discover

        with self.ui.status("looking for Ollama on this machine and the LAN..."):
            found = await discover()
        if not found:
            self.ui.warn("nothing answering on this machine or the network.")
            return None
        self.ui.console.print(f"  [{ACCENT}]Found Ollama on:[/]")
        for i, hit in enumerate(found, 1):
            self.ui.console.print(
                f"    [bold]{i}[/]  {hit.url}  "
                f"[{MUTED}]v{hit.version} {self.ui.g.dot} {hit.where}[/]")
        self.ui.console.print()
        answers = {str(i): hit.url for i, hit in enumerate(found, 1)}
        answers["m"] = "type a different address"
        picked = await self._question(
            f"where does Ollama serve? [1-{len(found)} or m]:",
            answers, default="1")
        if not picked:
            return None
        if picked == "m":
            raw = await self._type_in("address:", "")
            if not raw:
                return None
            return normalise_url(raw if "://" in raw else f"http://{raw}")
        return found[int(picked) - 1].url

    def _adopt_endpoint(self, url: str) -> None:
        """Point this session at a discovered server, and remember it."""
        url = normalise_url(url)
        for endpoint in self.config.endpoints:
            if endpoint.url == url:
                self.config.active_endpoint = endpoint.name
                break
        else:
            name = f"discovered-{len(self.config.endpoints) + 1}"
            self.config.endpoints.append(Endpoint(name=name, url=url))
            self.config.active_endpoint = name
        self.config.save()
        self.client = make_client(self.config)

    async def start(self) -> int:
        if not await self._connect():
            return 1
        return await self._loop()

    async def _loop(self) -> int:
        try:
            return await self._prompt_loop()
        finally:
            # She stops when wynxo does. A speech process is a child that
            # outlives its parent, so quitting mid-sentence used to leave the
            # voice talking to an empty terminal. In a finally at the
            # outermost level rather than at the bottom of the prompt loop,
            # so no early return can skip it.
            self.speaker.stop()
            # And so does anything started with shell(background=true). It is
            # in its own session so the whole command can be killed at once,
            # which also means the terminal never hangs it up -- a watcher or
            # a dev server would go on writing into the project long after
            # wynxo was gone.
            from .tools.shell import shutdown_background

            with contextlib.suppress(Exception):
                shutdown_background()
            await self.client.aclose()
            # The pet signs off -- a warm end, but never louder than a plain
            # "bye" when it is disabled.
            farewell = self.pet.remark("bye")
            if farewell:
                line = Text("  ")
                line.append(self.pet.name, style=f"bold {ACCENT}")
                line.append(f" — {farewell}", style=MUTED)
            else:
                line = Text("  bye", style=MUTED)
            # One row. The remarks run to forty characters and the name is
            # in front of them, so at a narrow width the tail wrapped to
            # column zero -- the last line of the session, and the only one
            # that broke the margin.
            self.ui.console.print(line, overflow="ellipsis", no_wrap=True)


    async def _prompt_loop(self) -> int:
        while True:
            # The window may have been resized while this prompt was
            # waiting, and prompt_toolkit owned SIGWINCH for all of it. A
            # draw is about to start, so measure now.
            self.ui.refresh_size()
            try:
                # The draft is dictation text waiting to be submitted. An
                # empty draft must stay an empty string -- `or None` would
                # pass None as prompt_toolkit's default, and Document(None)
                # is a TypeError on every startup.
                draft = self._dictation_draft or ""
                self._dictation_draft = ""
                text = await self.prompt_session.prompt_async(
                    self._prompt_message, bottom_toolbar=self._bottom_toolbar,
                    rprompt=self._prompt_rail, default=draft)
                # The composer wrote straight to the tty and erased itself,
                # leaving one newline behind that the console did not see.
                self.ui.console.wrote_elsewhere(1)
            except KeyboardInterrupt:
                # prompt_toolkit throws away the line and raises. One press
                # is "forget what I typed" and always was; a second within
                # the window is how every other terminal program is left,
                # and until now there was none -- only /quit or Ctrl-D.
                if self._quit_is_armed():
                    self.ui.console.print()
                    break
                self._interrupt_armed = time.monotonic() + self.QUIT_WINDOW
                self._prompt_note = ("press Ctrl-C again to quit",
                                     self._interrupt_armed)
                continue
            except EOFError:
                break

            # A line got typed, so the earlier Ctrl-C was not the start of a
            # quit. Disarming here rather than on a timer keeps "twice in a
            # row" literal.
            self._interrupt_armed = 0.0
            text = text.strip()
            if not text:
                continue
            # The composer erases itself on accept (erase_when_done), so the
            # transcript gets one compact line saying what was asked instead
            # of a stranded box. See _echo_prompt.
            self._echo_prompt(text)

            # Commands and the queue drain run with the same Ctrl-C handler
            # a turn gets. Without it, prompt_toolkit's own teardown had
            # already put SIGINT back to Python's default, so Ctrl-C during
            # a slow command (/model probing a server, /gh reaching the API)
            # raised KeyboardInterrupt straight out of the loop and ended
            # the session, conversation and all.
            self._arm_interrupt()
            if text.startswith("/"):
                if await self._guarded(self.command(text)) is False:
                    break
                continue

            # Only a turn that finished lets the queue run. An interrupt or
            # an error returns anything but True, and then whatever was
            # typed during the turn stays queued rather than starting the
            # moment you pressed stop -- the status strip says how much is
            # waiting, and Enter or /queue decides what happens to it.
            finished = await self._guarded(self.turn(text)) is True
            if not finished:
                self._report_held_queue()
                continue
            if await self._guarded(self._drain_queue()) is False:
                break

        return 0

    async def start_with(self, prompt: str) -> int:
        """Run one prompt, then drop into the REPL. `wynxo "fix the tests"`."""
        if not await self._connect():
            return 1
        await self.turn(prompt)
        return await self._loop()

    async def _guarded(self, coro):
        """Run one turn or command; survive anything it raises.

        A crash here used to end the process and take the conversation with
        it -- and the causes are all things a local model does on a bad day:
        a template that cannot parse its own tool-call output, a tool that
        raises something unforeseen, a bug of mine. None of that is worth
        losing the session over, and the traceback is written to the log
        where it can be read afterwards.

        Interrupted and CancelledError pass through: those are Ctrl-C, which
        the caller already handles, and swallowing them would make Ctrl-C
        look broken again. A bare KeyboardInterrupt is Ctrl-C too, but with
        nothing holding it -- it is reported here rather than re-raised,
        because the alternative was the process exiting.
        """
        import traceback

        try:
            return await coro
        except (Interrupted, asyncio.CancelledError):
            raise
        except KeyboardInterrupt:
            # A SIGINT that landed somewhere with no task to cancel -- most
            # often during a command. It means "stop this", never "throw the
            # session away", and as a BaseException it walked straight past
            # the handler below on its way out of the process.
            self.callbacks._end_stream()
            self.ui.console.print()
            self.ui.warn("Interrupted. The conversation is intact; "
                         "ask me something else.")
            return None
        except ProviderError as exc:
            self.callbacks._end_stream()
            self.ui.error(str(exc))
            self.journal.error(str(exc))
            return None
        except Exception as exc:                      # noqa: BLE001
            self.callbacks._end_stream()
            self.ui.error(f"{type(exc).__name__}: {exc}")
            self.ui.info("the conversation is intact -- this was not fatal")
            self.journal.error("".join(traceback.format_exception(exc)))
            if self.journal.path is not None:
                self.ui.info(f"details in {self.journal.path}")
            return None

    async def turn(self, text: str) -> bool:
        """Run one request, with a live status bar and mid-flight keybinds.

        Returns False if it was interrupted, so a queue drain can stop
        rather than launching the next thing the moment you press Ctrl-C.
        """
        async with self.callbacks._turn_lock:
            cb = self.callbacks
            # The previous turn's reasoning is retired into the history
            # rather than dropped: showing thinking is meant to show all of
            # it, and a turn boundary is a divider in that record, not the
            # end of it. Only the live buffers reset.
            if cb._thinking_buffer:
                cb._thinking_turns.append("".join(cb._thinking_buffer))
                del cb._thinking_turns[:-cb.THINKING_TURNS_KEPT]
            cb._thinking_buffer.clear()
            cb._thinking_unsent.clear()
            cb._thinking_total = 0
            cb._thinking_words = 0
            return await self._turn_locked(text)

    async def _turn_locked(self, text: str) -> bool:
        self.journal.user(text)
        text = self._expand_mentions(text)
        self.callbacks.tokens = 0
        # Cleared per turn: opening the panel should show this answer's
        # reasoning, not everything the model has thought this session.

        # One hint, not four. The whole set -- ^O thinking, ^T detail, ^D
        # diff, ^C stop -- is forty-four cells of key names competing with
        # the answer to "what is it doing" on every frame, and it is already
        # spelled out on the prompt's own border where there is room for it.
        # Mid-turn there is one binding somebody actually reaches for.
        bar = ActivityBar(self.ui, self.policy.name, "^C stop",
                          model=self.config.model, pet=self.pet)
        review_mark = self.agent.checkpoints.mark()
        bar.animate = self.config.animations
        bar.queued = self.pending.preview(ellipsis=self.ui.g.ellipsis)
        used = self.agent.session.token_estimate()
        limit, _ = self._context_limit()
        bar.context_pct = 100 * used / max(1, limit)
        if self._warming is not None and not self._warming.done():
            # The wait at the start of a session is usually not the model
            # thinking, it is the model being read off disk -- tens of
            # seconds for a 30B, and the strip said "thinking" through all
            # of it. Only until the load finishes: the first real stage the
            # agent reports replaces this.
            bar.activity = "loading the model"
        self.callbacks.bar = bar
        self.ui.bar = bar

        def typed(char: str) -> None:
            """A keystroke that no binding claimed, while a turn is running."""
            self.pending.key(char)
            bar.queued = self.pending.preview(ellipsis=self.ui.g.ellipsis)
            bar.refresh()

        watcher = KeyWatcher(
            {
                "ctrl+o": self.callbacks.toggle_thinking,
                "ctrl+t": self.callbacks.toggle_verbose,
                "ctrl+u": lambda: (self.pending.clear(),
                                   setattr(bar, "queued", ""), bar.refresh()),
                # Advertised in LIVE_KEYS and drawn into the activity bar as
                # "^D diff", but never bound: mid-turn it fell through to
                # type-ahead, which drops it for not being printable. The
                # prompt has had the same binding all along.
                "ctrl+d": self.callbacks.toggle_diff_detail,
                # The watcher holds the terminal in cbreak mode for the whole
                # turn, so it sees Ctrl-C as a keypress. Handling it here
                # works even where the tty driver does not raise SIGINT --
                # a pipe, a pty without a controlling terminal, or a
                # platform that swallows it.
                "ctrl+c": self.interrupt,
            },
            on_key=typed,
        )

        self.callbacks.watcher = watcher
        interrupted = False
        self._arm_interrupt()
        # The talker answers first: a 1B model is quick enough that the
        # acknowledgement lands before the coder has produced a token.
        if self.talker is not None:
            await self._talk(await self.talker.opening(text))
        self._task = asyncio.ensure_future(self.agent.run(text))
        bar.start()
        # Only here. Outside a turn there is no live region to pace against,
        # and in -p or a pipe the pacer is a pass-through -- output that is
        # being read by a program should arrive as fast as it is produced.
        self.callbacks.typed.start()
        # Only one thing may read stdin. prompt_toolkit is not reading during
        # a turn -- the prompt has returned -- so the watcher can hold the
        # terminal in cbreak mode and read it directly for the length of the
        # turn, which is what makes mid-turn keys and type-ahead possible.
        watcher.start()
        try:
            result = await self._task
        except (asyncio.CancelledError, Interrupted):
            # Reported *after* the teardown below, never here. Printing from
            # this block put "Interrupted. The conversation is intact" on
            # screen while the live region was still up -- so for that frame
            # the card above it said "streaming" and the strip said the tool
            # was running, underneath a line saying the work had stopped. The
            # rule is one way round: stop showing the work, then say it
            # stopped.
            interrupted = True
        finally:
            # Order matters: the terminal must be restored before anything
            # tries to read from it again, and any in-progress line has to be
            # flushed to the real scrollback before the bar stops -- it is a
            # transient Live, which erases its render area on stop, taking an
            # unflushed line with it.
            watcher.stop()
            self.callbacks.close_card()
            # Stopped before the flush inside _end_stream, so the drain loop
            # cannot write one more piece after the answer has been closed.
            self.callbacks.typed.stop()
            self.callbacks._end_stream()
            bar.stop()
            self.callbacks.bar = None
            self.ui.bar = None
            self.callbacks.watcher = None
            self._task = None
            # Stages are transient. Never leave a terminal-level status
            # behind after the live bar is disposed.
            self.callbacks._status_message = ""

        # The model is loaded by now, whether it was warmed or the turn
        # loaded it, so this is the first moment /api/ps has anything true
        # to say. Reported here rather than only in /doctor: a user who has
        # just watched a 30B crawl does not know there is a check to run,
        # and "wynxo is slower than ollama run" is the report that reaches
        # me instead.
        await self._report_placement()

        if interrupted:
            self.ui.console.print()
            self.ui.warn("Interrupted. The conversation is intact; "
                         "ask me something else.")
            return False

        if result.errors:
            for message in result.errors:
                self.journal.error(message)
            self.ui.error("\n".join(result.errors))
            return False
        self.journal.assistant(result.content, tokens=self.callbacks.tokens,
                               seconds=result.elapsed)


        if result.content and not self.config.stream:
            self.ui.assistant_markdown(result.content)
        elif result.content:
            self.ui.console.print()

        # The completion report is built from recorded task state -- changed
        # files, checks that ran, failures that remain -- never from the
        # model's prose. A "fixed" claim cannot outrun the evidence. Pure
        # conversation has no evidence and gets no report.
        if not result.interrupted:
            report = self.agent.task_state.completion_report()
            if report:
                self.ui.outcome(report)

        # No stats line here: the pinned bar under the input already shows
        # tokens, rate and context, and printing them again above the next
        # prompt was the same numbers twice, scrolling away from where you
        # are actually looking.
        self._last_elapsed = result.elapsed
        if result.compacted:
            self.ui.info("context was compacted during this turn")

        await self._review_changes(review_mark)
        await self._narrate(text, result)
        return not result.interrupted

    async def _review_changes(self, mark: int) -> None:
        """In review mode, put the whole turn's changes up as one diff.

        Manual mode interrupts a ten-file refactor ten times; auto never
        shows you the shape of what happened. This waits until the work is
        finished, then asks once.
        """
        if self.agent.permissions.mode is not Mode.REVIEW:
            return
        changes = self.agent.checkpoints.changes_since(mark)
        if not changes:
            return

        from .tools.files import make_diff

        diffs: list[tuple[str, str]] = []
        for snapshot in changes:
            name = self.ui.shorten_path(str(snapshot.path))
            try:
                now = snapshot.path.read_text(encoding="utf-8",
                                              errors="surrogateescape") \
                    if snapshot.path.exists() else ""
            except OSError as exc:
                diffs.append((name, f"(could not re-read: {exc})"))
                continue
            before = snapshot.content or ""
            if before == now:
                continue
            diffs.append((name, make_diff(before, now, name)))
        if not diffs:
            return

        self.ui.console.print()
        self.ui.console.print(Text(
            f"  {len(diffs)} file{'s' if len(diffs) != 1 else ''} changed",
            style=f"bold {ACCENT}"))
        for name, diff in diffs:
            self.ui.console.print()
            self.ui.console.print(Text(f"  {name}", style=f"bold {ACCENT}"))
            self.ui.diff(diff)

        self.ui.console.print()
        answer = await self._question(
            "[k] keep  [r] revert all  [s] step through:",
            {"k": "keep", "r": "revert", "s": "step",
             # The two words a hand types without reading the options.
             "y": "yes", "n": "no"},
            default="k")

        if answer in ("", "k", "y"):
            self.ui.success(f"kept {len(diffs)} file(s)")
            return
        if answer in ("r", "n"):
            reverted, problems = self.agent.checkpoints.revert_since(mark)
            for problem in problems:
                self.ui.warn(problem)
            self.ui.success(f"reverted {reverted} change(s)")
            return
        await self._step_through(mark, changes)

    async def _step_through(self, mark: int, changes) -> None:
        """One file at a time, keeping the rest.

        Reverting a single file cannot go through the checkpoint stack --
        that is ordered, and taking one out of the middle would put the
        others back too. The snapshot holds the original, so it is written
        straight back.
        """
        kept = reverted = 0
        for snapshot in changes:
            name = self.ui.shorten_path(str(snapshot.path))
            answer = await self._question(
                f"{name}  [k] keep  [r] revert:",
                {"k": "keep", "r": "revert", "n": "no"}, default="k")
            if answer in ("r", "n"):
                try:
                    if snapshot.existed:
                        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
                        snapshot.path.write_text(snapshot.content or "",
                                                 encoding="utf-8", newline="",
                                                 errors="surrogateescape")
                    elif snapshot.path.exists():
                        snapshot.path.unlink()
                    reverted += 1
                except OSError as exc:
                    self.ui.warn(f"could not revert {name}: {exc}")
            else:
                kept += 1
        self.ui.success(f"kept {kept}, reverted {reverted}")

    def _expand_mentions(self, text: str) -> str:
        """Inline any @path the user referenced, and say what could not be.

        A mention that quietly did nothing is worse than one that reports
        why, so problems are printed rather than swallowed.
        """
        from .mentions import expand, find

        if not find(text):
            return text
        expanded, problems = expand(text, self.workspace, self.boundary,
                                    self.agent.shield)
        for problem in problems:
            self.ui.warn(problem)
        if expanded != text:
            count = expanded.count("### ")
            self.ui.info(f"read {count} referenced "
                         f"{'file' if count == 1 else 'files'}")
        return expanded

    async def _narrate(self, request: str, result) -> None:
        """Have the talker say what the coder did, and speak it.

        With no talker configured the coder's own answer is what gets read
        out, so speech works on its own -- the two features are independent.
        """
        if self.talker is not None:
            line = await self.talker.report(
                request, "\n".join(result.errors) if result.errors else result.content,
                failed=bool(result.errors))
            if line:
                await self._talk(line)
                return
            if self.talker.last_error:
                self.ui.warn(f"talker: {self.talker.last_error}")
        if result.content and not result.errors:
            # Off the event loop. speakable() runs a stack of regexes over
            # the whole answer -- seconds on a long one -- and say() also
            # starts the synthesiser process; doing either here froze the
            # chat UI for exactly as long.
            await self.speaker.say_async(result.content)

    async def _talk(self, line: str) -> None:
        """Show one line from the talker, and say it out loud."""
        if not line:
            return
        self.ui.console.print()
        self.ui.console.print(
            Text(f"  {self.pet.name}  ", style=f"bold {ACCENT}")
            + Text(line, style=ACCENT))
        await self.speaker.say_async(line)

    def _prompt_message(self) -> HTML:
        """The opening edge of the input box, and the caret inside it.

        Two lines, and both of them live inside prompt_toolkit's own region
        -- which is the whole reason this can be a box at all. A border
        *printed* above the prompt would still be there after the composer
        erased itself on accept, and the scrollback would fill with stranded
        top edges; this one is redrawn and erased with the input it belongs
        to, so the transcript never sees it.

        The bottom edge is ``_bottom_toolbar``, with the status set into it,
        and ``_prompt_rail`` closes the right-hand side. Dumb terminals get
        the caret alone: prompt_toolkit draws no toolbar there, so an
        opening edge would have nothing to close it.

        Re-evaluated on every redraw, so a mid-prompt Ctrl-E shows up at
        once.
        """
        caret = '<b><style fg="%s">%s</style></b> ' % (
            ACCENT, escape(self.ui.g.caret))
        if is_dumb_terminal() or not self.ui.g.unicode:
            return HTML(caret)
        g = self.ui.g
        edge = escape(g.tl + g.hbar * max(2, self.ui.width - 2) + g.tr)
        return HTML('<style fg="%s">%s</style>\n<style fg="%s">%s</style> %s'
                    % (ACCENT, edge, ACCENT, escape(g.vbar), caret))

    def _prompt_rail(self) -> HTML:
        """The box's right-hand edge, drawn at the end of the input line.

        prompt_toolkit right-aligns this on the row the caret is on, which
        is the only way to close a box whose middle is a live buffer.
        """
        if is_dumb_terminal() or not self.ui.g.unicode:
            return HTML("")
        return HTML('<style fg="%s">%s</style>'
                    % (ACCENT, escape(self.ui.g.vbar)))

    def _echo_prompt(self, text: str, note: str = "") -> None:
        """Put what was asked into the transcript.

        The composer erases itself when it closes, which is what keeps a
        stranded frame from piling up in the scrollback on every turn. The
        cost is that the question would vanish with it, so it is reprinted
        here -- in the outline the design gives a user message, so the
        transcript is built from the pieces the opening screen introduced.
        """
        if is_dumb_terminal():
            return
        self.ui.user_line(text, note)

    def _bottom_toolbar(self):
        """The status bar, and the closing edge of the box you type into.

        One line rather than two: a multi-line toolbar does not render on
        terminals that cannot answer a cursor-position request, and the
        border is the natural place for the status anyway. It corners at
        both ends, because prompt_toolkit draws this immediately under the
        input and the composer draws the matching top edge -- so the two of
        them are one box rather than a rule with a caret above it.

        Frame, status, hints and marks are all palette colours, so the whole
        thing changes with /theme.
        """
        g = self.ui.g
        width = max(30, self.ui.width)
        left = self._status_line()

        # "<bl><hbar> " + status + " " + fill + " " + hint + " <hbar><hbar><br>"
        def total(status: str, tail: str, fill: int) -> int:
            head = 3 + cell_len(status) + 1 + fill
            return head + (1 + cell_len(tail) + 4 if tail else 2)

        # A transient note (Ctrl-E effort changes) replaces the hints for a
        # moment; the hints return once it expires.
        note, self._prompt_note = live_note(self._prompt_note)

        # The hints that fit, most useful first. ^C stop must survive the
        # longest, so it is trimmed last rather than with the rest.
        base = ["^O think", "^T detail", "^D diff", "^E effort", "^R talk"]
        anchor = "^C stop"
        if note:
            base = [note]
        elif (typing := command_hints(_composer_text(self))):
            # Half a command is a question -- "which one was it?" -- and the
            # answer was three keystrokes away behind Tab, which you only
            # press if you already suspect there is something to find.
            # Shown while it is still being typed, /mo says plainly that
            # /mode and /mommy exist next to /model, which is the whole
            # reason the abbreviation is ambiguous in the first place.
            base, anchor = typing, ""
        hint = ""
        for keep in range(len(base), -1, -1):
            candidate = "  ".join(base[:keep] + ([anchor] if anchor else []))
            if total(left, candidate, 1) <= width:
                hint = candidate
                break
        if total(left, hint, 1) > width:
            left = left[: max(0, width - 10)]
        fill = max(1, width - total(left, hint, 0))

        frame = _ansi_of(ACCENT)
        status_style = _ansi_of(MUTED)
        hint_style = _ansi_of(FAINT)
        reset = "\x1b[0m"
        line = f"{frame}{g.bl}{g.hbar}{reset} {status_style}{left}{reset} " \
            f"{frame}" + g.hbar * fill
        if hint:
            line += f"{reset} {hint_style}{hint}{reset} {frame}{g.hbar}"
        line += f"{g.hbar}{g.br}{reset}"
        return ANSI(line)

    def _border_plain(self) -> str:
        """The bottom border with the escapes stripped, for tests."""
        import re

        return re.sub(r"\x1b\[[0-9;]*m", "", self._bottom_toolbar().value)

    def _status_line(self) -> str:
        """The idle half of the status bar.

        prompt_toolkit renders this immediately below the input, in the same
        place the activity bar occupies while a turn runs -- so the strip is
        there the whole time and only its contents change.

        Three labelled facts, and then the numbers. Which model is
        answering, what mode it is in and what the companion is doing are
        true of the session rather than of one message, which is what a
        status bar is for; the effort level and the context figure are the
        two that move, so they are the last to be given up.
        """
        used = self.agent.session.token_estimate()
        limit, _ = self._context_limit()
        pieces = []
        # What the agent is doing, first and in front of the numbers.
        bar = getattr(getattr(self, "callbacks", None), "bar", None)
        if bar is not None and (activity := getattr(bar, "activity", "")):
            phase = f"{self.ui.g.gear} {activity}"
            if detail := getattr(bar, "detail", ""):
                phase += f" {detail}"
            pieces.append(phase)
        # Before the numbers: something you typed and have not seen run
        # outranks a token count. Without this a queue held by an interrupt
        # was invisible at the prompt and fired on the next thing typed.
        if waiting := len(self.pending):
            pieces.append(f"\u203a {waiting} queued")
        # The model gets a share of the line, not all of it. A namespaced
        # tag from the hub runs to thirty-odd characters, and at anything
        # under a hundred columns that pushed the effort level, the context
        # figure and every key hint off the border -- so the longest string
        # on the line, and the only one that never changes, was the one that
        # survived.
        room = max(12, self.ui.width // 3)
        model = self.ui.shorten_model(self.config.model, room)
        mode = self._agent_mode()
        has_working_mode = hasattr(self.agent, "working_mode")
        working_mode = getattr(self.agent, "working_mode", "code")
        workspace_info = getattr(self.agent, "workspace_info", None)
        if workspace_info is not None and workspace_info.provider == "github":
            location = workspace_info.label
        elif hasattr(self, "workspace"):
            location = self.ui.shorten_path(str(self.workspace))
        else:
            location = ""
        permission = self.policy.name
        context = f"ctx {100 * used / max(1, limit):.0f}%"

        separator = f" {self.ui.g.dot} "

        def joined(
            labelled: bool,
            *,
            include_mode: bool = True,
            include_location: bool = True,
        ) -> str:
            tail = []
            if include_mode and has_working_mode:
                tail.append(working_mode)
            if include_location and location:
                tail.append(location)
            tail.extend([permission, context])
            parts = pieces + [f"model: {model}" if labelled else model] + tail
            if mode != "agent":
                # Not a statistic: a mode where wynxo stops asking before
                # it writes is worth saying every time you look down, at
                # every width. It goes in the core, not in the optional
                # half of the line.
                parts.append(f"mode: {mode}" if labelled else mode)
            return separator.join(p for p in parts if p)

        # Preserve the useful model label on normal terminals. As the
        # terminal gets narrower, drop location and working mode before
        # dropping the label; the context percentage remains visible.
        core = joined(True)
        if cell_len(core) > self.ui.width - self.LABEL_RESERVE:
            core = joined(True, include_location=False)
        if cell_len(core) > self.ui.width - self.LABEL_RESERVE:
            core = joined(True, include_mode=False, include_location=False)
        if cell_len(core) > self.ui.width - self.LABEL_RESERVE:
            core = joined(False, include_mode=False, include_location=False)

        # What is left is decoration: the word "agent", which is only ever
        # said when nothing has been changed, and what the companion is
        # doing. Both are the shape the design asks for and both are the
        # least urgent things on the line, so they are added last and only
        # where there is room left for the hints beside them.
        extra = ([f"mode: {mode}"] if mode == "agent" else []) + \
            [f"companion: {self._companion_state()}"]
        budget = self.ui.width - self.STATUS_RESERVE
        for piece in extra:
            if cell_len(core) + cell_len(separator) + cell_len(piece) > budget:
                break
            core += separator + piece
        return core

    STATUS_RESERVE = 22
    """Cells the status line leaves for the closing edge and the stop hint.

    The bar has to hold "^C stop" at any width it is ever drawn at, and the
    two optional facts are the only things on the line that can be dropped
    to make that true."""

    LABEL_RESERVE = 10
    """Where the status line stops being able to afford its labels.

    The same margin ``_bottom_toolbar`` trims at, so the choice is made
    here, by dropping a word nobody needs, rather than there, by cutting
    the line in the middle of a number."""

    def _agent_mode(self) -> str:
        """The mode, for the status bar. "agent" unless it is something
        the user has changed and would want reminding of."""
        mode = getattr(getattr(self.agent, "permissions", None), "mode", None)
        if mode is not None and mode is not Mode.MANUAL:
            return mode.value
        return "agent"

    def _companion_state(self) -> str:
        """What the companion is doing, read from the callbacks that know.

        A view, not a second copy. TerminalCallbacks derives it from the
        agent's task state and the running tool; asking it rather than
        keeping our own is what stops the status bar from cheerfully saying
        "coding" a minute after the turn failed.
        """
        callbacks = getattr(self, "callbacks", None)
        if callbacks is None:
            return "ready"
        try:
            state = callbacks._companion_state()
        except Exception:                              # noqa: BLE001
            return "ready"
        return "ready" if state == companion.State.IDLE.value else state

    async def _drain_queue(self) -> bool:
        """Run whatever was typed during the turn, oldest first.

        Shown before each one runs: a message typed a minute ago and then
        silently executed is startling.

        A Ctrl-C stops the drain and leaves the rest queued. Pressing stop
        and having the next thing start immediately is the opposite of what
        the key means -- you interrupt because you want to look at
        something, and the queue firing on regardless takes that away. What
        is left is reported, and stays visible in the status strip until it
        runs or is dropped.
        """
        while (queued := self.pending.take()) is not None:
            self._echo_prompt(queued, note="queued")
            if queued.startswith("/"):
                if await self.command(queued) is False:
                    return False
                continue
            if await self.turn(queued) is False:
                self._report_held_queue()
                return True
        return True

    def _report_held_queue(self) -> None:
        """Say what an interrupt left waiting, and how to deal with it."""
        waiting = len(self.pending)
        if not waiting:
            return
        what = "message" if waiting == 1 else "messages"
        self.ui.info(f"{waiting} queued {what} still waiting "
                     f"{self.ui.g.dot} /queue run, or /queue clear")

    def _shift_effort(self, delta: int) -> None:
        """Step the effort level without typing a command.

        The change is shown in the toolbar, not printed: this runs from a
        key binding while the prompt is on screen, and a printed line would
        wedge itself between the box edge and the input.
        """
        policy = self.policy.bump(delta)
        if policy.name == self.policy.name:
            return
        self.config.effort = policy.name
        self.agent.set_effort(policy)
        self.policy = self.agent.policy
        self._prompt_note = (f"effort: {self.policy.name} -- "
                             f"{self.policy.headline}", time.monotonic() + 3)

    def _arm_resize(self) -> None:
        """Hear a resize immediately, while a turn is running.

        This is a nudge, not the mechanism. prompt_toolkit's Application
        takes SIGWINCH for the length of every read it does -- it saves our
        handler and restores it afterwards, which is correct of it, but for
        the whole time somebody is sitting at the prompt the signal is its
        and ``ui.width`` heard nothing. Resizing the window and *then*
        typing is the ordinary case, and it left every wrap in the session
        computed against the width the terminal had at launch: on a window
        made narrower, every streamed line ran off the edge and wrapped
        twice.

        So the honest re-measure happens at the two places a draw begins --
        once per prompt in ``_prompt_loop``, and once per repaint in
        ``ActivityBar._render`` -- and between them they cover the whole
        session. This handler only makes it instant rather than up to a
        repaint late. Windows has no SIGWINCH and does not need one for the
        same reason.
        """
        if sys.platform == "win32":
            return
        with contextlib.suppress(NotImplementedError, RuntimeError):
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGWINCH, self.ui.refresh_size)

    def _arm_interrupt(self) -> None:
        """Re-install the SIGINT handler. Must run before every turn.

        prompt_toolkit's Application installs its own SIGINT handler while it
        reads a line and calls loop.remove_signal_handler(SIGINT) in its
        finally -- and there is only one handler per signal, so that removes
        ours rather than restoring it. After the first prompt there is no
        handler left at all, which is why Ctrl-C did nothing for the whole
        rest of the session.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if sys.platform == "win32":
            with contextlib.suppress(Exception):
                signal.signal(
                    signal.SIGINT,
                    lambda *_: loop.call_soon_threadsafe(self.interrupt))
            return
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal.SIGINT, self.interrupt)

    def interrupt(self) -> None:
        # Silence her first: a voice still talking about the thing you just
        # cancelled is the most annoying possible response to Ctrl-C.
        self.speaker.stop()
        if self._task and not self._task.done():
            self._task.cancel()

    QUIT_WINDOW = 2.0
    """How long a first Ctrl-C on an idle prompt stays armed. Long enough to
    be a deliberate second press, short enough that a Ctrl-C now and another
    a minute later is not a quit."""

    def _quit_is_armed(self) -> bool:
        """Whether a previous Ctrl-C is still close enough to mean quit."""
        armed = getattr(self, "_interrupt_armed", 0.0)
        return bool(armed) and time.monotonic() < armed

    # -- speech to text ------------------------------------------------------

    def start_dictation(self) -> None:
        """Ctrl-R or /dictate: microphone -> transcription -> next prompt.

        The transcript is put on the next prompt line for review, never
        submitted -- what the microphone heard is a draft, and sending it is
        the user's decision. Safe to press while a dictation is already
        running: the second press cancels the first.
        """
        if self._dictation_task is not None and not self._dictation_task.done():
            self._dictation_task.cancel()
            return
        if not self.config.stt_enabled:
            self.ui.error("speech input is disabled: set stt_enabled=true in "
                          "config (or use /config)")
            return
        self._dictation_task = asyncio.ensure_future(self._dictate())

    async def _dictate(self) -> None:
        session, hint = stt_devices.create_session(
            stt_devices.SpeechConfig(
                language=self.config.stt_language,
                device=self.config.stt_device or None,
                silence_timeout=self.config.stt_silence_timeout,
                max_duration=self.config.stt_max_duration,
                transcription_timeout=self.config.stt_transcription_timeout,
            ),
            on_state=self._on_speech_state,
            backend=self.config.stt_backend,
        )
        if session is None:
            self.ui.error(hint)
            return
        try:
            text = await session.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ui.error(f"speech input failed: {exc}")
            return
        if session.state is stt_devices.SpeechState.CANCELLED:
            return
        if text:
            # A draft on the next prompt line, for review. It is never sent
            # on its own: the prompt shows it and Enter submits, edits first
            # if the microphone got something wrong.
            self._dictation_draft = text.strip()
            self.ui.info(f"heard: {self._dictation_draft}")
            self.ui.info("review it at the prompt, then Enter to send")
        elif session.state is stt_devices.SpeechState.ERROR:
            self.ui.error("speech input failed; nothing was heard")

    def _on_speech_state(self, snapshot) -> None:
        """Mirror the speech state machine into the transcript."""
        mic = "🎙" if self.ui.g.unicode else "o"
        state = snapshot.state.value
        labels = {
            "listening": f"{mic} Listening... (Ctrl-R again to stop)",
            "transcribing": f"{self.ui.g.gear} Transcribing...",
        }
        if message := labels.get(state):
            self.ui.info(message)

    # -- slash commands ----------------------------------------------------

    def _expand(self, name: str) -> str | None:
        """The command a typed name means, or None once that is reported.

        An exact command needs no expanding; everything else does -- and
        "everything else" includes an alias, which is the whole point of the
        table. The dispatcher's guard used to read ``name not in COMMANDS
        and name not in ALIASES``, so resolution was skipped in precisely
        the case it existed for: /mo, /m, /e, /eff, /t, /th, /mem, /sc, /st,
        /se, /c, /co and /status all fell past every branch below and came
        back "unknown command". Four of the eighteen worked anyway, by being
        named a second time in a branch of their own further down, which is
        why the table looked like it was doing something.
        """
        if name in COMMANDS:
            return name
        if (resolved := resolve_command(name)) is not None:
            return resolved

        near = suggest_commands(name)
        if not near:
            self.ui.warn(f"no command called {name}")
            self.ui.hint("/help for the list")
            return None
        # An unfinished command and a misspelt one are different questions.
        # "/mo could be any of these" is right; saying it of "/mdoe" claims
        # there is a command called /mdoe.
        unfinished = any(c.startswith(name) for c in near)
        self.ui.warn(f"{name} could be any of these" if unfinished
                     else f"no command called {name} -- did you mean")
        # The list, with what each one does. Naming them without saying what
        # they are is only half an answer when the reason you typed a prefix
        # is that you could not remember which of them you wanted.
        self.ui.table(["", ""], [(c, COMMANDS[c]) for c in near])
        return None

    async def command(self, text: str) -> bool:
        """Run one slash command. False means "leave".

        A lookup, not a chain of comparisons. It was fifty ``if name ==``
        branches sitting beside a separate COMMANDS dict describing them,
        and the two drifted exactly as often as you would expect: /status
        had a branch no input could reach for months because the resolver
        did not know the name, and every abbreviation in the alias table was
        dead because the guard in front of the chain skipped resolution for
        aliases. Both are unrepresentable now -- a command is one row that
        carries its own description and its own handler, and a test walks
        the table.
        """
        parts = text.split()
        name, args = parts[0].lower(), parts[1:]

        expanded = self._expand(name)
        if expanded is None:
            return True

        entry = REGISTRY.get(expanded)
        if entry is None:
            # Unreachable while the table is the source of both the
            # resolver's vocabulary and this lookup, which is the point --
            # but a wrong answer here is a command that silently does
            # nothing, so it says so instead.
            self.ui.error(f"{expanded} has no handler")
            return True

        outcome = getattr(self, entry.handler)(args)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return False if outcome is False else True

    # -- the commands ------------------------------------------------------

    def cmd_chat(self, args: list[str]) -> bool:
        """Switch to a direct, tool-free conversation."""
        # Keep the selected workspace in view so Chat -> Code can restore a
        # GitHub workspace without another selection step. Chat's structural
        # boundary is enforced by Agent.set_working_mode.
        self.agent.set_working_mode("chat")
        self.config.working_mode = "chat"
        self.config.save()
        self.ui.success("mode: chat · tools disabled · project scanning paused")
        return True

    def cmd_code(self, args: list[str]) -> bool:
        """Switch to the local coding agent, preserving an open GitHub workspace."""
        info = getattr(self.agent, "workspace_info", None)
        if info is None or info.provider != "github":
            self.agent.set_workspace(Workspace(provider="local", root=self.workspace))
        self.agent.set_working_mode("code")
        self.config.working_mode = "code"
        self.config.save()
        if self.project_info is None:
            self._refresh_map()
        self.ui.success("mode: code · local tools enabled" if
                        getattr(self.agent.workspace_info, "provider", "local") == "local"
                        else "mode: code · GitHub workspace preserved")
        return True

    def cmd_quit(self, args: list[str]) -> bool:
        return False

    def cmd_help(self, args: list[str]) -> bool:
        # Keep the first screen small enough to scan. Power-user controls
        # remain discoverable, but they do not compete with the two modes and
        # the workspace commands that explain Wynxo's product model.
        primary_names = {
            "/help", "/chat", "/code", "/github", "/model", "/cd", "/repo",
            "/session", "/context", "/clear", "/quit",
        }
        primary = [c for c in COMMAND_LIST if c.name in primary_names]
        self.ui.table(
            ["command", "what it does"],
            [(c.name, c.does) for c in primary],
            title="main commands",
        )
        if args and args[0].lower() in {"advanced", "all"}:
            advanced = [c for c in COMMAND_LIST if c.name not in primary_names]
            self.ui.table(
                ["command", "what it does"],
                [(c.name, c.does) for c in advanced],
                title="advanced commands",
            )
        else:
            self.ui.hint("More commands: /help advanced")
        self.ui.table(
            ["key", "does"],
            [("Ctrl-O", "show or hide the model's thinking (works mid-answer)"),
             ("Ctrl-T", "full tool output vs. one-line summary (mid-answer)"),
             ("Ctrl-D", "expand or collapse the edit diff (during or after)"),
             ("Ctrl-R", "speak a message: record, transcribe, review in the composer"),
             ("Ctrl-E", "step effort up"),
             ("Ctrl-B", "step effort down"),
             ("Ctrl-C", "interrupt the current turn, keep the conversation"),
             ("Alt-Enter", "newline instead of submitting"),
             ("Up / Down", "history"),
             ("Mouse wheel", "scroll back -- your terminal's own scrollback"),
             ("Drag", "select text; copy the way you always do"),
             ("/copy", "the whole conversation, or /copy last, to the clipboard")],
            title="keys",
        )
        return True

    def cmd_tools(self, args: list[str]) -> bool:
        self.ui.table(
            ["tool", "writes?", "what it does"],
            # "writes", not "yes": the heading is three rows up by the time
            # you reach the bottom of the list, and a bare "yes" under
            # nothing is not an answer. And the description is not pre-cut
            # here -- it used to be sliced at seventy characters, mid-word,
            # before the renderer ever saw it.
            [(t.signature(), "writes" if t.mutating else "",
              _first_sentence(t.description))
             for t in self.agent.tools],
            title=f"{len(self.agent.tools)} tools"
            + ("" if self.agent.native_tools else "  (Hermes prompted mode)"),
        )
        # Every schema here is sent with every request, so a tool that
        # cannot work is held back rather than offered. Said out loud:
        # "wynxo cannot read GitHub" is a confusing thing to learn from the
        # model claiming it has no such tool.
        if (withheld := getattr(self.agent.tools, "withheld", None)):
            self.ui.table(
                ["", ""],
                sorted(withheld.items()),
                title=f"{len(withheld)} held back on this machine",
            )
            self.ui.hint("held-back tools cost nothing and are offered "
                         "again once they can work")
        return True

    def cmd_plan(self, args: list[str]) -> bool:
        todo = self.agent.tools.get("todo_write")
        rendered = todo.render() if todo and hasattr(todo, "render") else ""
        if rendered:
            self.ui.todos(rendered)
        else:
            self.ui.info("no plan yet")
        return True

    def cmd_clear(self, args: list[str]) -> bool:
        self.agent.session = Session(workspace=self.workspace)
        self._leave_conversation()
        self.agent.refresh_system_prompt()
        self.ui.info("conversation cleared")
        return True

    async def cmd_compact(self, args: list[str]) -> bool:
        before = self.agent.session.token_estimate()
        with self.ui.status("compacting..."):
            await self.agent._compact()
        after = self.agent.session.token_estimate()
        self.ui.success(f"{before:,} -> {after:,} tokens")
        return True

    def cmd_yolo(self, args: list[str]) -> bool:
        self.agent.permissions.yolo = not self.agent.permissions.yolo
        if self.agent.permissions.yolo:
            self.ui.warn("Permission prompts off. The agent can write files "
                         "and run commands freely.")
        else:
            self.ui.info("Permission prompts back on.")
        return True

    async def cmd_doctor(self, args: list[str]) -> bool:
        from .doctor import Doctor

        await Doctor(self.client, self.config, self.ui,
                     workspace=self.workspace).run()
        return True

    def cmd_dictate(self, args: list[str]) -> bool:
        self.start_dictation()
        return True

    async def cmd_init(self, args: list[str]) -> bool:
        await self.turn(
            "Look at this project -- its layout, build files, tests, and "
            "conventions -- then write a WYNXO.md at the root that tells a "
            "new agent what it needs to know: what the project is, how to "
            "build and test it, and the conventions to follow. Be concrete "
            "and brief. No filler."
        )
        return True

    async def cmd_sessions(self, args: list[str]) -> bool:
        return await self.cmd_session(["list", *args])

    async def cmd_session(self, args: list[str]) -> bool:
        """Where you are, and how to get back to somewhere you were.

        One command rather than three: /session says what this conversation
        is, /session list shows the others, and /session <id> picks one up.
        They are the same subject, and splitting them across /session,
        /sessions and /resume meant the one you remembered was rarely the
        one you wanted. All three names still work.
        """
        word = (args[0].lower() if args else "")

        if word in ("list", "ls", "all"):
            return self._list_sessions()
        if word in ("resume", "open", "switch", "continue"):
            return await self.cmd_resume(args[1:])
        if word:
            # /session 4f21a0 -- an id, or the start of one.
            return await self.cmd_resume(args)

        return self._describe_session()

    def _context_limit(self) -> tuple[int, str]:
        """The window that actually governs, and what set it.

        Two numbers were on offer and both screens picked a different one:
        /session showed num_ctx (32,768) and /stats showed the effort
        policy's budget (16,000), for the same conversation at the same
        moment. Neither was wrong on its own and together they were, because
        the one that decides when to compact is the lower of the two.
        """
        budget = self.policy.context_budget
        window = self.config.num_ctx
        if budget and budget < window:
            return budget, f"{self.policy.name} effort"
        return window, "num_ctx"

    def _describe_session(self) -> bool:
        session = self.agent.session
        limit = min(self.policy.max_iterations, self.config.max_tool_iterations)
        used = session.token_estimate()
        window, set_by = self._context_limit()
        window = max(1, window)
        self.ui.table(
            ["", ""],
            [
                ("about", session.title() or "nothing said yet"),
                ("model", self.config.model, f"{self.policy.name} effort"),
                ("workspace", self.ui.shorten_path(str(self.workspace))),
                ("context", f"{used:,} of {window:,} tokens",
                 f"{100 * used // window}% used", f"set by {set_by}"),
                ("said", _plural(len(session.messages), "message"),
                 _plural(session.usage.requests, "request"),
                 _plural(session.usage.tool_calls, "tool call")),
                ("tools", _plural(len(self.agent.tools), "available", "available"),
                 f"up to {_plural(limit, 'call')} a turn"),
            ],
            title=f"session {session.session_id}",
        )
        others = len(Session.recent(limit=6, exclude=session.session_id))
        if others:
            self.ui.hint(f"/session list -- {others} other conversation"
                         f"{'s' if others != 1 else ''} you can pick up")
        return True

    def _list_sessions(self) -> bool:
        rows = Session.recent(exclude=self.agent.session.session_id)
        if not rows:
            self.ui.info("no other saved conversations")
            return True
        here = str(self.workspace)
        # The column only earns its place when it distinguishes something.
        # With every conversation from this directory it said "here" seven
        # times down the page and told you nothing.
        elsewhere = any(r["workspace"] != here for r in rows)
        self.ui.table(
            ["", "", "", "", ""],
            [tuple(cell for cell in (
                r["session_id"][:8],
                _ago(r.get("updated_at", 0)),
                _plural(r["messages"], "message"),
                (self.ui.shorten_path(r["workspace"])
                 if elsewhere and r["workspace"] != here else ""),
                r["preview"] or "(nothing said yet)",
            ) if cell) for r in rows],
            title="conversations you can pick up",
        )
        self.ui.hint("/session <id> to pick one up")
        return True

    def cmd_diff(self, args: list[str]) -> bool:
        """Show a read-only Git diff without involving the model."""
        from .repo import run_git

        if args and args != ["staged"]:
            self.ui.warn("usage: /diff [staged]")
            return True
        ok, _ = run_git(["rev-parse", "--git-dir"], cwd=self.workspace, timeout=10)
        if not ok:
            self.ui.warn("not a git repository")
            return True
        command = ["diff", "--staged"] if args else ["diff"]
        ok, diff = run_git(command, cwd=self.workspace, timeout=60)
        if not ok:
            self.ui.warn(f"could not read the diff: {_first(diff)}")
            return True
        if not diff.strip():
            self.ui.info("no " + ("staged " if args else "unstaged ") + "changes")
            return True
        files = []
        additions = deletions = 0
        for line in diff.splitlines():
            if line.startswith("diff --git "):
                files.append(line.rsplit(" b/", 1)[-1])
            elif line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
        self.ui.info(f"{len(files)} file(s) changed  +{additions} -{deletions}")
        if files:
            self.ui.table(["file"], [(f,) for f in files[:100]], title="changed files")
        self.ui.diff(diff[:100_000])
        if len(diff) > 100_000:
            self.ui.info("diff truncated visually; use git diff for the complete patch")
        return True

    async def cmd_review(self, args: list[str]) -> bool:
        """Ask the model to review the current changes. Read-only.

        Nothing is changed or committed; this is the review pass that happens
        before a commit decides it is done. Diff only, so a clean tree is an
        honest "nothing to review".
        """
        from .prompts import REVIEW_PROMPT
        from .repo import run_git

        target = args[0].lower() if args else "all"
        if target not in ("staged", "unstaged", "all"):
            self.ui.warn("usage: /review [staged | unstaged]")
            return True
        ok, _ = run_git(["rev-parse", "--git-dir"], cwd=self.workspace, timeout=10)
        if not ok:
            self.ui.warn("not a git repository")
            return True

        if target == "staged":
            git_commands = [["diff", "--staged"]]
        elif target == "unstaged":
            git_commands = [["diff"]]
        else:
            git_commands = [["diff"], ["diff", "--staged"]]
        diffs = []
        for git_command in git_commands:
            ok, diff = run_git(git_command, cwd=self.workspace, timeout=60)
            if not ok:
                self.ui.warn(f"could not read the diff: {_first(diff)}")
                return True
            if diff.strip():
                diffs.append(diff)
        if not diffs:
            self.ui.info("no changes to review")
            return True
        diff = "\n".join(diffs)

        files = additions = deletions = 0
        for line in diff.splitlines():
            if line.startswith("diff --git "):
                files += 1
            elif line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
        body = diff if len(diff) <= 24_000 else\
            diff[:24_000] + "\n\n... [diff truncated]"
        self.ui.info(f"reviewing {files} file(s), +{additions} -{deletions}...")
        with self.ui.status("reviewing the changes..."):
            turn = await self.agent._call_model(
                messages=[{"role": "user",
                           "content": f"{REVIEW_PROMPT}\n\n```diff\n{body}\n```"}],
                use_tools=False, stream_content=False)
        review = turn.content.strip()
        if not review:
            self.ui.warn("the model did not produce a review")
            return True
        self.ui.console.print()
        self.ui.console.print(Text("review", style=f"bold {ACCENT}"))
        self.ui.console.print(Text(review))
        return True

    async def cmd_test(self, args: list[str]) -> bool:
        """Run only the test command the project itself makes unambiguous."""
        if args:
            self.ui.warn("usage: /test")
            return True
        from . import testing

        runner = testing.detect(self.workspace)
        if runner is None:
            self.ui.warn("could not identify this project's test command")
            return True
        shell = self.agent.tools.get("shell")
        if shell is None:
            self.ui.warn("the shell tool is disabled; enable it to run tests")
            return True
        await self.callbacks.on_stage("testing", runner.command)
        result = await shell.invoke(
            {"command": runner.command, "timeout": testing.DEFAULT_TIMEOUT},
            timeout=testing.DEFAULT_TIMEOUT + 30,
        )
        if result.ok:
            self.ui.success(f"tests passed: {runner.command}")
        else:
            self.ui.error(f"tests failed: {runner.command}")
        if result.output.strip():
            self.ui.code(result.output, language="text")
        return True

    async def cmd_commit(self, args: list[str]) -> bool:
        """Write a commit message from what is actually staged, then commit.

        Reads the staged diff, has the model describe it, shows the result,
        and only commits once you say so. Nothing is staged for you: what to
        include is a decision the message should describe, not one this
        should make on your behalf.
        """
        from .prompts import COMMIT_PROMPT
        from .repo import run_git

        ok, _ = run_git(["rev-parse", "--git-dir"], cwd=self.workspace, timeout=10)
        if not ok:
            self.ui.warn("not a git repository")
            return True

        ok, staged = run_git(["diff", "--staged"], cwd=self.workspace, timeout=60)
        if not ok:
            self.ui.warn(f"could not read the staged diff: {staged}")
            return True
        if not staged.strip():
            self.ui.info("nothing staged")
            _, unstaged = run_git(["status", "--short"], cwd=self.workspace,
                                  timeout=30)
            if unstaged.strip():
                self.ui.info("stage what you want first:  git add -p")
            return True

        _, stat = run_git(["diff", "--staged", "--stat"], cwd=self.workspace,
                          timeout=30)
        for line in stat.strip().splitlines()[-1:]:
            self.ui.info(line.strip())

        # Cap what is sent: a huge diff would blow the context window, and
        # the top of it describes the change well enough to name it.
        body = staged if len(staged) <= 24_000 else (
            staged[:24_000] + "\n\n... [diff truncated]")

        message = " ".join(args).strip()
        if not message:
            with self.ui.status("reading the diff..."):
                turn = await self.agent._call_model(
                    messages=[{"role": "user",
                               "content": f"{COMMIT_PROMPT}\n\n```diff\n{body}\n```"}],
                    use_tools=False, stream_content=False)
            message = _clean_commit_message(turn.content)
        if not message:
            self.ui.warn("the model did not produce a message; write one yourself")
            return True

        self.ui.console.print()
        self.ui.console.print(Text("  " + message.splitlines()[0], style=f"bold {ACCENT}"))
        for line in message.splitlines()[1:]:
            self.ui.console.print(Text("  " + line, style=MUTED))
        self.ui.console.print()

        answer = await self._question(
            "[y] commit  [e] edit  [n] no:",
            {"y": "commit", "e": "edit", "n": "no"}, default="y")

        if answer == "e":
            edited = await self._type_in("message:",
                                         default=message.splitlines()[0])
            if not edited:
                self.ui.info("cancelled")
                return True
            message = edited
        elif answer != "y":
            self.ui.info("not committed")
            return True

        ok, output = run_git(["commit", "-m", message], cwd=self.workspace,
                             timeout=60)
        if not ok:
            self.ui.warn(f"commit failed: {_first(output)}")
            return True
        self.ui.success(_first(output) or "committed")
        if proud := self.pet.remark("proud"):
            self.ui.console.print(f"  {self.pet.name} — {proud}")
        self.journal.note("committed", subject=message.splitlines()[0])
        return True

    async def cmd_resume(self, args: list[str]) -> bool:
        """Carry on an earlier conversation, with its history.

        Sessions were already being written to disk after every turn; there
        was simply no way back into one, which made them a debugging
        artefact rather than something you could use.
        """
        rows = Session.recent(exclude=self.agent.session.session_id)
        if not rows:
            self.ui.info("no other saved conversations yet")
            return True

        if args:
            wanted = args[0].lstrip("#")
            match = next((r for r in rows
                          if r["session_id"].startswith(wanted)), None)
            if match is None:
                self.ui.warn(f"no saved conversation starting {wanted!r}")
                self.ui.hint("/session list to see them")
                return True
            return self._load_session(match["session_id"])

        here = str(self.workspace)
        options = [
            Choice(value=r["session_id"],
                   label=_ago(r.get("updated_at", 0)),
                   badge=(f"{r['messages']} msgs" if r["workspace"] == here
                          else self.ui.shorten_path(r["workspace"])),
                   badge_style="badge.muted",
                   hint=(r["preview"] or "(nothing said yet)"))
            for r in rows
        ]
        chosen = await self._pick("pick up which conversation?", options,
                                  options[0].value if options else "")
        if chosen is not NO_PICKER:
            if not chosen:
                return True
            return self._load_session(chosen)

        return self._list_sessions()

    def _load_session(self, session_id: str) -> bool:
        came_from = next(
            (r["workspace"] for r in Session.recent(limit=60)
             if r["session_id"] == session_id), "")
        restored = Session.load(session_id, self.workspace)
        if restored is None:
            self.ui.warn(f"could not read conversation {session_id[:8]}")
            return True

        # The system prompt is rebuilt rather than restored: effort, scope,
        # mode and memory may all have moved on since, and the saved one
        # would quietly reinstate the old ones.
        self.agent.session = restored
        self.agent.checkpoints.clear()
        self._leave_conversation()
        self.agent.refresh_system_prompt()
        self._last_elapsed = 0.0

        title = restored.title()
        self.ui.success(f"picked up {session_id[:8]}"
                        + (f" -- {title}" if title else "")
                        + f" ({len(restored.messages)} messages)")
        if came_from and came_from != str(self.workspace):
            # Resuming across projects is the point, not an accident, but
            # the tools are pointed at the directory you are in now -- so a
            # conversation about another repository will happily read files
            # that are not the ones it was talking about unless you say so.
            self.ui.hint(
                f"that conversation happened in "
                f"{self.ui.shorten_path(came_from)}; tools still work in "
                f"{self.ui.shorten_path(str(self.workspace))} "
                f"(/cd to move)")
        self.ui.hint("undo history is not restored; it belonged to that run")
        return True

    def _leave_conversation(self) -> None:
        """Drop everything that belonged to the conversation being left.

        Three commands replace the conversation -- /clear, /new and /resume
        -- and each kept its own idea of what that meant. Only /clear reset
        the task state, and none of them cleared the checklist, so work from
        an abandoned chat followed you into the next one: a recovery block
        citing failures from a different task, a completion report claiming
        files changed in another conversation, the repeat detector treating
        a first action as a repetition, a compaction summary told that
        someone else's steps were "still outstanding", and the plan panel
        sitting in the corner insisting on a checklist nobody was working
        on.

        The conversation's *history* is the caller's business -- restored,
        replaced or emptied depending on which command it is. This is only
        the state that has no meaning outside the chat it came from.
        """
        self.agent.task_state.reset()
        todo = self.agent.tools.get("todo_write")
        if todo is not None and hasattr(todo, "items"):
            todo.items = []

    def cmd_new(self, args: list[str]) -> bool:
        """A new chat, the way opening a new tab is new.

        /clear empties the message list in place. This goes further: a new
        session id and a new log file, so the old conversation stays intact
        and reviewable rather than being half-overwritten, undo history reset
        because those snapshots belong to the chat that is over, and a clean
        screen so what is in front of you matches what the model can see.

        Memory survives on purpose -- it is the thing that is supposed to
        outlive a conversation.
        """
        self.agent.session = Session(workspace=self.workspace)
        self.agent.checkpoints.clear()
        self._leave_conversation()
        self.agent.refresh_system_prompt()
        self.callbacks.tokens = 0
        self._last_elapsed = 0.0
        self.pending.clear()

        self.journal = Journal.open(self.agent_session_id(),
                                    enabled=self.config.log)
        self.callbacks.journal = self.journal

        self.ui.clear()
        self.ui.home(self.config.model, str(self.workspace),
                     mode=self._agent_mode(),
                     companion=self._companion_state(),
                     show_companion=self.config.pet,
                     show_art=True,
                     show_static_controls=False)
        self.ui.info("new chat -- memory kept, history and undo reset")
        return True

    async def cmd_effort(self, args: list[str]) -> bool:
        if not args:
            # The badge is what a level costs you in time, which is the
            # trade being made and the only thing the headline does not
            # say. EffortPolicy has carried a `speed` for every level since
            # it was written, documented "for the picker", and the picker
            # had never shown it -- six values written and none read.
            options = [
                Choice(value=name,
                       label=name,
                       badge=("current" if name == self.policy.name
                              else resolve(name).speed),
                       badge_style=("badge" if name == self.policy.name
                                    else "badge.muted"),
                       hint=resolve(name).headline)
                for name in ORDER
            ]
            chosen = await self._pick("effort", options, self.policy.name)
            if chosen is None:
                return True
            if chosen is NO_PICKER:
                self.ui.table(
                    ["level", "speed", "behaviour"],
                    [(name + ("  <-" if name == self.policy.name else ""),
                      resolve(name).speed, resolve(name).describe())
                     for name in ORDER],
                    title="effort levels",
                )
                return True
            args = [chosen]
        try:
            policy = resolve(args[0])
        except KeyError as exc:
            self.ui.warn(str(exc))
            return True
        previous = self.policy.name
        self.config.effort = policy.name
        self.agent.set_effort(policy)
        # set_effort() may downgrade thinking for the current model; read the
        # policy back rather than trusting the one just resolved, so the
        # status bar reports what the agent will actually do.
        self.policy = self.agent.policy
        self.config.effort = self.policy.name
        self.config.save()
        await self._effort_surge(previous, self.policy.name)
        self.ui.success(f"effort: {self.policy.name} -- {self.policy.describe()}")
        return True

    async def _effort_surge(self, previous: str, current: str) -> None:
        """Make stepping up to the top two levels feel like it costs
        something, because it does.

        Only on the way up, and only into max or ultra. That is what this
        has always said and not what it did: every upward step drew the
        band, so /effort high answered a one-word request with twenty-two
        cells of solid block spelling HIGH -- the same decoration that was
        taken out of the status strip for saying in four cells of colour
        what the word beside it already said. The success line under this
        names the level and what it changes; a band that fires on every
        change says nothing the line does not, and one that fires rarely
        means something.
        """
        from .ui import celebrate, surge

        if current not in ("max", "ultra") or not self.config.animations:
            return
        if ORDER.index(current) <= ORDER.index(previous):
            return          # celebrating the way down would be the wrong mood

        label = "ULTRA" if current == "ultra" else "MAX EFFORT"
        if not self.ui.live_ok:
            # Nowhere to animate (a pipe, a dumb terminal): drawn once.
            celebrate(self.ui, label, ORDER.index(current), 1)
            return
        await surge(self.ui, label,
                    ACCENT if current == "ultra" else BAR_ACCENT)

    async def cmd_pull(self, args: list[str]) -> bool:
        """Download a model from Ollama, then make it the active one.

        `ollama pull` is a separate step the wizard and /model used to send
        people off to run by hand; the provider already streams progress, so
        the command only needs to surface it and hand over to the switcher.
        """
        if not args:
            self.ui.info("usage: /pull <model>, e.g. /pull qwen3-coder:30b")
            return True
        target = args[0]
        if target not in (m.name for m in await self.client.list_models()):
            self.ui.info(f"pulling {target}...")
            last = ""
            try:
                async for line in self.client.pull(target):
                    line = line.strip()
                    if line and line != last and not line.endswith("success"):
                        last = line
                        self.ui.info(line)
            except ProviderError as exc:
                self.ui.error(str(exc))
                return True
        self._model_names = [m.name for m in await self.client.list_models()]
        return await self._switch_model(target, self._model_names)

    async def cmd_model(self, args: list[str]) -> bool:
        """Switch model. With no argument this is the same capability-aware
        picker the first-run wizard uses, rather than a second, dumber list."""
        from .provider import inspect_all
        from .wizard import _model_choice, _print_model_rows

        try:
            with self.ui.status("asking the server what it has..."):
                models = await self.client.list_models()
            self._model_names = [model.name for model in models]
        except ProviderError as exc:
            self.ui.error(str(exc))
            return True
        self._model_names = [model.name for model in models]
        if not models:
            self.ui.warn("that server has no models installed")
            return True

        if args:
            return await self._switch_model(args[0], [m.name for m in models])

        with self.ui.status(f"checking what {len(models)} model(s) can do..."):
            models = await inspect_all(self.client, models)
        models.sort(key=lambda m: (not m.supports_tools, m.name))

        chosen = await self._pick("model", [_model_choice(m) for m in models],
                                  self.config.model)
        if chosen is not NO_PICKER:
            if chosen and chosen != self.config.model:
                return await self._switch_model(chosen, [m.name for m in models])
            if chosen:
                self.ui.info(f"already using {chosen}")
            return True

        self.ui.console.print()
        _print_model_rows(self.ui, models)
        self.ui.info("switch with /model <name>")
        return True

    async def _switch_model(self, target: str, names: list[str]) -> bool:
        if target not in names:
            matches = [n for n in names if n.split(":")[0] == target
                       or n.startswith(target)]
            if len(matches) == 1:
                target = matches[0]
            else:
                self.ui.warn(f"{target} is not installed on that server.")
                if matches:
                    self.ui.info(f"did you mean: {', '.join(matches)}")
                else:
                    self.ui.info(f"pull it first:  /pull {target}")
                return True

        self.config.model = target
        self.agent.config.model = target
        self.config.save()
        await self.agent.detect_capabilities()
        self.policy = self.agent.policy
        self.ui.success(f"model: {target}")
        self.journal.note("model switched", model=target)
        if warning := await check_context(self.client, self.config):
            self.ui.warn(warning)
        return True

    _ENDPOINT_ADD = "\x00add"
    _ENDPOINT_TEST = "\x00test"
    """Sentinels for the two actions in the server picker. Not the plain
    words: a server can be named "add", and choosing it must select it."""

    async def cmd_endpoint(self, args: list[str]) -> bool:
        action = args[0].lower() if args else "list"

        if action == "list" and not args:
            # Bare /endpoint: pick which server to talk to. The actions live
            # in the same list as the servers, so this opens a picker even
            # with a single server configured -- which is the usual case, and
            # was the one that used to get a table and a line of syntax to
            # copy. Adding a second server is exactly what someone with one
            # server is here to do.
            #
            # Sentinel values rather than the words: a server may legitimately
            # be named "add" or "test", and picking it must not run a command.
            options = [
                Choice(value=endpoint.name, label=endpoint.name,
                       badge=("current" if endpoint.name ==
                              self.config.active_endpoint else ""),
                       hint=endpoint.url)
                for endpoint in self.config.endpoints
            ]
            options.append(Choice(value=self._ENDPOINT_ADD, label="add a server",
                                  hint="another machine running Ollama"))
            options.append(Choice(value=self._ENDPOINT_TEST, label="test all",
                                  hint="check which of them answer"))

            picked = await self._pick("ollama server", options,
                                      self.config.active_endpoint)
            if picked is None:
                return True
            if picked is not NO_PICKER:
                if picked == self._ENDPOINT_TEST:
                    return await self.cmd_endpoint(["test"])
                if picked == self._ENDPOINT_ADD:
                    typed = await self._type_in(
                        "url of the Ollama server (host, or host:port):")
                    if not typed:
                        return True
                    return await self.cmd_endpoint(["add", typed])
                if picked == self.config.active_endpoint:
                    self.ui.info(f"already using {picked}")
                    return True
                return await self.cmd_endpoint(["use", picked])

        if action == "list":
            self.ui.table(
                ["name", "url"],
                [(e.name + ("  <-" if e.name == self.config.active_endpoint else ""), e.url)
                 for e in self.config.endpoints],
                title="ollama servers",
            )
            dot = self.ui.g.dot
            self.ui.info(f"/endpoint add <url> {dot} /endpoint use <name> {dot} /endpoint test")
            return True

        if action == "test":
            for endpoint in self.config.endpoints:
                with self.ui.status(f"checking {endpoint.url}..."):
                    version = await probe(endpoint.url, timeout=5.0)
                if version:
                    self.ui.success(f"{endpoint.name}: {endpoint.url} (ollama {version})")
                else:
                    self.ui.warn(f"{endpoint.name}: {endpoint.url} unreachable")
            return True

        if action == "add" and len(args) > 1:
            url = normalise_url(args[1])
            name = args[2] if len(args) > 2 else url.split("://")[1].split(":")[0]
            with self.ui.status(f"checking {url}..."):
                version = await probe(url, timeout=6.0)
            if not version:
                self.ui.error(f"Nothing answering at {url}.\n\n{server_help()}")
                return True
            self.config.endpoints = [e for e in self.config.endpoints if e.name != name]
            self.config.endpoints.append(Endpoint(name=name, url=url))
            self.config.save()
            self.ui.success(f"added {name} -> {url} (ollama {version})")
            self.ui.info(f"switch to it with /endpoint use {name}")
            return True

        if action == "use" and len(args) > 1:
            name = args[1]
            if not any(e.name == name for e in self.config.endpoints):
                self.ui.warn(f"no endpoint named {name}. /endpoint list to see them.")
                return True
            self.config.active_endpoint = name
            self.config.save()
            await self.client.aclose()
            self.client = make_client(self.config)
            self.agent.client = self.client
            try:
                version = await self.client.ping()
            except ProviderError as exc:
                self.ui.error(str(exc))
                return True
            self.ui.success(f"now using {name}: {self.client.base_url} (ollama {version})")
            return True

        self.ui.warn("usage: /endpoint [list | test | add <url> [name] | use <name>]")
        return True

    async def cmd_mode(self, args: list[str]) -> bool:
        if not args:
            options = [(m.value, m.describe()) for m in Mode]
            chosen = await self._pick("permission mode", options,
                                      self.agent.permissions.mode.value)
            if chosen is None:
                return True
            if chosen is NO_PICKER:
                self.ui.table(
                    ["mode", "behaviour"],
                    [(m.value + ("  <-" if m is self.agent.permissions.mode else ""),
                      m.describe()) for m in Mode],
                    title="permission modes",
                )
                return True
            if chosen == self.agent.permissions.mode.value:
                self.ui.info(f"already in {chosen} mode")
                return True
            args = [chosen]
        try:
            mode = Mode.parse(args[0])
        except KeyError as exc:
            self.ui.warn(str(exc))
            return True
        self.agent.permissions.mode = mode
        self.agent.refresh_system_prompt()
        self.ui.success(f"mode: {mode.value} -- {mode.describe()}")
        if mode is Mode.YOLO:
            self.ui.warn("Nothing will ask for approval from here on.")
        return True

    def _refresh_map(self, note=None) -> None:
        """Rebuild the project map if the files have moved on.

        Never fatal: a project that cannot be walked, or a read-only one that
        cannot cache the result, still gets a session -- just without the
        head start.
        """
        from . import projectmap

        try:
            self.discovery.workspace = self.workspace.resolve()
            self.project_info = self.discovery.scan(force=True)
            self.agent.project_map = projectmap.load(self.workspace)
        except Exception as exc:
            self.agent.project_map = ""
            if note is not None:
                note(WARN, "project map", str(exc))
            return
        self.agent.refresh_system_prompt()

    def cmd_apps(self, args: list[str]) -> bool:
        """The applications actually discovered on this machine.

        Generated from the OS -- Start Menu shortcuts, App Paths, PATH, and
        the platform equivalents -- never from a code-level list, so what is
        shown is exactly what launch_application can launch.
        """
        if self._app_catalog is None:
            self._app_catalog = ApplicationCatalog()
        wanted = ""
        if args and args[0].lower() in ("refresh", "rescan", "again"):
            with self.ui.status("scanning for installed applications..."):
                entries = self._app_catalog.refresh()
            args = args[1:]
        else:
            entries = self._app_catalog.entries()
        if not entries:
            self.ui.info("no applications discovered on this machine")
            return True

        total = len(entries)
        if args:
            # Nearly five hundred applications is the ordinary case on
            # Linux, and a list that long answers nothing. A word narrows
            # it, which is what you came to do: /apps code, /apps firefox.
            wanted = " ".join(args).lower()
            entries = [e for e in entries
                       if wanted in e.name.lower()
                       or wanted in str(e.path).lower()]
            if not entries:
                self.ui.info(f"no application matching {wanted!r} "
                             f"among the {total:,} found")
                return True

        self.ui.table(
            ["application", "found in", "target"],
            [(e.name, e.where, self.ui.shorten_path(str(e.path)))
             for e in entries],
            title=(f"{len(entries):,} of {total:,} applications match "
                   f"{wanted!r}" if wanted else f"{total:,} applications"),
        )
        self.ui.hint("/apps <word> to narrow it  "
                     f"{self.ui.g.dot}  /apps refresh to rescan  "
                     f"{self.ui.g.dot}  the agent launches them by name")
        return True

    def cmd_map(self, args: list[str]) -> bool:
        """Show the map, or force a rebuild."""
        from . import projectmap

        if args and args[0].lower() in ("rebuild", "refresh", "again"):
            path = projectmap.cache_path(self.workspace)
            with contextlib.suppress(OSError):
                path.unlink()
            with self.ui.status("mapping the project..."):
                self._refresh_map()
            self.ui.success(projectmap.summarise(self.agent.project_map)
                            or "nothing to map here")
            return True

        if not self.agent.project_map:
            self.ui.info("no map for this project")
            self.ui.info("/map rebuild to try again")
            return True
        self.ui.console.print()
        for line in self.agent.project_map.splitlines():
            style = f"bold {ACCENT}" if line.startswith("#") else MUTED
            self.ui.console.print(Text("  " + line, style=style))
        self.ui.console.print()
        self.ui.info(f"{projectmap.cache_path(self.workspace)}  "
                     f"{self.ui.g.dot}  /map rebuild")
        return True

    def _boundary_summary(self, boundary) -> str:
        """describe(), but with the path shortened for the terminal.

        Boundary.describe() is also what goes into the system prompt, where
        the model needs the real, full path -- so the shortening happens
        here instead, only for what the user reads.
        """
        if boundary.unrestricted:
            return "the whole machine"
        root = self.ui.shorten_path(str(boundary.root))
        return f"the repository at {root}" if boundary.scope is Scope.REPO else root

    async def cmd_scope(self, args: list[str]) -> bool:
        if not args:
            current = self.agent.boundary
            means = {"folder": "only the directory wynxo started in",
                     "repo": "the whole git repository",
                     "machine": "no path restriction at all"}
            options = [(s.value, means[s.value]) for s in Scope]
            chosen = await self._pick(
                "scope", options, current.scope.value if current else "")
            if chosen is None:
                return True
            if chosen is NO_PICKER:
                self.ui.table(
                    ["scope", "means"],
                    [(s.value + ("  <-" if current and s is current.scope else ""),
                      means[s.value]) for s in Scope],
                    title="scope",
                )
                if current:
                    self.ui.info(f"currently: {self._boundary_summary(current)}")
                self.ui.info("/scope <path> or /cd <path> moves to another directory")
                return True
            if current and chosen == current.scope.value:
                self.ui.info(f"already scoped to {chosen}")
                return True
            args = [chosen]
        try:
            scope = Scope.parse(args[0])
        except KeyError:
            # Not one of the three words, so read it as a directory. Pointing
            # wynxo at another project mid-session is the obvious thing to
            # want from a command called /scope, and refusing it over a
            # vocabulary mismatch helps nobody.
            return self.cmd_cd(args)

        if scope is Scope.MACHINE:
            self.ui.warn(
                "Machine scope removes the path restriction entirely: the agent "
                "can read and write anywhere your user account can.")
            answer = await self._question(
                "really widen to the whole machine? [y/N]:",
                {"y": "yes", "n": "no"}, default="n")
            if answer != "y":
                self.ui.info("left unchanged")
                return True

        self._apply_scope(scope)
        self.ui.success(f"scope: {self._boundary_summary(self.agent.boundary)}")
        return True

    # A leading keyword is a subcommand, never a repository. Parsed in one
    # place so invalid subcommands get structured help instead of the clone
    # path swallowing them as "that does not look like a repository".
    _REPO_KEYWORDS = ("help", "status", "list", "ls", "clone", "add")

    def _repo_help(self) -> None:
        self.ui.table(
            ["command", "does"],
            [
                ("repo", "the current checkout"),
                ("repo status", "which branch the workspace is on"),
                ("repo list", "the current checkout (wynxo keeps no registry)"),
                ("repo owner/name", "clone a GitHub repository"),
                ("repo <url>", "clone from a full URL"),
                ("repo clone owner/name", "same, spelled out"),
            ],
            title="repo",
        )

    def _repo_status(self) -> None:
        from . import repo as repo_module

        current = repo_module.status(self.workspace)
        if current:
            self.ui.info(f"{self.ui.shorten_path(str(self.workspace))}  "
                         f"on {current}")
        else:
            self.ui.info("this folder is not a git checkout")

    async def cmd_repo(self, args: list[str]) -> bool:
        """Clone a GitHub repository and move the workspace into it."""
        from . import repo as repo_module

        if not args:
            self._repo_status()
            self._repo_help()
            return True

        first = args[0].lower()
        if first in self._REPO_KEYWORDS:
            if first in ("list", "ls"):
                self._repo_status()
                self.ui.info("wynxo keeps no registry of clones; to work in "
                             "one, /repo <owner/name> or /cd into it.")
                return True
            if first == "status":
                self._repo_status()
                return True
            if first == "help":
                self._repo_help()
                return True
            if first in ("clone", "add"):
                args = args[1:]          # repo clone owner/name -> owner/name
                if not args:
                    self._repo_help()
                    return True

        if not repo_module.git_available():
            self.ui.error("git is not installed, so wynxo cannot clone anything.")
            return True

        target = repo_module.parse(" ".join(args))
        if target is None:
            self._repo_help()
            self.ui.warn("that does not look like a repository name or URL.")
            return True

        with self.ui.status(f"fetching {target.slug}..."):
            ok, path, message = repo_module.clone_or_update(target)
        if not ok:
            self.ui.error(message)
            return True

        self.ui.success(message)
        self.cmd_cd([str(path)])
        # A checkout is a repository, so widen to it rather than pinning the
        # agent to whichever subdirectory happens to be the clone root.
        self._apply_scope(Scope.REPO)
        if branch := repo_module.status(path):
            self.ui.info(f"on {branch}")
        self.ui.info("wynxo does not push. Ask it to run git, and approve it.")
        return True

    def cmd_cd(self, args: list[str]) -> bool:
        """Move the agent to another directory, keeping the conversation."""
        if not args:
            self.ui.info(f"{self.workspace}")
            return True

        raw = " ".join(args).strip().strip('"').strip("'")
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = (self.workspace / target)
        try:
            target = target.resolve()
        except OSError as exc:
            self.ui.warn(f"cannot resolve {raw}: {exc}")
            return True

        if not target.exists():
            self.ui.warn(f"{target} does not exist")
            return True
        if not target.is_dir():
            self.ui.warn(f"{target} is a file, not a directory")
            return True

        self.workspace = target
        self.memory = Memory(target)
        self.agent.workspace = target
        self.agent.memory = self.memory
        self.agent.workspace_info = Workspace(provider="local", root=target)
        self._apply_scope(self.boundary.scope)
        self._refresh_map()
        self.ui.success(f"working in {self.ui.shorten_path(str(target))}")
        if reason := suspicious_workspace(target):
            self.ui.warn(reason)
        self.journal.note("workspace changed", path=str(target))
        return True

    def _apply_scope(self, scope: Scope) -> None:
        boundary = resolve_scope(self.workspace, scope)
        self.boundary = boundary
        if boundary.scope is not scope and scope is Scope.REPO:
            self.ui.warn("Not inside a git repository; staying with folder scope.")

        # The registry is rebuilt with new tool instances, so carry over the
        # state that lives on a tool -- otherwise changing scope silently
        # wipes the plan the agent is working through.
        previous_todo = self.agent.tools.get("todo_write")
        self.agent.boundary = boundary
        if getattr(self.agent, "working_mode", "code") == "chat":
            self.agent.tools = Registry([])
        elif (info := getattr(self.agent, "workspace_info", None)) is not None                 and info.provider == "github":
            self.agent.set_workspace(info)
        else:
            self.agent.tools = build_registry(
                self.workspace, allow_shell=self.config.allow_shell,
                boundary=boundary, memory=self.agent.memory,
                app_catalog=self._app_catalog,
                include_github=False)
        current_todo = self.agent.tools.get("todo_write")
        if previous_todo is not None and current_todo is not None:
            current_todo.items = previous_todo.items
        self.agent.refresh_system_prompt()

    def cmd_copy(self, args: list[str]) -> bool:
        """Copy the conversation, or the last answer, to the system clipboard.

        Selecting with the mouse works normally -- wynxo never captures it
        -- but a long conversation is easier to take in one piece than to
        drag over. Built from the session messages, so what you get is plain
        text, not ANSI escapes.
        """
        from .platforms import copy_to_clipboard

        messages = self.agent.session.messages
        only_last = bool(args) and args[0] == "last"
        if only_last:
            blocks = [m.get("content", "") for m in messages
                      if m.get("role") == "assistant" and m.get("content")]
            text = blocks[-1] if blocks else ""
            label = "last answer"
        else:
            lines: list[str] = []
            for message in messages:
                role = message.get("role")
                content = str(message.get("content") or "").strip()
                if role == "user" and content and not content.startswith("/"):
                    lines.append(f"> {content}")
                elif role == "assistant" and content:
                    lines.append(content)
            text = "\n\n".join(lines)
            label = "conversation"
        if not text.strip():
            self.ui.info("nothing to copy yet")
            return True
        if copy_to_clipboard(text):
            self.ui.success(f"copied the {label} ({len(text)} chars) to the clipboard")
        else:
            self.ui.error("could not copy: no clipboard tool on this machine")
        return True

    async def cmd_github(self, args: list[str]) -> bool:
        """Select the GitHub workspace without implying remote execution."""
        action = args[0].strip() if args else "status"
        lowered = action.lower()

        if lowered in ("local", "close"):
            self.gh_ws = None
            self.agent.set_workspace(
                Workspace(provider="local", root=self.workspace))
            self.agent.set_working_mode("code")
            self.config.working_mode = "code"
            self.config.save()
            self.ui.success(f"workspace: local · {self.ui.shorten_path(str(self.workspace))}")
            return True

        if lowered == "status":
            info = getattr(self.agent, "workspace_info", None)
            if info is not None and info.provider == "github":
                self.ui.info(
                    f"workspace: {info.label} · API-only · gh runs on this machine")
            elif self.gh_ws:
                self.ui.info(
                    f"workspace: github:{self.gh_ws['owner']}/{self.gh_ws['repo']}"
                    f"#{self.gh_ws['branch']} · API-only · gh runs on this machine")
            else:
                self.ui.info(
                    f"workspace: local · {self.ui.shorten_path(str(self.workspace))}")
                self.ui.hint("/github owner/repo to select a GitHub workspace")
            return True

        repo = action
        branch = args[1] if len(args) > 1 else ""
        if lowered in ("open", "repo"):
            if len(args) < 2:
                self.ui.warn("usage: /github owner/repo [branch]")
                return True
            repo, branch = args[1], args[2] if len(args) > 2 else ""
        if "/" not in repo:
            self.ui.warn("usage: /github owner/repo [branch]")
            return True

        await self.cmd_gh(["open", repo] + ([branch] if branch else []))
        if not self.gh_ws:
            return True
        selected = self.gh_ws
        self.agent.set_workspace(Workspace(
            provider="github",
            root=self.workspace,
            repository=f"{selected['owner']}/{selected['repo']}",
            branch=selected["branch"],
            execution="local",
            api_only=True,
        ))
        self.agent.set_working_mode("code")
        self.config.working_mode = "code"
        self.config.save()
        self.ui.success(
            f"workspace: github:{selected['owner']}/{selected['repo']}"
            f"#{selected['branch']} · API-only · local shell/filesystem disabled")
        return True

    async def cmd_gh(self, args: list[str]) -> bool:
        """Work on a GitHub repository in the cloud, through the gh CLI.

        Nothing is cloned: the tree, files and branches live on GitHub, and
        every operation goes through the API using the account ``gh auth
        login`` stored. This is the person-sized twin of the github_read /
        github_write tools the agent gets. Edits commit to the workspace
        branch; /gh branch creates one to work on, /gh pr ships it.
        """
        from .gh import GitHubClient, GitHubError

        if self.gh is None:
            self.gh = GitHubClient()
        action = args[0].lower() if args else "status"
        known = {"status", "login", "open", "ls", "cat", "edit",
                 "branch", "pr", "close"}
        if action not in known:
            self.ui.warn(f"unknown /gh action {action}. "
                         "status | login | open | ls | cat | edit | "
                         "branch | pr | close")
            return True
        ws = self.gh_ws

        try:
            if action == "login":
                self.ui.info("run `gh auth login` in a terminal to connect "
                             "your GitHub account, then /gh status here.")
                return True
            if action == "status":
                user = self.gh.auth_user()
                if ws:
                    files = sum(1 for e in ws["tree"]
                                if e.get("type") == "blob")
                    self.ui.info(f"GitHub: {user} · {ws['owner']}/{ws['repo']} "
                                 f"@{ws['branch']} · {files} files")
                else:
                    self.ui.info(f"GitHub: logged in as {user}. No repository "
                                 "open — /gh open owner/repo.")
                return True
            if action == "open":
                if len(args) < 2 or "/" not in args[1]:
                    self.ui.error("usage: /gh open owner/repo [branch]")
                    return True
                owner, repo = args[1].strip().split("/", 1)
                default = self.gh.repo_default_branch(owner, repo)
                branch = args[2] if len(args) > 2 else default
                tree = self.gh.tree(owner, repo, branch)
                self.gh_ws = {"owner": owner, "repo": repo,
                              "branch": branch, "default": default,
                              "tree": tree.entries}
                self.ui.success(
                    f"opened {owner}/{repo} @ {branch} in the cloud "
                    f"({len(tree.files)} files). /gh ls to browse, /gh cat "
                    f"<path> to read, /gh edit <path> to change.")
                if tree.truncated:
                    # The same thing the tool tells the model, told to the
                    # person: a listing this large is not the repository.
                    self.ui.warn(
                        "GitHub truncated this file listing, so it is only "
                        "part of the repository -- /gh ls will not show "
                        "everything.")
                return True
            if ws is None:
                self.ui.error("no repository open — /gh open owner/repo first")
                return True
            owner, repo, branch = ws["owner"], ws["repo"], ws["branch"]
            if action == "ls":
                prefix = args[1] if len(args) > 1 else ""
                lines = self._gh_ls(ws, prefix)
                self.ui.console.print("\n".join(lines) if lines else "nothing here.")
                return True
            if action == "cat":
                if len(args) < 2:
                    self.ui.error("usage: /gh cat <path>")
                    return True
                lines = self.gh.read(owner, repo, args[1], branch).text.splitlines()
                self.ui.console.print("\n".join(lines[:500]))
                if len(lines) > 500:
                    self.ui.info(f"... ({len(lines) - 500} more lines)")
                return True
            if action == "edit":
                if len(args) < 2:
                    self.ui.error("usage: /gh edit <path> [commit message]")
                    return True
                return await self._gh_edit(ws, args[1], " ".join(args[2:]) or None)
            if action == "branch":
                if len(args) < 2:
                    self.ui.error("usage: /gh branch <name>")
                    return True
                head = self.gh.ref_sha(owner, repo, branch)
                self.gh.create_branch(owner, repo, args[1], head)
                self.gh_ws["branch"] = args[1]
                self.ui.success(f"now working on {args[1]} in {owner}/{repo}.")
                return True
            if action == "pr":
                if branch == ws["default"]:
                    self.ui.warn("on the default branch — /gh branch <name> "
                                 "first, then /gh pr.")
                    return True
                title = " ".join(args[1:]) or None
                body = self._gh_pr_body(ws)
                url = self.gh.open_pr(
                    owner, repo, ws["default"], branch,
                    title or f"wynxo: changes on {branch}", body)
                self.ui.success(url)
                return True
            if action == "close":
                self.gh_ws = None
                self.ui.info("closed the cloud workspace.")
                return True
        except GitHubError as exc:
            self.ui.error(str(exc))
            return True
        return True

    def _gh_ls(self, ws: dict, prefix: str) -> list[str]:
        """The tree entries directly under a prefix, directories first."""
        prefix = prefix.strip("/")
        entries = ws["tree"]
        if prefix:
            entries = [e for e in entries
                       if e["path"] == prefix
                       or e["path"].startswith(prefix + "/")]
        lines: list[str] = []
        for entry in sorted(entries,
                            key=lambda e: (e.get("type") != "tree", e["path"])):
            path = entry["path"]
            if path == prefix:
                continue
            rest = path[len(prefix) + 1:] if prefix else path
            if "/" in rest:
                continue
            if entry.get("type") == "tree":
                lines.append(f"{path}/")
            else:
                size = entry.get("size")
                lines.append(f"{path}  ({size} B)" if size else path)
        return lines

    async def _gh_edit(self, ws: dict, path: str, message: str | None) -> bool:
        """Fetch a cloud file into a temp file, open the editor, review the
        diff, and commit back -- nothing goes up blind."""
        import os
        import shlex
        import subprocess
        import tempfile

        from .gh import GitHubError

        owner, repo, branch = ws["owner"], ws["repo"], ws["branch"]
        try:
            blob = self.gh.read(owner, repo, path, branch)
            content, sha = blob.text, blob.sha
        except GitHubError as exc:
            self.ui.error(str(exc))
            return True
        suffix = ".md" if path.endswith((".md", ".markdown")) else ".txt"
        fd, tmp = tempfile.mkstemp(prefix="wynxo-gh-", suffix=suffix)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            editor = (os.environ.get("VISUAL") or os.environ.get("EDITOR"))
            if editor:
                subprocess.run([*shlex.split(editor), tmp])
            elif sys.platform == "win32":
                # notepad returns immediately; Start-Process -Wait blocks
                # until the window is closed.
                subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Start-Process -Wait notepad "
                                "-ArgumentList $args[0]", tmp])
            else:
                subprocess.run(["nano", tmp])
            with open(tmp, encoding="utf-8") as handle:
                updated = handle.read()
            if updated == content:
                self.ui.info("no changes saved.")
                return True
            diff = self._gh_diff(content, updated, path)
            self.ui.console.print()
            self.ui.diff(diff)
            answer = await self._question(
                f"commit {path} to {owner}/{repo} @ {branch}? [y/n]",
                {"y": "yes", "n": "no"}, default="y")
            if answer != "y":
                self.ui.info("nothing committed.")
                return True
            commit = self.gh.write(owner, repo, path, updated,
                                   message or f"wynxo: edit {path}",
                                   branch, sha=sha)
            self.ui.success(f"committed {path} on {branch} ({commit[:10]}).")
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return True

    @staticmethod
    def _gh_diff(before: str, after: str, path: str) -> str:
        """A unified diff of what the editor changed, for the review prompt."""
        import difflib

        before_lines = before.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        lines = list(difflib.unified_diff(
            before_lines, after_lines, fromfile=f"a/{path}",
            tofile=f"b/{path}", n=2))
        return "".join(lines) if lines else "(no text changes)"

    def _gh_pr_body(self, ws: dict) -> str:
        """A PR body listing the commits made on the workspace branch."""
        from .gh import GitHubError

        try:
            messages = self.gh.commits(ws["owner"], ws["repo"], ws["branch"])
        except GitHubError:
            messages = []
        lines = [f"Changes on `{ws['branch']}` → `{ws['default']}`:"]
        lines += [f"- {m.splitlines()[0]}" for m in messages if m.splitlines()]
        return "\n".join(lines) or f"Changes on `{ws['branch']}`."

    def cmd_undo(self, args: list[str]) -> bool:
        if args and args[0] in ("list", "history"):
            history = self.agent.checkpoints.history()
            if not history:
                self.ui.info("nothing to undo")
                return True
            self.ui.table(
                ["file", "changed by"],
                [(s.label or s.path.name, s.tool) for s in history],
                title="undo history (most recent first)",
            )
            return True

        count = 1
        if args and args[0].isdigit():
            count = max(1, min(20, int(args[0])))
        for _ in range(count):
            done, message = self.agent.checkpoints.undo()
            if not done:
                self.ui.info(message)
                break
            self.ui.success(message)
        return True

    async def cmd_speak(self, args: list[str]) -> bool:
        """Turn the voice on and off, and say which synthesiser is doing it."""
        from .speech import (MOMMY_VOICES, Speaker, available, install_edge_tts,
                             install_hint, list_edge_voices,
                             pick as pick_engine)

        action = args[0].lower() if args else ""

        if not action:
            # Bare /speak used to print the status and stop, which told you
            # what the setting was and gave you no way to change it. Every
            # other setting opens a picker; this one offers the actions.
            engines = available()
            if not engines:
                self.ui.warn("No speech synthesiser found on this machine.")
                for line in install_hint().splitlines():
                    self.ui.info(line)
                return True
            edge_tts_missing = not any(
                e.name == "edge-tts" for e in engines)
            menu = [("on", "read answers out loud"),
                    ("off", "stay quiet"),
                    ("test", "say a sentence now, to check it works"),
                    ("engine", f"which synthesiser speaks "
                               f"({len(engines)} available here)"),
                    ("voice", "pick a voice / heard it speak first")]
            if edge_tts_missing:
                menu.append(("install",
                             "get Microsoft's natural voices -- no more robot"))
            chosen = await self._pick(
                "speech", menu,
                "on" if self.config.speak else "off",
            )
            if chosen is None:
                return True
            if chosen is NO_PICKER:
                action = "show"
            else:
                return await self.cmd_speak([chosen])

        if action in ("show", "status"):
            self.ui.info(f"speech: {self.speaker.describe()}")
            options = available()
            if options:
                self.ui.info("available: " + ", ".join(
                    f"{e.name} ({e.quality})" for e in options))
            else:
                self.ui.warn("No speech synthesiser found on this machine.")
                for line in install_hint().splitlines():
                    self.ui.info(line)
            return True

        if action in ("on", "off"):
            want = action == "on"
            if want and self.speaker.engine is None:
                engine = pick_engine(self.config.speech_engine)
                if engine is None:
                    self.ui.warn("No speech synthesiser found on this machine.")
                    for line in install_hint().splitlines():
                        self.ui.info(line)
                    return True
                self.speaker = Speaker(engine, voice=self.config.speech_voice,
                                       rate=self.config.speech_rate,
                                       model=self.config.speech_model)
            self.speaker.enabled = want
            if not want:
                self.speaker.stop()
            self.config.speak = want
            self.config.save()
            self.ui.success(f"speech: {self.speaker.describe()}")
            return True

        if action == "test":
            if not self.speaker.say("Hello. If you can hear this, the voice works."):
                self.ui.warn("Nothing was said. " +
                             (self.speaker.last_error or "Speech is off."))
                return True
            self.ui.success(f"spoke through {self.speaker.describe()}")
            return True

        if action == "engine" and len(args) == 1:
            options = [(e.name, e.quality) for e in available()]
            if not options:
                self.ui.warn("No speech synthesiser found on this machine.")
                for line in install_hint().splitlines():
                    self.ui.info(line)
                return True
            picked = await self._pick("speech engine", options,
                                      self.config.speech_engine)
            if picked is None:
                return True
            if picked is NO_PICKER:
                self.ui.info("available: " + ", ".join(n for n, _ in options))
                return True
            args = [action, picked]

        if action == "voice" and len(args) == 1:
            # With edge-tts around, a voice is a person-shaped choice: offer
            # the warm picks, then every female neural voice Microsoft has.
            # Without it, it is just an engine-specific name to type.
            if any(e.name == "edge-tts" for e in available()):
                with self.ui.status("fetching Microsoft's voices..."):
                    all_voices = await list_edge_voices()
                curated = dict(MOMMY_VOICES)
                options = list(MOMMY_VOICES)
                options += [(n, h) for n, h in all_voices if n not in curated]
                current = self.config.speech_voice or "en-US-JennyNeural"
                picked = await self._pick("voice", options, current)
                if picked is None:
                    return True
                if picked is NO_PICKER:
                    typed = await self._type_in(
                        "voice name (blank to cancel):", current)
                    if not typed:
                        return True
                    args = [action, typed]
                else:
                    args = [action, picked]
            else:
                typed = await self._type_in("voice name (blank to cancel):",
                                            self.config.speech_voice or "")
                if not typed:
                    return True
                args = [action, typed]

        if action == "install":
            if any(e.name == "edge-tts" for e in available()):
                self.ui.info("edge-tts is already here.")
                return await self.cmd_speak(["engine", "edge-tts"])
            self.ui.info("installing Microsoft's neural voices...")
            ok, detail = install_edge_tts()
            if not ok:
                self.ui.warn(f"could not install edge-tts: {detail}")
                return True
            self.ui.success("installed -- the natural voices are ready.")
            self.config.speak = True
            self.config.speech_engine = "edge-tts"
            self.speaker = Speaker(
                pick_engine("edge-tts"), voice=self.config.speech_voice,
                rate=self.config.speech_rate, model=self.config.speech_model)
            self.speaker.enabled = True
            self.config.save()
            self.ui.info(f"speech: {self.speaker.describe()}  "
                         f"(/speak voice to pick which)")
            return True

        if action in ("engine", "voice") and len(args) > 1:
            if action == "engine":
                engine = pick_engine(args[1])
                if engine is None:
                    self.ui.warn(f"{args[1]} is not available here.")
                    return True
                self.config.speech_engine = args[1]
                self.speaker = Speaker(engine, voice=self.config.speech_voice,
                                       rate=self.config.speech_rate,
                                       model=self.config.speech_model)
            else:
                self.config.speech_voice = args[1]
                self.speaker.voice = args[1]
            self.config.save()
            self.speaker.enabled = self.config.speak
            self.ui.success(f"speech: {self.speaker.describe()}")
            return True

        self.ui.info("/speak on | off | test | engine <name> | voice <name>")
        return True

    async def cmd_talker(self, args: list[str]) -> bool:
        """Set the small model that does the talking, or turn it off."""
        from .duo import Talker
        from .prompts import VOICES

        if not args:
            # Bare /talker used to name the current one and then tell you the
            # syntax for changing it. Ask the server what it has and offer
            # that instead -- a talker is a model, and you cannot pick a
            # model you cannot remember the tag of.
            try:
                with self.ui.status("asking the server what it has..."):
                    models = await self.client.list_models()
            except ProviderError as exc:
                self.ui.error(str(exc))
                return True

            options = [("off", "one model does both jobs")]
            for model in models:
                if model.name == self.config.model:
                    continue        # the coder cannot also be the talker
                hint = " ".join(part for part in (model.human_size(),
                                                   model.parameter_size,
                                                   model.quantization) if part)
                options.append((model.name, hint or "installed"))
            if len(options) == 1:
                self.ui.info("no other model on that server to talk with")
                return True

            chosen = await self._pick("talker", options,
                                      self.config.talker or "off")
            if chosen is None:
                return True
            if chosen is NO_PICKER:
                if self.talker is None:
                    self.ui.info("no talker -- one model does both jobs")
                    self.ui.info("/talker <model> to have a small one do the talking")
                else:
                    self.ui.info(f"talker: {self.talker.model}   "
                                 f"coder: {self.config.model}")
                return True
            args = [chosen]

        if args[0].lower() in ("off", "none"):
            self.talker = None
            self.config.talker = ""
            self.ui.success("talker off -- one model does both jobs")
            return True

        self.config.talker = args[0]
        self.talker = Talker(self.client, args[0],
                             voice_block=VOICES.get(self.config.voice, ""))
        self.ui.success(f"talker: {args[0]}   coder: {self.config.model}")
        return True

    async def cmd_theme(self, args: list[str]) -> bool:
        from . import theme as theme_module

        if not args:
            options = [(n, _theme_summary(n)) for n in theme_module.names()]
            chosen = await self._pick("theme", options, self.config.theme)
            if chosen is None:
                return True
            if chosen is NO_PICKER:
                self.ui.table(
                    ["theme", "look"],
                    [(n + ("  <-" if n == self.config.theme else ""), summary)
                     for n, summary in options],
                    title="themes",
                )
                self.ui.info("/theme <name> to change it")
                return True
            if chosen == self.config.theme:
                self.ui.info(f"already using {chosen}")
                return True
            args = [chosen]

        choice = args[0].lower()
        if choice not in theme_module.names():
            self.ui.warn(f"unknown theme; choose one of "
                         f"{', '.join(theme_module.names())}")
            return True
        self.config.theme = choice
        self.config.save()
        if choice == "minimal":
            # Reduced motion: no animated faces, no waveforms, no effects.
            # The interface stays fully usable -- just still.
            if self.config.animations:
                self.config.animations = False
                self.config.save()
            self.pet.animate = False
        else:
            # Leaving minimal restores whatever motion was configured.
            self.pet.animate = self.config.animations
        # Rebind now so the rest of this session picks it up, and say plainly
        # that anything already drawn keeps the old colours.
        from .ui import apply_palette

        self.ui.palette = theme_module.resolve(choice)
        apply_palette(self.ui.palette)
        self.ui.success(f"theme: {choice}")
        self._preview_theme()
        return True

    async def cmd_animate(self, args: list[str]) -> bool:
        """Turn motion on or off, or show the companion's states.

        Deterministic by design: the gallery is one still frame per state
        printed once, not a live loop, so the command never owns the screen
        or needs a timer of its own.

        There used to be a separate list of "scenes" here with their own
        names, frame rates and loop flags -- a vocabulary that existed only
        for this command, describing pictures the session never drew. The
        states the companion actually has are the only ones worth showing.
        """
        if args and args[0].lower() in ("on", "off"):
            on = args[0].lower() == "on"
            self.config.animations = on
            self.config.save()
            self.pet.animate = on
            if self.bar is not None:
                self.bar.animate = on
            self.ui.success(f"animations {'on' if on else 'off'}"
                            + ("  (reduced motion)" if not on else ""))
            return True

        # "list" is the whole gallery, which is also what no argument
        # means -- and it is the word the command's own description offers.
        # Passed through as a state name it matched none of them, so the
        # documented spelling was the one that failed.
        wanted = args[0].lower() if args else ""
        if wanted in ("list", "states", "all"):
            wanted = ""
        self._show_states(wanted)
        self.ui.info(
            f"one state: /animate <state>   motion: "
            f"{'on' if self.config.animations else 'off'}"
            + ("  (/animate off for reduced motion)"
               if self.config.animations else ""))
        return True

    async def cmd_todo(self, args: list[str]) -> bool:
        """Show the current plan."""
        todo = self.agent.tools.get("todo_write")
        rendered = todo.render() if todo and hasattr(todo, "render") else ""
        self.ui.todos(rendered) if rendered else self.ui.info("no plan yet")
        return True

    async def cmd_queue(self, args: list[str]) -> bool:
        """Show what is waiting, run it, or drop it.

        Type-ahead is collected while a turn runs and normally drains the
        moment it ends, so this is for the case where it did not: a Ctrl-C
        holds the rest of the queue rather than launching it, and then there
        has to be a way to see what is held and decide about it.
        """
        want = args[0].lower() if args else ""
        if want in ("clear", "drop", "empty"):
            dropped = self.pending.clear()
            self.ui.success(dropped or "nothing was queued")
            return True
        if want in ("run", "go", "resume"):
            if not self.pending.summary():
                self.ui.info("nothing queued")
                return True
            return await self._drain_queue()
        # "show" is what /queue advertises and what a person types when they
        # want the listing rather than a decision about it. It was the one
        # word of the three in its own description that this rejected.
        if args and want not in ("show", "list", "what"):
            self.ui.warn(f"/queue takes show, run or clear -- not {args[0]!r}")
            return True
        waiting = self.pending.summary()
        if not waiting:
            self.ui.info("nothing queued")
            return True
        self.ui.console.print()
        for index, message in enumerate(waiting, start=1):
            self.ui.console.print(
                Text(f"  {index}. ", style=MUTED) + Text(message))
        self.ui.console.print()
        self.ui.info("/queue run to send them, /queue clear to drop them")
        return True

    async def _question(self, question: str, answers: dict[str, str],
                        default: str = "") -> str:
        """Ask a short question.

        Every question in the session comes through here, so the escaping
        and the cancelled-vs-default rule can only be got wrong once.

        Returns the key that was answered, or "" for cancelled.
        """
        try:
            typed = (await self.prompt_session.prompt_async(
                HTML(f'<style fg="{ACCENT}">  {_escape(question)} </style>')
            )).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return ""
        finally:
            # prompt_toolkit's teardown removes the loop's SIGINT handler.
            # Without this, Ctrl-C is dead for the rest of the command that
            # asked the question.
            self._arm_interrupt()
        if not typed:
            return default
        for key, meaning in answers.items():
            if typed in (key, meaning) or typed[0] == key:
                return key
        return ""

    async def _type_in(self, question: str, default: str = "") -> str:
        """Read a line of free text."""
        try:
            return (await self.prompt_session.prompt_async(
                HTML(f'<style fg="{ACCENT}">  {_escape(question)} </style>'),
                default=default)).strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        finally:
            self._arm_interrupt()

    async def _pick(self, title: str, options: list[tuple[str, str]],
                    current: str) -> str | None:
        """Arrow-key chooser for a simple setting.

        Returns the chosen value, None if the user pressed escape, or
        NO_PICKER when this terminal cannot draw one. Cancelling and being
        unable to offer a choice are different things: escape means "never
        mind", and printing the table anyway ignores that.
        """
        choices = [
            option if isinstance(option, Choice) else
            Choice(value=option[0],
                   label=option[0],
                   badge="current" if option[0] == current else "",
                   badge_style="badge",
                   hint=option[1])
            for option in options
        ]
        if not arrows_supported():
            return NO_PICKER
        try:
            return await choose(
                choices,
                title=title,
                default=next((i for i, c in enumerate(choices)
                              if c.value == current), 0),
                footer=HINT if self.ui.g.unicode else HINT_ASCII,
                width=self.ui.width,
                unicode=self.ui.g.unicode,
            )
        finally:
            # The chooser is a prompt_toolkit Application too, and its
            # teardown takes the SIGINT handler with it.
            self._arm_interrupt()

    async def cmd_stream(self, args: list[str]) -> bool:
        """Watch a tool call being written, or do not.

        Ollama's native tool-calling parses the model's output before
        handing it over -- the arguments on the wire are a parsed map, not
        a string, so a half-written call cannot be represented and the
        whole thing arrives in one message once the parser has finished
        it. There is nothing partial to show. Revealing a finished string
        slowly would be an animation pretending to be a stream.

        Asked for as text instead, a tool call is ordinary content, and
        ordinary content streams: the command, or the file being written,
        appears a character at a time exactly as the answer does.

        A real trade, so it is a choice rather than a default. Native calls
        are parsed by the server against the model's own template and are
        the sturdier path on a tool-tuned model. What the other one buys,
        besides being watchable, is a shorter prompt -- the same tools cost
        about 3,400 tokens as JSON schemas and 1,300 described in prose --
        and on a model reading its prompt at CPU speed that is most of a
        minute off every request.
        """
        want = args[0].lower() if args else ""
        if want in ("yes", "show", "live"):
            want = "on"
        elif want in ("no", "hide"):
            want = "off"
        now = "on" if self.config.stream_tool_calls else "off"
        if want not in ("on", "off"):
            chosen = await self._pick(
                "tool calls",
                [("on", "watch the command or the file being written, and "
                        "send a shorter prompt"),
                 ("off", "let the server parse tool calls -- sturdier, but "
                         "they arrive finished")],
                now,
            )
            if chosen is NO_PICKER:
                self.ui.info(f"watching tool calls is {now}  "
                             f"{self.ui.g.dot}  /stream on | off")
                return True
            if chosen is None:
                return True
            want = chosen
        if want == now:
            return True
        self.config.stream_tool_calls = want == "on"
        self.config.save()
        # Detection reads the setting, so this is what actually moves the
        # agent onto the other path -- and it refreshes the system prompt,
        # which is where the tool descriptions live when they are not sent
        # as schemas.
        await self.agent.detect_capabilities()
        if want == "on":
            self.ui.success("tool calls stream as they are written")
            self.ui.info("the model writes them as text now, which is also "
                         "about 1,900 fewer tokens a request")
        else:
            self.ui.success("tool calls are parsed by the server")
            self.ui.info("sturdier, and they arrive finished rather than "
                         "being written in front of you")
        return True

    async def cmd_thinking(self, args: list[str]) -> bool:
        """Show or hide the model's reasoning.

        A picker rather than a bare toggle. Every other setting opens one,
        and a toggle is the one shape that cannot tell you what it is about
        to do: you press it to find out, and if it was already what you
        wanted you have to press it twice more to get back. With two named
        options the current one is marked and choosing it again is a no-op.
        """
        want = args[0].lower() if args else ""
        if want in ("show", "yes"):
            want = "on"
        elif want in ("hide", "no"):
            want = "off"
        elif want in ("all", "history", "replay"):
            # Nothing was thrown away while it was hidden, so there is always
            # a full record to go back over.
            self.callbacks._open_thinking(whole_session=True)
            if not self.callbacks._thinking_turns and not self.callbacks._thinking_buffer:
                self.ui.info("nothing thought yet this session")
            return True
        if want not in ("on", "off"):
            chosen = await self._pick(
                "thinking",
                [("on", "show the model's reasoning as it works"),
                 ("off", "keep only the answer; ^O reveals the reasoning "
                         "for one turn"),
                 ("all", "replay everything thought this session")],
                "on" if self.ui.show_thinking else "off",
            )
            if chosen is NO_PICKER:
                state = "on" if self.ui.show_thinking else "off"
                self.ui.info(f"thinking display is {state}  {self.ui.g.dot}  "
                             "/thinking on | off")
                return True
            if chosen is None:
                return True
            if chosen == "all":
                self.callbacks._open_thinking(whole_session=True)
                return True
            want = chosen

        self.ui.show_thinking = want == "on"
        self.config.show_thinking = self.ui.show_thinking
        self.config.save()
        self.ui.info(f"thinking display {'on' if self.ui.show_thinking else 'off'}")
        if self.ui.show_thinking and not self.policy.thinking:
            self.ui.warn(f"{self.policy.name} effort does not ask the model to "
                         "think, so there will be nothing to show. "
                         "/effort high or above turns reasoning on.")
        return True

    async def cmd_secrets(self, args: list[str]) -> bool:
        shield = self.agent.shield

        if args and args[0].lower() == "allow":
            if len(args) < 2:
                self.ui.info("/secrets allow <path>  -- let the agent read "
                             "one file it would otherwise refuse")
                return True
            target = " ".join(args[1:])
            shield.allow(target)
            self.ui.warn(f"{target} can now be read this session. It will be "
                         "sent to the model like any other file.")
            return True

        want = args[0].lower() if args else ""
        if want not in ("on", "off"):
            chosen = await self._pick(
                "secrets",
                [("on", "refuse .env files and private keys; mask credentials "
                        "found inside ordinary files, and in the session log"),
                 ("off", "no filtering -- every file goes to the model as-is")],
                "on" if self.config.protect_secrets else "off",
            )
            if chosen is NO_PICKER:
                state = "on" if self.config.protect_secrets else "off"
                self.ui.info(f"secret protection is {state}  {self.ui.g.dot}  "
                             "/secrets on | off | allow <path>")
                return True
            if chosen is None:
                return True
            want = chosen

        enabled = want == "on"
        self.config.protect_secrets = enabled
        self.config.save()
        shield.enabled = enabled
        if enabled:
            self.ui.success("credentials will be kept out of the model and "
                            "the log")
        else:
            # Warned rather than confirmed. The endpoint may well be another
            # machine, so this is a decision about what leaves this one.
            self.ui.warn("secret protection off -- .env files, keys and "
                         "tokens will be sent to the model verbatim and "
                         "written to the session log")
        return True

    def _preview_theme(self) -> None:
        """Show the new colours immediately, on this line.

        A theme change that only affects text drawn later looks like it did
        nothing -- the whole screen above is still the old palette, so
        without a sample there is nothing to see.
        """
        from rich.text import Text as _T

        row = _T("  ")
        # Straight off the palette, not the module-level names: cli.py
        # already has a WARN, and it is the status tag, not a colour.
        palette = self.ui.palette
        for index, (label, style) in enumerate((
                ("accent", palette.accent), ("text", palette.text),
                ("muted", palette.muted), ("ok", palette.good),
                ("warn", palette.warn), ("error", palette.bad))):
            # No leading pad on the first swatch: it put the row one cell
            # off the margin every other line in the conversation keeps.
            if index:
                row.append("  ")
            row.append(label, style=f"bold {style}")
        self.ui.console.print(row)
        self.ui.console.print()

    def cmd_log(self, args: list[str]) -> bool:
        action = args[0].lower() if args else "show"

        if action in ("off", "on"):
            self.journal.enabled = action == "on"
            self.config.log = self.journal.enabled
            self.config.save()
            self.ui.success(f"logging {action}")
            return True

        if action in ("list", "sessions"):
            logs = recent_logs()
            if not logs:
                self.ui.info("no logs yet")
                return True
            self.ui.table(
                ["log", "size"],
                [(p.name, f"{p.stat().st_size // 1024}KB") for p in logs],
                title="recent sessions",
            )
            return True

        if action == "tail":
            for record in self.journal.tail(20):
                kind = record.get("kind", "?")
                body = (record.get("text") or record.get("message")
                        or record.get("name") or "")
                self.ui.console.print(
                    f"  [{MUTED}]{kind:12}[/] {str(body)[:90]}")
            return True

        if not self.journal.enabled or self.journal.path is None:
            self.ui.info(f"logging is off  {self.ui.g.dot}  /log on to enable it")
            return True
        self.ui.info(f"{self.journal.path}")
        dot = self.ui.g.dot
        self.ui.info(f"{self.journal.size() // 1024}KB  {dot}  /log tail  {dot}  "
                     f"/log list  {dot}  /log off")
        return True

    def _show_states(self, only: str = "") -> None:
        """Every state the companion has, drawn by the renderer that draws
        it during a turn.

        Side by side, as many across as the terminal has room for: the
        companion is six rows tall, so a row each would be seventy-two
        lines to show twelve of them.

        Deliberately not a preview built from its own copy of the frames.
        There used to be one of those -- a whole module wrapping the scene
        tables a second time so /animate could show them -- and a gallery
        that renders differently from the real thing is worse than none,
        because it is the one you check when the real thing looks wrong.
        """
        from . import sprite
        from .companion import State

        states = [s for s in State if not only or s.value == only]
        if not states:
            self.ui.warn(f"no such state: {only}")
            self.ui.info("states: "
                         + " | ".join(s.value for s in State))
            return
        gap = STATE_GAP
        columns = gallery_columns(self.ui.width)
        for start in range(0, len(states), columns):
            group = states[start:start + columns]
            # The second frame, where there is one. Several states differ
            # from idle only in the part that moves -- the paws come up to
            # the keyboard, the progress bar starts filling -- so a gallery
            # of first frames shows three identical cats.
            art = [sprite.rows(state, 1, self.ui.palette) for state in group]
            for row in range(sprite.HEIGHT):
                line = Text("  ")
                for block in art:
                    line.append_text(block[row])
                    line.append(gap)
                self.ui.console.print(line)
            labels = Text("  ")
            for state in group:
                labels.append(f"{state.value:<{sprite.WIDTH}}", style=MUTED)
                labels.append(gap)
            self.ui.console.print(labels)
            self.ui.console.print()

    async def cmd_pet(self, args: list[str]) -> bool:
        from .prompts import VOICES

        if not args:
            self.ui.console.print()
            self._show_states()
            # The states are worth seeing, so they stay. What used to follow
            # them was a line of usage text -- it told you what you could
            # type and left you to type it. The picker acts instead.
            chosen = await self._pick(
                "companion",
                [("on", "draw her beside the live status"),
                 ("off", "the words alone (the default)"),
                 ("animate", "let her move"),
                 ("still", "one frame, held"),
                 ("name", f"what to call her (now: {self.pet.name})"),
                 ("voice", f"how she writes (now: {self.config.voice})")],
                ("on" if self.pet.enabled else "off"),
            )
            if chosen is None:
                return True
            if chosen is NO_PICKER:
                self.ui.info(f"name: {self.pet.name}   voice: {self.config.voice}   "
                             f"{'on' if self.pet.enabled else 'off'}"
                             f"{'' if self.pet.animate else ', still'}")
                self.ui.info(f"/pet off {self.ui.g.dot} /pet name <x> "
                             f"{self.ui.g.dot} /pet voice " + " | ".join(VOICES))
                return True
            return await self.cmd_pet([chosen])

        action = args[0].lower()

        if action in ("on", "off"):
            self.pet.enabled = action == "on"
            self.config.pet = self.pet.enabled
            self.config.save()
            self.ui.success(f"pet {action}")
            return True

        if action in ("still", "animate"):
            self.pet.animate = action == "animate"
            self.config.animations = self.pet.animate
            self.config.save()
            self.ui.success(f"animation {'on' if self.pet.animate else 'off'}")
            return True

        if action == "show":
            wanted = args[1].lower() if len(args) > 1 else ""
            self._show_states(wanted)
            return True

        if action == "name" and len(args) == 1:
            typed = await self._type_in("what should she be called?",
                                        self.pet.name)
            if not typed:
                return True
            args = [action, typed]

        if action == "name" and len(args) > 1:
            self.pet.name = " ".join(args[1:])[:24]
            self.config.pet_name = self.pet.name
            self.config.save()
            self.ui.success(f"hello, {self.pet.name}")
            return True

        if action == "voice":
            if len(args) < 2:
                options = [(v, _voice_summary(v)) for v in VOICES]
                picked = await self._pick("voice", options, self.config.voice)
                if picked is None:
                    return True
                if picked is NO_PICKER:
                    self.ui.table(
                        ["voice", "sounds like"],
                        [(v + ("  <-" if v == self.config.voice else ""),
                          _voice_summary(v)) for v in VOICES],
                        title="voice",
                    )
                    return True
                if picked == self.config.voice:
                    self.ui.info(f"already using {picked}")
                    return True
                args = [action, picked]
            choice = args[1].lower()
            if choice not in VOICES:
                self.ui.warn(f"unknown voice; choose one of {', '.join(VOICES)}")
                return True
            self._set_voice(choice)
            return True

        self.ui.warn("usage: /pet [on|off|still|animate|show <mood>|name <x>|voice <x>]")
        return True

    def _set_voice(self, choice: str) -> None:
        """Switch how the agent talks, everywhere the voice lives.

        One call instead of the four places a voice change must land in:
        the saved config, the pet's style, the talker's persona, and the
        agent's system prompt. Missing one of them means the new voice is
        spoken by half the program.
        """
        from .prompts import VOICES

        self.config.voice = choice
        self.config.save()
        self.pet.style_name = ("kawaii" if choice == "kawaii"
                               else "mommy" if choice == "mommy"
                               else "default")
        if self.talker is not None:
            self.talker.voice_block = VOICES.get(choice, "")
        self.agent.refresh_system_prompt()
        self.ui.success(f"voice: {choice} -- {_voice_summary(choice)}")

    def cmd_mommy(self, args: list[str]) -> bool:
        """Mommy-style talking, on or off; no argument toggles.

        A single-word convenience over the full voice picker: on is the
        doting mommy persona, off is the plain professional voice. The
        engineering underneath is identical either way.
        """
        current = self.config.voice
        if not args or args[0].lower() in ("toggle", "t"):
            want = "plain" if current == "mommy" else "mommy"
        else:
            action = args[0].lower()
            if action in ("on", "mommy"):
                want = "mommy"
            elif action in ("off", "plain", "default"):
                want = "plain"
            else:
                self.ui.warn(
                    f"usage: /mommy [on|off]  (now: {_voice_summary(current)})")
                return True
        if want == current:
            self.ui.info(
                f"mommy style already {'on' if want == 'mommy' else 'off'}")
            return True
        self._set_voice(want)
        return True

    def cmd_memory(self, args: list[str]) -> bool:
        memory = self.agent.memory

        if not args or args[0] in ("show", "list"):
            project, user = memory.counts()
            if not project and not user:
                self.ui.info("nothing remembered yet")
                self.ui.info("the agent writes here itself; or: /memory add <note>")
                return True
            if user:
                self.ui.table(["about you"], [(e.lstrip("-* "),)
                                              for e in memory.user.entries()])
            if project:
                self.ui.table(["about this project"], [(e.lstrip("-* "),)
                                                       for e in memory.project.entries()])
            self.ui.info(f"{memory.project.path}")
            return True

        action, rest = args[0], " ".join(args[1:]).strip()

        if action in ("add", "remember") and rest:
            scope = "user" if rest.startswith("user:") else "project"
            note = rest.split(":", 1)[1].strip() if rest.startswith("user:") else rest
            added, message = memory.remember(note, scope, explicit=True)
            self.ui.success(f"remembered: {message}") if added else self.ui.info(message)
            self.agent.refresh_system_prompt()
            return True

        if action in ("forget", "remove") and rest:
            count, message = memory.forget(rest, explicit=True)
            if not count:
                count, message = memory.forget(rest, "user", explicit=True)
            self.ui.info(message)
            self.agent.refresh_system_prompt()
            return True

        if action == "edit":
            path = memory.project.path
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(memory.project.header, encoding="utf-8")
            self.ui.info(f"open it in your editor: {path}")
            self.ui.info("changes are picked up with /memory reload")
            return True

        if action == "reload":
            self.agent.refresh_system_prompt()
            project, user = memory.counts()
            self.ui.success(f"reloaded: {project} project, {user} user")
            return True

        self.ui.warn("usage: /memory [show | add <note> | forget <text> | edit | reload]")
        return True

    CTX_SIZES = (4096, 8192, 16384, 32768, 65536, 131072)
    """The window sizes worth offering. Powers of two because that is what
    every model is trained and every runner tuned for."""

    async def cmd_ctx(self, args: list[str]) -> bool:
        if not args:
            return await self._pick_context()
        try:
            value = int(args[0])
        except ValueError:
            self.ui.warn("usage: /ctx <number>")
            return True
        self.config.num_ctx = value
        self.agent.config.num_ctx = value
        self.ui.success(f"num_ctx = {value}")
        if warning := await check_context(self.client, self.config):
            self.ui.warn(warning)
        return True

    async def _pick_context(self) -> bool:
        """Choose the context window from a list rather than be told the number.

        Bare /ctx used to report the setting and stop -- which is the one
        thing you already knew, since you asked. The sizes are the ones
        worth having; the model's own maximum is marked when the server
        told us what it is, because going past it costs memory and buys
        nothing.
        """
        used = self.agent.session.token_estimate()
        native = 0
        info = getattr(self.agent, "model_info", None)
        if info is not None:
            native = getattr(info, "context_length", 0) or 0

        sizes = sorted(set(self.CTX_SIZES) | {self.config.num_ctx}
                       | ({native} if native else set()))
        options = []
        for size in sizes:
            note = []
            if native and size == native:
                note.append("the model's own window")
            elif native and size > native:
                note.append(f"past the model's {native}")
            if size < 8192:
                note.append("too small for real work")
            if used and size < used:
                note.append(f"smaller than the {used} tokens already in use")
            options.append((str(size), ", ".join(note) or f"{size // 1024}k tokens"))
        options.append(("custom", "type an exact number"))

        chosen = await self._pick("context window", options,
                                  str(self.config.num_ctx))
        if chosen is None:
            return True
        if chosen is NO_PICKER:
            self.ui.info(
                f"num_ctx={self.config.num_ctx}, roughly {used} tokens in use "
                f"({100 * used / max(1, self.config.num_ctx):.0f}%)"
            )
            return True
        if chosen == "custom":
            typed = await self._type_in("context window, in tokens:",
                                        str(self.config.num_ctx))
            if not typed:
                return True
            chosen = typed
        return await self.cmd_ctx([chosen])

    def cmd_context(self, args: list[str]) -> bool:
        """Show the small set of state useful for diagnosing a turn."""
        metrics = getattr(self.client, "metrics", None)
        last = getattr(metrics, "last", None) if metrics is not None else None
        info = getattr(self.agent, "workspace_info", None)
        rows = [
            ("mode", getattr(self.agent, "working_mode", "code")),
            ("workspace", info.label if info is not None else
             self.ui.shorten_path(str(self.workspace))),
            ("tools", str(len(self.agent.tools))),
            ("requests this turn", str(getattr(metrics, "requests_per_turn", 0))),
        ]
        if last is not None:
            rows.extend([
                ("last model", last.model),
                ("last request", last.request_kind),
                ("prompt estimate", f"~{last.prompt_tokens_estimate:,} tokens"),
                ("time to first token",
                 f"{last.time_to_first_token_ms:.0f} ms"
                 if last.time_to_first_token_ms is not None else "n/a"),
                ("generation", f"{last.tokens_per_second:.1f} tok/s"
                 if last.tokens_per_second is not None else "n/a"),
                ("tools supplied", "yes" if last.tools_supplied else "no"),
            ])
        self.ui.table(["", ""], rows, title="context")
        return True

    def cmd_stats(self, args: list[str]) -> bool:
        usage = self.agent.session.usage
        used = self.agent.session.token_estimate()
        limit, set_by = self._context_limit()
        limit = max(1, limit)
        self.ui.table(
            ["", ""],
            [
                ("model", self.config.model),
                ("server", self.client.base_url),
                ("effort", f"{self.policy.name} -- {self.policy.describe()}"),
                ("requests", str(usage.requests)),
                ("tool calls", str(usage.tool_calls)),
                ("prompt tokens", str(usage.prompt_tokens)),
                ("output tokens", str(usage.completion_tokens)),
                ("speed", f"{usage.tokens_per_second():.1f} tok/s"),
                ("context", f"~{used:,} of {limit:,} tokens "
                             f"({100 * used // limit}%)", f"set by {set_by}"),
                ("compactions", str(self.agent.session.compactions)),
                ("reclaimed", f"~{self.agent.session.superseded_chars // 4} tokens"
                              " of superseded reads and test runs"),
                ("tool mode", "native" if self.agent.native_tools else "hermes (prompted)"),
                ("session", self.agent.session.session_id),
            ],
            title="session",
        )
        return True


def read_piped_stdin(grace: float = 0.25) -> str:
    """Read piped stdin, or return "" -- but never block waiting for a writer.

    A plain ``sys.stdin.read()`` here hangs forever whenever stdin is an open
    pipe that nobody writes to, which is the normal state of affairs under CI,
    process supervisors and editor terminals. The agent then sits there with no
    output and no explanation, which looks exactly like a crash.
    """
    if sys.stdin.isatty():
        return ""

    try:
        if stat.S_ISREG(os.fstat(sys.stdin.fileno()).st_mode):
            # A redirect from a real file always has an EOF coming.
            return sys.stdin.read().strip()
    except (OSError, ValueError):
        return ""

    # Gated on the platform, not on hasattr: Windows *has* select.select, it
    # just cannot use it on a pipe. Testing for the attribute sent Windows
    # down this branch, where the call raised and the input was dropped -- so
    # `git diff | wynxo -p "review"` silently reviewed nothing.
    if os.name != "nt":
        try:
            ready, _, _ = select.select([sys.stdin], [], [], grace)
        except (OSError, ValueError):
            return ""
        if not ready:
            return ""
        try:
            return sys.stdin.read().strip()
        except (OSError, UnicodeDecodeError):
            return ""

    # Windows: ask the pipe itself whether anything is waiting.
    #
    # The obvious alternative -- read in a thread and abandon it after the
    # grace period -- leaves a thread blocked in a read on stdin for the rest
    # of the process's life. That is not merely untidy: the handle stays
    # busy, and anything that later waits on this process's pipes can wait
    # for it forever.
    return _windows_pipe_text(grace)


def _windows_pipe_text(grace: float) -> str:
    """Whatever is already in the pipe, without blocking on it.

    PeekNamedPipe reports how many bytes are available and returns
    immediately either way, which is the thing select cannot do for a pipe
    on Windows. Nothing waiting means nothing piped.
    """
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
    except (ImportError, AttributeError):
        return ""

    try:
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    except (OSError, ValueError):
        return ""

    peek = ctypes.windll.kernel32.PeekNamedPipe
    peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD),
                     ctypes.POINTER(wintypes.DWORD),
                     ctypes.POINTER(wintypes.DWORD)]
    available = wintypes.DWORD(0)

    # Polled rather than asked once: a producer that is a fraction of a
    # second behind would otherwise look like an empty pipe.
    deadline = time.monotonic() + max(0.0, grace)
    while True:
        try:
            ok = peek(handle, None, 0, None, ctypes.byref(available), None)
        except OSError:
            return ""
        if not ok:
            return ""          # closed, or not a pipe at all
        if available.value:
            break
        if time.monotonic() >= deadline:
            return ""
        time.sleep(0.02)

    try:
        return sys.stdin.read().strip()
    except (OSError, UnicodeDecodeError, ValueError):
        return ""


async def run_once(config: Config, workspace: Path, ui: UI, prompt: str,
                   scope: Scope = Scope.FOLDER, mode: Mode = Mode.YOLO,
                   json_output: bool = False) -> int:
    """Non-interactive mode: answer one prompt and exit.

    With ``json_output`` the whole run is silent and a single JSON object
    -- answer, errors, usage -- is printed on stdout, for scripts.
    """
    import io
    import json as _json

    from rich.console import Console

    if json_output:
        # Tool progress and stage lines are chat, not data. Swap the
        # console for a sink so stdout carries nothing but the JSON.
        ui.console = Console(file=io.StringIO())

    client = make_client(config)
    try:
        await client.ping()
    except ProviderError as exc:
        if json_output:
            print(_json.dumps({"ok": False, "content": "", "errors": [str(exc)]}))
        else:
            ui.error(str(exc))
        await client.aclose()
        return 1

    callbacks = TerminalCallbacks(ui)
    agent = Agent(client, config, resolve(config.effort), workspace, callbacks,
                  boundary=resolve_scope(workspace, scope), memory=Memory(workspace))
    # No human to answer prompts, so nothing can be asked; the caller opted
    # into that by using -p. An explicit --mode still wins, so `-p --mode plan`
    # gives a read-only run.
    agent.permissions.mode = mode
    agent.refresh_system_prompt()
    await agent.detect_capabilities()

    try:
        result = await agent.run(prompt)
    except ProviderError as exc:
        if json_output:
            print(_json.dumps({"ok": False, "content": "", "errors": [str(exc)]}))
        else:
            ui.error(str(exc))
        await client.aclose()
        return 1
    callbacks._end_stream()
    usage = agent.session.usage
    await client.aclose()

    if json_output:
        print(_json.dumps({
            "ok": not result.errors,
            "content": result.content,
            "errors": result.errors,
            "model": config.model,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "requests": usage.requests,
                "tool_calls": usage.tool_calls,
            },
        }))
        return 0 if not result.errors else 1

    if result.errors:
        ui.error("\n".join(result.errors))
        return 1
    if result.content and not config.stream:
        ui.assistant_markdown(result.content)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wynxo",
        description="A terminal coding agent for local models via Ollama.",
    )
    parser.add_argument("prompt", nargs="*", help="run one prompt and exit")
    parser.add_argument("-p", "--print", action="store_true",
                        help="non-interactive: answer the prompt and exit")
    parser.add_argument("--json", action="store_true",
                        help="with -p: print the answer as one JSON object "
                             "on stdout, nothing else")
    parser.add_argument("-e", "--effort", choices=list(ORDER), help="effort level for this run")
    parser.add_argument("-m", "--model", help="model to use")
    parser.add_argument("--endpoint", help="Ollama URL, e.g. http://homelab:11434")
    parser.add_argument("--ctx", type=int, help="context window size (num_ctx)")
    parser.add_argument("-C", "--cwd", help="project directory (default: here)")
    parser.add_argument("--repo", metavar="OWNER/NAME",
                        help="clone a GitHub repository and work in it")
    parser.add_argument("--talker", metavar="MODEL",
                        help="small model that talks while the coder works")
    parser.add_argument("--speak", action="store_true",
                        help="read answers out loud")
    parser.add_argument("--no-speak", action="store_true",
                        help="stay quiet even if speech is on in the config")
    parser.add_argument("--setup", action="store_true", help="re-run first-time setup")
    parser.add_argument("--doctor", action="store_true",
                        help="check the server and model, and report what will not work")
    parser.add_argument("--ascii", metavar="IMAGE",
                        help="turn a picture into ASCII art and print it")
    parser.add_argument("--ascii-width", type=int, default=100,
                        metavar="N", help="columns wide (default 100)")
    parser.add_argument("--ascii-style", default="detail",
                        choices=("detail", "simple", "blocks"),
                        help="character ramp to draw with")
    parser.add_argument("--ascii-invert", action="store_true",
                        help="for a light terminal, where the ramp reads "
                             "the other way round")
    parser.add_argument("--no-stream", action="store_true", help="wait for the full response")
    parser.add_argument("--no-thinking", action="store_true", help="hide model reasoning")
    parser.add_argument("--yolo", action="store_true",
                        help="never ask permission (same as --mode yolo)")
    parser.add_argument("--mode", choices=[m.value for m in Mode],
                        help="how much it asks first: plan, manual, auto, yolo")
    parser.add_argument("--scope", choices=[s.value for s in Scope],
                        help="what it may touch: folder, repo, machine")
    parser.add_argument("--version", action="version", version=f"wynxo {__version__}")
    return parser


def apply_flags(config, args) -> None:
    """Let this run's flags override the saved settings.

    Its own function so it can be checked directly. The interesting rules
    are the ones where two flags interact, and those are exactly the ones
    that are impossible to test through main().
    """
    if args.model:
        config.model = args.model
    if args.effort:
        config.effort = args.effort
    if args.ctx:
        config.num_ctx = args.ctx
    if args.no_stream:
        config.stream = False
    if args.no_thinking:
        config.show_thinking = False
    if args.talker:
        config.talker = args.talker
    if args.speak:
        config.speak = True
    if args.no_speak:
        config.speak = False


async def amain(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    if not workspace.is_dir():
        print(f"No such directory: {workspace}", file=sys.stderr)
        return 1

    ui = UI(show_thinking=not args.no_thinking)

    # Before the configuration gate on purpose: turning a local picture into
    # text needs no model and no server, so being unconfigured is irrelevant
    # to it and asking the user to run setup first would be nonsense.
    if args.ascii:
        return _print_ascii(args, ui)

    if args.repo:
        from . import repo as repo_module

        if not repo_module.git_available():
            print("git is not installed, so --repo cannot clone anything.",
                  file=sys.stderr)
            return 1
        target = repo_module.parse(args.repo)
        if target is None:
            print(f"{args.repo!r} does not look like a repository. "
                  "Try owner/name or a GitHub URL.", file=sys.stderr)
            return 1
        with ui.status(f"fetching {target.slug}..."):
            ok, path, message = repo_module.clone_or_update(target)
        if not ok:
            ui.error(message)
            return 1
        ui.success(message)
        workspace = path

    # An endpoint supplied on the command line or in the environment is enough
    # to run: do not drag the user through setup for something they answered.
    endpoint_supplied = bool(
        args.endpoint or os.environ.get("WYNXO_ENDPOINT") or os.environ.get("OLLAMA_HOST")
    )
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if args.setup or (not is_configured() and not endpoint_supplied):
        if not interactive:
            print(
                "wynxo is not configured yet, and there is no terminal to ask on.\n"
                "Either run `wynxo --setup` interactively, or point it at a server:\n"
                "  wynxo --endpoint homelab:11434 -p \"your prompt\"\n"
                "  WYNXO_ENDPOINT=homelab:11434 wynxo -p \"your prompt\"",
                file=sys.stderr,
            )
            return 1
        try:
            config = await run_wizard(ui)
        except (KeyboardInterrupt, EOFError):
            ui.console.print()
            ui.info("setup cancelled")
            return 1
    else:
        config = load(workspace)

    if args.endpoint:
        url = normalise_url(args.endpoint)
        config.endpoints = [Endpoint(name="cli", url=url)]
        config.active_endpoint = "cli"
    apply_flags(config, args)
    ui.show_thinking = config.show_thinking

    if args.doctor:
        return await run_doctor(config, ui)

    prompt = " ".join(args.prompt).strip()

    # Piped input becomes context for the prompt: `git diff | wynxo -p "review"`.
    piped = read_piped_stdin()
    if piped:
        prompt = (
            f"{prompt}\n\n<piped-input>\n{piped}\n</piped-input>"
            if prompt
            else f"Here is some input:\n\n<piped-input>\n{piped}\n</piped-input>"
        )
        args.print = True  # nothing to be interactive with

    if prompt and args.print:
        once_mode = Mode.parse(args.mode) if args.mode else Mode.YOLO
        once_scope = Scope.parse(args.scope) if args.scope else Scope.FOLDER
        return await run_once(config, workspace, ui, prompt, once_scope,
                              once_mode, json_output=args.json)

    mode = Mode.parse(args.mode) if args.mode else Mode.MANUAL
    if args.yolo:
        mode = Mode.YOLO
    scope = Scope.parse(args.scope) if args.scope else Scope.FOLDER

    repl = Repl(config, workspace, ui, scope=scope, mode=mode)
    if mode is Mode.YOLO:
        ui.warn("Nothing will ask for approval: the agent writes and runs freely.")
    if scope is Scope.MACHINE:
        ui.warn("Machine scope: no path restriction. The agent can reach anything "
                "your user account can.")

    # Ctrl-C cancels the running turn rather than killing the process.
    loop = asyncio.get_running_loop()
    if sys.platform == "win32":
        # No add_signal_handler on Windows. The handler runs in the main thread,
        # so hop back onto the loop before touching the task.
        signal.signal(
            signal.SIGINT,
            lambda *_: loop.call_soon_threadsafe(repl.interrupt),
        )
    else:
        try:
            loop.add_signal_handler(signal.SIGINT, repl.interrupt)
        except NotImplementedError:
            pass

    # raw=True is load-bearing. patch_stdout routes everything through
    # prompt_toolkit's Vt100_Output.write, which replaces every ESC byte with
    # "?" as an escape-injection guard -- turning every colour code from rich
    # and from the status lines into literal "?[1;32m" garbage on screen.
    # write_raw passes them through, which is what a terminal UI needs.
    #
    # Output goes to the terminal's own scrollback and the mouse is never
    # captured, so scrolling, selecting and copying are the terminal's own
    # -- no alternate screen, no mouse reporting, nothing for the user to
    # fight.
    with patch_stdout(raw=True):
        if prompt:
            return await repl.start_with(prompt)
        return await repl.start()


def _print_ascii(args, ui: UI) -> int:
    """Print a picture as text.

    Deliberately not part of a turn: it is a local conversion of a local
    file, so it neither needs a model nor sends the image anywhere.
    """
    from . import asciiart

    source = Path(args.ascii).expanduser()
    if not source.is_file():
        ui.error(f"{source} is not a file.")
        return 1
    width = max(20, min(400, args.ascii_width))
    try:
        art = asciiart.from_image(source, width=width, style=args.ascii_style,
                                  invert=args.ascii_invert)
    except asciiart.ImageSupportMissing as exc:
        ui.error(str(exc))
        return 1
    except (OSError, ValueError) as exc:
        ui.error(f"Could not read {source.name}: {exc}")
        return 1
    # Straight to stdout, unstyled: this is meant to be redirected into a
    # file and pasted into a banner, and rich would wrap and colour it.
    print(art)
    return 0


def _write_crash_report(exc: BaseException) -> "Path | None":
    """Put the traceback somewhere it can be read later."""
    import traceback

    try:
        directory = data_dir() / "crashes"
        directory.mkdir(parents=True, exist_ok=True)
        import time as _time

        path = directory / f"{_time.strftime('%Y%m%d-%H%M%S')}.txt"
        path.write_text(
            f"wynxo {__version__}\n"
            f"python {sys.version}\n"
            f"platform {sys.platform}\n\n"
            + "".join(traceback.format_exception(exc)),
            encoding="utf-8")
        return path
    except Exception:
        return None


def main() -> None:
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except BaseException as exc:                       # noqa: BLE001
        # Last resort. Everything inside a session is already guarded, so
        # reaching here means start-up broke or something got past all of
        # it -- and a raw Python traceback is a bug report the person
        # reading it cannot act on. Say what happened in one line, keep the
        # traceback on disk for when it is actually wanted.
        report = _write_crash_report(exc)
        print("\nwynxo hit an unexpected error and had to stop.",
              file=sys.stderr)
        print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)
        if report is not None:
            print(f"\n  The full details are in {report}", file=sys.stderr)
            print("  That file is worth attaching to a bug report.",
                  file=sys.stderr)
        print("  Nothing you had open was written to; run wynxo again to "
              "carry on.\n", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
