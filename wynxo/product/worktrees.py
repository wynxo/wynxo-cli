"""Git worktree helpers for isolated agent tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..repo import run_git
from ..scope import git_root

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str = ""
    branch: str = ""


def _root(workspace: Path) -> Path | None:
    try:
        root = git_root(workspace)
    except Exception:
        return None
    return root.resolve() if root else None


def list_worktrees(workspace: Path) -> list[Worktree]:
    root = _root(workspace)
    if root is None:
        return []
    ok, output = run_git(["worktree", "list", "--porcelain"], cwd=root, timeout=20)
    if not ok:
        return []
    rows: list[Worktree] = []
    current: dict[str, str] = {}
    for line in output.splitlines() + [""]:
        if not line.strip():
            if current.get("worktree"):
                rows.append(Worktree(
                    path=Path(current["worktree"]),
                    head=current.get("HEAD", ""),
                    branch=current.get("branch", "").removeprefix("refs/heads/"),
                ))
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return rows


def create(workspace: Path, name: str, base: str = "HEAD") -> tuple[bool, Path | None, str]:
    root = _root(workspace)
    if root is None:
        return False, None, "this workspace is not a Git repository"
    name = name.strip()
    if not _SAFE.fullmatch(name):
        return False, None, "worktree name must use letters, numbers, dot, dash or underscore"
    branch = f"wynxo/{name}"
    target = root.parent / f"{root.name}-{name}"
    if target.exists():
        return False, target, f"{target} already exists"
    ok, output = run_git(
        ["worktree", "add", "-b", branch, str(target), base],
        cwd=root,
        timeout=120,
    )
    if not ok:
        return False, None, output.strip() or "git worktree add failed"
    return True, target, f"created {branch} at {target}"


def remove(workspace: Path, target: str) -> tuple[bool, str]:
    root = _root(workspace)
    if root is None:
        return False, "this workspace is not a Git repository"
    wanted = Path(target).expanduser()
    matches = list_worktrees(root)
    if not wanted.is_absolute():
        by_name = [
            row.path for row in matches
            if row.path.name == target or row.branch == target or row.branch == f"wynxo/{target}"
        ]
        if len(by_name) == 1:
            wanted = by_name[0]
        else:
            wanted = (root.parent / target).resolve()
    else:
        wanted = wanted.resolve()
    if wanted == root:
        return False, "the primary worktree cannot be removed here"
    ok, output = run_git(["worktree", "remove", str(wanted)], cwd=root, timeout=120)
    return (True, f"removed {wanted}") if ok else (False, output.strip() or "git worktree remove failed")
