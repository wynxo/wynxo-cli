"""Small cross-platform desktop-control tools for WORK mode.

These tools deliberately do not pretend the model can see the screen. They
cover deterministic computer-use actions: inspect basic desktop state, move or
click the pointer, type text, send shortcuts and scroll. Visual interpretation
can be added later without coupling it to the input backend.

No third-party Python package is required. Linux uses xdotool when installed;
macOS uses System Events through osascript; Windows uses standard Win32 APIs
plus the built-in PowerShell SendKeys implementation for text.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..schema import Field, Schema
from .base import Tool, ToolResult


class ComputerInfoInput(Schema):
    action = Field(
        str,
        "Desktop fact to read: screen_size or active_window.",
        choices=("screen_size", "active_window"),
        default="screen_size",
    )


class ComputerInfo(Tool):
    name = "computer_info"
    description = (
        "Read basic desktop state without changing anything. screen_size "
        "returns the primary display dimensions; active_window identifies "
        "the currently focused application/window when the platform exposes it. "
        "This is not visual screen understanding."
    )
    Input = ComputerInfoInput

    def unavailable(self) -> str:
        if sys.platform.startswith("linux") and not shutil.which("xdotool"):
            return "xdotool is not installed; install it to enable desktop control"
        if sys.platform == "darwin" and not shutil.which("osascript"):
            return "osascript is unavailable"
        return ""

    async def run(self, args: ComputerInfoInput) -> ToolResult:
        try:
            if sys.platform.startswith("linux"):
                return await asyncio.to_thread(_linux_info, args.action)
            if sys.platform == "darwin":
                return await asyncio.to_thread(_mac_info, args.action)
            if os.name == "nt":
                return await asyncio.to_thread(_windows_info, args.action)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return ToolResult.failure(f"computer info failed: {exc}")
        return ToolResult.failure(f"computer use is not supported on {sys.platform}")


class ComputerControlInput(Schema):
    action = Field(
        str,
        "Desktop action: move, click, type, key, or scroll.",
        choices=("move", "click", "type", "key", "scroll"),
    )
    x = Field(int, "Pointer x coordinate. -1 keeps the current position.",
              default=-1, ge=-1, le=100000)
    y = Field(int, "Pointer y coordinate. -1 keeps the current position.",
              default=-1, ge=-1, le=100000)
    button = Field(str, "Mouse button for click.", default="left",
                   choices=("left", "middle", "right"))
    text = Field(str, "Literal text for the type action.", default="")
    key = Field(
        str,
        "Key or shortcut, for example enter, ctrl+l, ctrl+shift+p or alt+f4.",
        default="",
    )
    amount = Field(
        int,
        "Scroll steps. Positive scrolls down; negative scrolls up.",
        default=0, ge=-50, le=50,
    )
    delay_ms = Field(
        int,
        "Delay between typed characters in milliseconds.",
        default=10, ge=0, le=250,
    )


class ComputerControl(Tool):
    name = "computer_control"
    description = (
        "Control the local desktop when the user explicitly asks for computer "
        "interaction. Can move/click the pointer, type literal text, send a "
        "keyboard shortcut, or scroll. It cannot understand pixels or guess "
        "where a control is; use keyboard-driven workflows or coordinates the "
        "user supplied. Never use it merely because a coding task could be "
        "done through the GUI."
    )
    Input = ComputerControlInput
    mutating = True
    concurrency_safe = False

    def unavailable(self) -> str:
        if sys.platform.startswith("linux") and not _linux_control_driver():
            return (
                "no Linux desktop-input backend found; install wdotool for "
                "Wayland/KDE, ydotool + ydotoold, or xdotool for X11"
            )
        if sys.platform == "darwin" and not shutil.which("osascript"):
            return "osascript is unavailable"
        return ""

    async def run(self, args: ComputerControlInput) -> ToolResult:
        error = _validate_action(args)
        if error:
            return ToolResult.failure(error)
        try:
            if sys.platform.startswith("linux"):
                await asyncio.to_thread(_linux_control, args)
            elif sys.platform == "darwin":
                await asyncio.to_thread(_mac_control, args)
            elif os.name == "nt":
                await asyncio.to_thread(_windows_control, args)
            else:
                return ToolResult.failure(
                    f"computer use is not supported on {sys.platform}")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return ToolResult.failure(f"computer control failed: {exc}")

        detail = _action_detail(args)
        return ToolResult.success(
            f"Computer action completed: {detail}.",
            display=detail,
            action=args.action,
        )


def _validate_action(args: ComputerControlInput) -> str:
    if args.action == "move" and (args.x < 0 or args.y < 0):
        return "move requires both x and y coordinates"
    if args.action == "type" and not args.text:
        return "type requires non-empty text"
    if args.action == "key" and not args.key.strip():
        return "key requires a key or shortcut"
    if args.action == "scroll" and args.amount == 0:
        return "scroll requires a non-zero amount"
    if (args.x >= 0) != (args.y >= 0):
        return "x and y must be supplied together"
    return ""


def _action_detail(args: ComputerControlInput) -> str:
    if args.action == "move":
        return f"moved pointer to {args.x},{args.y}"
    if args.action == "click":
        where = f" at {args.x},{args.y}" if args.x >= 0 else ""
        return f"{args.button} click{where}"
    if args.action == "type":
        return f"typed {len(args.text)} character(s)"
    if args.action == "key":
        return f"pressed {args.key}"
    return f"scrolled {args.amount} step(s)"


def _run(argv: list[str], *, env: dict | None = None) -> str:
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env=env,
    )
    if completed.returncode:
        message = (completed.stderr or completed.stdout).strip()
        raise OSError(message or f"{Path(argv[0]).name} exited {completed.returncode}")
    return completed.stdout.strip()


def _linux_info(action: str) -> ToolResult:
    if action == "screen_size":
        out = _run(["xdotool", "getdisplaygeometry"])
        parts = out.split()
        if len(parts) != 2:
            raise ValueError(f"unexpected xdotool geometry: {out!r}")
        return ToolResult.success(
            f"Primary screen: {parts[0]}x{parts[1]}.",
            display=f"screen {parts[0]}x{parts[1]}",
            width=int(parts[0]), height=int(parts[1]),
        )
    window = _run(["xdotool", "getactivewindow", "getwindowname"])
    return ToolResult.success(
        f"Active window: {window or '(unknown)'}.",
        display=f"active window: {window or 'unknown'}",
        window=window,
    )


def _linux_control_driver() -> str:
    """Best available Linux input backend for this session."""
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    if wayland and shutil.which("wdotool"):
        return "wdotool"
    if wayland and shutil.which("ydotool"):
        return "ydotool"
    if shutil.which("xdotool"):
        return "xdotool"
    if shutil.which("wdotool"):
        return "wdotool"
    if shutil.which("ydotool"):
        return "ydotool"
    return ""


def _linux_control(args: ComputerControlInput) -> None:
    driver = _linux_control_driver()
    if driver == "wdotool":
        _wdotool_control(args)
        return
    if driver == "ydotool":
        _ydotool_control(args)
        return
    if driver != "xdotool":
        raise OSError("no supported Linux desktop-input backend is installed")

    if args.action == "move":
        _run(["xdotool", "mousemove", "--sync", str(args.x), str(args.y)])
        return
    if args.action == "click":
        if args.x >= 0:
            _run(["xdotool", "mousemove", "--sync", str(args.x), str(args.y)])
        button = {"left": "1", "middle": "2", "right": "3"}[args.button]
        _run(["xdotool", "click", button])
        return
    if args.action == "type":
        _run(["xdotool", "type", "--clearmodifiers", "--delay",
              str(args.delay_ms), "--", args.text])
        return
    if args.action == "key":
        _run(["xdotool", "key", "--clearmodifiers", args.key.strip()])
        return
    button = "5" if args.amount > 0 else "4"
    _run(["xdotool", "click", "--repeat", str(abs(args.amount)), button])


def _wdotool_control(args: ComputerControlInput) -> None:
    if args.action == "move":
        _run(["wdotool", "mousemove", str(args.x), str(args.y)])
        return
    if args.action == "click":
        if args.x >= 0:
            _run(["wdotool", "mousemove", str(args.x), str(args.y)])
        button = {"left": "1", "middle": "2", "right": "3"}[args.button]
        _run(["wdotool", "click", button])
        return
    if args.action == "type":
        _run(["wdotool", "type", "--delay", str(args.delay_ms), args.text])
        return
    if args.action == "key":
        _run(["wdotool", "key", "--clearmodifiers", args.key.strip()])
        return
    _run(["wdotool", "scroll", "0", str(args.amount)])


_LINUX_KEYS = {
    "esc": 1, "escape": 1, "tab": 15, "enter": 28, "return": 28,
    "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "space": 57,
    "backspace": 14, "delete": 111, "home": 102, "up": 103,
    "pageup": 104, "left": 105, "right": 106, "end": 107,
    "down": 108, "pagedown": 109, "super": 125, "meta": 125, "win": 125,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
    "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7,
    "7": 8, "8": 9, "9": 10, "0": 11,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21,
    "u": 22, "i": 23, "o": 24, "p": 25, "a": 30, "s": 31,
    "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37,
    "l": 38, "z": 44, "x": 45, "c": 46, "v": 47, "b": 48,
    "n": 49, "m": 50,
}
_LINUX_MODIFIERS = frozenset(
    {"ctrl", "control", "shift", "alt", "super", "meta", "win"}
)


def _ydotool_key_sequence(raw: str) -> list[str]:
    pieces = [part.strip().lower() for part in raw.split("+") if part.strip()]
    if not pieces:
        raise ValueError("empty key")
    modifiers = [part for part in pieces if part in _LINUX_MODIFIERS]
    normal = [part for part in pieces if part not in _LINUX_MODIFIERS]
    if len(normal) != 1:
        raise ValueError(f"unsupported ydotool shortcut {raw!r}")
    try:
        mod_codes = [_LINUX_KEYS[part] for part in modifiers]
        key_code = _LINUX_KEYS[normal[0]]
    except KeyError as exc:
        raise ValueError(f"unsupported ydotool key {exc.args[0]!r}") from None
    sequence = [f"{code}:1" for code in mod_codes]
    sequence.extend((f"{key_code}:1", f"{key_code}:0"))
    sequence.extend(f"{code}:0" for code in reversed(mod_codes))
    return sequence


def _ydotool_control(args: ComputerControlInput) -> None:
    if args.action == "move":
        _run(["ydotool", "mousemove", "--absolute", "-x", str(args.x),
              "-y", str(args.y)])
        return
    if args.action == "click":
        if args.x >= 0:
            _run(["ydotool", "mousemove", "--absolute", "-x", str(args.x),
                  "-y", str(args.y)])
        button = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}[args.button]
        _run(["ydotool", "click", button])
        return
    if args.action == "type":
        _run(["ydotool", "type", "--key-delay", str(args.delay_ms), args.text])
        return
    if args.action == "key":
        _run(["ydotool", "key", *_ydotool_key_sequence(args.key)])
        return
    raise OSError(
        "ydotool does not expose wheel scrolling; use key=pageup/pagedown "
        "or install wdotool for native Wayland scroll events"
    )


def _mac_info(action: str) -> ToolResult:
    if action == "screen_size":
        out = _run([
            "osascript", "-e",
            'tell application "Finder" to get bounds of window of desktop',
        ])
        parts = [p.strip() for p in out.split(",")]
        if len(parts) != 4:
            raise ValueError(f"unexpected desktop bounds: {out!r}")
        width = int(parts[2]) - int(parts[0])
        height = int(parts[3]) - int(parts[1])
        return ToolResult.success(
            f"Primary screen: {width}x{height}.",
            display=f"screen {width}x{height}",
            width=width, height=height,
        )
    app = _run([
        "osascript", "-e",
        'tell application "System Events" to get name of first application process whose frontmost is true',
    ])
    return ToolResult.success(
        f"Active application: {app or '(unknown)'}.",
        display=f"active app: {app or 'unknown'}",
        window=app,
    )


def _mac_control(args: ComputerControlInput) -> None:
    if args.action == "move":
        raise OSError("pointer move without clicking is not supported on macOS")
    if args.action == "click":
        if args.x < 0:
            raise OSError("macOS click requires x and y coordinates")
        _run([
            "osascript", "-e",
            f'tell application "System Events" to click at {{{args.x}, {args.y}}}',
        ])
        return
    if args.action == "type":
        script = (
            'on run argv\n'
            'tell application "System Events" to keystroke (item 1 of argv)\n'
            'end run'
        )
        _run(["osascript", "-e", script, args.text])
        return
    if args.action == "key":
        _run(["osascript", "-e", _mac_key_script(args.key)])
        return
    code = "121" if args.amount > 0 else "116"
    script = (
        'tell application "System Events" to repeat '
        f'{abs(args.amount)} times\nkey code {code}\nend repeat'
    )
    _run(["osascript", "-e", script])


def _mac_key_script(raw: str) -> str:
    pieces = [p.strip().lower() for p in raw.replace("+", " ").split() if p.strip()]
    if not pieces:
        raise ValueError("empty key")
    mods = []
    aliases = {
        "cmd": "command down", "command": "command down",
        "ctrl": "control down", "control": "control down",
        "alt": "option down", "option": "option down",
        "shift": "shift down",
    }
    while pieces and pieces[0] in aliases:
        mods.append(aliases[pieces.pop(0)])
    key = "+".join(pieces)
    special = {
        "enter": 36, "return": 36, "tab": 48, "escape": 53, "esc": 53,
        "space": 49, "backspace": 51, "delete": 117,
        "left": 123, "right": 124, "down": 125, "up": 126,
        "pagedown": 121, "pageup": 116, "home": 115, "end": 119,
    }
    using = f" using {{{', '.join(mods)}}}" if mods else ""
    if key in special:
        return f'tell application "System Events" to key code {special[key]}{using}'
    if len(key) == 1:
        escaped = key.replace("\\", "\\\\").replace('"', '\\"')
        return f'tell application "System Events" to keystroke "{escaped}"{using}'
    raise ValueError(f"unsupported macOS key {raw!r}")


def _windows_info(action: str) -> ToolResult:
    user32 = ctypes.windll.user32
    if action == "screen_size":
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))
        return ToolResult.success(
            f"Primary screen: {width}x{height}.",
            display=f"screen {width}x{height}",
            width=width, height=height,
        )
    hwnd = user32.GetForegroundWindow()
    size = user32.GetWindowTextLengthW(hwnd) + 1
    buffer = ctypes.create_unicode_buffer(max(size, 2))
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    title = buffer.value
    return ToolResult.success(
        f"Active window: {title or '(unknown)'}.",
        display=f"active window: {title or 'unknown'}",
        window=title,
    )


def _windows_control(args: ComputerControlInput) -> None:
    user32 = ctypes.windll.user32
    if args.action == "move":
        if not user32.SetCursorPos(args.x, args.y):
            raise OSError("SetCursorPos failed")
        return
    if args.action == "click":
        if args.x >= 0 and not user32.SetCursorPos(args.x, args.y):
            raise OSError("SetCursorPos failed")
        flags = {
            "left": (0x0002, 0x0004),
            "right": (0x0008, 0x0010),
            "middle": (0x0020, 0x0040),
        }[args.button]
        user32.mouse_event(flags[0], 0, 0, 0, 0)
        user32.mouse_event(flags[1], 0, 0, 0, 0)
        return
    if args.action == "scroll":
        user32.mouse_event(0x0800, 0, 0, -120 * args.amount, 0)
        return
    if args.action == "type":
        _windows_sendkeys(_escape_sendkeys(args.text))
        return
    _windows_sendkeys(_shortcut_to_sendkeys(args.key))


def _powershell() -> str:
    return shutil.which("powershell") or shutil.which("pwsh") or "powershell"


def _windows_sendkeys(sequence: str) -> None:
    env = os.environ.copy()
    env["WYNXO_SENDKEYS"] = sequence
    _run([
        _powershell(), "-NoProfile", "-NonInteractive", "-Command",
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.SendKeys]::SendWait($env:WYNXO_SENDKEYS)",
    ], env=env)


def _escape_sendkeys(text: str) -> str:
    out = []
    for char in text:
        if char in "+^%~(){}[]":
            out.append("{" + char + "}")
        else:
            out.append(char)
    return "".join(out)


def _shortcut_to_sendkeys(raw: str) -> str:
    parts = [p.strip().lower() for p in raw.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key")
    prefix = ""
    aliases = {"ctrl": "^", "control": "^", "alt": "%", "shift": "+"}
    while parts and parts[0] in aliases:
        prefix += aliases[parts.pop(0)]
    key = "+".join(parts)
    special = {
        "enter": "{ENTER}", "return": "{ENTER}", "tab": "{TAB}",
        "escape": "{ESC}", "esc": "{ESC}", "space": " ",
        "backspace": "{BACKSPACE}", "delete": "{DELETE}",
        "left": "{LEFT}", "right": "{RIGHT}", "down": "{DOWN}", "up": "{UP}",
        "pagedown": "{PGDN}", "pageup": "{PGUP}", "home": "{HOME}", "end": "{END}",
    }
    if key in special:
        return prefix + special[key]
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return prefix + "{" + key.upper() + "}"
    if len(key) == 1:
        return prefix + key
    raise ValueError(f"unsupported Windows key {raw!r}")


__all__ = [
    "ComputerInfo", "ComputerControl",
    "ComputerInfoInput", "ComputerControlInput",
]
