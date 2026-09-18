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
| CHAT | Normal conversation. Zero tools and zero project context. |
| WORK | Full agent. Files, shell, tests, web, GitHub and available computer-control tools. |

| Autonomy | Meaning |
| --- | --- |
| PLAN | Read-only. Investigate and propose. |
| ASK | Ask before writes and commands. |
| AUTO | Edit inside scope; still ask before commands. |
| REVIEW | Work freely, then review the entire turn as one patch. |
| YOLO | Do not ask for approval. |

Workspace is a separate choice inside WORK: local files by default, or a GitHub
API workspace selected with /github. That means a session can be WORK · REVIEW,
WORK · AUTO or CHAT. Use /chat and /work for the top-level mode and /mode for
autonomy. /code remains a compatibility alias for /work.

## Everyday commands

| Command | What it does |
| --- | --- |
| /chat | normal conversation; zero tools |
| /work | full agent with every available tool |
| /github | choose the GitHub workspace used by WORK |
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

Old spellings such as /code, /ctx, /repo, /gh, /sessions, /todo, /yolo and /mommy
remain compatibility aliases, but they no longer compete in the main command
palette.


### Advanced commands

The main palette stays small, but the full power-user surface is still documented
and available through `/help advanced`: `/animate`, `/apps`, `/cd`,
`/commit`, `/compact`, `/copy`, `/dictate`, `/effort`, `/endpoint`,
`/init`, `/log`, `/map`, `/memory`, `/pet`, `/plan`, `/pull`,
`/queue`, `/quit`, `/review`, `/scope`, `/secrets`, `/stats`,
`/stream`, `/talker`, `/theme`, `/thinking`, and `/tools`.

### Windows policy-safe fallback

If Windows App Control, Device Guard, or execution policy blocks a generated
launcher or PowerShell activation, do not activate the virtual environment.
From the repository root, invoke its interpreter directly:

~~~powershell
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m wynxo
~~~

This also makes it obvious which interpreter owns the installation.

## A normal coding turn

~~~text
WYNXO  0.2.0  local AI
WORK | REVIEW  qwen3-coder:30b  main  ~/code/my-project

> fix the retry bug and verify it

✓ search  retry
✓ edit    src/client.py
✓ testing tests/test_client.py

1 file · +9 -3 · tests 14 passed · 4 tools · 18.7s
~~~

REVIEW mode then presents the turn's combined patch once. Keep it, revert the
whole turn, or step through files.

WORK also exposes deterministic desktop control when the platform supports it:
`computer_info` reads screen size/active-window state and `computer_control`
can move/click, type, send shortcuts and scroll. On Linux this uses xdotool;
when xdotool is missing the capability is shown as withheld instead of failing
mysteriously. Desktop input is permission-gated like a command and does not
pretend to provide visual screen understanding.

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
