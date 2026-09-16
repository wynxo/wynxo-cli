"""Focused command discovery for the terminal product.

The command registry is intentionally broad, but a broad registry should not
mean a wall of controls. This layer keeps `/help` small, makes individual
commands directly searchable, and leaves the registry/dispatcher itself
untouched.
"""

from __future__ import annotations

from typing import Iterable

_INSTALLED = False
_PREV_HELP = None


CORE = (
    "/chat", "/code", "/github", "/model", "/context", "/resume",
    "/cd", "/test", "/diff", "/commit", "/clear", "/quit",
)

KEYS = (
    ("Ctrl-C", "stop the current turn; press again at an idle prompt to exit"),
    ("Ctrl-O", "show or hide model reasoning while a turn is running"),
    ("Ctrl-T", "show or hide detailed tool output"),
    ("Ctrl-D", "show or hide full diffs"),
    ("Ctrl-R", "dictate a message when speech recognition is enabled"),
    ("Tab", "complete slash commands, options, models, and @file references"),
)


def _normalise_query(query: str) -> str:
    value = (query or "").strip().lower()
    if value and not value.startswith("/"):
        value = "/" + value
    return value


def find_commands(cli_mod, query: str, limit: int = 8):
    """Commands matching a name, typo, description, or product-intent word."""
    query = _normalise_query(query)
    if not query:
        return []

    canonical = cli_mod.resolve_command(query)
    if canonical in cli_mod.REGISTRY:
        return [cli_mod.REGISTRY[canonical]]

    names: list[str] = []

    def add(name: str) -> None:
        if name in cli_mod.REGISTRY and name not in names:
            names.append(name)

    # The product completion layer already knows human intent words such as
    # "llm", "chatter", and "repo". Give those intentional matches priority;
    # fuzzy spelling recovery is a fallback and can otherwise put a merely
    # similar command (for example /pull for "llm") ahead of the intended one.
    try:
        from . import experience

        for name in experience._semantic_commands(cli_mod, query):
            add(name)
    except Exception:  # noqa: BLE001 - help must never take down the prompt
        pass

    for name in cli_mod.suggest_commands(query, limit=max(limit, 12)):
        add(name)

    stem = query.lstrip("/")
    if len(stem) >= 2:
        for command in cli_mod.COMMAND_LIST:
            haystack = f"{command.name} {command.does}".lower()
            if stem in haystack:
                add(command.name)

    return [cli_mod.REGISTRY[name] for name in names[:limit]]


def aliases_for(cli_mod, command: str) -> tuple[str, ...]:
    return tuple(alias for alias, target in cli_mod.ALIASES.items()
                 if target == command)


def option_text(command) -> str:
    if not command.values:
        return ""
    return " | ".join(command.values)


def _rows(commands: Iterable) -> list[tuple[str, str]]:
    return [(command.name, command.does) for command in commands]


def _command_help(repl, cli_mod, command) -> None:
    repl.ui.table(["command", "what it does"], [(command.name, command.does)],
                  title="command")
    if command.values:
        repl.ui.hint(f"Options: {option_text(command)}")
    aliases = aliases_for(cli_mod, command.name)
    if aliases:
        repl.ui.hint("Short forms: " + "  ".join(aliases))


def _cmd_help(self, args: list[str]) -> bool:
    """Small front door, with direct lookup instead of a command wall."""
    from . import cli as cli_mod

    query = " ".join(args).strip()
    lowered = query.lower()

    if lowered in {"keys", "key", "keyboard", "shortcuts"}:
        self.ui.table(["key", "does"], KEYS, title="keyboard")
        return True

    if lowered in {"all", "advanced", "commands"}:
        self.ui.table(
            ["command", "what it does"],
            _rows(cli_mod.COMMAND_LIST),
            title="all commands",
        )
        self.ui.hint("Tip: /help <command> shows just one command and its options")
        return True

    if query:
        matches = find_commands(cli_mod, query)
        if len(matches) == 1:
            _command_help(self, cli_mod, matches[0])
            return True
        if matches:
            self.ui.table(["command", "what it does"], _rows(matches),
                          title=f"help: {query}")
            return True
        self.ui.warn(f"No command matches {query!r}")
        suggestions = find_commands(cli_mod, "/" + query.lstrip("/"), limit=4)
        if suggestions:
            self.ui.hint("Try: " + "  ".join(c.name for c in suggestions))
        return True

    core = [cli_mod.REGISTRY[name] for name in CORE if name in cli_mod.REGISTRY]
    self.ui.table(["command", "what it does"], _rows(core), title="commands")
    self.ui.hint("/help <command>  details    /help keys  shortcuts    /help all  everything")
    return True


def install() -> None:
    global _INSTALLED, _PREV_HELP
    if _INSTALLED:
        return

    from . import cli as cli_mod

    _PREV_HELP = cli_mod.Repl.cmd_help
    cli_mod.Repl.cmd_help = _cmd_help
    _INSTALLED = True
