#!/usr/bin/env sh
# One-line uninstall for wynxo (Linux, macOS, Termux).
#
#   curl -fsSL https://raw.githubusercontent.com/wynxo/wynxo-cli/main/rm.sh | sh
#
# Uses the uninstaller from the local checkout when there is one, and
# downloads it otherwise -- so this still works for an install made before
# uninstall.py existed, and for one whose checkout is already damaged.

set -eu

DEST="${WYNXO_SRC:-$HOME/.wynxo-src}"
RAW="https://raw.githubusercontent.com/wynxo/wynxo-cli/main/uninstall.py"

say() { printf '  %s\n' "$*"; }

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        PY="$candidate"; break
    fi
done
[ -n "$PY" ] || {
    say "Python 3.10 or newer is required and was not found."
    say "Remove these by hand instead:"
    say "  $DEST"
    say "  ~/.local/bin/wynxo"
    say "  ~/.config/wynxo  and  ~/.local/share/wynxo"
    say '  the "# added by wynxo installer" line in your shell profile'
    exit 1
}

SCRIPT="$DEST/uninstall.py"
CLEANUP=""
if [ ! -f "$SCRIPT" ]; then
    # No local copy (an older install, or a broken checkout). Fetch one into
    # a temp file -- deliberately not into $DEST, which is about to be
    # deleted and may not even exist.
    TMP="${TMPDIR:-/tmp}/wynxo-uninstall.$$.py"
    FETCHED=0
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$RAW" -o "$TMP" && FETCHED=1
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$TMP" "$RAW" && FETCHED=1
    else
        say "Need curl or wget to fetch the uninstaller."
        exit 1
    fi
    if [ "$FETCHED" -ne 1 ]; then
        rm -f "$TMP"
        say "Could not download the uninstaller from GitHub."
        say ""
        say "Remove these by hand instead:"
        say "  $DEST"
        say "  ~/.local/bin/wynxo"
        say "  ~/.config/wynxo  and  ~/.local/share/wynxo"
        say '  the "# added by wynxo installer" line in your shell profile'
        exit 1
    fi
    SCRIPT="$TMP"
    CLEANUP="$TMP"
fi

# The installer's questions need a terminal, and piping this script into sh
# has already consumed stdin. Reconnect when there is a terminal to use.
if (exec < /dev/tty) 2>/dev/null; then
    "$PY" "$SCRIPT" "$@" < /dev/tty || STATUS=$?
else
    say "no terminal to ask on; accepting the defaults"
    "$PY" "$SCRIPT" --yes "$@" || STATUS=$?
fi

[ -n "$CLEANUP" ] && rm -f "$CLEANUP"
exit "${STATUS:-0}"
