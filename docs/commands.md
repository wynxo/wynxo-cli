# Commands and workflows

## Work context and autonomy

The work context and autonomy are intentionally separate.

- /chat — CHAT: ordinary conversation with an empty tool registry.
- /work — WORK: full local agent tool registry.
- /github — choose the local/GitHub workspace used by WORK.
- /mode plan|ask|auto|review|yolo — how much autonomy WORK has.

The old /code spelling remains a compatibility alias for /work.

ASK maps to the legacy internal MANUAL policy. The public vocabulary uses ASK
because it describes the behaviour rather than the implementation.

## Computer use

WORK includes `launch_application` plus two computer-use tools when the
platform can support them. `computer_info` reads screen size or the active
window without changing anything. `computer_control` can move/click the
pointer, type literal text, send shortcuts and scroll.

This is deterministic input control, not visual screen understanding. Wynxo
must not invent coordinates or claim it can see a button. On Wayland, WORK
prefers wdotool and falls back to ydotool + ydotoold when available; on X11
it can use xdotool. The read-only computer_info helper currently needs an
X11-compatible xdotool path, while input control can still work on Wayland.
/tools explains any withheld backend. Desktop input prompts for permission in
ASK, AUTO and REVIEW unless you explicitly allow it for the session; YOLO
remains the explicit no-prompt mode.

## Context

/context
: show current context and request diagnostics.

/context window
: choose a context window.

/context window 32768
: set an exact window.

/context compact
: compact older conversation state while preserving the objective, decisions,
changed files, command outcomes and recent exact messages.

The old /ctx spelling remains compatible.

## GitHub

/github status
: current local/GitHub workspace.

/github open owner/repo [branch]
: work through the GitHub API workspace.

/github clone owner/repo
: clone locally and move Wynxo into that checkout.

/github local
: return from an API workspace to local files.

/github api ...
: advanced direct GitHub operations such as ls, cat, edit, branch and pr.

The old /repo and /gh spellings map into these workflows.

## Verification and changes

/diff
: show the changed-file list and line counts.

/diff full
: include the patch.

/diff staged
: staged changes only.

/test
: run the smallest affected pytest set when one can be identified, otherwise
fall back to the project's detected test command.

/test affected
: require a focused test set.

/test full
: run the full detected test command.

Automatic post-edit verification follows the same focused-first strategy.

## Undo and checkpoints

/undo
: revert the last agent turn as one transaction. A multi-file turn is reverted
as a unit.

/checkpoint before-refactor
: name the current undo position.

/restore before-refactor
: revert everything after that checkpoint.

Individual file snapshots still protect user edits: a restore refuses to
overwrite a file that drifted after Wynxo touched it.

## Worktrees

/worktree list
: list Git worktrees.

/worktree new auth-refactor
: create branch wynxo/auth-refactor in a sibling worktree.

/worktree open auth-refactor
: move the agent into it.

/worktree remove auth-refactor
: remove the secondary worktree without force.

Worktrees are optional isolation for large tasks, not a requirement for normal
editing.

## Conversations

/resume
: picker for saved conversations.

/resume parser
: search titles, previews and workspace paths.

/new
: new session id, new history and cleared undo state; durable memory survives.

/clear
: clear only the terminal screen. It does not erase the conversation.

## Persona and speech

/voice warm
: text persona.

/speak voice
: synthesizer/TTS voice.

The old /mommy shortcut maps to the text persona and is no longer a separate
product concept.

## Discovery

Typing / shows the small canonical command palette. /help shows the same front
door. /help advanced reveals power-user controls and /help aliases documents
legacy spellings.
