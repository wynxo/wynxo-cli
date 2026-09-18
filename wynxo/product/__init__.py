"""Single composition root for the installed Wynxo product.

Core execution modules stay importable and testable on their own. The installed
command applies product policy once here instead of stacking runtime UI patches
whose final behaviour depends on import order.
"""

from __future__ import annotations

_INSTALLED = False
_ORIGINAL_SMALL_TALK = None


def _install_conversation_policy() -> None:
    global _ORIGINAL_SMALL_TALK

    from .. import agent, experience, prompts

    prompts.CHAT_PROMPT = experience.CHAT_PROMPT
    old_identity = experience._OLD_CODE_IDENTITY
    new_identity = experience._NEW_CODE_IDENTITY
    if old_identity in prompts.BASE:
        prompts.BASE = prompts.BASE.replace(old_identity, new_identity, 1)

    agent._TASK_SIGNAL = experience._PRODUCT_TASK_SIGNAL
    if _ORIGINAL_SMALL_TALK is None:
        _ORIGINAL_SMALL_TALK = agent.is_small_talk
    original = _ORIGINAL_SMALL_TALK

    def is_small_talk(request: str) -> bool:
        return experience.extra_conversation(request) or original(request)

    agent.is_small_talk = is_small_talk


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from .. import cli
    from .commands import ProductCommandCompleter, ProductRepl, install_commands
    from .render import ProductCallbacks, ProductUI

    _install_conversation_policy()
    install_commands(cli)

    # One explicit product boundary. No product_ui -> clean_ui -> experience ->
    # help_ui patch chain: the installed CLI has one final class for each role.
    cli.UI = ProductUI
    cli.TerminalCallbacks = ProductCallbacks
    cli.CommandCompleter = ProductCommandCompleter
    cli.Repl = ProductRepl

    _INSTALLED = True
