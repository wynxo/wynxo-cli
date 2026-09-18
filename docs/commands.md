# Commands and workflows

## Work context and autonomy

The work context and autonomy are intentionally separate.

- /chat — CHAT: conversation, no project tools.
- /code — CODE: local project tools.
- /github — GITHUB: selected GitHub repository.
- /mode plan|ask|auto|review|yolo — how much autonomy the active work context has.

ASK maps to the legacy internal MANUAL policy. The public vocabulary uses ASK
because it describes the behaviour rather than the implementation.

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
