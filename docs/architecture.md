# Architecture

## Principle

The execution core and the installed product surface are different layers.

Core modules such as agent.py, provider.py, permissions.py, session.py,
checkpoints.py and tools/ own execution and invariants. The product package
owns public command vocabulary, terminal rendering, local model observations
and optional workflow conveniences.

## Single composition root

wynxo/product/__init__.py is the installed product composition root.

Older presentation experiments remain importable for compatibility and tests,
but bootstrap no longer stacks product_ui, clean_ui, experience and help_ui in
an order-dependent monkey-patch chain. The installed CLI chooses one final UI,
callback, completer and REPL class in one place.

## Product package

- product/commands.py — canonical commands, aliases, work/autonomy model,
  transaction-level undo, session search and worktree workflow.
- product/render.py — flat terminal UI and the callback adapter.
- product/events.py — typed product events between execution and presentation.
- product/health.py — bounded local observations for model diagnostics.
- product/worktrees.py — Git worktree isolation helpers.

This is the first extraction from the historically large cli.py. New product
features should prefer focused modules instead of growing cli.py again.

## Events

The core still exposes its stable callback protocol. ProductCallbacks translates
that protocol into typed product events while delegating execution rendering to
the mature callback implementation. This gives future renderers a stable event
vocabulary without replacing the agent loop in one risky migration.

## Safety boundaries

permissions.py remains authoritative for mutations. Work context
(CHAT/CODE/GITHUB) is not a permission level. Autonomy
(PLAN/ASK/AUTO/REVIEW/YOLO) is not a workspace selector. Keeping them orthogonal
prevents a UI switch from silently widening what the agent may change.

## Verification

The agent already tracks changed-file checkpoints and prefers focused pytest
commands when a relevant set can be identified. Product commands expose the
same strategy through /test.

## Direction

The 0.2 series is intentionally feature-frozen around consolidation. Prefer:

1. reliability and tests,
2. coherent command semantics,
3. smaller explicit modules,
4. terminal calmness and performance,
5. deletion of duplicate concepts,

before adding another subsystem.
