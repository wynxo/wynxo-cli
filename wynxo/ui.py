"""Terminal rendering.

Windows note: rich handles the VT sequences, but the box-drawing and arrow
glyphs used elsewhere in TUIs do not survive the default Windows console
font. Everything here sticks to ASCII plus a small set of glyphs that are
checked against the active encoding at startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
import time
from functools import lru_cache
from typing import Iterable

from rich.box import ASCII as ASCII_BOX, ROUNDED
from rich.cells import cell_len
from rich.console import COLOR_SYSTEMS, Console, Control, Group
from rich.live import Live
from rich.rule import Rule
from rich.text import Text

from .platforms import is_narrow, terminal_width
from . import pacing
from . import sprite

from . import theme as _theme
from .theme import Palette, resolve as resolve_theme

# Module-level names kept for the many call sites that reference them. They are
# rebound when a UI is constructed, so a palette change reaches everything
# without threading a theme object through every render function.
PALETTE: Palette = resolve_theme("purple")
ACCENT = PALETTE.accent
MUTED = PALETTE.muted
FAINT = PALETTE.faint
GOOD = PALETTE.good
WARN = PALETTE.warn
BAD = PALETTE.bad
BAR_STYLE = f"on {PALETTE.bar_bg}"
BAR_ACCENT = PALETTE.bar_accent
BAR_DIM = PALETTE.bar_dim


def _enable_forced_terminal_colour(console) -> None:
    """Make an explicitly forced terminal behave like a coloured terminal.

    Rich 15 infers ``no_color`` from an in-memory stream even when a caller
    later sets ``force_terminal`` for a render test or a terminal adapter.
    That leaves styled ``Text`` objects silently plain. The normal Wynxo
    console is detected at construction time; this small compatibility shim
    only changes a console that has explicitly been forced into terminal
    mode, and never changes redirected output.
    """
    if not getattr(console, "_force_terminal", False):
        return
    console.no_color = False
    if getattr(console, "_color_system", None) is None:
        console._color_system = COLOR_SYSTEMS["truecolor"]


def _ansi_of(style: str) -> str:
    """One rich style as the escape that turns it on."""
    from rich.style import Style

    try:
        return Style.parse(style).render("\x00").split("\x00")[0]
    except Exception:
        return ""


CODE_SPAN = PALETTE.code
"""`inline code` in the model's prose. Warm against the purple, so it reads
as a name rather than as emphasis.

A palette role now, not a constant. It was a hardcoded amber, so an answer
containing code was the one thing on screen /theme could not reach -- and
under a warm theme it was the same hue as the accent, which made a file
name read as a heading."""

KEYWORD = PALETTE.keyword
LITERAL = PALETTE.literal
SYMBOL = PALETTE.symbol
"""Syntax colour for a fenced block, in the same four roles the palette
defines. Highlighted code used raw ANSI names -- magenta keywords, cyan
numbers -- so a block and an inline span of the same expression came out in
two unrelated colour schemes."""

MIN_ACTIVITY_WIDTH = 16
"""Cells kept for the activity text before the stats start claiming space."""


# Modules that do `from .ui import ACCENT, MUTED, ...`. That kind of import
# binds a *copy* of the name in the importing module, so rebinding ours here
# never reached them -- which is why changing the theme used to require a
# restart. Pushing the new values into them is blunt, but it is one place and
# the alternative is threading a palette object through seventy call sites.
_COLOUR_NAMES = ("ACCENT", "MUTED", "FAINT", "GOOD", "WARN", "BAD",
                 "BAR_STYLE", "BAR_ACCENT", "BAR_DIM",
                 "CODE_SPAN", "KEYWORD", "LITERAL", "SYMBOL")

_COLOUR_CONSUMERS = ("wynxo.cli", "wynxo.wizard", "wynxo.doctor")
"""Modules to look in. Being on this list is not enough to be rewritten --
see below -- so a module that stops importing colours costs nothing."""


class _Missing:
    """A sentinel that equals nothing, so an absent name never matches."""

    def __eq__(self, other) -> bool:
        return False

    __hash__ = None


_MISSING = _Missing()


def apply_palette(palette: Palette) -> None:
    """Rebind the module colours, everywhere they were imported to.

    A name is rewritten only where it still holds the value this module had
    before the change. That is what makes it safe to sweep whole modules
    rather than keep a list of which names each one imported: cli.py imports
    WARN from .status, where it is the status tag "WARN" and not a colour,
    and blindly overwriting it printed a raw "[#f0c674]" on screen instead
    of "[ WARN ]". It no longer matches, so it is left alone.

    The per-module list this replaces had drifted, which is the failure a
    hand-kept list invites: cli.py imports GOOD, BAD and BAR_ACCENT too, and
    none of the three was named -- so after /theme the edit card's done and
    failed marks, and the effort surge, went on using the palette before
    last.
    """
    global PALETTE, ACCENT, MUTED, FAINT, GOOD, WARN, BAD, BAR_STYLE, BAR_ACCENT, BAR_DIM
    global CODE_SPAN, KEYWORD, LITERAL, SYMBOL
    here = globals()
    # Captured before the rebind: it is the *old* value that identifies a
    # name in another module as one of ours.
    was = {name: here[name] for name in _COLOUR_NAMES}

    # Anything that picks a colour per draw rather than at import -- the
    # mascot, whose colour depends on its mood -- asks the theme module
    # instead of holding a constant. Told here so there is one moment when
    # the palette changes, not two.
    _theme.use(palette)

    PALETTE = palette
    ACCENT = palette.accent
    MUTED = palette.muted
    FAINT = palette.faint
    GOOD = palette.good
    WARN = palette.warn
    BAD = palette.bad
    BAR_STYLE = f"on {palette.bar_bg}"
    BAR_ACCENT = palette.bar_accent
    BAR_DIM = palette.bar_dim
    CODE_SPAN = palette.code
    KEYWORD = palette.keyword
    LITERAL = palette.literal
    SYMBOL = palette.symbol

    import sys as _sys

    for module_name in _COLOUR_CONSUMERS:
        module = _sys.modules.get(module_name)
        if module is None:
            continue          # not imported in this run; nothing to update
        for name in _COLOUR_NAMES:
            if getattr(module, name, _MISSING) == was[name]:
                setattr(module, name, here[name])


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
"""Colour and hyperlink sequences. Stripped before measuring how a line
ends, since a reset code after the last newline is not a character on
screen but does stop the text from ending in one."""
"""C0, DEL, and C1.

The C1 range (U+0080-U+009F) is the single-character form of the same
sequences ESC introduces: U+009B is CSI, U+009D is OSC. A file holding the
bytes ``C2 9B`` decodes to U+009B, and terminals that recognise C1 in UTF-8
-- xterm among them -- then read ``U+009B 2 J`` as "erase the display".
Stripping ESC while letting its one-character twin through left the shorter
road open. Nothing in the range is text: they are control codes by
definition, in every encoding, so there is no legitimate output to lose.
U+00A0, the non-breaking space, is just past the end and is left alone."""


def elide_tail(text: str, room: int, ellipsis: str = "…") -> str:
    """Keep a useful suffix without exceeding the terminal cell budget."""
    if room <= 0:
        return ""
    if cell_len(text) <= room:
        return text
    marker = ellipsis if cell_len(ellipsis) <= room else "." * room
    budget = room - cell_len(marker)
    tail = ""
    for char in reversed(text):
        size = cell_len(char)
        if size > budget:
            break
        tail = char + tail
        budget -= size
    return marker + tail


def _chunks(word: str, room: int):
    """A word too wide for any line, cut into pieces that fit."""
    piece, width = "", 0
    for char in word:
        size = cell_len(char)
        if piece and width + size > room:
            yield piece
            piece, width = "", 0
        piece += char
        width += size
    if piece:
        yield piece


MEASURE = 84
"""The widest a line of prose is allowed to get.

Typography's rule of thumb is sixty to eighty characters: past that the eye
has to hunt for the start of the next line and reading slows measurably.
Terminals are routinely a hundred and sixty columns wide, and filling that
with a paragraph is the difference between an answer you read and one you
scan. Applied to prose only -- code, diffs and tool output are structured,
scanned by shape rather than read as sentences, and truncating those to
make them pretty would lose information."""


def wrap_cells(text: str, room: int) -> list[str]:
    """Wrap to a width in display cells rather than in characters.

    textwrap measures with len(). A Japanese or Chinese character is two
    cells wide, so a message in either came out twice as wide as it asked
    for and the console wrapped it a second time -- at column zero, which
    threw away the hanging indent the caller had wrapped it to get. The
    warnings and errors that go through here are the lines a person reads
    most carefully, and they were the ones that fell apart.

    Words are kept whole where they fit and cut by cells where they cannot,
    which is also how a script with no spaces in it wants to break.
    """
    room = max(1, room)
    lines: list[str] = []
    line, width = "", 0
    for word in text.split():
        size = cell_len(word)
        if size > room:
            if line:
                lines.append(line)
                line, width = "", 0
            *whole, last = list(_chunks(word, room))
            lines.extend(whole)
            line, width = last, cell_len(last)
            continue
        if width and width + 1 + size > room:
            lines.append(line)
            line, width = "", 0
        line = f"{line} {word}" if line else word
        width = cell_len(line)
    if line:
        lines.append(line)
    return lines or [""]


def sanitise(text: str) -> str:
    """Strip control characters out of text wynxo did not write itself.

    Model output, file contents and command output all end up on screen, and
    a terminal *acts* on escape sequences in them: ESC[2J clears the screen
    and takes the scrollback with it, ESC]0; renames the window. It does not
    take a hostile model -- a log with colour codes in it, a terminal
    recording, a test fixture, and the agent echoing any of them back is
    enough.

    rich neutralises this when it is handed a plain string, and does not
    when it is handed a Text, a Syntax or a Markdown, which is most of what
    this module builds. So it is done here, once, at the point where
    somebody else's text becomes something to draw.

    Newlines and tabs stay. Nothing else in the C0 range is meant literally,
    carriage returns included: a line rewriting itself makes no sense in a
    transcript that is a list of finished lines.
    """
    return _CONTROL.sub("", text)


class SafeConsole(Console):
    """A Console that cannot be talked into writing control sequences.

    ``sanitise`` above is opt-in: every call site that draws somebody else's
    text has to remember it, and seven of them did not. ``tool_start`` drew
    the model's own tool arguments raw on every single tool call;
    ``error`` drew the message it was handed; ``highlight`` and
    ``code_line`` drew file contents; ``rule``, ``table`` and ``todos`` drew
    whatever they were given. A file in the workspace containing
    ``ESC ] 52 ; c ; <base64> BEL`` wrote to the user's clipboard when the
    agent read it; ``ESC [ ? 1049 h`` switched the terminal to its alternate
    screen; ``ESC [ 1 ; 5 r`` pinned a scroll region and wedged the display.
    None of that needs a hostile model -- a log with colour codes in it is
    enough.

    Doing it here instead makes it structural. rich renders everything, of
    every kind, down to segments before it writes, and it keeps its *own*
    styling in ``Segment.style`` rather than in the text -- so scrubbing the
    text of every non-control segment removes exactly what somebody else put
    there and nothing wynxo chose. A display helper added tomorrow is
    covered without knowing this exists.
    """

    _tail = "\n\n"
    """The last two visible characters written, so ``gap()`` can tell
    whether the transcript already ends on a blank line.

    Read from what rich actually rendered rather than from what the caller
    passed. Guessing from the arguments got Markdown wrong every time: rich
    ends a rendered paragraph with its own blank line, so print(Markdown)
    left the screen blank while the argument was plainly not "" -- and the
    answer to every question was followed by two blank lines, or three when
    the turn had also printed its own separator.

    Spacing used to be decided by each caller: the prose block printed a
    blank before and after itself, the prompt echo did the same, and the
    completion report added one more. Every one of them was right on its
    own and wrong together -- an answer followed by a new question put
    three blank lines on screen, while two tool blocks in a row got none,
    so the transcript's rhythm depended on which blocks happened to be
    adjacent. ``gap()`` asks for a separation rather than for a newline,
    and one place decides what that costs.

    Known limitation, measured rather than guessed. While a turn runs this
    does not hold only the transcript. The activity bar repaints through
    the same console, and a repaint is a print of a Text rather than of
    Controls, so ``_transient`` does not catch it; the escapes rich wraps a
    repaint in are not caught either, because ``_ANSI_SGR`` strips colour
    and hyperlinks and the cursor-hide rich emits (``ESC[?25l``) is a CSI
    private-mode sequence. Instrumenting a real session finds this holding
    ``'^C stop '`` and ``'ESC[?25l\n\n'`` at moments when the transcript
    plainly ended somewhere else -- and in the second case a blank line
    that was asked for is dropped, which is the missing separation under a
    diff committed mid-turn.

    Two fixes were tried and both reverted: marking the bar's own frame,
    and widening the stripper to every escape. Each traded this defect for
    a worse one -- with the tail corrected, blank lines that do reach the
    screen started being suppressed instead, because the *committed*
    transcript really had ended blank and the live region's rows were what
    made it look otherwise. The two are entangled, and untangling them
    means keeping the live region's writes out of the console's record of
    the screen rather than filtering what gets recorded."""

    _transient = False
    """Set while a repaint that leaves nothing behind is being written."""


    def print(self, *args, **kwargs):
        _enable_forced_terminal_colour(self)
        # print() with no arguments is how the whole interface asks for a
        # separation. There was a gap() alias saying the same thing more
        # readably, and it kept breaking: it exists only on this subclass,
        # so any test or tool that swapped in a plain rich Console to
        # capture output raised AttributeError from inside a UI helper.
        # One method that every Console has beats a nicer name that only
        # this one does.
        #
        # A blank line is a request for separation, not for a newline, and
        # asking twice does not buy more of it. Thirty-four call sites each
        # decided their own spacing -- the prose block put one after itself,
        # the turn put one after the answer, the prompt echo put one before
        # the question -- and each was right alone and wrong together, so
        # the gap above a question was one, two or three lines depending on
        # what the turn before it had happened to print. Deciding it here
        # is the only way it can be decided once.
        # Only a caller's own blank line. rich writes NewLine() when a Live
        # tears down, and that one is load-bearing: it is what pushes the
        # region below the transcript before the erase runs. Dropped, the
        # status strip is left committed in the scrollback.
        if (not args or (len(args) == 1 and args[0] == "")) \
                and self._blank_rows() >= 1:
            return None
        # A Live repaint reaches the console as print(Control(...)): cursor
        # moves and erases, not transcript. Whatever it draws is taken back
        # before the next committed line, so it must not count as content --
        # a prompt that landed while the bar was up got a second blank line
        # from gap() separating it from a repaint that is no longer there.
        transient = bool(args) and all(isinstance(a, Control) for a in args)
        was, self._transient = self._transient, transient
        try:
            return super().print(*args, **kwargs)
        finally:
            self._transient = was

    def _blank_rows(self) -> int:
        """How many empty rows the transcript currently ends with."""
        trailing = len(self._tail) - len(self._tail.rstrip("\n"))
        return max(0, trailing - 1)

    def boundary(self) -> None:
        """Two empty rows: the seam between one exchange and the next.

        The transcript has exactly two levels of separation -- one row
        between the blocks inside a turn, two between turns -- and that is
        the whole vertical language. Asking for the seam rather than for
        newlines is what lets the two paths that open a turn (a typed line
        and one drained from the queue) end up looking the same, when the
        terminal has left a different number of rows behind each.
        """
        for _ in range(2 - self._blank_rows()):
            super().print()
            self._tail = (self._tail + "\n")[-8:]

    def wrote_elsewhere(self, newlines: int) -> None:
        """Something that is not this console wrote ``newlines`` newlines.

        Two things do. rich's Live writes one when it tears down, and
        prompt_toolkit draws the composer straight to the tty and erases it
        again when the line is accepted, leaving one more. Neither passes
        through ``print()``, so without this the console's idea of where
        the transcript ends runs two rows behind the screen -- which is why
        the space above a question was three rows after a typed line and
        one after a queued one, for the same seam.
        """
        self._tail = (self._tail + "\n" * max(0, newlines))[-8:]

    _TRAILING_SPACE = re.compile(r"[ \t]+(?=\n)")

    def _render_buffer(self, buffer) -> str:
        text = super()._render_buffer(_scrubbed(buffer))
        if text and not self._transient:
            visible = _ANSI_SGR.sub("", text)
            if visible:
                # Padded to the console width, a "blank" line rich emits is
                # a run of spaces and a newline, so a tail kept verbatim
                # ended " \n" and never looked blank. Enough of it is kept
                # that the padding of a line split across two writes still
                # collapses.
                self._tail = self._TRAILING_SPACE.sub(
                    "", self._tail + visible)[-8:]
        return text


def _scrubbed(buffer):
    """Segments with foreign control characters removed.

    Control segments are rich's own cursor moves -- what a Live display is
    made of -- and are passed through untouched. They carry no text from
    anywhere else.
    """
    for segment in buffer:
        if segment.control or not segment.text:
            yield segment
        elif _CONTROL.search(segment.text):
            yield segment._replace(text=_CONTROL.sub("", segment.text))
        else:
            yield segment


def _supports_unicode() -> bool:
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    if "utf" in encoding:
        return True
    try:
        "•─".encode(encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


ASCII_FALLBACK = {
    "\u2022": "*", "\u2192": "->", "\u2190": "<-", "\u2191": "up",
    "\u2193": "down", "\u2713": "+", "\u2717": "x", "\u25cf": "o",
    "\u00b7": ".", "\u2026": "...", "\u2014": "-", "\u2013": "-",
    "\u276f": ">", "\u2502": "|", "\u2500": "-", "\u256d": "+",
    "\u256e": "+", "\u2570": "+", "\u256f": "+", "\u201c": '"',
    "\u201d": '"', "\u2018": "'", "\u2019": "'",
}
"""Replacements for the glyphs used in labels the caller cannot re-word."""


def to_ascii(text: str) -> str:
    """Down-convert a display string for a terminal that cannot draw it.

    Anything outside the table is dropped rather than shown as a question
    mark, which is what an ASCII locale would otherwise print.
    """
    out = []
    for char in text:
        if char in ASCII_FALLBACK:
            out.append(ASCII_FALLBACK[char])
        elif ord(char) < 128:
            out.append(char)
    return "".join(out)


class Glyphs:
    def __init__(self, unicode_ok: bool):
        self.unicode = unicode_ok
        if unicode_ok:
            self.bullet, self.arrow, self.tick = "•", "→", "✓"
            self.cross, self.gear, self.dot = "✗", "●", "·"
            self.caret = "❯"
            # A plan step, in its three states. U+25C9 and U+25E6 rather
            # than the filled and hollow circles they resemble (U+25CF,
            # U+25CB): those two are East Asian Width "Ambiguous" and draw
            # two cells in a CJK locale, which breaks a list that is redrawn
            # in place.
            self.step_done, self.step_now, self.step_todo = "✓", "●", "○"
            self.spark = "✦"
            # The model's own reasoning, which is neither an action nor an
            # answer. U+273B is Neutral width, so it costs one cell in every
            # locale -- the filled stars that read better here (U+2605,
            # U+2736) are Ambiguous and take two in a CJK one.
            self.think = "✻"
            self.tool = "◈"
            self.task = "✦"
            # A ring, not a disc: the activity mark reads as "in progress"
            # rather than as a bullet in a list. U+25CC is Neutral width,
            # unlike the filled circles it resembles.
            self.busy = "◌"
            self.warn_mark = "⚠"
            # Rounded box corners, for the input field the prompt sits in.
            self.tl, self.tr, self.bl, self.br = "╭", "╮", "╰", "╯"
            self.hbar, self.vbar, self.ellipsis = "─", "│", "…"
        else:
            self.bullet, self.arrow, self.tick = "*", "->", "+"
            self.cross, self.gear, self.dot = "x", "o", "."
            self.caret = ">"
            self.step_done, self.step_now, self.step_todo = "+", ">", "-"
            self.spark = "*"
            self.think = "*"
            self.tool = "*"
            self.task = "*"
            self.busy = "o"
            self.warn_mark = "!"
            self.tl, self.tr, self.bl, self.br = "+", "+", "+", "+"
            self.hbar, self.vbar, self.ellipsis = "-", "|", "..."


class _SaidOnce:
    """What a spinner becomes where nothing can repaint.

    Carries the same shape as rich's Status -- a context manager with an
    update() -- so no caller has to know which one it got. Used when the
    live region is unavailable: a pipe, a captured stream, or a forced-off
    live flag in tests.
    """

    def __init__(self, ui: "UI", message: str):
        self.ui = ui
        self._say(message)

    def _say(self, message: str) -> None:
        if message:
            self.ui.console.print(Text(f"  {message}", style=MUTED))

    def update(self, status=None, **_kwargs) -> None:
        if status is not None:
            self._say(status if isinstance(status, str) else str(status))

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def __enter__(self) -> "_SaidOnce":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


@lru_cache(maxsize=64)
def _lexer(language: str):
    """The pygments lexer for a language name, or None.

    Asked through pygments rather than through rich. It used to go through
    ``rich.syntax.Syntax.get_lexer``, which rich 14 removed -- so on any
    current rich the call raised AttributeError, the bare ``except`` around
    it caught that exactly as it would catch an unknown language, and every
    fenced block in every streamed answer came out with no colour at all.
    Nothing said so, because falling back to plain text is what this is
    supposed to do when it cannot recognise a language.

    Cached because it is asked once per line of every block.
    """
    if not language or language in ("text", "plain"):
        return None
    try:
        from pygments.lexers import get_lexer_by_name

        return get_lexer_by_name(language, stripnl=False, ensurenl=False)
    except Exception:                                  # noqa: BLE001
        # An unknown language really is plain text. This is the case the
        # guard is for, and now the only one it catches.
        return None


def _version() -> str:
    from . import __version__

    return f"v{__version__}"


class UI:
    def __init__(self, theme: str = "purple", show_thinking: bool = False):
        self.console = SafeConsole(
            highlight=False,
            soft_wrap=False,
            # legacy_windows=False forces modern VT output on Windows Terminal.
            legacy_windows=False if sys.platform == "win32" else None,
        )
        self.g = Glyphs(_supports_unicode())
        self.bar: "ActivityBar | None" = None
        """The pinned bar, while a turn is running. Streamed text has
        to be handed to it rather than written straight out, or its
        repaint erases whatever landed on its row."""
        # rich's default panel box is Unicode; ASCII terminals need the
        # plain one or every border renders as question marks.
        self.box = ROUNDED if self.g.unicode else ASCII_BOX
        self.palette = resolve_theme(theme)
        apply_palette(self.palette)
        self.show_thinking = show_thinking
        self.narrow = is_narrow()
        """Phone-width terminals get a stacked layout instead of tables."""
        self.width = terminal_width()
        self.live_ok = True
        """Whether a rich Live may drive the screen."""

    def refresh_size(self) -> None:
        """Re-measure the terminal.

        Everything that wraps -- the streamers, the activity bar, the diff
        cards -- reads ``ui.width``, so this one call is the whole resize
        story. It is called where a draw begins rather than only from the
        SIGWINCH handler: prompt_toolkit owns that signal for the length of
        every read it does, so a resize while somebody sits at the prompt
        would otherwise never be heard.
        """
        self._resized(terminal_width())

    def _resized(self, width: int) -> None:
        """Adopt a new width, from either source.

        A live region has to be told, not just the wrap column. rich erases
        its region by moving the cursor up as many rows as its last render
        occupied -- arithmetic that is relative to where the cursor is, and
        a terminal reflows every line on the screen when the window changes
        width. After that the region no longer knows where it is, stops
        erasing, and appends: resizing the window during a turn filled the
        rest of it with a copy of the status strip per frame, twelve a
        second, and with the companion on, a column of stranded sprite.

        Taking it down and putting it back re-anchors it at the cursor,
        which is the only place the arithmetic can start from.
        """
        if width == self.width:
            return              # a height-only resize changes nothing here
        self.width = width
        self.narrow = width < 60
        if self.bar is not None:
            # Flagged, not done here. The bar re-measures from inside its
            # own render, so this runs *during* a repaint about half the
            # time -- and stopping a live region from inside its own render
            # does nothing at all, quietly. The flag is drained at the top
            # of refresh(), which is always outside one.
            self.bar.needs_reanchor = True

    # -- chrome ------------------------------------------------------------

    def banner(self, model: str, endpoint: str, effort: str, workspace: str,
               pet=None, greeting: str = "") -> None:
        """One line: who this is, what it is running, and where.

        Start-up should cost the screen a single row. It used to cost nine
        -- five of block art, two of dotted metadata, a rule and a greeting
        -- and then three, with the character standing beside a stacked
        identity. Three is not nine, but it is still a title card, and a
        title card is something you read once and then scroll past forever.

        The character is not here any more. It belongs to the work: it
        appears while a task runs, in the live region, and goes when the
        task does. A mascot on the identity line is a mascot you are
        looking at during every minute the agent is idle, which is most of
        them.

        The facts give way from the least useful end, so a narrow terminal
        keeps the model and loses the server.
        """
        # Two facts, and neither is a setting. The effort level and the
        # server used to be here: both are on the status line under the
        # prompt, both are one keystroke from /status, and neither changes
        # what you do next. A header is read once, so what belongs in it is
        # what you need to know once -- which model, and where it is
        # pointed. The endpoint in particular is nearly always the same
        # loopback address, which makes it the definition of a fact that
        # earns nothing by being on screen all session.
        facts = [model, self.shorten_path(workspace)]

        separator = f" {self.g.dot} "
        # "wynxo" plus the separator after it, so what is left is what the
        # facts actually have to fit in.
        budget = max(12, self.width - 5 - cell_len(separator) - 1)

        def room(parts: list[str]) -> int:
            return sum(cell_len(p) for p in parts) \
                + cell_len(separator) * max(0, len(parts) - 1)

        shown = list(facts)
        while len(shown) > 1 and room(shown) > budget:
            shown.pop()

        detail = Text()
        for index, part in enumerate(shown):
            if index:
                detail.append(separator, style=FAINT)
            detail.append(part, style="" if index == 0 else MUTED)

        line = Text()
        line.append("wynxo", style=f"bold {ACCENT}")
        line.append(separator, style=FAINT)
        line.append_text(detail)
        self.console.print()
        self.console.print(line, overflow="ellipsis", no_wrap=True)
        self.console.print()

    def home(self, model: str, workspace: str, *, mode: str = "agent",
             companion: str = "ready", version: str = "",
             show_companion: bool = False,
             show_art: bool = False,
             show_static_controls: bool = False) -> None:
        """The application's opening screen, drawn once.

        The header, workspace, welcome card and useful commands are one
        composition, measured to the terminal it landed in. The interactive
        prompt owns its own composer and toolbar; keeping those out of this
        one-shot landing render prevents a duplicate box below it.

        It gives way rather than degrading. A terminal too narrow for the
        illustration gets the conversation column at full width, one too
        narrow for the rail loses that too, and a pipe or a TERM=dumb
        console gets ``banner()`` -- one line, the same facts, no chrome
        that cannot be drawn where it is going.
        """
        from . import shell

        self.refresh_size()
        if not self.can_draw_shell():
            self.banner(model, "", "", workspace)
            return
        self.refresh_size()
        self.console.width = self.width
        self.console.print()
        self.console.print(shell.home(
            self, model=model, version=version or _version(),
            mode=mode, companion=companion, workspace=workspace,
            show_companion=show_companion,
            show_art=show_art,
            show_static_controls=show_static_controls))
        self.console.print()

    async def boot_sequence(self, model: str, workspace: str, *,
                            enabled: bool = True) -> None:
        """Play a short, transient reveal before the first prompt.

        The launch animation is a hand-off, not a loading screen: it lasts
        well under a second, never writes a frame into scrollback, and is
        skipped for pipes, dumb terminals, narrow layouts, and reduced
        motion. The rendered scene and the real prompt remain separate, so a
        resize or an interrupted connection cannot strand animation rows.
        """
        if not enabled or not self.live_ok or not self.console.is_terminal:
            return
        if self.narrow:
            return

        from . import shell

        frames = 8
        live = Live(
            shell.boot_frame(self, model, workspace, 0, frames),
            console=self.console,
            auto_refresh=False,
            transient=True,
        )
        try:
            live.start(refresh=True)
            for frame in range(1, frames):
                live.update(shell.boot_frame(
                    self, model, workspace, frame, frames), refresh=True)
                await asyncio.sleep(0.045)
            # Let the completed frame register before the transient region
            # disappears; the following launchpad is the lasting result.
            await asyncio.sleep(0.045)
        finally:
            with contextlib.suppress(Exception):
                live.stop()

    def can_draw_shell(self) -> bool:
        """Whether the composed screen can be drawn honestly here.

        Three ways it cannot. A dumb terminal has no colour and no redraw;
        a redirected stream is a file, and a screenful of box characters in
        a log is worse than useless; and a phone-width terminal has no room
        for two columns, so the composition would be a stack of boxes
        rather than a layout. Each of those gets ``banner()`` -- the same
        facts on one line.
        """
        from .platforms import is_dumb_terminal

        return not (is_dumb_terminal() or not self.console.is_terminal
                    or self.narrow)

    def clear(self) -> None:
        """Clear the screen and scrollback, so a session starts on a clean page.

        Scrollback too: clearing only the visible rows leaves the previous
        session one scroll away, which is worse than not clearing at all.
        """
        if not self.console.is_terminal:
            return
        self.console.clear()
        self.console.file.write("\x1b[3J")   # erase saved lines
        self.console.file.flush()

    def shorten_model(self, name: str, room: int) -> str:
        """A model tag trimmed to fit, from the least useful end.

        The namespace goes first. ``huihui_ai/Qwen3.8-abliterated:27b`` is
        the same model as ``Qwen3.8-abliterated:27b``, and the part before
        the slash is who published it -- which is not something anybody
        reads while typing, and it is a third of a narrow terminal.

        Then the head of the name, keeping the tag, because the tag is the
        size and the size is what tells you what to expect. Cut blindly
        from the right, a thirty-three character name at forty columns left
        ``huihui_ai/Qwen3.8-abliterated:`` and pushed the effort level and
        the context percentage off the line entirely -- so the one string
        on it that never changes survived, and the two facts that do were
        the ones that went.
        """
        name = name.strip()
        if room <= 0:
            return ""
        if cell_len(name) <= room:
            return name
        if "/" in name and cell_len(bare := name.rsplit("/", 1)[-1]) <= room:
            return bare
        name = name.rsplit("/", 1)[-1]
        if cell_len(name) <= room:
            return name
        return elide_tail(name, room, self.g.ellipsis)

    def shorten_path(self, path: str) -> str:
        """~/code/proj rather than /home/you/code/proj, and never the full
        thing when it would push everything else off the line."""
        import os

        home = os.path.expanduser("~")
        if path == home or path.startswith(home + os.sep):
            path = "~" + path[len(home):]
        budget = max(18, self.width // 3)
        if cell_len(path) <= budget:
            return path
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        # Successively shorter forms, first that fits. The old fallback
        # returned ".../" plus the last two components whatever their
        # length, so a long project directory came back well over budget --
        # and the banner then dropped the path entirely rather than showing
        # a shortened one, which is the opposite of what shortening is for.
        for keep in (2, 1):
            if len(parts) > keep:
                candidate = ".../" + "/".join(parts[-keep:])
                if cell_len(candidate) <= budget:
                    return candidate
        tail = parts[-1] if parts else path
        if cell_len(tail) <= budget:
            return tail
        return elide_tail(tail, budget, self.g.ellipsis)

    def rule(self, label: str = "") -> None:
        self.console.print(Rule(label, style=MUTED, characters=self.g.hbar))

    # -- messages ----------------------------------------------------------

    def detail_line(self, text: str, style: str, indent: int = 2) -> None:
        """A secondary line under a block, wrapped inside its own column.

        Two columns, because that is one step in from the heads -- which
        start at zero. The transcript used to hold every line at column two
        and put details at six, so nothing was ever flush with the edge and
        the whole session read as a formatted document rather than as
        terminal output. Indentation is worth spending where it means
        something: "this belongs to the line above it". Spent everywhere it
        means nothing at all.

        rich wraps at the console edge and resumes at column zero, so a
        detail longer than the terminal would otherwise fall out from under
        its own block on the second row. Wrapped here instead.
        """
        pad = " " * indent
        room = max(8, self.width - indent - 1)
        out = Text()
        for i, line in enumerate(sanitise(text).split("\n")):
            for piece in wrap_cells(line, room) or [""]:
                if i or out.plain:
                    out.append("\n")
                out.append(pad + piece, style=style)
        if out.plain.strip():
            self.console.print(out)

    def user_line(self, text: str, note: str = "") -> None:
        """What was asked, in the outline the design gives a user message.

        The composer erases itself when it closes, which is what keeps a
        stranded frame out of the scrollback on every turn. The cost is that
        the question would vanish with it, so it is reprinted here -- and
        reprinted as the same shape the home screen draws, so a transcript
        is made of the pieces the opening composition introduced rather than
        of a different set that happen to share its colours.
        """
        from . import shell

        self.console.boundary()
        if self.narrow or not self.g.unicode:
            # An outline costs two rows and four columns. On a phone-width
            # terminal that is the difference between the question fitting
            # on one line and not, so it goes.
            body = Text()
            body.append(f"{self.g.caret} ", style=f"bold {ACCENT}")
            first, *rest = text.splitlines() or [""]
            body.append(first, style="bold")
            for line in rest:
                body.append("\n  " + line, style="bold")
            if note:
                body.append(f"   {note}", style=MUTED)
            self.console.print(body, overflow="ellipsis", no_wrap=True)
            return
        self.console.print(shell.user_message(self, text, note))

    def _marked(self, marker: str, message: str, style: str) -> None:
        """One message, wrapped so it stays in its own column.

        rich wraps a Text at the console edge but starts the next line at
        column zero, so any message longer than the terminal is wide fell
        out from under its own marker and ran into the left edge -- the
        longer and more important the message, the worse it looked. These
        are the lines that carry warnings and errors, which are exactly the
        ones a person reads carefully.

        Wrapped here instead, with the continuation indented to sit under
        the first word rather than under the marker, so the "!" stays the
        only thing in its column and the prose forms a clean block.
        """
        _enable_forced_terminal_colour(self.console)
        head = f"{marker} " if marker else ""
        hang = " " * cell_len(head)
        width = max(20, self.width - 1)
        room = max(8, width - cell_len(head))

        out = Text()
        first = True
        # Wrap each of the message's own lines separately: a message that
        # already has structure keeps it.
        for line in sanitise(message).split("\n"):
            pieces = wrap_cells(line, room)
            for piece in pieces:
                if not first:
                    out.append("\n")
                if first and marker:
                    # The marker gets the emphasis (bold), the words keep the
                    # status colour, so the "!" reads as a marker and the
                    # prose as the message.
                    out.append(marker, style=f"bold {style}")
                    out.append(" " + piece, style=style)
                else:
                    out.append((head if first else hang) + piece, style=style)
                first = False
        self.console.print(out)

    def info(self, message: str) -> None:
        self._marked("", message, MUTED)

    def warn(self, message: str) -> None:
        self._marked("!", message, WARN)

    def error(self, message: str) -> None:
        """A failure, in the shape everything else in the transcript uses.

        It was a bordered Panel titled "error", drawn hard against column
        zero while every other line -- the user's own, the answer, every
        tool block -- sits at column two. So the one moment the eye is
        pulled hardest was also the one element that broke the margin, and
        a four-line connection hint became eight rows of box to say it. The
        border was carrying no information the red mark does not.

        First line as the headline, the rest indented under it: the same
        head-and-detail shape as ``tool_call``, so a failed tool and a
        failed connection read as the same kind of event.
        """
        body = sanitise(message).strip()
        if not body:
            return
        head, *rest = body.splitlines()
        self.console.print()
        line = Text()
        line.append(f"{self.g.cross} ", style=BAD)
        line.append(head, style=f"bold {BAD}")
        self.console.print(line)
        for extra in rest:
            # Relative indentation kept. An error can carry a worked
            # example -- the Ollama one is four lines of shell -- and
            # stripping every line to the same column turns the commands
            # into prose.
            depth = len(extra) - len(extra.lstrip())
            self.detail_line(extra.strip(), BAD, indent=2 + depth)

    def success(self, message: str) -> None:
        self._marked(self.g.tick, message, GOOD)

    def hint(self, message: str) -> None:
        """What you can do next, said quietly and at the detail column.

        Distinct from info(): info is a fact about what happened, a hint is
        an offer. They were the same call, so "/session <id> to pick one up"
        arrived with the same weight as "resumed 4f21a0 -- 12 messages", and
        the eye had to read both to find out which was which. Held at FAINT
        and set apart from whatever it follows, an offer can be skipped at a
        glance and still be there when it is wanted.

        The blank row matters more than it sounds. Printed tight under a
        listing, an indented faint line reads as one more row of it -- so
        "/session list -- 6 other conversations" arrived looking like a
        seventh fact about this one.
        """
        body = sanitise(message).strip()
        if body:
            self.console.print()
            self.detail_line(body, FAINT)

    def outcome(self, report: str) -> None:
        """How a coding turn ended: a headline, then the evidence under it.

        The whole block used to be printed FAINT at a flat two-space indent,
        so "✓ completed" -- the one line that answers "did it work" -- was
        the dimmest thing on screen and sat at the same level as the files
        it summarised. Now the verdict carries the colour and the evidence
        stays quiet at the same detail column tool blocks use, so the shape
        is one the eye has already learnt by the time it gets here.
        """
        lines = report.splitlines()
        if not lines:
            return
        head, *rest = lines
        style = GOOD if head.lstrip().startswith(self.g.tick) else WARN
        self.console.print()
        self.console.print(Text(head.strip(), style=f"bold {style}"))
        for line in rest:
            # The report indents its own detail by two, which is already
            # the transcript's detail column, so its structure carries
            # over rather than being flattened or doubled.
            depth = len(line) - len(line.lstrip())
            self.detail_line(line.strip(), MUTED, indent=max(2, depth))

    def help_block(self, text: str) -> None:
        """A block of guidance, in the transcript's column.

        Printed raw, a multi-line help string starts at column zero while
        everything else in the transcript sits at column two -- and the
        longest, most carefully written text in the session was the one
        thing that looked like it had escaped the conversation. Its own
        indentation is kept, because the shell commands in it are indented
        on purpose.
        """
        if not text.strip():
            return
        for line in sanitise(text).splitlines():
            if not line.strip():
                self.console.print()
                continue
            depth = len(line) - len(line.lstrip())
            self.detail_line(line.strip(), MUTED, indent=depth)

    def assistant_markdown(self, text: str) -> None:
        """An answer that arrived all at once, drawn like one that streamed.

        Through the same CodeStreamer, fed in one go. It went through rich's
        Markdown, so turning streaming off changed what the answer looked
        like: rich's own headings, its own bullets, and code in a pygments
        theme inside a filled panel -- a third code renderer, in a third
        colour scheme, for the same text the streamed path draws with the
        palette. The measure, the marks and the code all come from one place
        now, and `--no-stream` is a question of when the words appear rather
        than of what they look like.
        """
        if not text.strip():
            return
        self.console.print()
        streamer = CodeStreamer(self)
        streamer.feed(text)
        streamer.finish()
        self.console.print()

    # -- tools -------------------------------------------------------------

    def tool_call(self, name: str, target: str, detail: str = "",
                  ok: bool = True) -> None:
        """One tool call: what ran, and what came of it underneath.

            → read src/auth.py
              214 lines

            → tests
              ✗ 2 failed

        One mark, and it means one thing. There was a tick on the result of
        every call for a while, level with the arrow -- two marks per call,
        in two columns, on lines that nearly always said the same thing:
        it worked. A column of ticks carries no information when almost
        nothing fails, and it costs the one mark that should stop the eye.
        So success is silent and failure is marked, and scanning a session
        for what went wrong is scanning for the only ✗ on the page.

        The detail sits one step in, under the call it belongs to, rather
        than level with it. That is the whole hierarchy: what the agent
        did, and beneath it what happened.
        """
        # Every committed block opens by separating itself from whatever
        # came before, rather than each block closing with a blank of its
        # own. One rule, applied by the thing that knows it is starting.
        self.console.print()
        line = Text()
        line.append(f"{self.g.arrow} ", style=ACCENT if ok else BAD)
        line.append(verb(name), style="bold" if ok else f"bold {BAD}")
        if target:
            line.append(f" {sanitise(target)[:120]}", style=MUTED)
        # One row, always. The head is a label -- what ran, on what -- and a
        # label that wraps stops being scannable, so a long target is cut
        # rather than folded. The detail underneath is prose and wraps.
        self.console.print(line, overflow="ellipsis", no_wrap=True)
        if detail:
            body = detail[:400]
            if not ok:
                body = f"{self.g.cross} {body}"
            self.detail_line(body, BAD if not ok else MUTED)
        elif not ok:
            self.detail_line(f"{self.g.cross} failed", BAD)

    def tool_result(self, name: str, ok: bool, display: str, output: str) -> None:
        if display.startswith(("--- ", "diff --git")) or "\n+++ " in display[:200]:
            self.diff(display)
            return
        text = sanitise(display or output).strip()
        if not text:
            return
        first = text.splitlines()[0]
        extra = len(text.splitlines()) - 1
        line = Text("    ")
        line.append(self.g.tick if ok else self.g.cross, style=GOOD if ok else BAD)
        line.append(" ")
        line.append(first[:150], style="" if ok else BAD)
        if extra > 0:
            line.append(f"  (+{extra} lines)", style=MUTED)
        self.console.print(line)

    def tool_output(self, line: str) -> None:
        """One line from a command that is still running.

        Dimmed and indented past the tool line so a long build reads as
        something happening underneath the step, rather than as the agent's
        own words. Truncated per line: a stray 5000-column line from a
        minifier would otherwise wrap into a screenful.
        """
        text = sanitise(line).rstrip()
        if not text.strip():
            return
        limit = max(24, self.width - 4)
        self.console.print(Text("  " + text[:limit], style=MUTED))

    MAX_DIFF_LINES = 120
    """Past this a diff stops being something you read and starts being
    something you scroll past. The rest is counted, never dropped silently."""

    def diff(self, text: str) -> None:
        if not text.strip():
            return
        text = sanitise(text)
        limit = max(24, self.width - 6) if self.narrow else 10_000
        body = Text()
        # The ---/+++ header names the file, and the tool line directly
        # above it just named the file. Two rows to say it a second time,
        # at the top of every diff.
        all_lines = [l for l in text.splitlines()
                     if not l.startswith(("--- ", "+++ "))]
        dropped = max(0, len(all_lines) - self.MAX_DIFF_LINES)
        for line in (l[:limit] for l in all_lines[:self.MAX_DIFF_LINES]):
            if line.startswith("+"):
                body.append(line + "\n", style=GOOD)
            elif line.startswith("-"):
                body.append(line + "\n", style=BAD)
            elif line.startswith("@@"):
                body.append(line + "\n", style=ACCENT)
            else:
                body.append(line + "\n", style=MUTED)
        if dropped:
            # Say so. A diff cut off at exactly 120 lines with no mark reads
            # as a diff that ended there, which is a different claim.
            body.append(f"{self.g.ellipsis} {dropped} more line"
                        f"{'' if dropped == 1 else 's'}\n",
                        style=f"{MUTED} italic")
        body.rstrip()
        if not body.plain.strip():
            return
        # Indented, not boxed. A diff is already the most structured thing
        # in the transcript -- every line begins with +, - or @@, and the
        # colour says which -- so a border around it draws a shape the
        # content was drawing anyway, and spends two columns and two rows
        # doing it. The indent says what every other indent here says: this
        # belongs to the call above it.
        #
        # The indent is written into the text rather than applied with
        # Padding, which pads each row out to the full console width -- so
        # every line of a diff you selected and copied came with a tail of
        # trailing spaces.
        indented = Text()
        for index, line in enumerate(body.split("\n")):
            if index:
                indented.append("\n")
            indented.append("  ")
            indented.append_text(line)
        self.console.print(indented)

    def todos(self, rendered: str) -> None:
        if not rendered.strip():
            return
        self.console.print(plan_block(rendered, self.g))

    MAX_TABLE_ROWS = 60
    """The ceiling on a listing, for the reason a block of code has one.

    /apps on an ordinary Linux box found four hundred and ninety-two
    applications and printed all of them: five hundred rows of scrollback to
    answer "what is installed", past the point where anyone was still
    reading and taking the rest of the session with it. What is cut is
    counted, never dropped in silence -- and every list that can grow gets
    the rule, not just the one that grew."""

    MAX_CODE_LINES = 120
    """The same ceiling a diff gets, for the same reason.

    A diff has stopped at a hundred and twenty lines with an honest count of
    the remainder for a long time; a block of output had no limit at all, so
    a test run that printed five hundred lines put five hundred lines into
    the scrollback -- thirty-eight kilobytes past the point where anyone was
    still reading. What is cut is counted, never dropped in silence."""

    def code(self, text: str, language: str = "text") -> None:
        """A block of somebody else's code or output.

        Drawn a line at a time through the same highlighter a streamed
        fenced block uses. It went through rich's Syntax with a pygments
        theme, so the transcript had two code renderers with two unrelated
        colour schemes: the same Python was one set of colours when the
        model wrote it and another when a tool printed it, and only one of
        them followed /theme. Syntax also fills a background band behind
        every row, which is the panel look the rest of this design has been
        taking out.
        """
        body = sanitise(text).rstrip("\n")
        if not body.strip():
            return
        lines = body.split("\n")
        dropped = max(0, len(lines) - self.MAX_CODE_LINES)
        # Through code_line, which is the one place that knows how a line
        # of code is drawn. This had its own loop, so the two disagreed the
        # moment either changed: a long line kept its gutter when the model
        # streamed it and lost it when a tool printed it.
        for line in lines[:self.MAX_CODE_LINES]:
            self.code_line(line, language)
        if dropped:
            self.detail_line(
                f"{self.g.ellipsis} {dropped} more line"
                f"{'' if dropped == 1 else 's'}",
                f"{MUTED} italic")

    def highlight(self, line: str, language: str = "text") -> Text:
        """One line, syntax-highlighted, with no block chrome.

        Syntax() would draw its own background band per line and stack into a
        ragged column, so the lexer is used directly instead.

        Safe on a half-written line: pygments will mis-lex an unterminated
        string or a keyword that is still being typed, and both correct
        themselves on the next character. That is the price of showing code
        as it arrives rather than a line at a time.
        """
        rendered = Text.from_ansi(line) if "\x1b" in line else Text(line)
        lexer = _lexer(language)
        if lexer is None:
            return rendered
        try:
            tokens = list(lexer.get_tokens(line))
        except Exception:                              # noqa: BLE001
            # pygments on a half-written line: an unterminated string, a
            # keyword still being typed. It corrects itself on the next
            # character, and showing the line uncoloured for one frame is
            # the price of showing code as it arrives.
            return rendered
        out = Text()
        for token, value in tokens:
            if value.endswith("\n"):
                value = value[:-1]
            if value:
                out.append(value, style=self._token_style(token))
        if out.plain != line.rstrip("\n"):
            # A lexer that does not give the line back is a lexer that ate
            # it, and colour is never worth a word of somebody's output.
            #
            # pygments' session lexers do exactly this. BashSessionLexer
            # -- which is what "console" resolves to -- returns *no tokens
            # at all* for a line with no shell prompt on it, so every line
            # of every command's output rendered as an empty gutter: the
            # expansion was there, the text was gone, and nothing said so
            # because a blank line is what an empty result looks like.
            return rendered
        return out

    def code_gutter(self) -> Text:
        """The two cells a line of quoted code opens with.

        A rule rather than a box. Indentation alone was doing the whole job
        of saying "this is code and not the answer", and two spaces is the
        same thing a tool's detail line uses -- so a fenced block read as a
        deeply indented paragraph that happened to be coloured. One column
        of rule says it outright, and costs a column rather than a frame.

        Declined where the rule would not be one cell wide: U+2502 is East
        Asian Width Ambiguous, and a terminal that draws it as two would
        push every line of every block one column right of the line above
        it. Plain indentation there, which is what this was before.
        """
        if self.g.unicode and not sprite._ambiguous_is_wide():
            return Text(f"{self.g.vbar} ", style=FAINT)
        return Text("  ")

    def code_line(self, line: str, language: str = "text",
                  indent: str = "") -> None:
        """One line of code, wrapped inside its own column.

        rich wraps at the console edge and resumes at column zero, so on a
        narrow terminal the tail of a long line landed flush left with no
        gutter -- reading as prose, in the column the answer uses, in the
        middle of a code block. Broken here instead, by cells rather than
        characters, and never reflowed: a line of code means what its
        characters say and rearranging it would make it say something else.
        """
        _enable_forced_terminal_colour(self.console)
        gutter = self.code_gutter()
        room = max(8, self.width - cell_len(gutter.plain)
                   - cell_len(indent) - 1)
        rendered = self.highlight(line, language)
        # Where to cut, measured in cells but sliced by index -- the two
        # differ the moment the code contains a wide character.
        cuts, width = [], 0
        for index, char in enumerate(rendered.plain):
            size = cell_len(char)
            if width and width + size > room:
                cuts.append(index)
                width = 0
            width += size
        for piece in (rendered.divide(cuts) if cuts else [rendered]):
            # soft_wrap, not no_wrap: no_wrap truncates, which would answer
            # a line too long by throwing characters away -- the failure
            # this method exists to prevent. Each piece is already cut to
            # fit, so there is nothing left for the terminal to wrap.
            self.console.print(Text(indent) + gutter + piece,
                               highlight=False, soft_wrap=True)

    def _token_style(self, token) -> str:
        """A pygments token as one of the palette's four syntax roles.

        It named raw ANSI colours -- magenta, cyan, bright_blue -- so code
        was the one part of the interface /theme could not touch, and on a
        terminal whose magenta is not the theme's the block clashed with
        everything around it. Comments go to FAINT rather than MUTED: they
        are the one part of a program you are meant to be able to skip, and
        that is exactly what FAINT is for.
        """
        from pygments.token import (Comment, Error, Keyword, Name, Number,
                                    Operator, String)

        for kind, style in ((Comment, FAINT), (String, LITERAL),
                            (Number, LITERAL), (Keyword, KEYWORD),
                            (Name.Function, SYMBOL), (Name.Class, SYMBOL),
                            (Name.Builtin, SYMBOL), (Operator, MUTED),
                            (Error, BAD)):
            if token in kind:
                return style
        return ""

    # -- transient ---------------------------------------------------------

    def status(self, message: str):
        """A spinner while something slow happens.

        rich's Status is a Live: it hides the cursor, redraws in place and
        carriage-returns over itself. Sent to a stream that cannot repaint
        (a pipe, a captured stream) those arrive as literal "?25l", "?25h"
        and "^M" in the middle of the output.

        So where a Live cannot go, the message is simply said once. It is the
        same information, minus the animation.
        """
        if not self.live_ok:
            return _SaidOnce(self, message)
        return self.console.status(Text(message, style=MUTED), spinner="dots")

    def table(self, columns: Iterable[str], rows: Iterable[Iterable[str]],
              title: str = "") -> None:
        """A list of things and what they are, as text.

        Not a grid. Every list in the application went through rich's Table
        and came out as a bordered box with vertical rules between the
        columns -- /help was two of them stacked, forty rows of box-drawing
        to say what forty commands do. Borders are for holding a shape that
        the content cannot hold by itself, and a name beside a description
        holds its own shape perfectly well: the names are short, they line
        up in a gutter, and the eye follows the column without needing a
        line drawn down it.

        It also broke the content. Three columns of prose in eighty
        characters left about thirty per cell, so /tools rendered tool
        descriptions cut off mid-word -- "Send the whole list each ti" --
        with no ellipsis to say anything had been removed. Here the first
        column is a gutter sized to the longest name, and everything else
        wraps in the space that is left, which is most of the line.
        """
        columns = list(columns)
        rows = [[str(cell) for cell in row] for row in rows]
        if not rows:
            return
        dropped = max(0, len(rows) - self.MAX_TABLE_ROWS)
        rows = rows[:self.MAX_TABLE_ROWS]
        self.console.print()
        if title:
            self.console.print(Text(title, style=f"bold {ACCENT}"))

        names = [row[0] for row in rows]
        # The longest name, up to a limit. The limit is what stops one
        # sixty-character tool signature from indenting every description
        # in the list; anything past it takes its own row instead. Sizing
        # to a percentile rather than the maximum was worse: it wrapped
        # "/sessions" and "Mouse wheel" in a list whose longest name was
        # eleven characters, to save four columns nobody wanted.
        gutter = min(max((cell_len(n) for n in names), default=0), 28)
        room = max(20, self.width - gutter - 3)

        for row in rows:
            head, *rest = row
            # Anything after the first column is description, joined by the
            # separator and never labelled. A cell reading "yes" under a
            # column heading three rows up says nothing on its own, so a
            # caller that wants it understood passes the word: ("write_file",
            # "writes", "Replaces the file.") rather than a bare "yes".
            body = f"  {self.g.dot}  ".join(
                value for value in rest if value)
            line = Text()
            if cell_len(head) > gutter:
                # Its own row rather than a name cut mid-token. Truncating
                # here is what made /tools unreadable in the first place,
                # and a signature is the one thing on the line you cannot
                # guess the rest of.
                line.append(sanitise(head), style="bold")
                line.append("\n" + " " * gutter)
            else:
                line.append(sanitise(head).ljust(gutter), style="bold")
            first = True
            for piece in wrap_cells(sanitise(body), room) or [""]:
                if not first:
                    line.append("\n" + " " * gutter)
                line.append("  " + piece, style=MUTED)
                first = False
            self.console.print(line)
        if dropped:
            self.detail_line(f"{self.g.ellipsis} {dropped:,} more, not shown",
                             FAINT, indent=0)


VERBS = {
    "read_file": "read", "write_file": "write", "edit_file": "edit",
    "multi_edit": "edit", "list_dir": "ls", "glob": "find",
    "grep": "search", "web_search": "search", "web_fetch": "fetch",
    "run_tests": "tests", "shell": "run", "todo_write": "plan",
    "github_read": "github", "github_write": "github",
    "projectmap": "map", "launch_application": "launch",
    "computer_info": "inspect", "computer_control": "control",
    "remember": "remember", "recall": "recall",
}
"""What a tool is called on screen, where that is not what it is called in
the registry.

"read_file" and "write_file" are names for a dispatch table. On a line a
person reads while waiting, they are two syllables of noise each and they
all rhyme, so a column of them is harder to scan than a column of verbs --
which is exactly the job that column has. The registry keeps its names;
this is the only place they are translated, and anything not listed is
shown as it is."""


def verb(name: str) -> str:
    """The display name for a tool."""
    return VERBS.get((name or "").strip().lower(), name)


def plan_steps(rendered: str) -> list[tuple[str, str]]:
    """(state, text) for each step. State is "done", "now" or "todo".

    The tool writes "[x] ", "[>] " and "[ ] "; anything else is a line the
    model wrapped or a note it added, and belongs to the step above it.
    """
    out: list[tuple[str, str]] = []
    for line in rendered.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for marker, state in (("[x]", "done"), ("[>]", "now"), ("[ ]", "todo")):
            if stripped.startswith(marker):
                out.append((state, stripped[len(marker):].strip()))
                break
        else:
            if out:
                out[-1] = (out[-1][0], f"{out[-1][1]} {stripped}")
    return out


def plan_block(rendered: str, glyphs: "Glyphs", *, complete: bool = False) -> Text:
    """The plan, as a short list.

    A list rather than a framed panel, and one renderer rather than two.
    There used to be a Panel here and a different Panel in the activity bar,
    which drew the same plan two ways -- one showing raw "[x]" markers, the
    other glyphs. On a hundred-column terminal the box spent four cells of
    border and eighty of trailing whitespace per row to say four short
    things, which is the definition of a widget dominating a UI.

    Every step gets a mark, including the ones not started: without one they
    were bare indented text and read as wrapped continuations of the step
    above.

    The steps sit one level in from the heading. Flush with it, "plan 1/4"
    was just the first row of the list rather than the thing the list
    belongs to, and the block had no top edge at all -- the indent is the
    only structure here, since there is no border to carry it.
    """
    steps = plan_steps(rendered)
    if not steps:
        return Text()
    done = sum(1 for state, _ in steps if state == "done")
    if complete:
        done = len(steps)
    body = Text()
    body.append("  plan", style=GOOD if done == len(steps) else "bold")
    body.append(f"  {done}/{len(steps)}\n", style=MUTED)
    for state, text in steps:
        if complete:
            state = "done"
        mark, style = {
            "done": (glyphs.step_done, f"{MUTED} strike"),
            "now": (glyphs.step_now, f"bold {ACCENT}"),
            "todo": (glyphs.step_todo, MUTED),
        }[state]
        body.append(f"    {mark} ", style=GOOD if state == "done" else style)
        body.append(text + "\n", style=style)
    body.rstrip()
    return body


class CodeStreamer:
    """Renders streamed assistant text as it arrives.

    Prose is written out word by word as soon as each word is complete, with
    wrapping done here rather than by the terminal -- so text flows the way it
    is generated instead of appearing a whole line at a time when a newline
    finally shows up. A model writing one long paragraph used to produce
    nothing at all until it finished.

    Fenced code is different: it is highlighted per line, once the line is
    whole, because a half-written line cannot be lexed.
    """

    def __init__(self, ui: "UI", indent: str = "", style: str = "",
                 code: bool = True, literal: bool = False):
        self.ui = ui
        self.indent = indent
        self.style = style
        self.code = code
        """False for reasoning: a model's scratchpad is full of stray
        backticks and half-fences that are not code blocks."""
        self.literal = literal
        """True when the text is known to be a file's contents. Prose drops
        whitespace at the start of a line, which is right for a sentence and
        destroys the indentation of every line of Python."""
        self.buffer = ""
        self.column = 0
        self.in_code = False
        self.language = "text"
        self.started = False
        self.partial = ""
        """The half-written code line, while inside a fence."""
        self.word = ""
        """The word being typed, so a wrap can carry it down whole."""
        self.in_span = False
        """Inside a `code span`. Reset at every newline, so an unpaired
        backtick colours one line rather than the rest of the answer."""
        self.in_bold = False
        self.in_heading = False
        self.pending_star = ""
        """Asterisks held back until it is clear what they are.

        One is held for a character to see whether a second follows, so
        `2 * 3` survives. Two are held for one more, because "**" opens bold
        only when something other than a space comes next -- otherwise
        "2 ** 8 == 256" lost its operator and told you the answer to a
        different sum.
        """
        self.pending_hashes = ""
        self._pen_shown = ""
        """The style the terminal is currently set to, when writing to it
        directly. Only used where there is no bar to redraw through."""
        self.marks_code = not literal
        """Whether backticks mean anything here. Inside a file being written
        they are just characters."""
        self.line = self._blank()
        """The line being written. While the activity bar is up this is the
        bar's lead line rather than terminal output, so a partial line and a
        repainting bar can share the screen."""

    @property
    def width(self) -> int:
        """The wrap column, read fresh every time.

        Captured once in the constructor it could not follow a resize: a
        streamer lives for a whole turn, so widening the window mid-answer
        left the rest of the reply wrapped to the old, narrower column and
        narrowing it ran every line off the edge.

        Capped at the measure, because prose is the one thing here that is
        read rather than scanned. A line of a hundred and sixty characters
        makes the eye lose its place coming back to the left margin -- the
        reason books are not printed the width of the paper.
        """
        room = self.ui.width - cell_len(self.indent) - 1
        return max(30, min(room, MEASURE))

    # -- entry point -------------------------------------------------------

    def feed(self, text: str) -> None:
        # Everything streamed goes through here -- the answer, the reasoning,
        # a file being written -- so it is the one place worth cleaning.
        self.buffer += sanitise(text)
        while self.buffer:
            newline = self.buffer.find("\n")
            if newline != -1:
                self._segment(self.buffer[:newline], end_of_line=True)
                self.buffer = self.buffer[newline + 1:]
                continue

            # Everything else goes out the moment it arrives -- code and
            # prose alike, one character at a time. Holding back the partial
            # word is what made replies land in jumps, and holding back the
            # partial line is what made a function appear all at once when
            # the model finally pressed enter.
            held = self._held_length()
            if held >= len(self.buffer):
                return
            self._segment(self.buffer[:len(self.buffer) - held],
                          end_of_line=False)
            self.buffer = self.buffer[len(self.buffer) - held:]
            return

    def _held_length(self) -> int:
        """How much of the tail cannot be shown yet.

        Exactly one thing can't: text that might turn out to be a fence.
        Three backticks at the start of a line stop being characters and
        become a marker, and once one has been printed as prose there is no
        taking it back -- which is how "``" and then "`" leaked onto the
        screen either side of a code block.

        So a line that has begun with a backtick waits, and nothing else
        does. The wait is at most a couple of characters: the moment the
        line turns out to be `inline code` rather than a fence it is
        released, and a fence's own line is never printed anyway.
        """
        if not self.code:
            return 0
        # A fence only means anything at the start of a line. Mid-expression
        # -- inside a string, in a comment -- a backtick is just a character,
        # and waiting on it would stall the stream.
        if self.in_code:
            if self.partial.strip():
                return 0
        elif self.line.plain:
            return 0
        stripped = self.buffer.lstrip(" \t")
        if not stripped:
            return 0
        if stripped.startswith("```") or "```".startswith(stripped):
            # Either a fence, or still too short to tell. Its whole line
            # goes: the rest of an opening fence is the language name, which
            # is a label rather than text to show.
            return len(self.buffer)
        return 0

    # -- the two modes -----------------------------------------------------

    def _segment(self, text: str, end_of_line: bool) -> None:
        if self.code and (self.in_code or self._opens_a_fence(text)):
            self._code_segment(text, end_of_line)
            return
        if self.literal:
            self._literal(text)
            if end_of_line:
                self._newline()
            return
        self._prose(text)
        if end_of_line:
            self._flush_marks()
            self._newline()

    def _opens_a_fence(self, text: str) -> bool:
        """Whether this segment is a fence opening, rather than prose.

        The segment has to be the start of its line. A chunk boundary can
        fall anywhere, so "see ```` ``` ```` in the docs" arrives as a
        segment beginning with three backticks without being a fence at all.
        """
        return not self.line.plain and text.lstrip().startswith("```")

    def _code_segment(self, text: str, end_of_line: bool) -> None:
        if not end_of_line:
            # A partial fence never reaches here: _held_length keeps the
            # whole of a line that begins with a backtick until its newline
            # arrives, so anything unterminated is code being written now.
            self.partial += text
            self._show_partial()
            return

        whole = self.partial + text
        self.partial = ""
        if whole.lstrip().startswith("```"):
            self._clear_partial()
            if self.in_code:
                self.in_code = False
            else:
                self.in_code = True
                self.language = _language(whole.lstrip()[3:].strip())
                self._ensure_started()
            return
        self._ensure_started()
        self._clear_partial()
        # The gutter is code_line's, not this caller's. Adding two spaces
        # here as well put streamed code four columns in while a block from
        # a tool sat at two -- the same content, two indentations, depending
        # only on who printed it.
        self.ui.code_line(whole, self.language, indent=self.indent)

    def _show_partial(self) -> None:
        """Put the half-written line in the live region, highlighted.

        The bar redraws in place, so the line can grow a character at a time
        without each version being left behind in the scrollback. Without a
        bar there is nowhere to redraw, so the line waits for its newline --
        the old behaviour, which is correct when nothing is pinned.
        """
        if self.ui.bar is None:
            return
        # The same gutter the committed line gets, so a line does not shift
        # sideways at the moment it stops being provisional.
        line = Text(self.indent) + self.ui.code_gutter()
        line.append_text(self.ui.highlight(self.partial, self.language))
        self.ui.bar.set_lead(line)

    def _clear_partial(self) -> None:
        if self.ui.bar is not None:
            self.ui.bar.set_lead(None)

    def _literal(self, text: str) -> None:
        """Every character exactly as written, indentation included.

        No reflowing: a line of code means what its leading whitespace says,
        and a line too long for the terminal is broken at the edge rather
        than rearranged into something that no longer parses.
        """
        for char in text:
            if self.column + cell_len(char) > self.width:
                self._newline(hard=False)
            if not self.column and self.indent:
                self._write(self.indent)
            self._write(char)

    def _prose(self, text: str) -> None:
        """One character at a time, wrapping without splitting words.

        Emitting whole words is easier and reads worse: the answer arrives in
        little jumps, and a model that pauses mid-word appears to have
        stopped. Writing every character as it lands is what makes the reply
        look like it is being typed.

        Wrapping is the reason this is not simply "print the character". The
        line width is only exceeded part-way through a word, and by then the
        word is already on screen -- so it is lifted off the end of the line
        and carried down to the next one, which is what a word processor does
        and what the eye expects.
        """
        for char in text:
            if self.marks_code:
                consumed, char = self._mark(char)
                if consumed:
                    continue

            if char.isspace():
                self.word = ""
                if self.column:
                    self._write(char)
                continue

            if self.column + cell_len(char) > self.width:
                if self._can_carry():
                    self._carry_word_down()
                else:
                    # Either the word is longer than the line, or the line has
                    # already gone to the terminal and cannot be taken back.
                    self._newline(hard=False)
            if not self.column:
                self._write(self.indent)
            self._write(char)
            self.word += char

    def _mark(self, char: str) -> tuple[bool, str]:
        """Handle the markdown a model actually writes, one character at a time.

        `code`, **bold**, and a ## heading. Per character rather than per
        finished line, because a line is not finished when it is drawn --
        by the time it is, its characters have gone to the terminal and
        cannot be restyled.

        Returns (consumed, char): the mark itself is swallowed, everything
        else comes back to be written.
        """
        # Inside a code span nothing is markup but the backtick that ends
        # it. `a**b` is an expression, and reading its asterisks as bold
        # deleted them: what reached the screen was `ab`, which is a
        # different expression that happens to look plausible.
        if self.in_span and self.marks_code:
            if char == "`":
                self.in_span = False
                return True, char
            return False, char

        # A heading is decided by what starts the line. The hashes are held
        # rather than written, because "#" is also just a character and only
        # "## " makes it a heading.
        if self.pending_hashes:
            if char == "#" and len(self.pending_hashes) < 6:
                self.pending_hashes += "#"
                return True, char
            if char == " ":
                self.in_heading = True
                self.pending_hashes = ""
                return True, char
            held, self.pending_hashes = self.pending_hashes, ""
            self._prose_out(held)
        elif char == "#" and not self.column and not self.line.plain:
            self.pending_hashes = "#"
            return True, char

        if self.pending_star == "**":
            # Two asterisks, and now the character that decides what they
            # were. Bold cannot open on a space in markdown, and a model
            # writing arithmetic relies on that: "2 ** 8" is a power, not
            # the start of an emphasis that never ends.
            self.pending_star = ""
            if char.isspace():
                self._prose_out("**")
            else:
                self.in_bold = True
        elif self.pending_star == "*":
            self.pending_star = ""
            if char == "*":
                if self.in_bold:
                    # Closing needs no lookahead: the run that opened it is
                    # what made this a pair.
                    self.in_bold = False
                    return True, char
                self.pending_star = "**"
                return True, char
            self._prose_out("*")          # a lone asterisk, meant literally
        elif char == "*":
            self.pending_star = "*"
            return True, char

        if char == "`":
            self.in_span = True
            return True, char
        return False, char

    def _flush_marks(self) -> None:
        """Write out anything held back that turned out to be literal.

        A line ending on a single "*" was waiting to see whether a second
        followed. None ever does, and without this the asterisk was simply
        dropped.
        """
        held, self.pending_hashes = self.pending_hashes, ""
        held += self.pending_star
        self.pending_star = ""
        if held:
            self._prose_out(held)

    def _prose_out(self, text: str) -> None:
        """Write held-back characters through the ordinary prose path."""
        for char in text:
            if self.column + 1 > self.width:
                self._newline(hard=False)
            if not self.column:
                self._write(self.indent)
            self._write(char)
            self.word += char

    def _can_carry(self) -> bool:
        """Whether lifting the half-written word onto the next line helps.

        Measured in cells rather than characters. A Japanese or Chinese
        character is two cells wide and a run of them has no spaces to break
        at, so a len()-based guard passed every time: the word never looked
        too long for a line it had already overflowed, so it was lifted onto
        a new line, overflowed that one, and was lifted again. A paragraph
        came out as a column of bare indents with one enormous line at the
        end of it.

        It does not help either when the word is all the line holds. Moving
        it to a fresh line leaves the indent behind and puts the word back
        where it started, no closer to fitting. Breaking at the edge is the
        honest answer there.
        """
        if not (self.word and self._rewritable):
            return False
        if cell_len(self.word) + cell_len(self.indent) >= self.width:
            return False
        kept = self.line.plain[: len(self.line.plain) - len(self.word)]
        return bool(kept.strip())

    @property
    def _rewritable(self) -> bool:
        """Whether the line in progress can still be changed.

        While the activity bar is up the line lives inside it and is redrawn
        on every repaint, so a word can be lifted off the end. Written
        straight to the terminal it is already gone, and the only honest
        thing left is to break at the edge.
        """
        return self.ui.bar is not None

    def _carry_word_down(self) -> None:
        """Move the half-written word to the next line, taking it with us.

        The part that stays behind is *sliced*, not rebuilt. It was rebuilt
        from ``self.line.plain`` -- a fresh Text made out of the characters,
        which is every span on the line thrown away. So a line that wrapped
        by carrying a word lost the colour of everything before it: a
        sentence with `a code span` and a **bold** word in it came out
        entirely plain, and it looked deliberate because the wrap was the
        only visible difference.

        Invisible until a wrap fell after something styled. The first
        paragraph of an answer is usually plain up to its first wrap, which
        is why this survived being looked at.
        """
        cut = len(self.line.plain) - len(self.word)
        carried = self.word
        self.line = self.line[:cut]
        self.column = cell_len(self.line.plain)
        self._newline(hard=False)
        self.word = ""
        self._write(self.indent)
        self._write(carried)
        self.word = carried

    # -- output ------------------------------------------------------------

    def _blank(self, text: str = "") -> Text:
        """A fresh line, styled once for all of it."""
        return Text(text, style=self.style or "")

    @property
    def _pen(self) -> str:
        """The style for what is being written right now."""
        if self.in_span:
            return CODE_SPAN
        if self.in_heading:
            return f"bold {ACCENT}"
        if self.in_bold:
            return "bold"
        return ""

    def _stylize(self, style: str, start: int, end: int) -> None:
        """Style a slice, growing the previous span where it can.

        A code span arrives one character at a time like everything else,
        and a span per character is what made a coloured line cost an escape
        pair per letter. Adjacent characters in the same style are one span.
        """
        spans = self.line.spans
        if spans:
            last = spans[-1]
            if last.end == start and last.style == style:
                try:
                    spans[-1] = last._replace(end=end)
                    return
                except AttributeError:
                    pass          # a rich that does not use a NamedTuple
        self.line.stylize(style, start, end)

    def _pen_change(self) -> str:
        """The escape needed to bring the terminal to the current pen.

        Nothing at all when there is no terminal to bring. This is the one
        place in the streamer that writes escapes to ``console.file``
        directly -- a path rich never sees -- so rich's own rule, that
        colour is for terminals and not for redirected output, has to be
        applied here by hand. Without it `wynxo > notes.md` collected a
        truecolor escape pair around every inline-code span in the answer,
        while every other line in the same file came out clean.
        """
        if not self.ui.console.is_terminal:
            return ""
        wanted = self._pen
        if wanted == self._pen_shown:
            return ""
        self._pen_shown = wanted
        if not wanted:
            return "\x1b[0m"
        return _ansi_of(wanted)

    def _write(self, text: str) -> None:
        """Add to the line in progress.

        The activity bar is a rich Live: it repaints by erasing the rows it
        owns, so anything written straight to the terminal on its row is gone
        at the next repaint. That is how "Done, wrote out.txt." came out as
        "out.txt." after a tool call, and why the two-space indent kept
        vanishing from the first line of an answer.

        So while the bar is up, a half-finished line lives *inside* it, as a
        lead line drawn above the status strip. It still grows a word at a
        time; it just grows somewhere the repaint can see. The moment the
        line is complete it is printed normally and scrolls up out of the
        live region like any other output.
        """
        self._ensure_started()
        # Plain unless something is emphasised: the line carries the style,
        # not each character. Appending with a style creates a span per
        # call, and calls are per character now -- which turned every
        # streamed line into one escape pair per letter, ten bytes of colour
        # for each byte of text, all of it kept in the transcript and
        # re-rendered on every repaint.
        start = len(self.line.plain)
        self.line.append(text)
        if pen := self._pen:
            self._stylize(pen, start, start + len(text))
        self.column += cell_len(text)
        if self.ui.bar is not None:
            self.ui.bar.set_lead(self.line)
        else:
            # Straight to the terminal, so the colour has to be written as
            # well as chosen -- and only when it changes, or every character
            # would carry its own escape pair.
            self.ui.console.file.write(self._pen_change() + text)
            self.ui.console.file.flush()

    def _newline(self, hard: bool = True) -> None:
        """End the line in progress.

        ``hard`` is a real end of line in the text. ``hard=False`` is a
        wrap, which is this renderer's own decision about where the terminal
        runs out -- not something the model wrote.

        The difference decides whether emphasis is reset, and it was not
        being made. A wrap inside `a code span` closed the span, so the
        backtick that really closed it *opened* one instead, and the rest of
        the sentence came out in the code colour until the next wrap turned
        it off again. Narrower terminals wrap more, so the answer was most
        wrong exactly where there was least room to spare. The reasoning was
        already written two lines down for the held marks -- "a wrap is not
        the end of a line" -- and the emphasis state was missed by it.
        """
        if self.started:
            if self.ui.bar is not None:
                # A caller may replace the normal SafeConsole with a
                # force-terminal capture console (as the rendering tests and
                # terminal adapters do). Rich 15 still infers no colour from
                # that in-memory stream, so make the same explicit-terminal
                # compatibility adjustment before the Live region commits
                # the styled line.
                _enable_forced_terminal_colour(self.ui.console)
                self.ui.bar.set_lead(None)
                self.ui.console.print(self.line, markup=False, highlight=False,
                                      soft_wrap=True)
            else:
                self.ui.console.file.write(
                    ("\x1b[0m" if self._pen_shown else "") + "\n")
                self.ui.console.file.flush()
        self.line = self._blank()
        self.column = 0
        # Emphasis is reset at a real end of line, so one stray backtick or
        # asterisk cannot colour the rest of the answer -- but not at a
        # wrap, which is where the terminal ran out rather than where the
        # model stopped. What is *held* is never reset here: a half-seen
        # "*" may still pair with the next character either way.
        if hard:
            self.in_span = self.in_bold = self.in_heading = False
        self._pen_shown = ""

    def _ensure_started(self) -> None:
        if not self.started:
            self.ui.console.print()
            self.started = True

    def finish(self) -> str:
        if self.buffer or self.partial:
            self._segment(self.buffer, end_of_line=True)
            self.buffer = ""
        self._flush_marks()
        if self.column:
            self._newline()
        self.in_code = False
        if self.started:
            # A bare print() is already a request for separation rather
            # than for a newline -- SafeConsole drops it when the
            # transcript is on a blank row -- so this is gap() by another
            # name, and it keeps working when a test swaps in a plain rich
            # Console to capture what was written.
            self.ui.console.print()
        return ""


def _words(text: str):
    """Split into words and the whitespace between them, keeping both."""
    current = ""
    for char in text:
        if char == " ":
            if current:
                yield current
                current = ""
            yield " "
        else:
            current += char
    if current:
        yield current


def _language(tag: str) -> str:
    """Normalise a fence tag to something pygments knows."""
    tag = (tag or "text").split()[0].lower() if tag else "text"
    return {"sh": "bash", "shell": "bash", "console": "bash", "py": "python",
            "js": "javascript", "ts": "typescript", "yml": "yaml",
            "": "text"}.get(tag, tag)


class ThoughtStreamer(CodeStreamer):
    """The model's reasoning: same flow, dimmed, at the detail column.

    Reasoning is not code even when it contains backticks, so fence handling
    is off -- a stray ``` in a scratchpad would otherwise swallow the rest of
    the thought into a syntax highlighter.

    Two spaces, not four. The transcript puts heads at column zero and what
    belongs to them at two, and reasoning was the one block still sitting at
    two and four -- a whole indentation scheme of its own for the longest
    thing on the screen.
    """

    def __init__(self, ui: "UI", indent: str = "  "):
        super().__init__(ui, indent=indent, style=MUTED, code=False)

    @staticmethod
    def head(ui: "UI", label: str = "thinking") -> Text:
        """The line a block of reasoning opens with.

        Its own mark rather than the tool arrow: an arrow means wynxo did
        something, and thinking is the model talking to itself. One place, so
        the live block and the replayed ones cannot drift apart.
        """
        line = Text()
        line.append(f"{ui.g.think} ", style=FAINT)
        line.append(label, style=f"bold {MUTED}")
        return line


SURGE_FRAMES = (
    "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588",
    "\u2588\u2587\u2586\u2585\u2584\u2583\u2582\u2581",
)


def celebrate(ui: "UI", label: str, level: int, steps: int) -> None:
    """A band of colour for stepping up, drawn once.

    The alternative -- ``surge`` below -- animates, which needs a place to
    repaint. This is the same idea committed in one line, for the case where
    there is nowhere to repaint (output going to a pipe, or a terminal that
    cannot drive a live region): the band is longer and brighter the further
    up you went, so the top of the scale still reads as an event.
    """
    from rich.text import Text as _T

    sweep = [(255, 120, 200), (255, 96, 190), (246, 74, 186), (228, 64, 190),
             (204, 62, 200), (176, 70, 214), (150, 84, 226), (132, 104, 236)]
    span = max(8, min(46, 8 + level * 7))
    block = "█" if ui.g.unicode else "#"
    row = _T("  ")
    for i in range(span):
        r, g, b = sweep[(i + level * 2) % len(sweep)]
        row.append(block, style=f"#{r:02x}{g:02x}{b:02x}")
    row.append(f"  {label}", style="bold #ff78c8")
    ui.console.print(row)


async def surge(ui: "UI", label: str, style: str, width: int = 34) -> None:
    """A short wave across the line, for stepping up to max or ultra.

    Drawn on one line and then rewritten, so it costs a line of scrollback
    rather than a screenful. Skipped when there is no terminal to animate --
    a pipe would otherwise collect every frame as separate output.
    """
    if not ui.console.is_terminal:
        return
    if not ui.live_ok:
        # Same reason: in-place redrawing has nowhere to happen here.
        ui.console.print(f"  {label}", style=f"bold {style}")
        return
    blocks = SURGE_FRAMES[0] if ui.g.unicode else "-=#"
    span = min(width, max(10, ui.width - 20))
    with Live("", console=ui.console, refresh_per_second=30,
              transient=True) as live:
        for step in range(span + 6):
            bar = Text("  ")
            for cell in range(span):
                distance = abs(cell - step)
                if distance < len(blocks):
                    bar.append(blocks[len(blocks) - 1 - distance], style=style)
                else:
                    bar.append(" ")
            bar.append(f"  {label}", style=f"bold {style}")
            live.update(bar)
            await asyncio.sleep(0.012)


class ActivityBar:
    """The pinned bar: what is happening, and the tokens as they arrive.

    It holds the bottom line while output scrolls above it, and is styled to
    match the prompt's toolbar so the two read as one continuous strip -- the
    bar is there while you type, and stays there while the answer streams.
    """

    SPINNER = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
    SPINNER_ASCII = "|/-\\"

    STILL_MARK = "\u25c9"
    """The activity mark when nothing may move: reduced motion with the
    mascot off. U+25C9 FISHEYE, which is one cell in every locale -- the
    filled circles that read more obviously as a status dot (U+25CF, U+25CB)
    are East Asian Width "Ambiguous" and draw two cells in a CJK terminal,
    which would push the rest of the line sideways."""
    STILL_MARK_ASCII = "*"

    def __init__(self, ui: "UI", effort: str, hint: str = "", model: str = "",
                 pet=None):
        self.ui = ui
        self.state = "idle"
        """The companion's state, set from the tool and the task state. A
        name rather than an enum so nothing here has to import the state
        machine to draw."""
        self.step_started = 0.0
        """When the current activity began -- not the turn.

        Two clocks, because there are two questions. The strip's elapsed is
        how long this turn has taken, which is what you want at the end; the
        scene's is how long *this step* has been running, which is what you
        want while it runs. Sharing one made every step read "· 41.2s" and
        say nothing about the step it was attached to."""
        self.stalled = False
        """Set when nothing has arrived for long enough to be worth saying
        so. Never used to invent activity -- only to stop claiming it."""
        self.effort = effort
        self.hint = hint
        self.model = model
        self.pet = pet
        """The voice, for the lines it says. It no longer draws: the
        companion is the sprite in the scene above the strip."""
        self.activity = "thinking"
        self.detail = ""
        self.tokens = 0
        self.context_pct = 0.0
        self.queued = ""
        """What the user is typing, or how many messages are waiting."""
        self.animate = True
        """Off means a still bar: no sweep, no dots. Follows the same
        setting the companion's animation does."""
        self.plan: str = ""
        """The current todo list, rendered. Held in the live region and
        redrawn in place, rather than printed again on every update -- the
        plan is one thing that changes, not a stream of panels."""
        self.plan_done_frame = 0
        """Non-zero while the completion animation is playing."""
        self.card = None
        """The edit being streamed right now, if any.

        It draws here rather than in the conversation because what is
        written to the terminal is the record: it goes into the scrollback
        and cannot be taken back. The live region is the one place wynxo
        may redraw, so it is where anything provisional belongs."""
        self.lead: Text | None = None
        """A half-written line of the answer, drawn just above the strip.

        Streamed prose cannot be written to the terminal while the bar owns
        that row -- the next repaint erases it. Carrying the partial line
        inside the live region instead lets the answer arrive a word at a
        time without fighting the bar for the same cells."""
        self.started = time.monotonic()
        self.step_started = self.started
        self._seen = self.started
        """When an event last arrived. Only ever used to stop claiming
        activity, never to invent it."""
        self._live: Live | None = None
        self.needs_reanchor = False
        """Set when the terminal changed width, drained by refresh().

        Not acted on where it is set: the bar re-measures the terminal from
        inside its own render, so about half of all resizes are noticed
        during a repaint, and stopping a live region from inside its own
        render does nothing at all -- quietly."""
        self._painted = 0.0
        """When the live region was last repainted at someone's request.
        See REFRESH_INTERVAL: the stream asks once per character, and the
        terminal cannot usefully be asked that often."""
        self._frame = 0
        self._beat: "asyncio.Task | None" = None
        """The repaint heartbeat, while the region is up."""
        self._essential_width = MIN_ACTIVITY_WIDTH
        """Cells the sign of life and the activity word need. Measured while
        the left half is built, and read by the right half to decide what it
        can afford."""

    # -- content -----------------------------------------------------------

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def rate(self) -> float:
        seconds = self.elapsed()
        return self.tokens / seconds if seconds > 0.4 and self.tokens else 0.0

    def _activity_text(self) -> Text:
        """The activity word. Held still, and read rather than watched.

        This used to run a highlight through it a character at a time, so
        every letter changed style on every frame, with cycling dots after
        it and -- on one theme -- a sparkle in front. Together with the
        mascot that is four things moving at twelve frames a second in a
        strip eighty cells wide, and the word itself was the hardest of them
        to actually read. The mascot is the sign of life; the word is the
        answer to what is happening, and answers should hold still.
        """
        out = Text(style=BAR_STYLE)
        if self.activity:
            out.append(self.activity, style="bold")
        return out

    BUSY_CONTEXT = 60.0
    """Context use worth interrupting the line for. Below this it is a number
    nobody is waiting on; above it, a compaction is coming."""

    def _segments(self) -> list[tuple[str, str]]:
        """(text, style) pairs, most important first, for a fit-aware build.

        What a person actually wants from this line while they wait, in
        order: how long has it been, is anything still arriving, and how
        fast. Everything else is a setting rather than news.

        The model name and the context percentage used to live here too,
        and claimed their space *before* the activity did -- so on an
        eighty-column terminal the answer to "what is it doing" was one
        word adrift at the left while facts that had not changed since
        start-up filled the rest. They are on the idle strip under the
        prompt, which is where a setting belongs.
        """
        out: list[tuple[str, str]] = []
        if rate := self.rate():
            out.append((f"{rate:.0f} tok/s", ""))
        if self.context_pct >= self.BUSY_CONTEXT:
            out.append((f"ctx {self.context_pct:.0f}%", ""))
        return out

    def _clock(self) -> str:
        """Elapsed time, with a decimal while a decimal still means something.

        Whole seconds alone read as stopped for the first second of every
        turn -- and the first second is exactly when somebody is watching to
        see whether anything happened at all.
        """
        seconds = self.elapsed()
        return f"{seconds:.1f}s" if seconds < 10 else f"{seconds:.0f}s"

    def _render(self) -> Text:
        self._frame += 1
        width = max(20, self.ui.width)

        # Exactly one thing in this region moves, and it is whichever of
        # these is the sign of life. With the companion drawn it is the
        # companion; without it -- the default -- it is the spinner here.
        # Never both, and never a third.
        left = Text(style=BAR_STYLE)
        if self._companion_drawn():
            # The companion is directly above, animating. A second sign of
            # life on the row underneath it is not reassurance, it is two
            # things moving in six rows.
            left.append(" ")
        elif self.animate:
            frames = self.SPINNER if self.ui.g.unicode else self.SPINNER_ASCII
            left.append(f" {frames[self._frame % len(frames)]}  ",
                        style=f"bold {BAR_ACCENT}")
        else:
            # Reduced motion with no mascot: a still mark, in the same cells
            # the moving one would have used, so turning animation off shifts
            # nothing.
            mark = self.STILL_MARK if self.ui.g.unicode else self.STILL_MARK_ASCII
            left.append(f" {mark}  ", style=f"bold {BAR_ACCENT}")
        if not self._has_scene():
            # The activity word lives one row up, beside the companion,
            # with the seconds it has been running. Repeating it here put
            # "writing" on screen twice, six cells apart, which is the
            # duplicate status this strip is supposed to be free of. It
            # comes back when there is no scene to carry it -- a terminal
            # too narrow for one, or a turn with nothing to say yet.
            left.append_text(self._activity_text())
        # Everything up to here has to survive: the sign of life, and the
        # word saying what is happening.
        self._essential_width = left.cell_len + 2
        if self.queued:
            # And what you are typing, which beats what the agent is doing:
            # you cannot see your own keystrokes anywhere else, and the
            # detail is still one line up. So it counts as essential too.
            left.append("  ")
            left.append(f"\u203a {self.queued}", style=f"bold {BAR_ACCENT}")
            self._essential_width = left.cell_len + 2
        elif self.detail and not self._has_scene():
            # The detail is the flexible part: a long path may lose its tail.
            left.append("  ")
            left.append(self.detail, style=BAR_DIM)

        # What is happening claims its space first, and the numbers take
        # what is left. It used to be the other way round, with a comment
        # saying the token counter was the point of the bar -- it is not.
        # The point is the answer to "what is it doing", and a long file
        # path losing its tail to make room for a model name nobody was
        # reading is the wrong trade.
        # The right-hand group gives way, one item at a time, until what
        # is on the left fits beside it.
        #
        # Order matters and is the whole design of this line. Least
        # important first, because that is the order things are dropped in:
        # the key hint, the rate, the context percentage, the token counter.
        # The clock is never dropped -- it is the one number somebody is
        # actually watching while they wait.
        #
        # What it yields *to* is the sign of life, the word saying what is
        # happening, and whatever the user is typing. Those three used to
        # lose: the metrics claimed their space first, so at thirty columns
        # the answer to "what is it doing" came out as "editin…" while a
        # token counter sat beside it with room to spare, and a message
        # being typed mid-turn was trimmed in favour of a rate nobody had
        # asked for.
        #
        # The clock sits second from the right, behind a hint of constant
        # width, so it holds still instead of sliding left every time a
        # token count appears next to it.
        def _joined(parts: list[tuple[str, str]]) -> Text:
            out = Text(style=BAR_STYLE)
            for text, style in parts:
                if out.cell_len:
                    out.append(f"  {self.ui.g.dot}  ", style=BAR_DIM)
                out.append(text, style=style)
            return out

        clock = (self._clock(), "")
        tokens = [(f"{self.tokens} tok", "bold")] if self.tokens else []
        ladder = [[*self._segments(), *tokens, clock],
                  [*tokens, clock],
                  [clock]]

        essential = min(width, self._essential_width)
        for parts in ladder:
            stats = _joined(parts)
            hint = Text(style=BAR_STYLE)
            if self.hint and parts is ladder[0]:
                hint.append("   ")
                hint.append(self.hint, style=BAR_DIM)
            if essential + stats.cell_len + hint.cell_len + 2 <= width:
                stats.append_text(hint)
                break
        stats.append(" ")

        # One cell of gutter, always: a left half trimmed to exactly the room
        # available put its ellipsis hard against the first character of the
        # stats, and "reading  ca…^C stop" reads as one broken word.
        room = max(0, width - stats.cell_len)
        if left.cell_len > room:
            left.truncate(max(0, room - 1), overflow="ellipsis")
        left.append(" " * max(0, room - left.cell_len))
        left.append_text(stats)
        if left.cell_len > width:
            left.truncate(width)
        return left

    # -- lifecycle ---------------------------------------------------------

    HEARTBEAT = 1.0 / 12
    """How often the region repaints with nothing new to say.

    Enough for a clock counting tenths and for a companion that blinks;
    not so often that a screen with nothing happening on it is being
    redrawn for no reason."""

    def start(self) -> None:
        if not self.ui.live_ok:
            return       # nowhere to repaint: the caller prints instead
        if not self.ui.console.is_terminal:
            return
        # auto_refresh off, and a heartbeat of our own instead. rich's own
        # refresh thread was a second clock: it painted on its schedule
        # while the stream painted on ours, and two unsynchronised clocks
        # put frames 1 ms apart and then 32 ms apart. Erase-and-redraw at
        # irregular intervals is exactly what "less smooth than ollama run"
        # looks like. One clock, one cadence, and the throttle in refresh()
        # coalesces the heartbeat with whatever the stream asked for.
        self._live = Live(self, console=self.ui.console, auto_refresh=False,
                          transient=True)
        self._live.start()
        self._beat = None
        try:
            self._beat = asyncio.ensure_future(self._heartbeat())
        except RuntimeError:
            pass        # no running loop: refresh() on change is all there is

    async def _heartbeat(self) -> None:
        """Repaint on a steady beat, so time keeps passing on screen.

        Not forced: it goes through the same throttle as everything else,
        so while an answer is streaming this costs nothing -- the stream is
        already painting more often than this -- and when nothing is
        arriving it is the only thing keeping the elapsed clock moving.
        """
        while True:
            await asyncio.sleep(self.HEARTBEAT)
            self.refresh()

    STALL_AFTER = 20.0
    """Seconds of silence before saying so.

    Long enough that a model thinking hard about a large prompt is not
    accused of being stuck -- prompt evaluation alone can run past ten
    seconds on a big context -- and short enough that a genuinely dead
    connection does not sit there looking busy for a minute."""

    def update(self, activity: str | None = None, detail: str | None = None,
               tokens: int | None = None, context_pct: float | None = None,
               state: str | None = None) -> None:
        if activity is not None:
            if activity != self.activity:
                self.step_started = time.monotonic()
            self.activity = activity
        if detail is not None:
            self.detail = detail
        if tokens is not None:
            self.tokens = tokens
        if context_pct is not None:
            self.context_pct = context_pct
        if state is not None:
            self.state = state
        self._seen = time.monotonic()
        # A change of what wynxo is doing is an event and lands in the frame
        # it happens. A token count is not: it ticks with every chunk, and
        # nobody reads a number that changes forty times a second.
        self.refresh(force=activity is not None or detail is not None
                     or state is not None)

    def tick(self) -> None:
        """Advance the clocks. Called once per frame, by the frame.

        The elapsed count and the stall check are read from the wall clock
        rather than counted in frames, so a terminal that throttles repaints
        does not slow time down."""
        self.stalled = (time.monotonic() - self._seen) > self.STALL_AFTER

    def add_token(self, count: int = 1) -> None:
        """One streamed chunk is one token, near enough, for a live counter.
        The exact figure arrives with the final chunk and replaces it."""
        self.tokens += count
        self.refresh()

    PLAN_DONE_FRAMES = 8
    """Long enough to register at 12fps, short enough not to be in the way."""

    def set_plan(self, rendered: str) -> None:
        self.plan = rendered or ""
        self.plan_done_frame = 0
        self.refresh(force=True)

    def plan_is_complete(self) -> bool:
        """Every line ticked, and there was at least one."""
        lines = [ln for ln in self.plan.splitlines() if ln.strip()]
        steps = [ln for ln in lines if ln.lstrip().startswith(("[ ]", "[>]", "[x]"))]
        return bool(steps) and all(ln.lstrip().startswith("[x]") for ln in steps)

    async def finish_plan(self) -> None:
        """Tick the whole plan, hold it for a beat, then take it away.

        The point is to show the thing completing rather than having it
        vanish between frames -- a plan that simply disappears reads as
        having been abandoned.
        """
        if not self.plan:
            return
        for frame in range(1, self.PLAN_DONE_FRAMES + 1):
            self.plan_done_frame = frame
            # A deliberate animation, paced by its own sleep. Every frame of
            # it is meant to be seen, so none of them may be coalesced away.
            self.refresh(force=True)
            await asyncio.sleep(0.06)
        self.plan = ""
        self.plan_done_frame = 0
        self.refresh(force=True)

    def _plan_panel(self):
        """The plan, drawn inside the live region.

        One place and one renderer -- the same ``plan_block`` the transcript
        uses, so the plan cannot look like two different things depending on
        which of them drew it. It used to have a second home pinned to the
        top-right corner with a DECSTBM scrolling region, a mechanism that
        costs the terminal its scrollback for the rows below it: the panel
        was bought with the user's ability to scroll back through the
        conversation.
        """
        if not self.plan:
            return None
        return plan_block(self.plan, self.ui.g,
                          complete=bool(self.plan_done_frame))

    def set_lead(self, line: Text | None) -> None:
        """Show (or clear) the line of the answer currently being written.

        Clearing is forced: it happens when the line is about to be
        committed to the scrollback, and a stale copy still in the live
        region for a frame after that is the same text on screen twice.
        """
        self.lead = line
        self.refresh(force=line is None)

    REFRESH_INTERVAL = pacing.TICK
    """The floor between two repaints asked for by the stream.

    The same interval the answer is paced at, and it has to be: the pacer
    releases one piece per frame, and a screen redrawn less often than that
    puts two or three of them on at once -- which is the lumpiness it exists
    to remove, reintroduced one layer down.

    A repaint is not free. Every one erases and redraws the whole pinned
    block -- the plan, the line being written, the strip -- which on a
    two-row bar is around a hundred and fifty bytes of escape sequences.
    Asked for once per streamed character, a five-hundred-character answer
    spent seventy-five kilobytes of terminal traffic to show five hundred
    characters, and on a slow terminal or over ssh the display, not the
    model, is what you are waiting for.

    Nothing is lost by skipping one. Live runs its own refresh thread and
    the renderable is this object, so the next scheduled paint shows the
    newest state whether or not anyone asked for it -- at worst the token
    counter is fifty milliseconds behind, which is under the interval at
    which a human can read it changing.
    """

    def refresh(self, force: bool = False) -> None:
        """Nudge Live to repaint.

        ``force`` for the moments that must land in the frame they happen --
        a tool starting, the bar being taken down -- rather than whenever
        the next frame comes round.
        """
        if self._live is None:
            return
        if self.needs_reanchor:
            self.needs_reanchor = False
            self._reanchor()
            return              # _reanchor repaints on its way out
        now = time.monotonic()
        if not force and now - self._painted < self.REFRESH_INTERVAL:
            return
        self._painted = now
        self._live.refresh()

    def _renderable(self):
        """The pinned block: the card, the line being written, the
        companion and its status, then the strip. Everything here is
        redrawn in place.

        No history of finished calls. Six of them used to be kept and
        redrawn here as "✓ read calc.py (5 lines)  0.00s", which is the
        third rendering of a call the transcript had already committed as
        a block two lines above -- and the block above is the one that
        stays. A pinned region is for what is true *now*; anything that has
        finished belongs to the record, which does not need repainting.

        It also never trimmed its own trailing newline, so the group grew a
        blank row for as long as any call had ever run: the empty line that
        sat between the answer and the next question all session.
        """
        parts = []
        # A card is provisional and the plan is a state; both belong above
        # the strip and neither is ever committed from here. The card first:
        # it is what is happening *now*, and the eye reads down to the strip.
        if self.card is not None and self.card.live:
            body = Text()
            for line in self.card.render(self.ui.g, min(self.ui.width, 100)):
                body.append(line + "\n", style=BAR_DIM)
            body.rstrip()
            parts.append(body)
        # The line being written comes first, directly under the lines
        # already committed above it. It used to sit under the companion,
        # which put six rows of cat between "    try:" and the half-written
        # "        return await self._onc" that continues it -- the one
        # place in the whole layout where a block of code was cut in half
        # by something that was not code. What is being written belongs
        # against what has been; the character and the strip are the
        # furniture underneath both.
        if self.lead is not None and self.lead.plain:
            parts.append(self.lead)
        parts.extend(self._scene())
        parts.append(self._render())
        return parts[0] if len(parts) == 1 else Group(*parts)

    def _scene(self) -> list[Text]:
        """The companion, with what it is doing written beside it.

        Two columns sharing five rows rather than two stacked blocks. The
        plan used to be a list pinned here in full, four or five rows of its
        own that said the same thing on every frame; it is committed to the
        transcript when the steps change and shows here as the one line that
        does not -- which step, and how far along.

        The character is the first thing dropped. It is seventh in the
        hierarchy and the words beside it are third and fourth, so on a
        narrow terminal, with animation off, or where half-blocks will not
        render, the same three lines are drawn without it.
        """
        if not self._has_scene():
            return []
        lines = self._scene_lines()
        if not lines:
            return []
        if not self._companion_drawn():
            return lines
        art = sprite.rows(self.state, self._frame // self.SCENE_PACE,
                          self.ui.palette)
        # The words sit against the middle of the character rather than
        # its top. Aligned to the first row they landed level with the ear
        # tips, with three empty rows beside the face -- which reads as two
        # things that happen to be next to each other rather than as one
        # block. One or two lines against a five-row drawing want centring.
        top = max(0, (sprite.HEIGHT - len(lines)) // 2)
        out = []
        for index in range(sprite.HEIGHT):
            row = Text()
            row.append_text(art[index])
            beside = index - top
            if 0 <= beside < len(lines):
                row.append("  ")
                row.append_text(lines[beside])
            out.append(row)
        return out

    def _step_clock(self) -> str:
        """How long the current step has run, at the strip's precision."""
        seconds = time.monotonic() - (self.step_started or self.started)
        return f"{seconds:.1f}s" if seconds < 10 else f"{seconds:.0f}s"

    def _mark(self) -> str:
        """The one-cell mark in front of the activity word.

        A ring while something is running, and the outcome once it is not.
        It does not spin: the elapsed count beside it already moves, the
        companion beside that already moves, and a third moving thing in
        one six-row region is how a status area becomes a slot machine.
        """
        terminal = {"success": self.ui.g.tick, "error": self.ui.g.cross,
                    "cancelled": self.ui.g.cross}
        return terminal.get(self.state, self.ui.g.busy)

    def _companion_drawn(self) -> bool:
        """Whether the companion is actually on screen this frame.

        The strip asks as well as the scene, because exactly one thing in
        the region may move. With the companion drawn it is the companion;
        without it -- which is the default, since the companion is opt-in
        -- the strip keeps its spinner, or the only thing moving in a
        six-row region would be the tenths digit of a clock.
        """
        return (self.pet is not None and self.pet.enabled
                and self.animate
                and sprite.fits(self.ui.width, self.ui.g.unicode))

    SCENE_PACE = 4
    """Frames of the bar's clock per frame of the companion. The strip
    repaints twelve times a second, which is right for a token counter and
    far too fast for a blink."""

    SCENE_MIN_COLUMNS = 60
    """Below this the live state goes back into the strip, on one row.

    The scene costs three rows and the sprite five. That is a fair trade on
    a terminal with room and a bad one at forty columns, where five rows is
    a third of the screen and the thing being pushed off the top is the
    answer. Under pressure the order is conversation, composer, strip --
    and the strip can say "reading  auth.py" perfectly well by itself."""

    def _has_scene(self) -> bool:
        """Whether the rows above the strip are carrying the live state.

        A predicate rather than ``if self._scene_lines()``: the strip asks
        this three times per frame to decide what it must not repeat, and
        building three throwaway Texts a dozen times a second to answer a
        yes-or-no question is work nobody sees.
        """
        if self.ui.width < self.SCENE_MIN_COLUMNS:
            return False
        return bool(self.activity or self.detail or self.plan)

    def _scene_lines(self) -> list[Text]:
        """What is happening, in words: at most three lines.

        Ordered by the hierarchy -- what the agent is doing, what it is
        doing it to, then how far through the task it is."""
        out = []
        if self.activity:
            head = Text()
            if self.stalled:
                # Never invented activity. When nothing has arrived for long
                # enough to notice, the honest line is that we are waiting,
                # not a spinner implying progress that is not happening.
                head.append(f"{self.ui.g.warn_mark} waiting for model",
                            style=f"bold {WARN}")
            else:
                head.append(f"{self._mark()} ", style=f"bold {BAR_ACCENT}")
                head.append(self.activity, style="bold")
                head.append(f"  {self.ui.g.dot}  {self._step_clock()}",
                            style=BAR_DIM)
            out.append(head)
        if self.detail:
            # No arrow. The transcript's arrow means "a call was made", and
            # putting one here made the live region look like it was
            # announcing a second call that never got a verb. This is the
            # thing the activity above is being done to, so it sits under
            # it and says only that.
            out.append(Text("  " + sanitise(self.detail)[:60], style=BAR_DIM))
        if (step := self._plan_line()) is not None:
            out.append(step)
        return out

    def _plan_line(self) -> "Text | None":
        """The plan as one line: how far, and which step is running."""
        steps = plan_steps(self.plan) if self.plan else []
        if not steps:
            return None
        done = sum(1 for state, _ in steps if state == "done")
        if self.plan_done_frame:
            done = len(steps)
        line = Text("  ")
        line.append(f"{self.ui.g.task} ", style=BAR_ACCENT)
        line.append(f"{done}/{len(steps)}", style="bold")
        current = next((text for state, text in steps if state == "now"), "")
        if current:
            line.append(f"  {current[:44]}", style=BAR_DIM)
        return line

    def __rich_console__(self, console, options):
        """One frame. Measure the terminal, then draw against that width.

        Measuring here rather than inside the parts is what makes a resize
        arrive: this is the only per-frame entry point, and everything the
        frame contains -- the strip, the plan, the card, and the streamer
        writing between frames -- reads ``ui.width``. The SIGWINCH handler
        is a nudge that only fires while nothing else owns the signal, so it
        cannot be the mechanism. Twelve frames a second is twelve ioctls.

        Re-render on every refresh, not just when something calls update().

        Live was being handed the *result* of _renderable() -- a finished
        Text object. Auto-refresh then redrew that same frozen object twelve
        times a second, so the elapsed clock only moved when a token happened
        to arrive and call update(). A model that spends thirty seconds in
        prompt evaluation before its first token showed a stopped clock for
        all thirty of them. Handing Live the bar itself makes each refresh
        recompute.
        """
        self.ui.refresh_size()
        self.tick()
        yield self._renderable()

    def _reanchor(self) -> None:
        """Put the live region back where the cursor is now.

        rich erases its region by moving the cursor up as many rows as its
        last render occupied. That arithmetic starts from where the cursor
        is, and a terminal reflows every line on screen when the window
        changes width -- so after a resize the region no longer knows where
        it is, stops erasing, and appends instead. Resizing during a turn
        filled the rest of it with one copy of the status strip per frame,
        twelve a second, and with the companion on, a column of stranded
        sprite between them.

        Taking it down and putting it back is what re-anchors it: stop()
        leaves the cursor on a fresh line and start() measures from there.
        Never called from inside a render -- see needs_reanchor.
        """
        self.stop()
        self.start()
        self._painted = 0.0
        if self._live is not None:
            self._live.refresh()

    def stop(self) -> None:
        beat, self._beat = self._beat, None
        if beat is not None:
            beat.cancel()
            beat.add_done_callback(lambda t: t.cancelled() or t.exception())
        live, self._live = self._live, None
        if live is not None:
            with contextlib.suppress(Exception):
                live.stop()
            # rich ends a Live by erasing the region and writing a newline
            # of its own, straight past print(). It cannot be dropped -- it
            # is what pushes the region clear before the erase -- so it is
            # counted instead.
            self.ui.console.wrote_elsewhere(1)

    def __enter__(self) -> "ActivityBar":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
