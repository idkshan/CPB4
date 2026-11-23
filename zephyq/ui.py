"""Terminal colors, boxes and tables (turned off automatically when output is piped)."""
from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from typing import List, Optional, Sequence

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
          "blue": "34", "magenta": "35", "cyan": "36", "grey": "90"}
_color = False
_unicode = True


def init(mode: str = "auto") -> None:
    """mode: 'auto' (colors only in a real terminal), 'always' or 'never'."""
    global _color, _unicode
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "─│┌┐└┘├┤┬┴┼›•★".encode(encoding)
        _unicode = True
    except (UnicodeEncodeError, LookupError):
        _unicode = False
    if mode == "never" or (mode == "auto" and os.environ.get("NO_COLOR")):
        _color = False
    elif mode == "always":
        _color = True
    else:
        _color = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    if _color and os.name == "nt":
        _enable_windows_ansi()


def _enable_windows_ansi() -> None:
    """Turn on ANSI escape processing in the Windows console (no-op elsewhere)."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32           # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def color_on() -> bool:
    return _color


# Styling
def style(text: str, *names: str) -> str:
    if not _color or not names:
        return text
    return "\x1b[%sm%s\x1b[0m" % (";".join(_CODES[n] for n in names), text)


def bold(t: str) -> str:
    return style(t, "bold")


def dim(t: str) -> str:
    return style(t, "dim")


def ok(t: str) -> str:
    return style(t, "green")


def warn(t: str) -> str:
    return style(t, "yellow")


def bad(t: str) -> str:
    return style(t, "red")


def info(t: str) -> str:
    return style(t, "cyan")


def tag(label: str, color: str) -> str:
    """A bracketed label such as [update], in bold color."""
    return style("[%s]" % label, "bold", color)


def symbol(name: str) -> str:
    table = {"arrow": ("›", ">"), "dot": ("•", "*"), "star": ("★", "*")}
    return table[name][0 if _unicode else 1]


def visible_len(s: str) -> int:
    return len(_ANSI.sub("", s))


def plain(s: str) -> str:
    return _ANSI.sub("", s)


def pad(s: str, width: int, align: str = "<") -> str:
    gap = max(0, width - visible_len(s))
    if align == ">":
        return " " * gap + s
    if align == "^":
        return " " * (gap // 2) + s + " " * (gap - gap // 2)
    return s + " " * gap


def term_width() -> int:
    cols = shutil.get_terminal_size((100, 24)).columns
    return max(60, min(cols, 120)) - 1


def _box() -> dict:
    if _unicode:
        return dict(tl="┌", tr="┐", bl="└", br="┘", h="─", v="│", lt="├", rt="┤", tt="┬", bt="┴", x="┼")
    return dict(tl="+", tr="+", bl="+", br="+", h="-", v="|", lt="+", rt="+", tt="+", bt="+", x="+")


def truncate(s: str, width: int) -> str:
    if visible_len(s) <= width:
        return s
    s = plain(s)
    ell = "…" if _unicode else "..."
    return s[: max(0, width - len(ell))] + ell


# Panels
def panel(title: str, lines: Sequence[str], color: str = "cyan", width: Optional[int] = None) -> str:
    """A box with a title in the top border.  Long lines are wrapped."""
    b = _box()
    width = width or min(term_width(), 76)
    inner = width - 4
    body: List[str] = []
    for line in lines:
        if visible_len(line) <= inner:
            body.append(line)
        else:
            body.extend(textwrap.wrap(plain(line), inner, subsequent_indent="  ") or [""])
    head = b["h"] + " " + bold(title) + " "
    top = style(b["tl"], color) + style(b["h"], color) + " " + style(bold(title), color) + " " + \
        style(b["h"] * max(0, width - 2 - visible_len(head)), color) + style(b["tr"], color)
    rows = [style(b["v"], color) + " " + pad(line, inner) + " " + style(b["v"], color) for line in body]
    bottom = style(b["bl"] + b["h"] * (width - 2) + b["br"], color)
    return "\n".join([top] + rows + [bottom])


def fields(pairs: Sequence[Sequence[str]], label_width: Optional[int] = None) -> List[str]:
    """Aligned 'Label    value' lines for use inside a panel."""
    label_width = label_width or max(visible_len(k) for k, _ in pairs) + 3
    return [dim(pad(k, label_width)) + v for k, v in pairs]


def rule(title: str = "", color: str = "cyan") -> str:
    """A horizontal line with an optional title: ── Title ─────────"""
    b = _box()
    width = min(term_width(), 76)
    if not title:
        return style(b["h"] * width, color)
    lead = b["h"] * 2 + " "
    return style(lead, color) + bold(title) + " " + style(b["h"] * max(0, width - len(lead) - len(title) - 1), color)


# Tables
def table(headers: Sequence[str], rows: Sequence[Sequence[str]], aligns: Optional[str] = None,
          flex: int = -1, color: str = "grey") -> str:
    """A bordered table.  Column `flex` shrinks (with …) to fit the terminal."""
    b = _box()
    n = len(headers)
    aligns = aligns or "<" * n
    widths = [max([visible_len(h)] + [visible_len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    total = sum(widths) + 3 * n + 1
    if total > term_width():
        f = flex % n
        widths[f] = max(visible_len(headers[f]), widths[f] - (total - term_width()))

    def line(left: str, mid: str, right: str) -> str:
        return style(left + mid.join(b["h"] * (w + 2) for w in widths) + right, color)

    def row(cells: Sequence[str], header: bool = False) -> str:
        out = []
        for i, c in enumerate(cells):
            c = truncate(c, widths[i])
            out.append(" " + pad(bold(c) if header else c, widths[i], aligns[i]) + " ")
        sep = style(b["v"], color)
        return sep + sep.join(out) + sep

    parts = [line(b["tl"], b["tt"], b["tr"]), row(headers, header=True), line(b["lt"], b["x"], b["rt"])]
    parts += [row(r) for r in rows]
    parts.append(line(b["bl"], b["bt"], b["br"]))
    return "\n".join(parts)
