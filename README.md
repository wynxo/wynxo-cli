# Wynxo

**A local AI assistant in your terminal. Chat normally. Give it a project when you want it to work.**

Wynxo runs against Ollama and keeps the agent loop, project tools, session history,
memory, permissions and model observations on your machine. It is useful as a
normal conversational assistant and as a serious coding agent without making
those two jobs feel like separate applications.

## Install

Windows PowerShell:

~~~powershell
irm https://raw.githubusercontent.com/wynxo/wynxo-cli/main/get.ps1 | iex
~~~

Linux, macOS and Termux:

~~~bash
curl -fsSL https://raw.githubusercontent.com/wynxo/wynxo-cli/main/get.sh | sh
~~~

Have Ollama running with at least one model, then open a project and run:

~~~bash
cd ~/code/my-project
wynxo
~~~

Python 3.10+ is required. Wynxo itself uses pure-Python runtime dependencies.

## The product model

Two independent choices describe a session.

| Work | Meaning |
| --- | --- |
| CHAT | Conversation only. Project tools are off. |
| CODE | Work against the local project. |
| GITHUB | Work against a selected GitHub repository. |

| Autonomy | Meaning |
| --- | --- |
| PLAN | Read-only. Investigate and propose. |
| ASK | Ask before writes and commands. |
| AUTO | Edit inside scope; still ask before commands. |
| REVIEW | Work freely, then review the entire turn as one patch. |
| YOLO | Do not ask for approval. |

That means a session can be CODE · REVIEW, GITHUB · ASK, CHAT · ASK, and so on.
Use /chat, /code or /github for the work context and /mode for autonomy.

## Everyday commands

| Command | What it does |
| --- | --- |
| /chat | tool-free conversation |
| /code | local project agent |
| /github | GitHub workspace, clone or API workflow |
| /mode | PLAN / ASK / AUTO / REVIEW / YOLO |
| /model | installed-model capability and local performance view |
| /context | status, context window and compaction |
| /resume | search and continue saved conversations |
| /new | create a genuinely new conversation |
| /clear | clear only the terminal screen |
| /diff | compact change summary; add full for the patch |
| /test | affected tests first; add full for the full suite |
| /undo | revert the last agent turn as one transaction |
| /checkpoint | name the current undo position |
| /restore | return to a named checkpoint |
| /worktree | isolate a larger task in a Git worktree |
| /voice | text persona; speech voices remain under /speak |
| /doctor | diagnose Ollama/model setup and show actionable fixes |
| /help | the small command surface; /help advanced for everything else |

Old spellings such as /ctx, /repo, /gh, /sessions, /todo, /yolo and /mommy
remain compatibility aliases, but they no longer compete in the main command
palette.

## A normal coding turn

~~~text
WYNXO  0.2.0  local AI
CODE | REVIEW  qwen3-coder:30b  main  ~/code/my-project

> fix the retry bug and verify it

✓ search  retry
✓ edit    src/client.py
✓ testing tests/test_client.py

1 file · +9 -3 · tests 14 passed · 4 tools · 18.7s
~~~

REVIEW mode then presents the turn's combined patch once. Keep it, revert the
whole turn, or step through files.

## Local-first does not mean reckless

Reads are cheap. Mutations are controlled by autonomy and scope. Remote GitHub
writes still require an explicit decision. Secret protection is on by default.
Undo checkpoints refuse to overwrite files that changed after the agent edited
them.

Wynxo never silently switches models. /model can show capabilities, placement
and locally observed speed so you can choose.

## More documentation

- docs/install.md — platforms, Ollama and troubleshooting
- docs/commands.md — canonical commands, aliases and workflows
- docs/models.md — model capabilities, context and performance observations
- docs/architecture.md — product/core boundary and repository layout

## Development

~~~bash
python -m pytest -q
ruff check .
~~~

The 0.2 line is a polish/consolidation release: prefer coherence, performance,
tests and removal of duplicate product concepts over adding unrelated features.

MIT license.
