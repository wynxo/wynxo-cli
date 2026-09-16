"""Obvious general-assistant turns should not pay for an intent model call."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from wynxo.experience import extra_conversation


@pytest.mark.parametrize("text", [
    "explain black holes",
    "write me a poem about winter",
    "translate this sentence to German",
    "recommend a scary movie",
    "brainstorm birthday gift ideas",
    "compare coffee and tea",
    "what is photosynthesis?",
    "how does gravity work?",
    "who was Ada Lovelace?",
])
def test_obvious_general_requests_skip_classification(text):
    assert extra_conversation(text) is True


@pytest.mark.parametrize("text", [
    "open firefox",
    "run the tests",
    "fix this bug",
    "write parser.py",
    "find the config file",
    "create an app",
    "make a website",
    "what does this function do?",
    "where is config?",
])
def test_project_or_system_shaped_requests_stay_on_the_router(text):
    assert extra_conversation(text) is False


def test_long_writing_request_is_still_obviously_conversation():
    text = "write me a short story about a lighthouse. " + ("quiet sea " * 80)
    assert len(text) > 120
    assert extra_conversation(text) is True


def test_installed_router_uses_the_fast_path():
    """The product layer must actually reach Agent's routing function."""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            from wynxo import agent, experience
            experience.install()
            assert agent.is_small_talk('write me a poem')
            assert agent.is_small_talk('what is photosynthesis?')
            assert not agent.is_small_talk('write parser.py')
            assert not agent.is_small_talk('open firefox')
        """)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
