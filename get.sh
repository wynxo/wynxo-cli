#!/usr/bin/env sh
# One-line install for wynxo (Linux, macOS, Termux).
#
#   curl -fsSL https://raw.githubusercontent.com/wynxo/wynxo-cli/main/get.sh | sh
#
# Clones into ~/.wynxo-src (or updates it if already there), then runs the
# real installer. Everything it does is visible below -- it is deliberately
# short, because nobody reads a long script they are about to pipe to a shell.

set -eu

REPO="https://github.com/wynxo/wynxo-cli"
DEST="${WYNXO_SRC:-$HOME/.wynxo-src}"

say() { printf '  %s\n' "$*"; }

command -v git >/dev/null 2>&1 || {
    say "git is required and was not found."
    say "  Debian/Ubuntu:  sudo apt install git"
    say "  macOS:          xcode-select --install"
    say "  Termux:         pkg install git"
    exit 1
}

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        PY="$candidate"; break
    fi
done
[ -n "$PY" ] || {
    say "Python 3.10 or newer is required and was not found."
    say "  Debian/Ubuntu:  sudo apt install python3 python3-venv"
    say "  macOS:          brew install python"
    say "  Termux:         pkg install python"
    exit 1
}

if [ -d "$DEST/.git" ]; then
    say "updating $DEST"
    git -C "$DEST" pull --ff-only --quiet || say "could not update; using what is there"
else
    say "cloning into $DEST"
    git clone --quiet --depth 1 "$REPO" "$DEST"
fi

# The installer needs a terminal to ask its questions, and piping this script
# into sh consumes stdin. Reconnect to the controlling terminal when there is
# one -- testing -r is not enough, since /dev/tty exists but cannot be opened
# when the process has no controlling terminal at all.
if (exec < /dev/tty) 2>/dev/null; then
    exec "$PY" "$DEST/install.py" "$@" < /dev/tty
fi
say "no terminal to ask on; accepting the defaults"
exec "$PY" "$DEST/install.py" --yes "$@"
