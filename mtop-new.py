#!/usr/bin/env python3
"""mtop - a live picture of what your Mac is doing (Apple chip or Intel).

Three screens, switched with 1 / 2 / 3, Tab, or a click on their names:
  1 Overview        CPU, GPU, memory, temperatures, SSD, battery, network, power, busiest apps
  2 Ports & power   the Mac seen from above: what is plugged in where, watts in and out, heat
  3 Core            the chip as a living thing: every core in orbit, big dials

Run:   python3 mtop.py             (asks for your password: the CPU / GPU watt split needs an administrator)
Basic: python3 mtop.py --no-sudo   (no password; everything except that split and the clock speeds)
Demo:  python3 mtop.py --demo      (made-up numbers, no password, any computer)

Keys:  q quit    1 2 3 or Tab change screen    m motion: smooth, still or eco
       on the Overview: up / down select an app, k quit it, s sort by CPU or RAM, c clear the RAM cache
Mouse: every button, screen name and app row can be clicked; the wheel moves the selection.

Standard library only. mtop reads macOS `powermetrics`, the SMC and HID sensors, the kernel's own
counters, `ps`, `ioreg`, `system_profiler` and `diskutil`. It never writes to hardware.
"""
import ctypes
import math
import os
import re
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from collections import deque

# ============================================================ colours
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
REV = "\x1b[7m"
TEXT = "\x1b[39m" + BOLD          # terminal's own text colour: readable on light and dark themes
WHITE = (255, 255, 255)

TRUECOLOR = True                  # set in main(); False falls back to the 256-colour palette
_esc = {}


def _to256(r, g, b):
    if abs(r - g) < 12 and abs(g - b) < 12 and abs(r - b) < 12:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + min(23, int(round((r - 8) / 247.0 * 24)))
    q = lambda v: int(round(v / 255.0 * 5))
    return 16 + 36 * q(r) + 6 * q(g) + q(b)


def rgb(c, bg=False):
    """Escape code for an (r, g, b) colour."""
    # snapped to steps of 8: animation would otherwise create endless one-off colours to remember
    c = (c[0] & 0xF8, c[1] & 0xF8, c[2] & 0xF8)
    key = (c, bg)
    code = _esc.get(key)
    if code is None:
        if len(_esc) > 6000:
            _esc.clear()
        lead = 48 if bg else 38
        code = "\x1b[%d;2;%d;%d;%dm" % ((lead,) + c) if TRUECOLOR else "\x1b[%d;5;%dm" % (lead, _to256(*c))
        _esc[key] = code
    return code


def grad(stops, t):
    """Colour at position t (0..1) along a list of colour stops."""
    t = min(1.0, max(0.0, t))
    if len(stops) == 1:
        return stops[0]
    x = t * (len(stops) - 1)
    i = min(int(x), len(stops) - 2)
    a, b, f = stops[i], stops[i + 1], x - i
    return (int(a[0] + (b[0] - a[0]) * f + 0.5), int(a[1] + (b[1] - a[1]) * f + 0.5), int(a[2] + (b[2] - a[2]) * f + 0.5))


def mix(a, b, f):
    """Colour a blended towards colour b by f (0..1)."""
    return grad((a, b), f)


# palette: mid-tone, saturated colours that hold up on dark and light backgrounds
P_MUTED = (140, 140, 150)
P_DIM = (108, 108, 120)
P_BORDER = (92, 95, 115)
P_SEG = (66, 68, 86)        # unlit segments of a meter, resting tracks
P_ACCENT = (70, 130, 245)
P_E = (150, 110, 255)       # efficiency cores
P_P = (255, 85, 135)        # performance cores
P_GPU = (0, 190, 178)
P_RAM = (238, 160, 35)
P_PWR = (72, 192, 98)
P_TMP = (245, 122, 50)
P_DSK = (84, 144, 242)
P_NET = (205, 100, 228)
P_UP = (60, 175, 232)
GOOD, WARN, HOT, BAD = (60, 196, 110), (232, 190, 40), (245, 135, 45), (235, 64, 72)

G_LOAD = [GOOD, WARN, HOT, BAD]
G_TEMP = [GOOD, GOOD, GOOD, (150, 200, 80), WARN, HOT, BAD]
G_E = [(92, 92, 225), (160, 110, 255), (222, 130, 255)]
G_P = [(232, 60, 132), (255, 95, 120), (255, 162, 80)]
G_GPU = [(0, 150, 172), (0, 200, 180), (96, 230, 160)]
G_RAM = [(216, 122, 22), (240, 165, 35), (250, 208, 70)]
G_PWR = [(40, 160, 92), (80, 200, 100), (172, 225, 90)]
G_DSK = [(62, 112, 222), (100, 172, 250)]
G_NET = [(150, 82, 222), (218, 102, 226)]
G_UP = [(40, 140, 222), (72, 202, 236)]
G_LOGO = [(150, 110, 255), (255, 85, 135), (255, 162, 80)]
G_CPU = [(120, 100, 250), (220, 90, 200), (255, 95, 120), (255, 162, 80)]      # every core together
HEAT = [(245, 170, 60), (245, 95, 50), (235, 45, 55)]
SEG_APP, SEG_WIRED, SEG_COMP, SEG_CACHE = (240, 165, 35), (236, 96, 76), (190, 102, 232), (92, 132, 176)

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
TOKEN_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|.", re.S)
VBAR = " ▁▂▃▄▅▆▇█"
HBAR = " ▏▎▍▌▋▊▉█"


def vlen(s):
    return len(ANSI_RE.sub("", s))


def pad(s, width):
    return s + " " * max(0, width - vlen(s))


def clip(s, width):
    """Cut a coloured string to `width` visible characters."""
    out, seen = [], 0
    for tok in TOKEN_RE.findall(s):
        if tok[0] == "\x1b":
            out.append(tok)
        elif seen < width:
            out.append(tok)
            seen += 1
    return "".join(out) + RESET


def fit(s, width):
    return pad(clip(s, width) if vlen(s) > width else s, width)


def cut(s, n):
    return s if len(s) <= n else s[:max(0, n - 1)] + "…"


def muted(s):
    return rgb(P_MUTED) + s + RESET


def dim(s):
    return rgb(P_DIM) + s + RESET


def load_rgb(frac):
    return GOOD if frac < 0.5 else WARN if frac < 0.75 else HOT if frac < 0.9 else BAD


def temp_rgb(c):
    return GOOD if c < 70 else WARN if c < 85 else HOT if c < 95 else BAD


def pct(frac):
    """'42%' in the text colour, or in a warning colour when the value is high."""
    col = BOLD + rgb(load_rgb(frac)) if frac >= 0.75 else TEXT
    return col + "%3d%%" % round(frac * 100) + RESET


def bar(frac, width, stops=G_LOAD, track="░"):
    """Instrument-style meter: lit segments with a hairline gap between them, colour running along it."""
    frac = min(1.0, max(0.0, frac))
    cells = frac * width
    full = int(cells)
    rest = cells - full
    part = "▌" if rest >= 0.5 else "▎" if rest >= 0.15 or (frac > 0.004 and full == 0) else ""
    if full >= width:
        part = ""
    out, last = [], None
    for i in range(full + (1 if part else 0)):
        col = rgb(grad(stops, i / float(max(1, width - 1))))
        if col != last:
            out.append(col)
            last = col
        out.append("▊" if i < full else part)
    used = full + (1 if part else 0)
    if track == " ":
        return "".join(out) + RESET + " " * (width - used)
    return "".join(out) + rgb(P_SEG) + "▊" * (width - used) + RESET


def stacked(segments, total, width):
    """One segmented meter split into coloured parts: [(value, colour), ...] out of `total`."""
    out, last = [], None
    for i in range(width):
        x, acc, col = (i + 0.5) / width * total, 0.0, None
        for value, c in segments:
            acc += value
            if x < acc:
                col = c
                break
        code = rgb(col or P_SEG)
        if code != last:
            out.append(code)
            last = code
        out.append("▊")
    return "".join(out) + RESET


def graph(values, width, height, top, stops, bottom=0.0, invert=False):
    """Filled braille area graph, `height` rows tall, two samples per character.

    Returns a list of `height` strings. With invert=True the area hangs from the top.
    """
    n = width * 2
    vals = list(values)[-n:] if n > 0 else []
    vals = [None] * (n - len(vals)) + vals
    dots = height * 4
    level, frac = [], []
    for v in vals:
        if v is None:
            level.append(0)
            frac.append(0.0)
        else:
            f = 0.0 if top <= bottom else min(1.0, max(0.0, (v - bottom) / float(top - bottom)))
            level.append(max(1, int(round(f * dots))))
            frac.append(f)
    left, right = (0x40, 0x04, 0x02, 0x01), (0x80, 0x20, 0x10, 0x08)     # dots, bottom to top
    rows = []
    for r in range(height):
        base = (r if invert else height - 1 - r) * 4
        depth = (base / 4.0 + 1) / height
        line, last = [], None
        for c in range(width):
            bits = 0
            for side, idx in ((left, 2 * c), (right, 2 * c + 1)):
                for j in range(min(4, max(0, level[idx] - base))):
                    bits |= side[3 - j] if invert else side[j]
            if not bits:
                line.append(" ")
                continue
            col = rgb(grad(stops, max(frac[2 * c], frac[2 * c + 1]) * (0.55 + 0.45 * depth)))
            if col != last:
                line.append(col)
                last = col
            line.append(chr(0x2800 + bits))
        rows.append("".join(line) + RESET)
    return rows


def graph_rows(values, width, height, top, stops, bottom=0.0, hi="", lo="", invert=False):
    """Graph with a 7-character gutter holding the scale labels."""
    out = []
    for i, row in enumerate(graph(values, width, height, top, stops, bottom, invert)):
        label = hi if i == 0 and (height > 1 or not lo) else lo if i == height - 1 else ""
        out.append(dim("%6s " % label) + row)
    return out


def human_rate(b):
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if b < 1000 or unit == "GB/s":
            return ("%.0f %s" if unit == "B/s" else "%.1f %s") % (b, unit)
        b /= 1000.0


def short_rate(b):
    for unit in ("B", "K", "M", "G"):
        if b < 1000 or unit == "G":
            return ("%.0f%s" if unit == "B" or b >= 100 else "%.1f%s") % (b, unit)
        b /= 1000.0


def human_mem(mb):
    return "%.0f MB" % mb if mb < 1000 else "%.1f GB" % (mb / 1024.0)


def human_size(b):
    """Drive sizes the way they are printed on the box: 256 GB, 1 TB."""
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if b >= div * 0.95 or unit == "MB":
            v = b / div
            return ("%.0f %s" if v >= 9.95 or abs(v - round(v)) < 0.06 else "%.1f %s") % (v, unit)


def human_wh(wh):
    return "%.0f mWh" % (wh * 1000) if wh < 1 else "%.2f Wh" % wh


def panel(title, lines, width, color=P_ACCENT, right=""):
    """A framed instrument panel: bright corner brackets in the panel's colour, hairline edges."""
    inner = width - 4
    title = title.upper()
    extra = vlen(right) + 2 if right else 0
    fill = width - 7 - len(title) - extra
    if fill < 1:
        right, fill = "", max(0, width - 7 - len(title))
    b, a = rgb(P_BORDER), BOLD + rgb(color)
    top = (a + "┏━ " + title + " " + RESET + b + "╶" + "─" * max(0, fill - 1) + RESET
           + (" " + right + RESET + " " if right else "") + a + "━┓" + RESET)
    body = [b + "│ " + RESET + fit(l, inner) + b + " │" + RESET for l in lines]
    return [top] + body + [a + "┗" + RESET + b + "─" * (width - 2) + a + "┛" + RESET]


def side_by_side(a, b, wa, wb):
    edge = rgb(P_BORDER)
    blank = lambda w: edge + "│" + RESET + " " * (w - 2) + edge + "│" + RESET
    h = max(len(a), len(b))
    a = a[:-1] + [blank(wa)] * (h - len(a)) + a[-1:]
    b = b[:-1] + [blank(wb)] * (h - len(b)) + b[-1:]
    return [x + " " + y for x, y in zip(a, b)]


def gauge(label, frac, color, value_txt, extra, bw, stops=G_LOAD):
    return (BOLD + rgb(color) + "%-6s" % label + RESET + " " + bar(frac, bw, stops) + " "
            + value_txt + RESET + " " + muted(extra))


def dot(color, text):
    return rgb(color) + "● " + RESET + text


# ============================================================ the screen
class Screen:
    """What the terminal is showing right now. Only the characters that changed are sent to it,
    so the terminal app has as little work as possible."""

    BLANK = "\x1b[0m"

    def __init__(self):
        self.size = None
        self.raw = []          # each row's coloured text, as last drawn
        self.chars = []        # each row as a list of characters ...
        self.styles = []       # ... and the matching list of styles
        self._ops = {}
        self._codes = {}

    def _apply(self, tok, state):
        """Fold one colour code into the running [bold, reverse, colour, background] state."""
        ops = self._ops.get(tok)
        if ops is None:
            ops = []
            if tok.endswith("m"):
                p = tok[2:-1].split(";")
                i = 0
                while i < len(p):
                    v = p[i]
                    if v in ("", "0"):
                        ops.append((4, None))
                    elif v in ("1", "22"):
                        ops.append((0, v == "1"))
                    elif v in ("7", "27"):
                        ops.append((1, v == "7"))
                    elif v == "39":
                        ops.append((2, ""))
                    elif v == "49":
                        ops.append((3, ""))
                    elif v in ("38", "48") and i + 1 < len(p):
                        n = 4 if p[i + 1] == "2" else 2
                        ops.append((2 if v == "38" else 3, ";".join(p[i:i + n + 1])))
                        i += n
                    elif v.isdigit() and (30 <= int(v) <= 37 or 90 <= int(v) <= 97):
                        ops.append((2, v))
                    elif v.isdigit() and (40 <= int(v) <= 47 or 100 <= int(v) <= 107):
                        ops.append((3, v))
                    i += 1
            if len(self._ops) > 8000:
                self._ops.clear()
            self._ops[tok] = ops
        for slot, val in ops:
            if slot == 4:
                state[0] = state[1] = False
                state[2] = state[3] = ""
            else:
                state[slot] = val

    def _code(self, state):
        key = (state[0], state[1], state[2], state[3])
        code = self._codes.get(key)
        if code is None:
            code = ("\x1b[0" + (";1" if key[0] else "") + (";7" if key[1] else "")
                    + (";" + key[2] if key[2] else "") + (";" + key[3] if key[3] else "") + "m")
            if len(self._codes) > 8000:
                self._codes.clear()
            self._codes[key] = code
        return code

    def cells(self, line, cols):
        """A coloured line as two lists, `cols` long: its characters and each one's complete style."""
        chars, styles = [], []
        state = [False, False, "", ""]
        code = self.BLANK
        for tok in TOKEN_RE.findall(line):
            if tok[0] == "\x1b":
                if len(tok) > 1:
                    self._apply(tok, state)
                    code = self._code(state)
            elif len(chars) < cols:
                chars.append(tok)
                # an empty cell looks the same in any text colour
                styles.append(self.BLANK if tok == " " and not state[1] and not state[3] else code)
        n = cols - len(chars)
        if n > 0:
            chars += [" "] * n
            styles += [self.BLANK] * n
        return chars, styles

    def snapshot(self):
        return [list(r) for r in self.chars], [list(r) for r in self.styles]

    def draw(self, lines, cols, rows, window=None, behind=None, edge=None):
        """The text to send so the terminal shows `lines`. Empty when nothing changed.

        window=(x0, x1) is for transitions: only those columns come from `lines`; the others
        keep showing `behind` (an earlier snapshot) or nothing. `edge` is a style for the
        sweeping line drawn where the new picture ends.
        """
        out = []
        if (cols, rows) != self.size:
            self.size = (cols, rows)
            self.raw = [None] * rows
            self.chars = [[" "] * cols for _ in range(rows)]
            self.styles = [[self.BLANK] * cols for _ in range(rows)]
            out.append("\x1b[0m\x1b[2J")
        cur = None
        for y in range(rows):
            line = lines[y] if y < len(lines) else ""
            if window is None and line == self.raw[y]:
                continue
            chars, styles = self.cells(line, cols)
            if window is not None:
                x0, x1 = max(0, window[0]), min(cols, window[1])
                if behind and y < len(behind[0]) and len(behind[0][y]) == cols:
                    bc, bs = behind[0][y], behind[1][y]
                else:
                    bc, bs = [" "] * cols, [self.BLANK] * cols
                chars = bc[:x0] + chars[x0:x1] + bc[x1:] if x0 < x1 else list(bc)
                styles = bs[:x0] + styles[x0:x1] + bs[x1:] if x0 < x1 else list(bs)
                if edge:
                    for ex in (window[0] - 1, window[1]):
                        if 0 < ex < cols - 1:
                            chars[ex], styles[ex] = "┃", edge
            old_c, old_s = self.chars[y], self.styles[y]
            self.raw[y] = line if window is None else None
            if chars == old_c and styles == old_s:
                continue
            x = 0
            while x < cols:
                if chars[x] == old_c[x] and styles[x] == old_s[x]:
                    x += 1
                    continue
                end, gap, k = x + 1, 0, x + 1
                while k < cols and gap < 4:           # bridge short unchanged stretches: cheaper than moving
                    if chars[k] != old_c[k] or styles[k] != old_s[k]:
                        end, gap = k + 1, 0
                    else:
                        gap += 1
                    k += 1
                out.append("\x1b[%d;%dH" % (y + 1, x + 1))
                for k in range(x, end):
                    if styles[k] != cur:
                        cur = styles[k]
                        out.append(cur)
                    out.append(chars[k])
                x = end
            self.chars[y], self.styles[y] = chars, styles
        if out:
            out.append("\x1b[0m")
        return "".join(out)


# ============================================================ reading the system
def sh(cmd, timeout=5):
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=timeout).decode(errors="ignore").strip()
    except Exception:
        return ""


_LIBC = []


def libc():
    """macOS's system library, for the kernel counters that need no helper program."""
    if not _LIBC:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        size_p, void_p, c_int = ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_int
        lib.sysctlbyname.argtypes = [ctypes.c_char_p, void_p, size_p, void_p, ctypes.c_size_t]
        lib.sysctl.argtypes = [ctypes.POINTER(c_int), ctypes.c_uint, void_p, size_p, void_p, ctypes.c_size_t]
        lib.mach_host_self.restype = ctypes.c_uint32
        lib.host_statistics64.argtypes = [ctypes.c_uint32, c_int, void_p, ctypes.POINTER(ctypes.c_uint32)]
        lib.host_processor_info.argtypes = [ctypes.c_uint32, c_int, ctypes.POINTER(ctypes.c_uint32),
                                            ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32)),
                                            ctypes.POINTER(ctypes.c_uint32)]
        lib.vm_deallocate.argtypes = [ctypes.c_uint32, void_p, ctypes.c_size_t]
        lib.proc_pid_rusage.argtypes = [c_int, c_int, void_p]
        lib.mach_timebase_info.argtypes = [void_p]
        _LIBC.append(lib)
        _LIBC.append(lib.mach_host_self())
    return _LIBC[0]


def host_port():
    libc()
    return _LIBC[1]


def sysctl_bytes(name, size=64):
    try:
        buf = ctypes.create_string_buffer(size)
        n = ctypes.c_size_t(size)
        if libc().sysctlbyname(name.encode(), buf, ctypes.byref(n), None, 0) != 0:
            return b""
        return buf.raw[:n.value]
    except Exception:
        return b""


def sysctl_int(name, default=0):
    b = sysctl_bytes(name)
    return int.from_bytes(b[:8], "little") if b else default


def sysctl_str(name):
    return sysctl_bytes(name, 256).split(b"\0", 1)[0].decode(errors="ignore")


def chip_info():
    """(chip name, core summary), both read from this Mac. Nothing is assumed."""
    name = sysctl_str("machdep.cpu.brand_string") or "Mac"
    p, e = sysctl_int("hw.perflevel0.logicalcpu"), sysctl_int("hw.perflevel1.logicalcpu")
    if sysctl_int("hw.nperflevels") >= 2 and e and p:      # Apple chip: efficiency + performance cores
        parts = ["%dE" % e, "%dP" % p]
    else:                                                  # Intel, or a chip with one kind of core
        n = sysctl_int("hw.physicalcpu") or sysctl_int("hw.logicalcpu")
        parts = ["%d cores" % n] if n else []
    m = re.search(r'"gpu-core-count" = (\d+)', sh(["ioreg", "-r", "-d", "1", "-w0", "-c", "IOAccelerator"]))
    if m:
        parts.append(m.group(1) + " GPU")
    else:                                                  # Intel: no GPU core count, show the graphics chip's name
        displays = sh(["system_profiler", "SPDisplaysDataType"], 15)
        m = re.search(r"Total Number of Cores:\s*(\d+)", displays)
        models = [g.strip() for g in re.findall(r"Chipset Model:\s*(.+)", displays)]
        if m:
            parts.append(m.group(1) + " GPU")
        elif models:
            parts.append(models[-1])
    return name, " + ".join(parts)


def boot_time():
    b = sysctl_bytes("kern.boottime", 16)
    return float(struct.unpack_from("<q", b)[0]) if len(b) >= 8 else None


def uptime_text(seconds):
    m = int(seconds // 60)
    d, h, m = m // 1440, (m % 1440) // 60, m % 60
    return "%dd %dh" % (d, h) if d else "%dh %02dm" % (h, m) if h else "%dm" % m


class _VMStats(ctypes.Structure):
    """The kernel's memory counters (vm_statistics64), in pages."""
    _u32, _u64 = ctypes.c_uint32, ctypes.c_uint64
    _fields_ = [("free", _u32), ("active", _u32), ("inactive", _u32), ("wire", _u32),
                ("zero_fill", _u64), ("reactivations", _u64), ("pageins", _u64), ("pageouts", _u64),
                ("faults", _u64), ("cow_faults", _u64), ("lookups", _u64), ("hits", _u64), ("purges", _u64),
                ("purgeable", _u32), ("speculative", _u32),
                ("decompressions", _u64), ("compressions", _u64), ("swapins", _u64), ("swapouts", _u64),
                ("compressor", _u32), ("throttled", _u32), ("external", _u32), ("internal", _u32),
                ("uncompressed", _u64)]


def memory():
    """Memory in GB, counted the way Activity Monitor counts it.

    used    everything that is neither free nor a file cache ("Memory Used")
    app     apps' own memory ("App Memory")       wired / comp   as Activity Monitor shows them
    cached  files kept in spare memory ("Cached Files"), given back the moment an app needs it
    """
    g = 1024.0 ** 3
    total = float(sysctl_int("hw.memsize"))
    page = float(sysctl_int("hw.pagesize") or 4096)
    vm = _VMStats()
    n = ctypes.c_uint32(ctypes.sizeof(vm) // 4)
    try:
        ok = libc().host_statistics64(host_port(), 4, ctypes.byref(vm), ctypes.byref(n)) == 0
    except Exception:
        ok = False
    if not ok:
        return {"used": 0.0, "total": total / g, "swap": 0.0, "swap_total": 0.0, "cached": 0.0,
                "wired": 0.0, "comp": 0.0, "app": 0.0}
    free = max(0, vm.free - vm.speculative)                # read-ahead pages are files, not free memory
    cached = vm.external + vm.purgeable
    used = max(0.0, total / page - free - cached)
    sw = sysctl_bytes("vm.swapusage", 32)
    sw_total, _, sw_used = struct.unpack_from("<QQQ", sw) if len(sw) >= 24 else (0, 0, 0)
    return {"used": used * page / g, "total": total / g, "swap": sw_used / g, "swap_total": sw_total / g,
            "cached": cached * page / g, "wired": vm.wire * page / g, "comp": vm.compressor * page / g,
            "app": max(0, vm.internal - vm.purgeable) * page / g}


def memory_pressure():
    return {1: "normal", 2: "warning", 4: "critical"}.get(sysctl_int("kern.memorystatus_vm_pressure_level"), "normal")


def disk_usage():
    """(used, total, free) of the built-in drive, in GB (1000-based, as drives are sold)."""
    for path in ("/System/Volumes/Data", "/"):
        try:
            v = os.statvfs(path)
            return ((v.f_blocks - v.f_bfree) * v.f_frsize / 1e9, v.f_blocks * v.f_frsize / 1e9,
                    v.f_bavail * v.f_frsize / 1e9)
        except Exception:
            pass
    return 0.0, 0.0, 0.0


def smart_status():
    m = re.search(r"SMART Status:\s*(.+)", sh(["diskutil", "info", "disk0"]))
    return m.group(1).strip() if m else "unknown"


def net_counters():
    """(bytes received, bytes sent) since boot on the real network interfaces.

    Wi-Fi, Ethernet and phone tethering are counted. VPN tunnels and bridges are not: their
    traffic already travels over one of those, so adding them would count it twice.
    """
    try:
        lib = libc()
        mib = (ctypes.c_int * 6)(4, 17, 0, 0, 6, 0)        # CTL_NET, PF_ROUTE, 0, 0, NET_RT_IFLIST2, 0
        need = ctypes.c_size_t(0)
        if lib.sysctl(mib, 6, None, ctypes.byref(need), None, 0) != 0:
            return None
        buf = ctypes.create_string_buffer(need.value + 4096)
        need.value = len(buf)
        if lib.sysctl(mib, 6, buf, ctypes.byref(need), None, 0) != 0:
            return None
        data, i, n_in, n_out = buf.raw[:need.value], 0, 0, 0
        while i + 4 <= len(data):
            size, _, kind = struct.unpack_from("<HBB", data, i)
            if size == 0:
                break
            if kind == 0x12 and i + 168 <= len(data):      # RTM_IFINFO2: one interface and its counters
                name = data[i + 168:i + 168 + data[i + 165]].decode(errors="ignore")
                if re.match(r"(en|pdp_ip|ppp)\d", name):
                    received, sent = struct.unpack_from("<QQ", data, i + 96)
                    n_in, n_out = n_in + received, n_out + sent
            i += size
        return n_in, n_out
    except Exception:
        return None


def net_counters_slow():
    """The same totals from `netstat`, for the day the kernel's table changes shape."""
    n_in = n_out = 0
    for line in sh(["netstat", "-ibn"]).splitlines():
        parts = line.split()
        if len(parts) >= 10 and "<Link#" in line and re.match(r"(en|pdp_ip|ppp)\d", parts[0]):
            try:
                n_in, n_out = n_in + int(parts[-5]), n_out + int(parts[-2])
            except ValueError:
                pass
    return n_in, n_out


class NetMeter:
    """Download and upload speed, from the difference between two looks at the counters."""

    def __init__(self):
        self.read = net_counters if net_counters() is not None else net_counters_slow
        self.prev = None

    def rates(self):
        now, cur = time.time(), self.read() or (0, 0)
        prev, self.prev = self.prev, (now, cur)
        if not prev or now <= prev[0]:
            return 0.0, 0.0
        return tuple(max(0.0, (c - p) / (now - prev[0])) for c, p in zip(cur, prev[1]))


def _cpu_seconds(text):
    """ps prints CPU time as minutes:seconds.hundredths."""
    total = 0.0
    for part in text.replace("-", ":").split(":"):
        try:
            total = total * 60 + float(part)
        except ValueError:
            return 0.0
    return total


class Procs:
    """The list of running apps, measured the way Activity Monitor measures them:
    CPU is the time used since the last look, memory is the app's real footprint."""

    def __init__(self):
        self.prev, self.at = {}, 0.0
        self.buf = (ctypes.c_uint64 * 12)()               # struct rusage_info_v0
        self.tick = 1e-9                                   # seconds per unit of the kernel's CPU clock
        try:
            tb = (ctypes.c_uint32 * 2)()
            libc().mach_timebase_info(ctypes.byref(tb))
            if tb[1]:
                self.tick = tb[0] / float(tb[1]) / 1e9
        except Exception:
            pass

    def _usage(self, pid):
        """(CPU seconds used so far, memory footprint in MB), or None when the system will not say."""
        try:
            if libc().proc_pid_rusage(pid, 0, ctypes.byref(self.buf)) != 0:
                return None
        except Exception:
            return None
        return (self.buf[2] + self.buf[3]) * self.tick, self.buf[9] / 1048576.0

    def read(self):
        """[(pid, user, cpu %, memory MB, name)]"""
        out = sh(["ps", "-Aceo", "pid=,user=,time=,rss=,pcpu=,comm="])
        now = time.time()
        dt = now - self.at if self.at else 0.0
        rows, seen = [], {}
        for line in out.splitlines():
            parts = line.split(None, 5)
            if len(parts) != 6:
                continue
            try:
                pid, rss, pcpu = int(parts[0]), int(parts[3]) / 1024.0, float(parts[4])
            except ValueError:
                continue
            use = self._usage(pid)                         # works for your own apps, and for all with sudo
            cpu_s, mem = use if use else (_cpu_seconds(parts[2]), rss)
            if use and mem <= 0:
                mem = rss
            old = self.prev.get(pid)
            cpu = max(0.0, (cpu_s - old) / dt * 100.0) if old is not None and dt > 0.2 else pcpu
            seen[pid] = cpu_s
            rows.append((pid, parts[1], cpu, mem, parts[5]))
        self.prev, self.at = seen, now
        return rows


def _signed(v):
    """ioreg prints negative numbers as huge unsigned ones."""
    return v - 2.0 ** 64 if v is not None and v > 2.0 ** 63 else v


def battery():
    """Battery state plus the power coming in from the charger. None on a Mac without a battery."""
    out = sh(["ioreg", "-rn", "AppleSmartBattery", "-w0"])
    if not out:
        return None

    def val(key):
        m = re.search(r'^[\s|]*"%s" = (.*)$' % key, out, re.M)
        return m.group(1).strip() if m else None

    def num(key):
        try:
            return _signed(float(val(key)))
        except (TypeError, ValueError):
            return None

    def inner(key, text):
        m = re.search(r'"%s"=(\d+)' % key, text or "")
        return _signed(float(m.group(1))) if m else None

    pct_ = num("CurrentCapacity")
    if pct_ is None:
        return None
    mx = num("MaxCapacity") or 100
    if mx > 100:                      # older firmware reports mAh
        pct_ = 100 * pct_ / mx
    deep = lambda key: num(key) or inner(key, out)      # newer Macs keep these inside "BatteryData"
    raw, design = deep("NominalChargeCapacity") or deep("AppleRawMaxCapacity"), deep("DesignCapacity")
    t = deep("Temperature") or deep("VirtualTemperature")
    plugged = val("ExternalConnected") == "Yes"
    charging = val("IsCharging") == "Yes"
    full = val("FullyCharged") == "Yes"
    amps, volts = num("Amperage"), num("Voltage")
    mins = num("TimeRemaining")
    tele, adapter = val("PowerTelemetryData"), val("AdapterDetails") or ""
    kilo = lambda v: v / 1000.0 if v is not None else None
    name = re.search(r'"(?:Name|Description)"="([^"]+)"', adapter)
    if charging:
        state = "charging"
    elif plugged:
        state = "full, plugged in" if full or pct_ >= 100 else "plugged in, not charging"
    else:
        state = "on battery"
    return {
        "pct": pct_,
        # a stand-in until macOS's own "Maximum Capacity" figure has been read (see battery_health)
        "health": min(100, math.floor(100 * raw / design)) if raw and design else None,
        "condition": "",
        "cycles": int(num("CycleCount") or 0),
        "temp": t / 100 if t else None,
        "state": state,
        "plugged": plugged,
        "mins": mins if mins and 0 < mins < 6000 else None,       # to full when charging, to empty on battery
        # measured by the Mac's own power chip, in watts / volts / amps
        "in_w": (kilo(inner("SystemPowerIn", tele)) or 0.0) if plugged else 0.0,
        "in_v": (kilo(inner("SystemVoltageIn", tele)) or 0.0) if plugged else 0.0,
        "in_a": (kilo(inner("SystemCurrentIn", tele)) or 0.0) if plugged else 0.0,
        "load_w": kilo(inner("SystemLoad", tele)),
        "bat_w": amps * volts / 1e6 if amps is not None and volts else None,      # + charging, - draining
        "adapter_w": inner("Watts", adapter) if plugged else None,
        "adapter_name": name.group(1) if name and plugged else "",
    }


def battery_health():
    """(maximum capacity %, condition) exactly as macOS shows them in System Settings.

    Read from macOS's own report rather than worked out from raw capacity numbers, which
    wander by a percent or two from hour to hour. Returns (None, "") on a Mac that has no such report.
    """
    out = sh(["system_profiler", "-json", "SPPowerDataType"], 20)
    m = re.search(r'"sppower_battery_health_maximum_capacity"\s*:\s*"(\d+)', out)
    c = re.search(r'"sppower_battery_health"\s*:\s*"([^"]*)"', out)
    return (int(m.group(1)) if m else None), (c.group(1) if c else "")


# ============================================================ sensors (read-only)
class SMC:
    """Reads the Mac's sensor chip (the SMC) through IOKit: temperatures, fans, watts. Read-only."""

    class _Vers(ctypes.Structure):
        _fields_ = [("major", ctypes.c_uint8), ("minor", ctypes.c_uint8), ("build", ctypes.c_uint8),
                    ("reserved", ctypes.c_uint8), ("release", ctypes.c_uint16)]

    class _PLimit(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint16), ("length", ctypes.c_uint16), ("cpu", ctypes.c_uint32),
                    ("gpu", ctypes.c_uint32), ("mem", ctypes.c_uint32)]

    class _KeyInfo(ctypes.Structure):
        _fields_ = [("dataSize", ctypes.c_uint32), ("dataType", ctypes.c_uint32), ("dataAttributes", ctypes.c_uint8)]

    class _KeyData(ctypes.Structure):
        pass

    _KeyData._fields_ = [("key", ctypes.c_uint32), ("vers", _Vers), ("pLimit", _PLimit), ("keyInfo", _KeyInfo),
                         ("result", ctypes.c_uint8), ("status", ctypes.c_uint8), ("data8", ctypes.c_uint8),
                         ("data32", ctypes.c_uint32), ("bytes", ctypes.c_uint8 * 32)]

    READ_BYTES, READ_INDEX, READ_KEYINFO = 5, 8, 9
    FLT = int.from_bytes(b"flt ", "big")          # Apple-chip sensors
    SP78 = int.from_bytes(b"sp78", "big")         # Intel sensors
    PREFIXES = ("Tp", "Te", "Tf", "Tg", "TC", "TG")

    def __init__(self):
        io = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
        io.IOServiceMatching.restype = ctypes.c_void_p
        io.IOServiceMatching.argtypes = [ctypes.c_char_p]
        io.IOServiceGetMatchingService.restype = ctypes.c_uint32
        io.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        io.IOServiceOpen.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        io.IOConnectCallStructMethod.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_size_t,
                                                 ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
        self.io = io
        svc = io.IOServiceGetMatchingService(0, io.IOServiceMatching(b"AppleSMC"))
        if not svc:
            raise OSError("AppleSMC not found")
        self.conn = ctypes.c_uint32()
        task = ctypes.c_uint32.in_dll(libc(), "mach_task_self_").value
        if io.IOServiceOpen(svc, task, 0, ctypes.byref(self.conn)) != 0:
            raise OSError("cannot open AppleSMC")
        self.little = os.uname().machine == "arm64"       # Apple chips store whole numbers low byte first
        self.kinds = {}
        self.cpu, self.gpu = self._discover()

    def _call(self, kd):
        out = self._KeyData()
        size = ctypes.c_size_t(ctypes.sizeof(out))
        r = self.io.IOConnectCallStructMethod(self.conn.value, 2, ctypes.byref(kd), ctypes.sizeof(kd),
                                              ctypes.byref(out), ctypes.byref(size))
        return out if r == 0 and out.result == 0 else None

    def _info(self, key):
        kd = self._KeyData(key=key, data8=self.READ_KEYINFO)
        o = self._call(kd)
        return (o.keyInfo.dataSize, o.keyInfo.dataType) if o else (0, 0)

    def _read(self, key, size):
        kd = self._KeyData(key=key, data8=self.READ_BYTES)
        kd.keyInfo.dataSize = size
        o = self._call(kd)
        return bytes(o.bytes[:size]) if o else None

    def _temp(self, key, typ):
        if typ == self.FLT:
            b = self._read(key, 4)
            return struct.unpack("<f", b)[0] if b else None
        b = self._read(key, 2)
        return struct.unpack(">h", b)[0] / 256.0 if b else None

    def _discover(self):
        count_b = self._read(int.from_bytes(b"#KEY", "big"), 4)
        count = int.from_bytes(count_b, "big") if count_b else 0
        found = {p: [] for p in self.PREFIXES}
        for i in range(count):
            o = self._call(self._KeyData(data8=self.READ_INDEX, data32=i))
            if not o:
                continue
            name = o.key.to_bytes(4, "big").decode("latin1")
            if name[:2] not in found:
                continue
            size, typ = self._info(o.key)
            if (typ, size) not in ((self.FLT, 4), (self.SP78, 2)):
                continue
            v = self._temp(o.key, typ)
            if v is None or not 5 < v < 130:
                continue
            # M3-family chips name their GPU sensors Tf1x / Tf2x instead of Tg..
            is_gpu = name[:2] == "Tf" and name[2] in "12"
            found["Tg" if is_gpu else name[:2]].append((o.key, typ))
        cpu = found["Tp"] + found["Te"] + found["Tf"]
        gpu = found["Tg"]
        if not cpu:                               # Intel names: TC.. for the CPU, TG.. for the GPU
            cpu, gpu = found["TC"], gpu or found["TG"]
        return cpu, gpu

    def value(self, name):
        """One numeric sensor as a float, or None when this Mac does not have it."""
        key = int.from_bytes(name.encode("latin1"), "big")
        kind = self.kinds.get(name)
        if kind is None:
            kind = self.kinds[name] = self._info(key)
        size, typ = kind
        if not 0 < size <= 8:
            return None
        b = self._read(key, size)
        if not b:
            return None
        if typ == self.FLT and size == 4:
            return struct.unpack("<f", b)[0]
        t = typ.to_bytes(4, "big")
        if t[:2] in (b"ui", b"si"):
            return float(int.from_bytes(b, "little" if self.little else "big", signed=t[:2] == b"si"))
        if t == b"fpe2" and size == 2:
            return int.from_bytes(b, "big") / 4.0
        if t == b"sp78" and size == 2:
            return struct.unpack(">h", b)[0] / 256.0
        return None

    def power(self):
        """Live readings from the power chip. Watts, volts, amps and degrees C; None where a Mac lacks one.

        in_*   what the charger is delivering        sys_w  what the whole Mac is drawing
        bat_w  into the battery (+) or out of it (-) bat_t  battery temperature
        """
        v = self.value
        bat_w, bat_t = v("B0AP"), v("TB0T")
        if bat_t is None or not 0 < bat_t < 90:
            kelvin10 = v("B0AT")
            bat_t = kelvin10 / 10.0 - 273.15 if kelvin10 and kelvin10 > 2000 else None
        return {"in_v": v("VD0R"), "in_a": v("ID0R"), "in_w": v("PDTR"), "sys_w": v("PSTR"),
                "bat_w": bat_w / 1000.0 if bat_w is not None else None, "bat_t": bat_t}

    def port_out(self):
        """{port number: (volts, amps)} measured on each USB-C port by the Mac's own power sensors."""
        out = {}
        for n in range(1, 7):
            a, v = self.value("D%dJI" % n), self.value("D%dJV" % n)
            if a is not None and v is not None:
                out[n] = (v, a)
        return out

    def fans(self):
        """[(rpm now, slowest, fastest)] for each fan. Empty on a Mac without fans."""
        out = []
        for i in range(int(min(self.value("FNum") or 0, 8))):
            rpm = self.value("F%dAc" % i)
            if rpm is not None:
                out.append((rpm, self.value("F%dMn" % i) or 0.0, self.value("F%dMx" % i) or 0.0))
        return out

    def read(self):
        def vals(keys):
            return [v for v in (self._temp(k, t) for k, t in keys) if v is not None and 5 < v < 130]
        return vals(self.cpu), vals(self.gpu)


class HIDTemps:
    """Reads the HID temperature sensors (SoC die, SSD/NAND, battery). Read-only."""

    def __init__(self):
        io = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        vp = ctypes.c_void_p
        for fn, res, args in (
            (cf.CFStringCreateWithCString, vp, [vp, ctypes.c_char_p, ctypes.c_uint32]),
            (cf.CFNumberCreate, vp, [vp, ctypes.c_int, vp]),
            (cf.CFDictionaryCreate, vp, [vp, vp, vp, ctypes.c_long, vp, vp]),
            (cf.CFArrayGetCount, ctypes.c_long, [vp]),
            (cf.CFArrayGetValueAtIndex, vp, [vp, ctypes.c_long]),
            (cf.CFStringGetCString, ctypes.c_bool, [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]),
            (cf.CFRelease, None, [vp]),
            (io.IOHIDEventSystemClientCreate, vp, [vp]),
            (io.IOHIDEventSystemClientSetMatching, ctypes.c_int, [vp, vp]),
            (io.IOHIDEventSystemClientCopyServices, vp, [vp]),
            (io.IOHIDServiceClientCopyProperty, vp, [vp, vp]),
            (io.IOHIDServiceClientCopyEvent, vp, [vp, ctypes.c_int64, ctypes.c_int32, ctypes.c_int64]),
            (io.IOHIDEventGetFloatValue, ctypes.c_double, [vp, ctypes.c_int32]),
        ):
            fn.restype, fn.argtypes = res, args
        self.io, self.cf = io, cf
        cfstr = lambda s: cf.CFStringCreateWithCString(None, s.encode(), 0x08000100)
        page, usage = ctypes.c_int32(0xFF00), ctypes.c_int32(5)
        keys = (vp * 2)(cfstr("PrimaryUsagePage"), cfstr("PrimaryUsage"))
        values = (vp * 2)(cf.CFNumberCreate(None, 3, ctypes.byref(page)), cf.CFNumberCreate(None, 3, ctypes.byref(usage)))
        match = cf.CFDictionaryCreate(None, keys, values, 2,
                                      ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
                                      ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks")))
        self.client = io.IOHIDEventSystemClientCreate(None)
        if not self.client:
            raise OSError("HID client unavailable")
        io.IOHIDEventSystemClientSetMatching(self.client, match)
        self.product = cfstr("Product")

    def read(self):
        """{sensor name: degrees C}"""
        out = {}
        services = self.io.IOHIDEventSystemClientCopyServices(self.client)
        if not services:
            return out
        buf = ctypes.create_string_buffer(256)
        for i in range(self.cf.CFArrayGetCount(services)):
            svc = self.cf.CFArrayGetValueAtIndex(services, i)
            name_ref = self.io.IOHIDServiceClientCopyProperty(svc, self.product)
            if not name_ref:
                continue
            ok = self.cf.CFStringGetCString(name_ref, buf, 256, 0x08000100)
            self.cf.CFRelease(name_ref)
            event = self.io.IOHIDServiceClientCopyEvent(svc, 15, 0, 0)
            if not event:
                continue
            t = self.io.IOHIDEventGetFloatValue(event, 15 << 16)
            self.cf.CFRelease(event)
            if ok and 5 < t < 130:
                out[buf.value.decode(errors="ignore")] = t
        self.cf.CFRelease(services)
        return out


class Temps:
    def __init__(self):
        self.smc = self.hid = None
        try:
            self.smc = SMC()
        except Exception:
            pass
        try:
            self.hid = HIDTemps()
        except Exception:
            pass

    def read(self):
        cpu, gpu, hid, fans = [], [], {}, []
        if self.smc:
            try:
                cpu, gpu = self.smc.read()
                fans = self.smc.fans()
            except Exception:
                pass
        if self.hid:
            try:
                hid = self.hid.read()
            except Exception:
                pass
        if not cpu:
            cpu = [v for k, v in hid.items() if "tdie" in k.lower()]
        if not gpu:
            gpu = [v for k, v in hid.items() if "gpu" in k.lower()]
        ssd = [v for k, v in hid.items() if "nand" in k.lower()]
        avg = lambda xs: sum(xs) / len(xs) if xs else None
        return {"cpu": avg(cpu), "cpu_max": max(cpu) if cpu else None, "gpu": avg(gpu),
                "gpu_max": max(gpu) if gpu else None, "ssd": max(ssd) if ssd else None,
                "fans": [f[0] for f in fans], "fan_top": max([f[2] for f in fans] or [0.0])}


# ============================================================ activity without administrator rights
def _efficiency_map(n_cpu, n_e):
    """For every logical CPU, True when it is an efficiency core."""
    kinds = {}
    for b in _blocks(sh(["ioreg", "-p", "IODeviceTree", "-r", "-n", "cpus", "-w0", "-l"])):
        i, kind = _prop(b, "logical-cpu-id"), _prop(b, "cluster-type")
        try:
            if i is not None and kind:
                kinds[int(i)] = '"E"' in kind
        except ValueError:
            pass
    if len(kinds) == n_cpu and sum(kinds.values()) == n_e:
        return [kinds.get(i, False) for i in range(n_cpu)]
    return [i < n_e for i in range(n_cpu)]            # the usual order: efficiency cores first


class LocalStats:
    """CPU, GPU and disk activity read without administrator rights.

    Works on every Mac. Used for basic mode (--no-sudo), and to fill in whatever
    `powermetrics` does not report on a given model (per-core load on Intel).
    """

    def __init__(self):
        self.n_e = sysctl_int("hw.perflevel1.logicalcpu") if sysctl_int("hw.nperflevels") >= 2 else 0
        self.host = self.task = None
        try:
            self.host = host_port()
            self.task = ctypes.c_uint32.in_dll(libc(), "mach_task_self_").value
        except Exception:
            pass
        self.ticks = None
        self.counters = None
        self.gpu_mem = None
        n = len(self.cpu())
        self.is_e = _efficiency_map(n, self.n_e) if self.n_e else [False] * n

    def cpu(self):
        """Load of every core (0..1) since the previous call."""
        if self.host is None:
            return []
        lib = libc()
        n, info, count = ctypes.c_uint32(), ctypes.POINTER(ctypes.c_uint32)(), ctypes.c_uint32()
        if lib.host_processor_info(self.host, 2, ctypes.byref(n), ctypes.byref(info), ctypes.byref(count)) != 0:
            return []
        now = [tuple(info[i * 4 + j] for j in range(4)) for i in range(n.value)]    # user, system, idle, nice
        lib.vm_deallocate(self.task, ctypes.cast(info, ctypes.c_void_p), count.value * 4)
        prev, self.ticks = self.ticks, now
        if not prev or len(prev) != len(now):
            return [0.0] * len(now)
        out = []
        for a, b in zip(prev, now):
            d = [(y - x) & 0xFFFFFFFF for x, y in zip(a, b)]
            total = sum(d)
            out.append((total - d[2]) / float(total) if total else 0.0)
        return out

    def split(self, loads):
        """(efficiency cores, performance cores)"""
        if len(loads) != len(self.is_e):
            return [], list(loads)
        return [v for v, e in zip(loads, self.is_e) if e], [v for v, e in zip(loads, self.is_e) if not e]

    def gpu(self):
        """GPU load (0..1). Also records the memory the GPU is using, in GB, as self.gpu_mem."""
        out = sh(["ioreg", "-r", "-d", "1", "-w0", "-c", "IOAccelerator"])
        mem = [int(v) for v in re.findall(r'"In use system memory"=(\d+)', out)]
        self.gpu_mem = sum(mem) / 1024.0 ** 3 if mem else None
        vals = re.findall(r'"Device Utilization %"=(\d+)', out)
        return min(1.0, max(int(v) for v in vals) / 100.0) if vals else 0.0

    def disk(self):
        """(bytes read, bytes written) per second on all drives since the previous call."""
        out = sh(["ioreg", "-r", "-d", "1", "-w0", "-c", "IOBlockStorageDriver"])
        rd = sum(int(v) for v in re.findall(r'"Bytes \(Read\)"=(\d+)', out))
        wr = sum(int(v) for v in re.findall(r'"Bytes \(Write\)"=(\d+)', out))
        now = time.time()
        prev, self.counters = self.counters, (now, rd, wr)
        if not prev or now <= prev[0]:
            return 0.0, 0.0
        return max(0.0, (rd - prev[1]) / (now - prev[0])), max(0.0, (wr - prev[2]) / (now - prev[0]))

    def sample(self):
        e, p = self.split(self.cpu())
        rd, wr = self.disk()
        return {"e_cores": e, "p_cores": p, "e_mhz": 0, "p_mhz": 0,
                "gpu": self.gpu(), "gpu_mem": self.gpu_mem, "gpu_mhz": 0,
                "cpu_w": 0.0, "gpu_w": 0.0, "ane_w": 0.0, "total_w": 0.0,
                "thermal": None, "has_power": False, "disk_r": rd, "disk_w": wr, "net_in": 0.0, "net_out": 0.0}


# ============================================================ power / usage source (administrator)
class PowerMetrics:
    """macOS's own `powermetrics` tool: per-core load, clock speeds and the chip's watts."""

    def __init__(self, interval_ms):
        import plistlib                               # only needed here, so only loaded here
        self.loads = plistlib.loads
        self.interval = interval_ms / 1000.0
        self.proc = subprocess.Popen(
            ["powermetrics", "--samplers", "cpu_power,gpu_power,thermal,disk", "-f", "plist",
             "-i", str(interval_ms)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.fd = self.proc.stdout.fileno()
        self.buf = b""

    def read(self):
        chunk = os.read(self.fd, 65536)
        if not chunk:
            raise RuntimeError("powermetrics exited (run with sudo)")
        self.buf += chunk
        sample = None
        while True:
            end = self.buf.find(b"</plist>")
            if end < 0:
                break
            raw = self.buf[:end + 8].lstrip(b"\x00\r\n\t ")
            self.buf = self.buf[end + 8:]
            try:
                sample = self.parse(self.loads(raw))
            except Exception:
                pass
        return sample

    def parse(self, d):
        proc = d.get("processor", {})
        e_cores, p_cores, e_f, p_f = [], [], [], []
        for cl in proc.get("clusters", []):
            # a core is busy only when it is neither idle nor powered down
            cores = [min(1.0, max(0.0, 1.0 - float(c.get("idle_ratio", 1.0)) - float(c.get("down_ratio", 0.0))))
                     for c in cl.get("cpus", [])]
            is_e = cl.get("name", "").upper().startswith("E")
            (e_cores if is_e else p_cores).extend(cores)
            (e_f if is_e else p_f).append(cl.get("freq_hz", 0) / 1e6)

        def watts(name):
            if name + "_power" in proc:
                return float(proc[name + "_power"]) / 1000.0
            if name == "cpu" and "package_watts" in proc:          # Intel
                return float(proc["package_watts"])
            return float(proc.get(name + "_energy", 0)) / 1000.0 / self.interval

        if not p_f and proc.get("freq_hz"):                        # Intel: one average frequency
            p_f = [float(proc["freq_hz"]) / 1e6]

        gpu, disk = d.get("gpu", {}), d.get("disk", {})
        cpu_w, gpu_w, ane_w = watts("cpu"), watts("gpu"), watts("ane")
        total = float(proc["combined_power"]) / 1000.0 if "combined_power" in proc else cpu_w + gpu_w + ane_w
        return {
            "e_cores": e_cores, "p_cores": p_cores,
            "e_mhz": sum(e_f) / len(e_f) if e_f else 0, "p_mhz": sum(p_f) / len(p_f) if p_f else 0,
            "gpu": 1.0 - float(gpu["idle_ratio"]) if "idle_ratio" in gpu else None,
            "gpu_mem": None,
            "gpu_mhz": gpu.get("freq_hz", 0) if "idle_ratio" in gpu else 0,
            "cpu_w": cpu_w, "gpu_w": gpu_w, "ane_w": ane_w, "total_w": total,
            "thermal": d.get("thermal_pressure", "Nominal"), "has_power": True,
            "disk_r": float(disk.get("rbytes_per_s", 0)), "disk_w": float(disk.get("wbytes_per_s", 0)),
            "net_in": 0.0, "net_out": 0.0,
        }

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


# ============================================================ ports
def _blocks(text):
    """Split ioreg output into one chunk of text per registry entry."""
    return re.split(r"\n(?=[\s|]*\+-o )", text)


def _prop(block, key):
    m = re.search(r'"%s" = (.*)' % re.escape(key), block)
    return m.group(1).strip() if m else None


def read_ports():
    """Every physical port the Mac reports, with what is active on it right now."""
    out = sh(["ioreg", "-r", "-c", "IOPort", "-w0", "-l"])
    ports, charge = {}, {}
    for b in _blocks(out):
        desc = _prop(b, "PortDescription")
        if desc:
            desc = desc.strip('"')
            try:
                num = int(_prop(b, "PortNumber") or 1)
            except ValueError:
                num = 1
            ports[desc] = {
                "id": desc, "num": num,
                "kind": (_prop(b, "PortTypeDescription") or desc).strip('"'),
                "active": _prop(b, "ConnectionActive") == "Yes",
                "transports": re.findall(r'"(\w+)"', _prop(b, "TransportsActive") or ""),
                "limits": [int(x) for x in re.findall(r"\d+", _prop(b, "IOAccessoryPowerCurrentLimits") or "")],
                "charge": None,
            }
            continue
        win = _prop(b, "WinningPowerSourceOption")
        owner = (_prop(b, "Description") or "").strip('"').split("/")[0]
        if win and owner:
            g = lambda k: float((re.search(r'"%s"=(\d+)' % re.escape(k), win) or [0, 0])[1])
            charge[owner] = {"v": g("Voltage (mV)") / 1000, "a": g("Max Current (mA)") / 1000,
                             "w": g("Max Power (mW)") / 1000}
    for owner, c in charge.items():
        if owner in ports:
            ports[owner]["charge"] = c
    return sorted(ports.values(), key=lambda p: (p["kind"], p["num"]))


def read_volumes():
    """{"disk6": [(volume name, used bytes, free bytes)]} for every mounted volume."""
    out = {}
    for line in sh(["df", "-k"]).splitlines():
        m = re.match(r"/dev/(disk\d+)s\d+\s+(\d+)\s+(\d+)\s+(\d+)\s+\S+\s+\S+\s+\S+\s+\S+\s+(/.*)$", line)
        if m and m.group(5).startswith("/Volumes/"):
            out.setdefault(m.group(1), []).append(
                (os.path.basename(m.group(5)), int(m.group(3)) * 1024, int(m.group(4)) * 1024))
    return out


def read_usb():
    """Everything plugged in over USB, one entry per plug: names, speed, reserved current, drive details."""
    devs, vols = [], None
    for tree in re.split(r"\n(?=\+-o )", sh(["ioreg", "-r", "-c", "IOUSBHostDevice", "-w0", "-l"])):
        blocks = _blocks(tree)
        top = blocks[0]
        if not _prop(top, "USB Product Name") or _prop(top, "Built-In") == "Yes":
            continue
        names, ma, bps, disk = [], 0, 0, None
        for b in blocks:
            if "<class IOUSBHostDevice" in b.split("\n", 1)[0]:
                n = (_prop(b, "USB Product Name") or "").strip('"')
                if n and n not in names:
                    names.append(n)
                try:
                    ma += int(_prop(b, "UsbPowerSinkAllocation") or 0)
                    bps = max(bps, int(_prop(b, "UsbLinkSpeed") or 0))
                except ValueError:
                    pass
            stats = _prop(b, "Statistics")
            if stats and disk is None and "Bytes (Read)" in stats:
                g = lambda k: int((re.search(r'"%s"=(\d+)' % re.escape(k), stats) or [0, 0])[1])
                disk = {"read": g("Bytes (Read)"), "written": g("Bytes (Write)"), "bsd": "", "size": 0, "vols": []}
            if disk is not None and not disk["bsd"] and _prop(b, "Whole") == "Yes":
                disk["bsd"] = (_prop(b, "BSD Name") or "").strip('"')
                try:
                    disk["size"] = int(_prop(b, "Size") or 0)
                except ValueError:
                    pass
        if disk and disk["bsd"]:
            if vols is None:
                vols = read_volumes()
            disk["vols"] = vols.get(disk["bsd"], [])
        try:
            bus = int(_prop(top, "locationID") or -1) >> 24
        except ValueError:
            bus = -1
        speed = "USB 3, %d Gb/s" % (bps // 10 ** 9) if bps >= 10 ** 9 else "USB 2" if bps >= 4 * 10 ** 8 else \
            "USB 1" if bps else ""
        vendor = (_prop(top, "USB Vendor Name") or "").strip('"')
        real = [n for n in names if "hub" not in n.lower()] or names[:1]
        if vendor and len(real) == 1 and vendor.lower() not in real[0].lower() and len(vendor) < 12:
            real = [vendor + " " + real[0]]
        devs.append({"name": real[0], "names": real, "ma": ma, "speed": speed, "bus": bus, "bps": bps,
                     "disk": disk, "r": 0.0, "w": 0.0, "peak": 0.0})
    return devs


def read_thunderbolt():
    """{receptacle number: {"speed": "Up to 120 Gb/s", "devices": [names]}}"""
    out = {}
    try:
        import json                                   # only this slow, occasional call needs it
        buses = json.loads(sh(["system_profiler", "-json", "SPThunderboltDataType"], 20) or "{}")
    except Exception:
        return out

    def names(items):
        found = []
        for it in items or []:
            n = it.get("device_name_key") or it.get("_name")
            if n:
                found.append(((it.get("vendor_name_key") or "") + " " + n).strip())
            found += names(it.get("_items"))
        return found

    for bus in buses.get("SPThunderboltDataType", []):
        tag = bus.get("receptacle_1_tag") or {}
        try:
            num = int(tag.get("receptacle_id_key"))
        except (TypeError, ValueError):
            continue
        out[num] = {"speed": tag.get("current_speed_key", ""), "devices": names(bus.get("_items"))}
    return out


def tb_name(speed, chip):
    """Thunderbolt generation of a port, from the top speed macOS reports for it."""
    m = re.search(r"(\d+)\s*Gb", speed or "")
    gb = int(m.group(1)) if m else 0
    if gb >= 80:
        return "Thunderbolt 5"
    if gb >= 40:
        if "Intel" in chip:
            return "Thunderbolt 3"
        mm = re.search(r"\bM(\d+)( Pro| Max| Ultra)?", chip)
        # same 40 Gb/s speed: Apple calls it Thunderbolt 4 on Pro/Max/Ultra chips and on every M4 or newer
        if mm and (mm.group(2) or int(mm.group(1)) >= 4):
            return "Thunderbolt 4"
        return "Thunderbolt 3 / USB4"
    return "USB-C"


class PortWatch(threading.Thread):
    """Keeps the slow-to-read facts fresh in the background, so the screen never waits for them:
    ports, plugged-in devices, and the battery's health as macOS reports it."""

    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self.model, self.ports, self.usb, self.tb = "", [], [], {}
        self.health, self.condition = None, ""
        self.watching = False                         # True while the ports screen is up: look more often
        self.wake = threading.Event()
        self.start()

    def run(self):
        m = re.search(r'"machine_name"\s*:\s*"([^"]+)"', sh(["system_profiler", "-json", "SPHardwareDataType"], 15))
        self.model = m.group(1).strip() if m else "Mac"
        seen, tb_at, health_at, prev = None, 0.0, 0.0, {}
        while True:
            try:
                if time.time() - health_at > 600:
                    self.health, self.condition = battery_health()
                    health_at = time.time()
                ports, usb = read_ports(), read_usb()
                t = time.time()
                for d in usb:                                      # drive read / write speed since last look
                    old = prev.get(d["name"])
                    if d["disk"] and old and t > old[0]:
                        d["r"] = max(0.0, (d["disk"]["read"] - old[1]) / (t - old[0]))
                        d["w"] = max(0.0, (d["disk"]["written"] - old[2]) / (t - old[0]))
                    d["peak"] = max(d["r"], d["w"], old[3] if old else 0.0)
                prev = {d["name"]: (t, d["disk"]["read"], d["disk"]["written"], d["peak"]) for d in usb if d["disk"]}
                now = tuple((p["id"], p["active"], tuple(p["transports"])) for p in ports)
                if now != seen or time.time() - tb_at > 30:        # slow call: only when something changed
                    self.tb, seen, tb_at = read_thunderbolt(), now, time.time()
                self.ports, self.usb = ports, usb
            except Exception:
                pass
            self.wake.wait(1.5 if self.watching else 6.0)
            self.wake.clear()


class DemoPorts:
    model = "MacBook Pro"
    health, condition, watching = 94, "Good", False
    wake = threading.Event()
    tb = {1: {"speed": "Up to 120 Gb/s", "devices": []}, 2: {"speed": "Up to 120 Gb/s", "devices": []},
          3: {"speed": "Up to 120 Gb/s", "devices": []}}
    usb = [{"name": "Portable SSD", "names": ["Portable SSD"], "ma": 896, "speed": "USB 3, 10 Gb/s", "bus": 0,
            "bps": 10 ** 10, "r": 182e6, "w": 0.0, "peak": 940e6,
            "disk": {"bsd": "disk6", "size": 1e12, "read": 0, "written": 0, "vols": [("Portable SSD", 598e9, 402e9)]}}]
    ports = [
        {"id": "m", "num": 1, "kind": "MagSafe 3", "active": True, "transports": ["CC"], "limits": [],
         "charge": {"v": 20.0, "a": 4.8, "w": 96.0}},
        {"id": "c1", "num": 1, "kind": "USB-C", "active": True, "transports": ["CC", "USB3"], "limits": [], "charge": None},
        {"id": "c2", "num": 2, "kind": "USB-C", "active": False, "transports": [], "limits": [], "charge": None},
        {"id": "c3", "num": 3, "kind": "USB-C", "active": False, "transports": [], "limits": [], "charge": None},
        {"id": "h", "num": 1, "kind": "HDMI", "active": True, "transports": ["DisplayPort"], "limits": [], "charge": None},
        {"id": "s", "num": 1, "kind": "SD Card", "active": False, "transports": [], "limits": [], "charge": None},
    ]


def port_layout(model, ports):
    """(left side, right side). Which side a port sits on comes from the kind of Mac."""
    by = lambda kind: sorted((p for p in ports if p["kind"].startswith(kind)), key=lambda p: p["num"])
    mag, usb, hdmi, sd = by("MagSafe"), by("USB-C"), by("HDMI"), by("SD")
    known = mag + usb + hdmi + sd
    other = [p for p in ports if p not in known]
    audio = [{"id": "audio", "num": 1, "kind": "Audio", "active": False, "transports": [], "static": True}]
    if "Book" not in model:                       # desktop Mac: no left and right, just a list
        half = (len(usb) + 1) // 2
        return mag + usb[:half] + other, usb[half:] + hdmi + sd + audio
    if hdmi:                                      # 14" and 16" MacBook Pro
        return mag + usb[:2] + audio, hdmi + usb[2:] + sd + other
    if len(usb) >= 4:                             # older 4-port MacBook Pro
        return mag + usb[:2] + other, usb[2:] + audio
    return mag + usb + other, audio               # MacBook Air, 13" MacBook Pro


def usb_by_port(pw):
    """{port id: [usb devices]}, or None when devices cannot be matched to ports with certainty.

    Apple-chip Macs give every USB-C port its own USB controller, numbered in port order.
    """
    usbc = [p for p in pw.ports if p["kind"].startswith("USB-C")]
    out = {p["id"]: [] for p in usbc}
    busy = [p for p in usbc if "USB2" in p["transports"] or "USB3" in p["transports"]]
    if len(busy) == 1:
        out[busy[0]["id"]] = list(pw.usb)
        return out
    for d in pw.usb:
        hit = [p for p in busy if p["num"] - 1 == d["bus"]]
        if not hit:
            return None
        out[hit[0]["id"]].append(d)
    return out


# ============================================================ demo data
class Demo:
    """Made-up but lively numbers, for --demo."""

    def __init__(self):
        self.t = 0.0
        self.seed = 20261112
        self.names = ["Google Chrome Helper", "Xcode", "Python", "Discord", "WindowServer", "Spotify",
                      "Finder", "Code Helper", "kernel_task", "mds_stores", "Safari", "Slack", "Terminal",
                      "Music"]

    def noise(self):
        self.seed = (self.seed * 1103515245 + 12345) & 0x7FFFFFFF
        return self.seed / float(0x7FFFFFFF) - 0.5

    def wave(self, k, base, amp):
        return min(1, max(0, base + amp * math.sin(self.t * k) + 0.16 * self.noise()))

    def sample(self):
        self.t += 0.35
        e = [self.wave(0.5 + i * 0.1, 0.25, 0.15) for i in range(4)]
        p = [self.wave(0.3 + i * 0.07, 0.5, 0.35) for i in range(8)]
        g = self.wave(0.2, 0.4, 0.4)
        cpu_w, gpu_w = 2 + 28 * sum(p) / 8, 25 * g
        ane_w = 1.5 * self.wave(0.15, 0.2, 0.3)
        return {"e_cores": e, "p_cores": p, "e_mhz": 1733, "p_mhz": 3912, "gpu": g, "gpu_mem": 0.6 + 2.4 * g, "gpu_mhz": 1617,
                "cpu_w": cpu_w, "gpu_w": gpu_w, "ane_w": ane_w, "total_w": cpu_w + gpu_w + ane_w,
                "thermal": "Nominal", "has_power": True,
                "disk_r": 4e6 * self.wave(0.4, 0.3, 0.3), "disk_w": 1e6 * self.wave(0.6, 0.3, 0.3),
                "net_in": 2e6 * self.wave(0.3, 0.35, 0.35) ** 2, "net_out": 3e5 * self.wave(0.5, 0.3, 0.3) ** 2}

    def temps(self):
        c = 45 + 40 * self.wave(0.3, 0.5, 0.35)
        return {"cpu": c, "cpu_max": c + 6, "gpu": c - 8, "gpu_max": c - 3, "ssd": 38.0,
                "fans": [1200 + 40 * (c - 45)] * 2, "fan_top": 7800.0}

    def memory(self):
        used = 14.4 + math.sin(self.t / 3)
        return {"used": used, "total": 24.0, "swap": 0.0, "swap_total": 0.0, "cached": 6.2, "wired": 2.1,
                "comp": 0.8, "app": used - 3.4}

    def power(self, chip_w):
        """What the power sensors would say: the Mac's draw, the charger, the battery."""
        mac = chip_w + 7.5
        charge = 26.0 + 3 * math.sin(self.t / 2)
        return {"in_v": 20.1, "in_a": (mac + charge) / 20.1, "in_w": mac + charge, "sys_w": mac,
                "bat_w": charge, "bat_t": 31.0}

    def battery(self):
        return {"pct": 87, "health": 94, "condition": "Good", "cycles": 143, "temp": 31.0, "state": "charging",
                "plugged": True, "mins": 48, "in_w": 58.0, "in_v": 20.1, "in_a": 2.9, "load_w": 30.0,
                "bat_w": 28.0, "adapter_w": 96, "adapter_name": "pd charger"}

    def procs(self):
        user = os.environ.get("USER", "you")
        return [(400 + i, "root" if n in ("WindowServer", "kernel_task") else user,
                 max(0.0, 80 * self.wave(0.2 + i * 0.13, 0.4 - i * 0.03, 0.3)), 150 + (i * 397) % 2400, n)
                for i, n in enumerate(self.names)]


# ============================================================ state
VIEWS = (("main", "1", "Overview"), ("mac", "2", "Ports & power"), ("core", "3", "Core"))
MOTIONS = ("smooth", "still", "eco")
REST_EVERY = 5.0                # seconds between redraws while resting (eco, or window in the background)


class State:
    def __init__(self, n=600):
        keys = ("e", "p", "cpu", "gpu", "ram", "cpu_w", "gpu_w", "ane_w", "total_w", "mac_w", "in_w",
                "t_cpu", "t_gpu", "net_in", "net_out")
        self.h = {k: deque(maxlen=n) for k in keys}
        self.peak = {k: 0.0 for k in ("cpu_w", "gpu_w", "ane_w", "total_w", "mac_w")}
        self.sum = dict(self.peak)
        self.n = 0              # samples of the chip's power
        self.n_mac = 0          # samples of the whole Mac's power
        self.wh = 0.0           # energy the chip used since start
        self.wh_mac = 0.0       # energy the whole Mac used since start
        self.wh_in = 0.0        # energy that came from the charger since start
        self.purge = None
        self.purge_before = 0.0
        self.msg = ""
        self.sel = 0
        self.sel_pid = None
        self.rows = 8
        self.sort = "cpu"
        self.confirm = None
        self.view = "main"
        self.motion = "smooth"
        self.focus = True       # False while the terminal window is in the background
        self.wipe = None        # a screen change in progress: (time started, +1 or -1, picture before)
        self.live = {}          # the power sensors' latest readings, lightly smoothed
        self.live_t = 0.0
        self.outs = {}          # per USB-C port: (volts, amps) going out
        self.pw = {"plugged": False, "in_w": 0.0, "in_v": 0.0, "in_a": 0.0, "bat_w": None, "mac_w": None}
        self.log_t = 0.0
        self.port_stats = {}    # per USB-C port: peak watts and milliwatt-hours delivered
        self.port_seen = None   # which ports had something plugged in last time we looked
        self.events = deque(maxlen=5)
        self.orbit = {"e": [], "p": []}      # where each core's dot is on its ring, in radians
        self.orbit_t = 0.0
        self.procs = []
        self.last = None
        self.tabs = []          # clickable screen names in the header: (x0, x1, view)
        self.hot = []           # clickable footer buttons: (x0, x1, action)
        self.foot_y = 0
        self.rows_hit = (0, 0, 0, 0)   # app rows: (first line, count, x0, x1)

    def push(self, s, mem, temps, dt):
        avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
        self.h["e"].append(avg(s["e_cores"]))
        self.h["p"].append(avg(s["p_cores"]))
        self.h["cpu"].append(avg(s["e_cores"] + s["p_cores"]))
        self.h["gpu"].append(s["gpu"] or 0.0)
        self.h["ram"].append(mem["used"] / mem["total"] if mem["total"] else 0)
        self.h["net_in"].append(s["net_in"])
        self.h["net_out"].append(s["net_out"])
        if temps.get("cpu") is not None:
            self.h["t_cpu"].append(temps["cpu"])
        if temps.get("gpu") is not None:
            self.h["t_gpu"].append(temps["gpu"])
        self.n += 1
        for k in ("cpu_w", "gpu_w", "ane_w", "total_w"):
            self.h[k].append(s[k])
            self.peak[k] = max(self.peak[k], s[k])
            self.sum[k] += s[k]
        self.wh += s["total_w"] * dt / 3600.0

    def sorted_procs(self):
        key = (lambda r: r[2]) if self.sort == "cpu" else (lambda r: r[3])
        return sorted(self.procs, key=key, reverse=True)[:self.rows]

    def resting(self):
        """True when the screen should only refresh now and then: eco mode, or nobody is looking."""
        return self.motion == "eco" or not self.focus

    def moving(self):
        """True when the picture on screen is animated right now."""
        return self.motion == "smooth" and self.focus and (self.view != "main" or self.wipe is not None)


# ============================================================ how power is flowing
def sample_power(st, smc, fake=None, fake_outs=None):
    """One look at the live power sensors, folded into st.live and st.outs.

    Readings are eased over about half a second so the digits stay readable, but a real
    jump (plugging the charger in, a drive spinning up) shows at once.
    """
    now = time.time()
    dt = now - st.live_t if st.live_t else 0.0
    st.live_t = now
    raw, outs = fake or {}, fake_outs or {}
    if smc is not None:
        try:
            raw, outs = smc.power(), smc.port_out()
        except Exception:
            raw, outs = {}, {}
    k = 1.0 if dt <= 0 or dt > 3 else 1.0 - math.exp(-dt / 0.5)

    def ease(old, new):
        if old is None or abs(new - old) > max(1.5, 0.5 * abs(old)):
            return new
        return old + k * (new - old)

    for key, v in raw.items():
        if v is None or not -500 < v < 500:
            st.live.pop(key, None)
        else:
            st.live[key] = ease(st.live.get(key), v)
    fresh = {}
    for n, (v, a) in outs.items():
        old = st.outs.get(n)
        fresh[n] = (ease(old[0] if old else None, v), ease(old[1] if old else None, a))
    st.outs = fresh


def power_picture(st, bat):
    """Where the power is going right now: {plugged, in_w, in_v, in_a, bat_w, mac_w}.

    The charger and the battery are each measured. What the Mac itself uses is whatever
    comes in and does not go into the battery (or, on battery, what the battery gives).
    A Mac without a battery reports its own draw directly.
    """
    lv = st.live
    pic = {"plugged": bool(bat and bat.get("plugged")), "in_w": 0.0, "in_v": 0.0, "in_a": 0.0,
           "bat_w": None, "mac_w": None}
    if bat:
        pic["bat_w"] = bat.get("bat_w")
        pic["mac_w"] = bat.get("load_w")
        if pic["plugged"]:
            pic["in_w"], pic["in_v"], pic["in_a"] = bat.get("in_w") or 0.0, bat.get("in_v") or 0.0, bat.get("in_a") or 0.0
            if lv.get("in_w", 0) > 0.5 and lv.get("in_v", 0) > 3:
                pic["in_w"], pic["in_v"] = lv["in_w"], lv["in_v"]
                pic["in_a"] = lv.get("in_a") or lv["in_w"] / lv["in_v"]
        if lv.get("bat_w") is not None:
            pic["bat_w"] = lv["bat_w"]
        if pic["bat_w"] is not None and (pic["in_w"] > 0.5 or not pic["plugged"]):
            pic["mac_w"] = max(0.0, pic["in_w"] - pic["bat_w"])
    elif lv.get("sys_w", 0) > 0.3:
        pic["mac_w"] = lv["sys_w"]
    return pic


def log_power(st, pw, now):
    """Histories and running totals. Called about once a second, whatever screen is showing."""
    pic = st.pw
    dt = min(5.0, now - st.log_t) if st.log_t else 0.0
    st.log_t = now
    if pic["mac_w"] is not None:
        mac = pic["mac_w"]
        st.h["mac_w"].append(mac)
        st.h["in_w"].append(pic["in_w"])
        st.n_mac += 1
        st.sum["mac_w"] += mac
        st.peak["mac_w"] = max(st.peak["mac_w"], mac)
        st.wh_mac += mac * dt / 3600.0
        st.wh_in += pic["in_w"] * dt / 3600.0
    # per-port totals: the highest watts seen and the energy handed over since the device was plugged in
    for num, (v, a) in st.outs.items():
        w = v * a
        if w < 0.02:
            st.port_stats.pop(num, None)
            continue
        ps = st.port_stats.setdefault(num, {"peak": 0.0, "mwh": 0.0})
        ps["peak"] = max(ps["peak"], w)
        ps["mwh"] += w * dt / 3.6
    # remember what got plugged in or pulled out, and when
    seen = {p["id"]: p["active"] for p in pw.ports}
    if st.port_seen is not None:
        for p in pw.ports:
            if p["id"] in st.port_seen and st.port_seen[p["id"]] != p["active"]:
                what = "port %d" % p["num"] if p["kind"].startswith("USB-C") else p["kind"]
                st.events.append("%s  %s %s" % (time.strftime("%H:%M:%S"), what,
                                                "plugged in" if p["active"] else "unplugged"))
    if seen:
        st.port_seen = seen


# ============================================================ advice
def health_check(s, mem, pressure, temps, dsk, bat, procs):
    tips = []
    sw_u = mem["swap"]
    if pressure != "normal" or sw_u > 1.0:
        tips.append((BAD if pressure == "critical" else WARN,
                     "RAM is tight (swap %.1f GB): quit apps at the top of the RAM list (press s)" % sw_u))
    if s["thermal"] not in (None, "Nominal"):
        tips.append((BAD, "Mac is slowing itself down from heat: close heavy apps and keep the vents clear"))
    elif temps.get("cpu_max") and temps["cpu_max"] > 95:
        tips.append((HOT, "CPU is very hot (%d°C): heavy work is running" % temps["cpu_max"]))
    d_used, d_total, d_free = dsk
    if d_total and d_free / d_total < 0.15:
        tips.append((WARN, "SSD only has %.0f GB free: keep 15-20%% free so it stays fast" % d_free))
    busy = [p for p in procs if p[2] > 80 and p[4] not in ("kernel_task", "WindowServer")]
    if busy:
        p = max(busy, key=lambda r: r[2])
        tips.append((WARN, "%s is using %.0f%% CPU: quit it if you don't need it (select + k)" % (p[4], p[2])))
    if bat and bat["health"] and bat["health"] < 80:
        tips.append((WARN, "Battery health is %.0f%%: Apple recommends service below 80%%" % bat["health"]))
    elif bat and bat.get("condition") and bat["condition"].lower() not in ("good", "normal"):
        tips.append((WARN, "macOS reports the battery's condition as: " + bat["condition"]))
    return tips


# ============================================================ drawing surfaces
class Canvas:
    """A grid of coloured characters, for pictures that are placed rather than written line by line."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.ch = [[" "] * w for _ in range(h)]
        self.st = [[""] * w for _ in range(h)]

    def put(self, x, y, text, style=""):
        if 0 <= y < self.h:
            for i, c in enumerate(text):
                if 0 <= x + i < self.w:
                    self.ch[y][x + i], self.st[y][x + i] = c, style

    def lines(self):
        out = []
        for chars, styles in zip(self.ch, self.st):
            row, last = [], None
            for c, s in zip(chars, styles):
                if s != last:
                    row.append(RESET + s)
                    last = s
                row.append(c)
            out.append("".join(row) + RESET)
        return out


class Dots:
    """A finer canvas: every character holds 2 x 4 dots (braille), so circles and arcs look round.
    Each character has one colour; the last dot drawn into it decides."""

    BIT = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))       # [row in the cell][column in the cell]

    def __init__(self, w, h):
        self.w, self.h = w, h                    # in characters
        self.bits = [0] * (w * h)
        self.col = [None] * (w * h)

    def dot(self, x, y, col):
        x, y = int(x), int(y)
        if 0 <= x < self.w * 2 and 0 <= y < self.h * 4:
            i = (y >> 2) * self.w + (x >> 1)
            self.bits[i] |= self.BIT[y & 3][x & 1]
            self.col[i] = col

    def arc(self, cx, cy, r, a0, a1, col, sparse=1.0):
        """Dots along a circle of radius r from angle a0 to a1 (radians, clockwise from 3 o'clock).
        `col` is one colour, or a function of how far along the arc we are (0..1)."""
        n = max(4, int(abs(a1 - a0) * r * 1.3 * sparse))
        pick = col if callable(col) else None
        for k in range(n + 1):
            f = k / float(n)
            a = a0 + (a1 - a0) * f
            self.dot(cx + r * math.cos(a) + 0.5, cy + r * math.sin(a) + 0.5, pick(f) if pick else col)

    def disc(self, cx, cy, r, col):
        ir = int(r + 1)
        for dy in range(-ir, ir + 1):
            for dx in range(-ir, ir + 1):
                d = math.sqrt(dx * dx + dy * dy)
                if d <= r:
                    self.dot(cx + dx + 0.5, cy + dy + 0.5, col(d / max(r, 0.001)) if callable(col) else col)

    def blit(self, cv, x0, y0):
        for cy in range(self.h):
            base = cy * self.w
            for cx in range(self.w):
                b = self.bits[base + cx]
                if b:
                    cv.put(x0 + cx, y0 + cy, chr(0x2800 + b), rgb(self.col[base + cx]))


BIG = {"0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111",
       "4": "101101111001001", "5": "111100111001111", "6": "111100111101111", "7": "111001010010010",
       "8": "111101111101111", "9": "111101111001111", "%": "101001010100101", "°": "111101111000000"}


def big_text(cv, x, y, text, style):
    """Large numerals, three rows tall, built from half blocks. Returns the width used."""
    start = x
    for ch in text:
        px = BIG.get(ch)
        if px:
            for r in range(3):
                for c in range(3):
                    top = px[r * 6 + c] == "1"
                    bottom = r < 2 and px[r * 6 + 3 + c] == "1"
                    if top or bottom:
                        cv.put(x + c, y + r, "█" if top and bottom else "▀" if top else "▄", style)
        x += 4
    return x - 1 - start


def cv_bar(cv, x, y, frac, width, stops, glow=None):
    """Segmented meter drawn on a canvas. glow(i) -> 0..1 brightens lit segments (for animation)."""
    lit = int(round(min(1.0, max(0.0, frac)) * width))
    for i in range(width):
        if i < lit:
            col = grad(stops, i / float(max(1, width - 1)))
            if glow:
                col = mix(col, WHITE, 0.45 * glow(i))
            cv.put(x + i, y, "▊", rgb(col))
        else:
            cv.put(x + i, y, "▊", rgb(P_SEG))


def cv_ansi(cv, x, y, text):
    """Copy an already-coloured string (a graph row, for example) onto a canvas."""
    style = ""
    for tok in TOKEN_RE.findall(text):
        if tok[0] == "\x1b":
            style = "" if tok == RESET else style + tok
        else:
            if tok != " ":
                cv.put(x, y, tok, style)
            x += 1


def wave(x, t, period, speed, sharp=3):
    """A bump of light that glides along x as time passes: 0..1."""
    return (0.5 + 0.5 * math.sin(2 * math.pi * (x / float(period) - t * speed))) ** sharp


# ============================================================ screen 2: ports, power & heat
PORT_GLYPH = {"USB-C": "(══)", "MagSafe": "[▪▪]", "HDMI": "◥██◤", "SD": "[▭▭]", "Audio": "(◉)"}
HEAT = [(245, 170, 60), (245, 95, 50), (235, 45, 55)]


def port_text(p, pw, bat, chip, sole_charger, devices, out=None):
    """(name, status lines, flow, colour) for one port. flow: None, "in" (power in) or "both" (a device)."""
    kind, num = p["kind"], p["num"]
    if p.get("static"):
        return "Headphone jack", ["3.5 mm audio"], None, P_DIM
    if kind.startswith("USB-C"):
        name = "%s · port %d" % (tb_name((pw.tb.get(num) or {}).get("speed"), chip), num)
    else:
        name = "SD card slot" if kind.startswith("SD") else kind
    lines, flow, col = [], None, P_MUTED
    c = p.get("charge")
    if c:
        flow, col = "in", P_PWR
        if sole_charger and bat and bat.get("in_w"):
            lines.append("power in %.1f W" % bat["in_w"])
            lines.append("%.1f V × %.2f A" % (bat.get("in_v") or 0, bat.get("in_a") or 0))
        else:
            lines.append("charger connected")
        lines.append("charger can give %.0f W" % c["w"])
    labels = {"CIO": "Thunderbolt device", "USB3": "USB 3 device", "USB2": "USB 2 device",
              "DisplayPort": "display", "SD": "card inserted"}
    found = [labels[t] for t in p["transports"] if t in labels]
    if found:
        flow = "both"
        col = P_NET if found == ["display"] else P_UP
        tb_devs = (pw.tb.get(num) or {}).get("devices", []) if kind.startswith("USB-C") else []
        named = tb_devs + [n for d in devices for n in d["names"]]
        lines += named or [" + ".join(found)]
        for d in devices:
            k = d.get("disk")
            if k and k["size"]:
                free = sum(v[2] for v in k["vols"])
                lines.append("drive %s" % human_size(k["size"]) + (" · %s free" % human_size(free) if k["vols"] else ""))
        ma = sum(d["ma"] for d in devices)
        if kind.startswith("USB-C") and not c:
            if out:                                   # measured live on this port
                lines.append("power out %.2f W" % (out[0] * out[1]))
                lines.append("%.2f V × %.3f A" % out + (" · max %.1f W" % (ma * 5 / 1000.0) if ma else ""))
            else:
                lines.append("power in 0 W · out up to %.1f W" % (ma * 5 / 1000.0) if ma else "power in 0 W")
        for d in devices:
            if d.get("disk"):
                lines.append("now R %s · W %s" % (human_rate(d["r"]), human_rate(d["w"])))
                if d.get("peak", 0) >= 1e6:
                    lines.append("fastest seen %s" % human_rate(d["peak"]))
        fastest = max(devices, key=lambda d: d["bps"]) if devices else None
        if fastest and fastest["speed"]:
            # what the cable link can carry, after USB's own overhead: a ceiling, not the drive's real speed
            cap = {20: 2400, 10: 1200, 5: 500}.get(fastest["bps"] // 10 ** 9)
            if cap and any(d.get("disk") for d in devices):
                lines.append("cable limit ~%d MB/s" % cap)
            else:
                lines.append("link " + fastest["speed"])
    if not lines:
        lines.append("plugged in, no data" if p["active"] else "empty")
        if p["active"]:
            col = WARN
    return name, lines, flow, col


def mac_view(st, s, temps, bat, dsk, info, W, H):
    """The Mac seen from above: ports on its sides, the chip in the middle, heat leaving the case."""
    pw = info["ports"]
    now = time.time() if st.moving() else 0.0      # a still picture when motion is off
    cw = W - 4
    cv = Canvas(cw, H)
    model = pw.model or "Mac"
    left, right = port_layout(model, pw.ports)
    laptop, fanless = "Book" in model, "Air" in model
    flow_w = 7 if cw >= 96 else 4
    pic = st.pw
    if bat:
        bat = dict(bat, in_w=pic["in_w"], in_v=pic["in_v"], in_a=pic["in_a"], bat_w=pic["bat_w"])
    bw = max(28, min(84, int(cw * 0.46)))
    lw = (cw - bw - 2 * flow_w) // 2
    bx0 = lw + flow_w
    bx1 = bx0 + bw - 1
    by0, by1 = 2, H - 3
    edge = rgb(P_BORDER)

    # ---- the body
    cv.put(bx0, by0, "╭" + "─" * (bw - 2) + "╮", edge)
    cv.put(bx0, by1, "╰" + "─" * (bw - 2) + "╯", edge)
    for y in range(by0 + 1, by1):
        cv.put(bx0, y, "│", edge)
        cv.put(bx1, y, "│", edge)

    # ---- the chip
    t_cpu, t_gpu = temps.get("cpu"), temps.get("gpu")
    hottest = temps.get("cpu_max") or t_cpu or 40.0
    heat = min(1.0, max(0.0, (hottest - 35) / 60.0))
    hot = grad(HEAT, heat)
    white = (255, 255, 255)
    ix0, iw = bx0 + 4, bw - 8                      # the space inside the body, clear of the port shapes
    cwid, cx0, cy0 = iw, ix0, by0 + 3
    ok = lambda y: y < by1                         # never draw over the bottom edge
    label = rgb(P_DIM)
    chip_edge = rgb(grad([hot, white], 0.12 + 0.12 * math.sin(now * (2 + 4 * heat))))
    title = " " + cut(info["chip"].upper(), cwid - 16) + " "
    cv.put(cx0, cy0, "╭─" + "─" * (cwid - 3) + "╮", chip_edge)
    cv.put(cx0 + 2, cy0, title, BOLD + chip_edge)
    if s.get("has_power", True):
        tag = " %.1f W " % s["total_w"]
        cv.put(cx0 + cwid - 2 - len(tag), cy0, tag, BOLD + rgb(P_PWR))
    cores = s["e_cores"] + s["p_cores"]
    mw = max(4, cwid - 21)
    rows_c = (("CPU", P_P, G_P, t_cpu, sum(cores) / len(cores) if cores else 0.0),
              ("GPU", P_GPU, G_GPU, t_gpu if t_gpu is not None else t_cpu, s["gpu"] or 0.0))
    for k, (name, col, stops, t, load) in enumerate(rows_c):
        y = cy0 + 1 + k
        cv.put(cx0, y, "│", chip_edge)
        cv.put(cx0 + cwid - 1, y, "│", chip_edge)
        cv.put(cx0 + 2, y, name, BOLD + rgb(col))
        cv_bar(cv, cx0 + 6, y, load, mw, stops)
        cv.put(cx0 + 7 + mw, y, "%3d%%" % round(100 * load), TEXT)
        cv.put(cx0 + 12 + mw, y, "%3d°C" % t if t is not None else "  n/a", BOLD + rgb(temp_rgb(t or 0)))
    y = cy0 + 3
    cv.put(cx0, y, "│", chip_edge)
    cv.put(cx0 + cwid - 1, y, "│", chip_edge)
    cv.put(cx0 + 2, y, "CORES", label)
    x = cx0 + 8
    for group, stops in ((s["e_cores"], G_E), (s["p_cores"], G_P)):
        for c in group:
            if x < cx0 + cwid - 2:
                cv.put(x, y, VBAR[max(1, int(round(c * 8)))], rgb(grad(stops, c)))
            x += 1
        x += 1
    cv.put(cx0, cy0 + 4, "╰" + "─" * (cwid - 2) + "╯", chip_edge)

    # ---- fans
    fans = [f for f in temps.get("fans") or [] if f is not None]
    if fans and not ("Air" in model):
        bits = []
        for k, rpm in enumerate(fans[:2]):
            spin = "◐◓◑◒"[int(now * min(12.0, rpm / 250.0) + k * 2) % 4] if rpm > 100 else "○"
            bits.append("FAN %s %s" % (spin, "%d rpm" % rpm if rpm > 100 else "resting"))
        text = cut("     ".join(bits), max(0, cwid - 10))
        cv.put(cx0 + (cwid - len(text)) // 2, by0 + 1, text, rgb(grad([P_MUTED, hot], heat)))

    # ---- where the power is going right now
    y = cy0 + 6
    def wire(x0, x1, yy, col, speed=1.6):
        for x in range(x0, x1):
            f = (0.5 + 0.5 * math.sin(2 * math.pi * (x / 6.0 - now * speed))) ** 3
            cv.put(x, yy, "━", BOLD + rgb(grad([grad([col, P_BORDER], 0.6), grad([col, white], 0.45)], f)))
        cv.put(x1, yy, "▶", BOLD + rgb(col))

    load_w = pic["mac_w"]
    if ok(y + 2):
        cv.put(ix0, y, "POWER FLOW", label)
        y += 1
        if bat and bat.get("plugged") and bat.get("in_w"):
            src = "CHARGER %.1f W" % bat["in_w"]
            cv.put(ix0, y, src, BOLD + rgb(P_PWR))
            dst = "MAC %.1f W" % load_w if load_w else "MAC"
            cv.put(ix0 + iw - len(dst), y, dst, TEXT)
            wire(ix0 + len(src) + 1, ix0 + iw - len(dst) - 2, y, P_PWR)
            bw_ = bat.get("bat_w") or 0.0
            if bw_ > 0.3 and ok(y + 1):
                dst = "BATTERY +%.1f W" % bw_
                cv.put(ix0 + iw - len(dst), y + 1, dst, BOLD + rgb(P_RAM))
                cv.put(ix0 + len(src) + 3, y + 1, "┗", BOLD + rgb(P_RAM))
                wire(ix0 + len(src) + 4, ix0 + iw - len(dst) - 2, y + 1, P_RAM, 1.1)
                y += 1
        elif bat:
            src = "BATTERY %.1f W" % abs(bat.get("bat_w") or load_w or 0)
            cv.put(ix0, y, src, BOLD + rgb(P_RAM))
            dst = "MAC %.1f W" % load_w if load_w else "MAC"
            cv.put(ix0 + iw - len(dst), y, dst, TEXT)
            wire(ix0 + len(src) + 1, ix0 + iw - len(dst) - 2, y, P_RAM)
        else:
            cv.put(ix0, y, "WALL POWER", BOLD + rgb(P_PWR))
            wire(ix0 + 11, ix0 + iw - 5, y, P_PWR)
            cv.put(ix0 + iw - 3, y, "MAC", TEXT)
        y += 2

    # ---- battery
    if bat and ok(y + 3):
        lvl = bat["pct"] / 100.0
        b_col = GOOD if bat["pct"] > 40 else WARN if bat["pct"] > 20 else BAD
        cv.put(ix0, y, "BATTERY", label)
        head = "%d%%  %s" % (bat["pct"], bat["state"])
        if bat.get("mins") and bat["state"] in ("charging", "on battery"):
            head += "  ·  %s in %d:%02d" % ("full" if bat["state"] == "charging" else "empty",
                                            bat["mins"] // 60, bat["mins"] % 60)
        cv.put(ix0 + 9, y, cut(head, iw - 9), TEXT)
        charging = bat["state"] == "charging"
        glow = (lambda i: (0.5 + 0.5 * math.sin(2 * math.pi * (i / 9.0 - now * 0.9))) ** 2) if charging else None
        for r in (1, 2):
            cv_bar(cv, ix0, y + r, lvl, iw - 2, [b_col, grad([b_col, white], 0.25)], glow)
            cv.put(ix0 + iw - 2, y + r, "▌", rgb(P_BORDER))
        bits = []
        if bat.get("health"):
            bits.append("health %d%%" % bat["health"])
        bits.append("%d cycles" % bat["cycles"])
        if bat.get("temp"):
            bits.append("%d°C" % bat["temp"])
        if bat.get("adapter_w"):
            bits.append("charger %d W" % bat["adapter_w"])
        if ok(y + 3):
            cv.put(ix0, y + 3, cut("  ·  ".join(bits), iw), rgb(P_MUTED))
        y += 5

    # ---- built-in SSD
    d_used, d_total, d_free = dsk
    if d_total and ok(y + 1):
        cv.put(ix0, y, "STORAGE", label)
        txt = "%d%%  ·  %.0f GB free of %.0f" % (round(100 * d_used / d_total), d_free, d_total)
        cv.put(ix0 + 9, y, cut(txt, iw - 9), TEXT)
        cv_bar(cv, ix0, y + 1, d_used / d_total, iw, G_DSK)
        if ok(y + 2):
            io = "read %s  ·  write %s" % (human_rate(s["disk_r"]), human_rate(s["disk_w"]))
            if temps.get("ssd") is not None:
                io += "  ·  %d°C" % temps["ssd"]
            cv.put(ix0, y + 2, cut(io, iw), rgb(P_MUTED))
        y += 4

    # ---- power history: what came in from the charger and what the Mac used, over the last minutes
    if len(st.h["mac_w"]) > 1 and ok(y + 2):
        cv.put(ix0, y, "POWER HISTORY", label)
        cv.put(ix0 + 15, y, cut("used %s" % human_wh(st.wh_mac)
                                + (", from charger %s" % human_wh(st.wh_in) if st.wh_in > 0.0005 else "")
                                + " since mtop started", iw - 15), rgb(P_MUTED))
        y += 1
        gw = iw - 9
        series = [("MAC", st.h["mac_w"], G_RAM, P_RAM)]
        if max(st.h["in_w"]) > 0.5:
            series.insert(0, ("IN", st.h["in_w"], G_PWR, P_PWR))
        shown = [v for _, hist, _, _ in series for v in list(hist)[-gw * 2:]]
        top = max(5.0, max(shown) * 1.1)
        gh = max(1, min(3, (by1 - y) // len(series)))
        for name, hist, stops, col in series:
            if not ok(y + gh - 1):
                break
            cv.put(ix0, y, name, BOLD + rgb(col))
            cv.put(ix0, y + gh - 1 if gh > 1 else y, "%-4s%3dW" % (name if gh == 1 else "", round(hist[-1])),
                   BOLD + rgb(col))
            for r, line in enumerate(graph(hist, gw, gh, top, stops)):
                cv_ansi(cv, ix0 + 9, y + r, line)
            y += gh

    # ---- heat: red worms crawl from the chip to where the heat leaves the case
    speed, count = 5 + 16 * heat, 1 + int(heat * 3)
    xl, xr = cx0 + 2, cx0 + cwid - 3
    if fanless:                                   # no fan: heat soaks into the whole metal case
        mid = bx0 + bw // 2
        side_l = [(bx0, y, "┃") for y in range(by0 + 1, by1)]
        side_r = [(bx1, y, "┃") for y in range(by0 + 1, by1)]
        paths = [[(x, by0, "━") for x in range(mid, bx0, -1)] + side_l + [(x, by1, "━") for x in range(bx0 + 1, mid)],
                 [(x, by0, "━") for x in range(mid + 1, bx1)] + side_r + [(x, by1, "━") for x in range(bx1 - 1, mid, -1)]]
        speed *= 0.5
        msg = "no fan: heat spreads through the metal case"
    else:                                         # fans push hot air out of the back edge
        paths = [[(xl, y, "┃") for y in range(cy0 - 1, by0, -1)] + [(x, by0, "━") for x in range(xl, bx0, -1)],
                 [(xr, y, "┃") for y in range(cy0 - 1, by0, -1)] + [(x, by0, "━") for x in range(xr, bx1)]]
        msg = "▲ hot air leaves at the back" + (" (hinge vents)" if laptop else "")
        for x in range(bx0 + 2, bx1 - 1, 3):      # air shimmering off the back edge
            f = 0.5 + 0.5 * math.sin(now * (2 + 5 * heat) + x * 1.7)
            cv.put(x, by0 - 1, "˙" if f > 0.66 else "·" if f > 0.33 else ".", rgb(grad([P_BORDER, hot], 0.25 + 0.75 * f)))
    for path in paths:
        loop = len(path) + 8.0
        for j in range(count):
            head = (now * speed + j * loop / count) % loop          # not rounded: the worm glides between cells
            for k in range(max(0, int(head) - 7), min(len(path), int(head) + 2)):
                d = head - k
                f = 1 + d if d < 0 else 1 - d / 7.0                 # soft nose, long fading tail
                if f > 0:
                    x, y, c = path[k]
                    cv.put(x, y, c, (BOLD if f > 0.6 else "") + rgb(grad([P_BORDER, hot], f)))
    msg = cut(msg, cw)
    cv.put(max(0, bx0 + (bw - len(msg)) // 2), 0, msg, BOLD + rgb(hot))

    # ---- the ports
    outs = st.outs
    chargers = [p for p in pw.ports if p.get("charge")]
    matched = usb_by_port(pw)

    def side(items, is_left):
        if not items:
            return
        span = by1 - by0 - 1
        slot = max(2, span // len(items))
        for i, p in enumerate(items):
            y = by0 + 1 + i * span // len(items) + max(0, (slot - 3) // 2)
            name, lines, flow, col = port_text(p, pw, bat, info["chip"], len(chargers) == 1,
                                               (matched or {}).get(p["id"], []),
                                               outs.get(p["num"]) if p["kind"].startswith("USB-C") else None)
            ps = st.port_stats.get(p["num"]) if p["kind"].startswith("USB-C") else None
            if ps:
                at = next((k for k, l in enumerate(lines) if l.startswith("power out")), None)
                if at is not None:
                    total = "%.0f mWh" % ps["mwh"] if ps["mwh"] < 1000 else "%.2f Wh" % (ps["mwh"] / 1000)
                    lines.insert(min(len(lines), at + 2), "peak %.2f W, sent %s" % (ps["peak"], total))
            live = flow is not None or p["active"]
            glow = BOLD + rgb(grad([col, (255, 255, 255)], 0.3 + 0.3 * math.sin(now * 5))) if live else rgb(P_DIM)
            glyph = next((g for k, g in PORT_GLYPH.items() if p["kind"].startswith(k)), "(··)")
            cv.put(bx0 - 1 if is_left else bx1 + 2 - len(glyph), y, glyph, glow)
            xs = range(lw, bx0 - 1) if is_left else range(bx1 + 2, bx1 + 2 + flow_w)
            inward = 1 if is_left else -1         # which way is "into the Mac" on this side
            xs = list(xs)
            white = (255, 255, 255)
            for n, x in enumerate(xs):
                if not flow:
                    cv.put(x, y, "┄", rgb(P_DIM))
                    continue
                # light glides along the cable: brightness is a wave, so it moves smoothly between cells
                into = 0.5 + 0.5 * math.sin(2 * math.pi * (inward * x / 5.0 - now * 1.6))
                f = into ** 3
                if flow == "both":
                    f = max(f, (0.5 + 0.5 * math.sin(2 * math.pi * (-inward * x / 5.0 - now * 1.1))) ** 3)
                near_mac = n == (len(xs) - 1 if is_left else 0)
                near_dev = n == (0 if is_left else len(xs) - 1)
                ch = "━"
                if near_mac:
                    ch = "▶" if is_left else "◀"
                elif near_dev and flow == "both":
                    ch = "◀" if is_left else "▶"
                cv.put(x, y, ch, BOLD + rgb(grad([grad([col, P_BORDER], 0.6), grad([col, white], 0.45)], f)))
            soft = rgb(grad([col, P_MUTED], 0.6))             # first detail line stands out, the rest sit back
            rows_ = [(name, TEXT if live else rgb(P_MUTED))] + [
                (l, (BOLD + rgb(col) if k == 0 else soft) if live else rgb(P_DIM)) for k, l in enumerate(lines)]
            for k, (text, style) in enumerate(rows_[:slot]):
                if is_left:
                    text = cut(text, lw - 1)
                    cv.put(lw - 1 - len(text), y + k, text, style)
                else:
                    x = bx1 + 3 + flow_w
                    cv.put(x, y + k, cut(text, cw - x), style)

    side(left, True)
    side(right, False)

    # ---- notes under the body
    loose = [d["name"] for d in pw.usb] if matched is None else []
    if not pw.ports:
        note = "this macOS does not report its ports, so only power and heat are shown"
    elif loose:
        note = "USB devices: " + ", ".join(loose)
    else:
        note = "port sides come from the Mac model  ·  what is plugged in is read live"
    if pw.usb and matched is not None:
        note = ("power out is measured live by the Mac's own port sensors" if outs else
                "\"out up to\" is the most a device may draw: this Mac has no port power sensors mtop can read")
    cv.put(1, H - 2, cut(note, cw - 2), rgb(P_DIM))
    cv.put(1, H - 1, "━▶", BOLD + rgb(P_PWR))
    cv.put(4, H - 1, "power in", rgb(P_MUTED))
    cv.put(15, H - 1, "◀▶", BOLD + rgb(P_UP))
    cv.put(18, H - 1, "device", rgb(P_MUTED))
    cv.put(27, H - 1, "◀▶", BOLD + rgb(P_NET))
    cv.put(30, H - 1, "display", rgb(P_MUTED))
    cv.put(40, H - 1, "━", BOLD + rgb(hot))
    cv.put(42, H - 1, "heat", rgb(P_MUTED))
    if st.events:
        ev = cut("last change  " + st.events[-1], max(0, cw - 52))
        cv.put(cw - 1 - len(ev), H - 1, ev, rgb(P_MUTED))
    return panel("Ports, power & heat", cv.lines(), W, P_PWR, muted(model))


# ============================================================ screen 1: overview
def overview(st, s, mem, pressure, temps, dsk, bat, info, W, rows, fixed):
    """Six panels: CPU, GPU & memory, temperatures, storage / battery / network, power, busiest apps."""
    two = W >= 100
    wl = (W - 1) // 2 if two else W
    wr = W - 1 - wl if two else W
    # graph height grows with the terminal: each of the 3 panel rows is 7 + 2h lines tall
    h = max(1, min(4, (rows - fixed - 21) // 6)) if two else 1
    body_lines = 5 + 2 * h
    th = s["thermal"]
    body = []

    def row(a, b):
        body.extend(side_by_side(a, b, wl, wr) if two else a + b)

    def cores_row(cs, avail):
        wide = 2 if len(cs) * 3 - 1 <= avail else 1
        gap = " " if len(cs) * (wide + 1) - 1 <= avail else ""
        return dim("%6s " % "cores") + gap.join(
            rgb(grad(G_LOAD, c)) + VBAR[max(1, int(round(c * 8)))] * wide for c in cs) + RESET

    # ---- row 1: CPU | GPU & Memory
    inner = wl - 4
    bw, gw = max(8, inner - 24), inner - 7
    e, p = st.h["e"][-1], st.h["p"][-1]
    speed = lambda mhz, cores: "%4d MHz" % mhz if mhz else "%d cores" % len(cores)
    if s["e_cores"] and s["p_cores"]:
        cpu = [gauge("E-CPU", e, P_E, pct(e), speed(s["e_mhz"], s["e_cores"]), bw, G_E)]
        cpu += graph_rows(st.h["e"], gw, h, 1.0, G_E, hi="100%", lo="0%" if h > 1 else "")
        cpu += [cores_row(s["e_cores"], gw), "",
                gauge("P-CPU", p, P_P, pct(p), speed(s["p_mhz"], s["p_cores"]), bw, G_P)]
        cpu += graph_rows(st.h["p"], gw, h, 1.0, G_P, hi="100%", lo="0%" if h > 1 else "")
        cpu.append(cores_row(s["p_cores"], gw))
    else:                                         # Intel, or any chip with one kind of core
        cs = s["p_cores"] or s["e_cores"]
        v = p if s["p_cores"] else e
        cpu = [gauge("CPU", v, P_P, pct(v), speed(s["p_mhz"] or s["e_mhz"], cs), bw, G_P), ""]
        cpu += graph_rows(st.h["p" if s["p_cores"] else "e"], gw, body_lines - 4, 1.0, G_P, hi="100%", lo="0%")
        cpu += ["", cores_row(cs, gw)]
    load = info.get("load")
    cpu_right = muted("load ") + TEXT + "%.2f" % load if load is not None else ""

    used, total, sw_u, cached, wired, comp = (mem[k] for k in ("used", "total", "swap", "cached", "wired", "comp"))
    ram_f = used / total if total else 0
    inner = wr - 4
    bw, gw = max(8, inner - 24), inner - 7
    app = max(0.0, used - wired - comp)
    cache_seg = max(0.0, min(cached, total - used))
    p_col = GOOD if pressure == "normal" else WARN if pressure == "warning" else BAD
    gm = [gauge("GPU", s["gpu"], P_GPU, pct(s["gpu"]), "%4d MHz" % s["gpu_mhz"] if s["gpu_mhz"] else "", bw, G_GPU)]
    gm += graph_rows(st.h["gpu"], gw, h, 1.0, G_GPU, hi="100%", lo="0%" if h > 1 else "")
    gm.append(dim("%6s " % "VRAM") + TEXT + "%.1f GB" % s["gpu_mem"] + RESET + muted(" in use, shared with RAM")
              if s.get("gpu_mem") is not None else "")
    gm.append(BOLD + rgb(P_RAM) + "%-6s" % "RAM" + RESET + " "
              + stacked([(app, SEG_APP), (wired, SEG_WIRED), (comp, SEG_COMP), (cache_seg, SEG_CACHE)], total, bw)
              + " " + pct(ram_f) + " " + muted("%.1f/%.0f GB" % (used, total)))
    gm.append(" " + " ".join(dot(c, muted(n + " ") + TEXT + "%.1f" % v + RESET) for c, n, v in (
        (SEG_APP, "app", mem["app"]), (SEG_WIRED, "wired", wired), (SEG_COMP, "comp", comp), (SEG_CACHE, "cache", cached))))
    gm += graph_rows(st.h["ram"], gw, h, 1.0, G_RAM, hi="100%", lo="0%" if h > 1 else "")
    swap_txt = ("%.1f GB used" % sw_u) if sw_u > 0.05 else "inactive"
    gm.append(BOLD + rgb(P_MUTED) + "%-6s" % "Swap" + RESET + " " + TEXT + swap_txt + RESET
              + muted("    pressure ") + dot(p_col, BOLD + rgb(p_col) + pressure + RESET))
    row(panel("CPU", cpu, wl, P_E, cpu_right),
        panel("GPU & Memory", gm, wr, P_GPU, muted("%.0f GB memory" % total) if total else ""))

    # ---- row 2: Temperatures | Storage, Battery & Network
    inner = wl - 4
    bw, gw = max(8, inner - 24), inner - 7
    tl = []
    for label, key, hist, col in (("CPU", "cpu", "t_cpu", P_P), ("GPU", "gpu", "t_gpu", P_GPU)):
        v = temps.get(key)
        if v is None:
            why = "no separate sensor: it shares the CPU's chip" if key == "gpu" and temps.get("cpu") is not None \
                else "sensor not available"
            tl += [BOLD + rgb(P_MUTED) + "%-6s" % label + RESET + " " + dim(why)] + [""] * h
        else:
            tl.append(gauge(label, v / 105.0, col, BOLD + rgb(temp_rgb(v)) + "%3d°C" % v,
                            "max %d°C" % temps[key + "_max"], bw, G_TEMP))
            tl += graph_rows(st.h[hist], gw, h, 105, G_TEMP, 20, hi="105°", lo="20°" if h > 1 else "")
        if key == "cpu":
            tl.append("")
    extra = []
    if temps.get("ssd") is not None:
        extra.append(muted("SSD ") + BOLD + rgb(temp_rgb(temps["ssd"])) + "%d°C" % temps["ssd"] + RESET)
    if bat and bat.get("temp"):
        extra.append(muted("Battery ") + BOLD + rgb(temp_rgb(bat["temp"])) + "%d°C" % bat["temp"] + RESET)
    if temps.get("fans"):
        rpm = max(temps["fans"])
        extra.append(muted("Fans ") + TEXT + ("%d rpm" % rpm if rpm > 100 else "off") + RESET)
    tl.append(" " + "    ".join(extra) if extra else dim(" no SSD / battery sensors"))
    tl.append(" " + "  ".join(dot(c, dim(t)) for c, t in ((GOOD, "<70"), (WARN, "70-85"), (HOT, "85-95"),
                                                           (BAD, "95+ °C"))))
    th_col = GOOD if th == "Nominal" else BAD

    inner = wr - 4
    bw, gw = max(8, inner - 24), inner - 7
    d_used, d_total, d_free = dsk
    d_f = d_used / d_total if d_total else 0
    smart = info["smart"]
    sl = [gauge("SSD", d_f, P_DSK, pct(d_f), "%.0f GB free" % d_free, bw, G_DSK if d_f < 0.85 else [WARN, BAD]),
          dim("%6s " % "I/O") + muted("R ") + TEXT + human_rate(s["disk_r"]) + RESET + muted("  W ") + TEXT
          + human_rate(s["disk_w"]) + RESET + "  "
          + dot(GOOD if smart == "Verified" else WARN, muted("SMART " + ("ok" if smart == "Verified" else smart)))]
    if h > 1:
        sl.append("")
    if bat:
        b_col = GOOD if bat["pct"] > 40 else WARN if bat["pct"] > 20 else BAD
        sl.append(gauge("Batt", bat["pct"] / 100.0, P_PWR, TEXT + "%3d%%" % bat["pct"], bat["state"].split(",")[0], bw, [b_col]))
        detail = []
        if bat["health"]:
            detail.append(muted("health ") + TEXT + "%.0f%%" % bat["health"] + RESET)
        detail.append(TEXT + str(bat["cycles"]) + RESET + muted(" cycles"))
        if bat.get("mins") and bat["state"] in ("charging", "on battery"):
            detail.append(TEXT + "%d:%02d" % (bat["mins"] // 60, bat["mins"] % 60) + RESET
                          + muted(" to full" if bat["state"] == "charging" else " left"))
        sl.append(" " * 7 + "   ".join(detail))
    else:
        sl += [BOLD + rgb(P_MUTED) + "%-6s" % "Batt" + RESET + " " + dim("no battery (desktop Mac)"), ""]
    if h > 1:
        sl.append("")
    sl.append(BOLD + rgb(P_NET) + "%-6s" % "Net" + RESET + " " + rgb(P_NET) + "↓ " + TEXT + human_rate(s["net_in"])
              + RESET + "   " + rgb(P_UP) + "↑ " + TEXT + human_rate(s["net_out"]) + RESET)
    g = max(2, body_lines - len(sl))          # mirrored graph: download grows up, upload hangs down
    up_rows = g // 2
    n_in, n_out = list(st.h["net_in"])[-gw * 2:], list(st.h["net_out"])[-gw * 2:]
    top_in, top_out = max(n_in + [10e3]), max(n_out + [10e3])
    sl += graph_rows(n_in, gw, g - up_rows, top_in, G_NET, hi=short_rate(top_in))
    sl += graph_rows(n_out, gw, up_rows, top_out, G_UP, lo=short_rate(top_out), invert=True)[:up_rows] if up_rows else []
    row(panel("Temperatures", tl, wl, P_TMP, dot(th_col, muted(th.lower())) if th else ""),
        panel("Storage, Battery & Network", sl, wr, P_DSK, muted("%.0f GB" % d_total) if d_total else ""))

    # ---- row 3: Power | Top apps
    apps_top = 1 + len(body) + (0 if two else body_lines + 2)
    inner = wl - 4
    gw = inner - 7
    sw = max(6, inner - 37)
    has_chip, has_mac = s.get("has_power", True), st.n_mac > 0
    main = "mac_w" if has_mac else "total_w"            # the series drawn large: the whole Mac when it is known
    if has_chip and has_mac:
        wrows = (("cpu_w", "CPU", P_P, G_P), ("gpu_w", "GPU", P_GPU, G_GPU), ("total_w", "Chip", P_E, G_E),
                 ("mac_w", "Mac", P_PWR, G_PWR))
    elif has_chip:
        wrows = (("cpu_w", "CPU", P_P, G_P), ("gpu_w", "GPU", P_GPU, G_GPU), ("ane_w", "ANE", P_E, G_E),
                 ("total_w", "Total", P_PWR, G_PWR))
    else:
        wrows = (("mac_w", "Mac", P_PWR, G_PWR),)
    pw = []
    for key, label, color, stops in wrows:
        n = st.n_mac if key == "mac_w" else st.n
        pw.append(BOLD + rgb(color) + "%-5s" % label + RESET + " " + TEXT + "%6.2f W" % (st.h[key][-1] if st.h[key] else 0)
                  + RESET + muted("  avg %5.2f  pk %5.2f " % (st.sum[key] / max(1, n), st.peak[key]))
                  + graph(st.h[key], sw, 1, max(st.peak[key], 0.5), stops)[0])
    if not has_chip:
        pic = st.pw
        if pic["plugged"] and pic["in_w"] > 0.5:
            pw.append(muted(" charger gives ") + TEXT + "%.1f W" % pic["in_w"] + RESET
                      + (muted(", battery takes ") + TEXT + "%.1f W" % pic["bat_w"] + RESET
                         if pic["bat_w"] and pic["bat_w"] > 0.3 else ""))
        elif bat:
            pw.append(muted(" running on the battery"))
        pw.append(dim(" CPU / GPU split: run without --no-sudo"))
    if h > 1:
        pw.append("")
    top_w = max(1.0, math.ceil(st.peak[main]))
    pw += graph_rows(st.h[main], gw, max(1, body_lines - len(pw) - 1), top_w, G_PWR, hi="%d W" % top_w, lo="0 W")
    pw.append(muted(" energy used ") + TEXT + human_wh(st.wh_mac if has_mac else st.wh) + RESET
              + muted(" since start" + (", whole Mac" if has_mac else "")))
    pw_right = muted("peak ") + TEXT + "%.1f W" % st.peak[main]
    if not has_chip and not has_mac:
        pw_right = muted("basic mode")
        pw = ["", muted(" Watts and MHz need administrator access."), "",
              muted(" Press ") + TEXT + "q" + RESET + muted(", then run mtop without ") + TEXT + "--no-sudo" + RESET,
              muted(" and type your Mac password when asked."), "",
              dim(" Everything else on this screen is live.")]

    inner = wr - 4
    name_w = max(6, inner - 29)
    st.rows = body_lines - 1
    procs = st.sorted_procs()
    pids = [r[0] for r in procs]
    if st.sel_pid in pids:                        # keep the same app selected when the list reorders
        st.sel = pids.index(st.sel_pid)
    st.sel = min(st.sel, max(0, len(procs) - 1))
    st.sel_pid = pids[st.sel] if pids else None
    arrow = lambda k: "▾" if st.sort == k else " "
    pl = [muted(" %6s %5s%s%5s %7s%s %s" % ("PID", "CPU%", arrow("cpu"), "", "RAM", arrow("mem"), "APP"))]
    for i, (pid, user, cpu_p, rss, name) in enumerate(procs):
        f = min(1.0, cpu_p / 100.0)
        if i == st.sel:
            cells = f * 5
            plain = "█" * int(cells) + (HBAR[int((cells - int(cells)) * 8)] if int(cells) < 5 else "")
            txt = " %6d %5.1f %-5s %7s  %s" % (pid, min(cpu_p, 999.9), plain, human_mem(rss), cut(name, name_w))
            pl.append(rgb(P_ACCENT, bg=True) + "\x1b[97m" + BOLD + pad(txt, inner) + RESET)
        else:
            lc = load_rgb(f)
            pl.append(dim(" %6d " % pid) + rgb(lc) + "%5.1f " % min(cpu_p, 999.9) + bar(f, 5, [lc], " ")
                      + " " + muted("%7s" % human_mem(rss)) + "  " + cut(name, name_w))
    row(panel("Power", pw, wl, P_PWR, pw_right),
        panel("Top apps", pl, wr, P_ACCENT, muted("sorted by ") + TEXT + ("CPU" if st.sort == "cpu" else "RAM")))

    st.rows_hit = (apps_top + 3, len(procs), wl + 2 if two else 1, W)
    return body


# ============================================================ screen 3: core
def advance_orbits(st, s, now):
    """Move every core's dot along its ring. A busy core circles fast, an idle one barely drifts."""
    e, p = s["e_cores"], s["p_cores"]
    if len(st.orbit["e"]) != len(e) or len(st.orbit["p"]) != len(p):
        st.orbit = {"e": [2 * math.pi * i / max(1, len(e)) for i in range(len(e))],
                    "p": [2 * math.pi * i / max(1, len(p)) + 0.4 for i in range(len(p))]}
        st.orbit_t = now
    dt = min(0.25, max(0.0, now - st.orbit_t))
    st.orbit_t = now
    for key, loads, sign in (("e", e, -1.0), ("p", p, 1.0)):
        ring = st.orbit[key]
        for i, load in enumerate(loads):
            ring[i] = (ring[i] + sign * (0.18 + 2.6 * load) * dt) % (2 * math.pi)


def draw_reactor(cv, x0, y0, w, h, st, s, mem, temps, t):
    """The chip as a small solar system: a warm core, a ring of efficiency cores, a ring of
    performance cores, and an outer ring that fills with memory use."""
    d = Dots(w, h)
    cx, cy = w - 0.5, 2 * h - 0.5
    R = min(w, 2 * h) - 1.5
    tau = 2 * math.pi
    ram = mem["used"] / mem["total"] if mem["total"] else 0.0
    d.arc(cx, cy, R, 0, tau, P_SEG, 0.5)
    for rr in (R, R - 1):
        d.arc(cx, cy, rr, -math.pi / 2, -math.pi / 2 + tau * ram, lambda f: grad(G_RAM, f))
    rings = (("e", s["e_cores"], 0.47 * R, G_E, -1.0), ("p", s["p_cores"], 0.74 * R, G_P, 1.0))
    if not s["e_cores"]:                              # one kind of core: a single ring
        rings = (("p", s["p_cores"], 0.62 * R, G_P, 1.0),)
    for key, loads, r, stops, sign in rings:
        d.arc(cx, cy, r, 0, tau, P_SEG, 0.28)
        for i, load in enumerate(loads):
            a = st.orbit[key][i] if i < len(st.orbit[key]) else 0.0
            col = grad(stops, 0.25 + 0.75 * load)
            tail = int(5 + 30 * load)
            for k in range(tail, 0, -1):              # a fading tail behind the dot, thicker near it
                ak = a - sign * k * 1.3 / r
                shade = mix(col, P_SEG, k / float(tail + 1))
                for rr in ((r, r - 1, r + 1) if k < tail * 0.5 else (r,)):
                    d.dot(cx + rr * math.cos(ak) + 0.5, cy + rr * math.sin(ak) + 0.5, shade)
            px, py = cx + r * math.cos(a), cy + r * math.sin(a)
            for dx in (-1, 0, 1, 2):                  # the core itself: a bright bead
                for dy in (-1, 0, 1, 2):
                    if (dx, dy) not in ((-1, -1), (2, -1), (-1, 2), (2, 2)):
                        d.dot(px + dx + 0.5, py + dy + 0.5, mix(col, WHITE, 0.45))
    cores = s["e_cores"] + s["p_cores"]
    load = sum(cores) / len(cores) if cores else 0.0
    hottest = temps.get("cpu_max") or temps.get("cpu") or 40.0
    hot = grad(HEAT, min(1.0, max(0.0, (hottest - 35) / 60.0)))
    r0 = R * (0.15 + 0.09 * load) * (1.0 + 0.05 * math.sin(t * (1.5 + 4 * load)))
    d.arc(cx, cy, r0 + 4, t * 0.4, t * 0.4 + tau, mix(hot, P_SEG, 0.6), 0.3)     # warmth around the chip
    d.disc(cx, cy, r0, lambda f: mix(hot, WHITE, 0.45 * f * f))
    d.blit(cv, x0, y0)
    if temps.get("cpu") is not None:
        label = "%d°" % temps["cpu"]
        cv.put(x0 + w // 2 - len(label) // 2, y0 + h // 2, label, "\x1b[97m" + BOLD)


def draw_dial(cv, x0, y0, w, h, frac, stops, title, color, notes):
    """A round gauge: the arc fills clockwise from lower left to lower right."""
    frac = min(1.0, max(0.0, frac))
    d = Dots(w, h)
    cx, R = w - 0.5, min(w, 2.3 * h) - 1.5
    cy = R + 1
    a0, sweep = math.pi * 0.75, math.pi * 1.5
    d.arc(cx, cy, R - 1, a0, a0 + sweep, P_SEG, 0.6)
    if frac > 0.004:
        for rr in (R, R - 1, R - 2):
            d.arc(cx, cy, rr, a0, a0 + sweep * frac, lambda f: grad(stops, f * frac))
        a = a0 + sweep * frac                          # a bright marker where the reading is now
        for rr in range(-4, 2):
            d.dot(cx + (R + rr) * math.cos(a) + 0.5, cy + (R + rr) * math.sin(a) + 0.5, WHITE)
    d.blit(cv, x0, y0)
    mid = int(cy / 4)
    text = "%d%%" % round(frac * 100)
    if h >= 9 and w >= 22:
        big_text(cv, x0 + (w - (4 * len(text) - 1)) // 2, y0 + mid - 1, text, BOLD + rgb(grad(stops, frac)))
    else:
        cv.put(x0 + (w - len(text)) // 2, y0 + mid, text, TEXT)
    row = y0 + min(h - 1, int((cy + 0.71 * R) / 4) + 1)      # just under the two ends of the arc
    cv.put(x0 + (w - len(title)) // 2, row, title, BOLD + rgb(color))
    for i, (text, style) in enumerate(notes):                 # notes may run a little wider than the dial
        text = cut(text, w + 10)
        cv.put(x0 + (w - len(text)) // 2, row + 1 + i, text, style)


def core_view(st, s, mem, pressure, temps, dsk, bat, info, W, H):
    """Screen 3: the chip alive. Each dot is one core; the busier it is, the faster it circles."""
    now = time.time()
    t = now if st.moving() else 0.0
    advance_orbits(st, s, now)
    cw = W - 4
    vis = max(8, H - 4)                                # rows for the picture; the rest is the strip below
    cv = Canvas(cw, vis)
    rh = max(6, min(vis - 1, 28, (cw - 2) // 2))       # the orbit picture is twice as wide as it is tall
    rw = rh * 2
    dh = min(vis - 1, 13, (cw - rw - 8) // 4)
    dials = dh >= 6
    xr, yr = (cw - rw) // 2, max(0, (vis - 1 - rh) // 2)
    draw_reactor(cv, xr, yr, rw, rh, st, s, mem, temps, t)
    cores = s["e_cores"] + s["p_cores"]
    cpu = sum(cores) / len(cores) if cores else 0.0
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
    if dials:
        dw, yd = dh * 2, max(0, (vis - 4 - dh) // 2 - 3)       # room under each dial for notes and meters
        notes = []
        if s["e_cores"] and s["p_cores"]:
            notes.append(("efficiency %d%%, performance %d%%" % (round(100 * avg(s["e_cores"])),
                                                                round(100 * avg(s["p_cores"]))), rgb(P_MUTED)))
        if s.get("has_power", True):
            notes.append(("%.1f W" % s["cpu_w"], rgb(P_MUTED)))
        draw_dial(cv, (xr - dw) // 2, yd, dw, dh, cpu, G_CPU, "CPU, %d cores" % len(cores), P_P, notes)
        notes = []
        if s.get("gpu_mem") is not None:
            notes.append(("%.1f GB of memory in use" % s["gpu_mem"], rgb(P_MUTED)))
        if s.get("has_power", True):
            notes.append(("%.1f W" % s["gpu_w"], rgb(P_MUTED)))
        draw_dial(cv, xr + rw + (cw - xr - rw - dw) // 2, yd, dw, dh, s["gpu"] or 0.0, G_GPU, "GPU", P_GPU, notes)
        # ---- under the dials: every core on its own meter, and the GPU's last minutes
        base, free = yd + dh + 4, vis - 2 - (yd + dh + 4)
        named = ([("E%d" % (i + 1), v, G_E) for i, v in enumerate(s["e_cores"])]
                 + [("P%d" % (i + 1), v, G_P) for i, v in enumerate(s["p_cores"])])
        side = xr - 4
        if free >= 2 and side >= 26 and named:
            ncol = min(3, -(-len(named) // free))
            each = side // ncol
            x_left = max(1, (xr - ncol * each) // 2)
            for i, (name, v, stops) in enumerate(named[:free * ncol]):
                px, py = x_left + (i // free) * each, base + i % free
                cv.put(px, py, name, rgb(P_MUTED))
                cv_bar(cv, px + 4, py, v, max(3, each - 11), stops)
                cv.put(px + 5 + max(3, each - 11), py, "%3d%%" % round(100 * v), TEXT)
        x_right, width = xr + rw + 4, cw - (xr + rw + 4) - 3
        if free >= 3 and width >= 20:
            cv.put(x_right, base, "GPU over the last minutes", rgb(P_MUTED))
            for r, line in enumerate(graph(st.h["gpu"], width, free - 1, 1.0, G_GPU)):
                cv_ansi(cv, x_right, base + 1 + r, line)
    key = [(P_E, "efficiency cores"), (P_P, "performance cores"), (P_RAM, "outer ring: memory")]
    if not s["e_cores"]:
        key = key[1:]
    x = max(0, (cw - sum(len(n) + 5 for _, n in key) - 44) // 2)
    for col, name in key:
        cv.put(x, vis - 1, "●", rgb(col))
        cv.put(x + 2, vis - 1, name, rgb(P_MUTED))
        x += len(name) + 5
    cv.put(x, vis - 1, cut("the busier a core, the faster it circles", max(0, cw - x)), rgb(P_DIM))

    # ---- the strip of four readouts under the picture
    colw = max(14, (cw - 6) // 4)
    ram = mem["used"] / mem["total"] if mem["total"] else 0.0
    pic, has_mac = st.pw, st.n_mac > 0
    wkey = "mac_w" if has_mac else "total_w"
    watts = st.h[wkey][-1] if st.h[wkey] else 0.0
    if pic["plugged"] and pic["in_w"] > 0.5:
        power_note = "charger gives %.1f W" % pic["in_w"]
    elif pic["bat_w"] is not None and pic["bat_w"] < -0.2:
        power_note = "battery gives %.1f W" % -pic["bat_w"]
    else:
        power_note = "peak %.1f W" % st.peak[wkey]
    tc = temps.get("cpu")
    fans = temps.get("fans") or []
    heat_note = ("fans %d rpm" % max(fans) if max(fans) > 100 else "fans off") if fans else "no fans"
    if temps.get("ssd") is not None:
        heat_note += ", SSD %d°C" % temps["ssd"]
    n_in = list(st.h["net_in"])[-colw * 2:]
    cells = (
        (BOLD + rgb(P_RAM) + "Memory" + RESET + "  " + TEXT + "%.1f of %.0f GB" % (mem["used"], mem["total"]) + RESET,
         bar(ram, colw - 5, G_RAM) + " " + pct(ram),
         muted("swap %s, pressure %s" % ("%.1f GB" % mem["swap"] if mem["swap"] > 0.05 else "off", pressure))),
        (BOLD + rgb(P_PWR) + "Power" + RESET + "  " + TEXT + "%.1f W" % watts + RESET
         + muted(" whole Mac" if has_mac else " chip"),
         graph(st.h[wkey], colw, 1, max(st.peak[wkey], 0.5), G_PWR)[0], muted(power_note)),
        (BOLD + rgb(P_TMP) + "Heat" + RESET + "  " + (BOLD + rgb(temp_rgb(tc)) + "%d°C" % tc + RESET if tc is not None
                                                     else dim("no sensor")),
         bar((tc or 0) / 105.0, colw, G_TEMP), muted(heat_note)),
        (BOLD + rgb(P_NET) + "Network" + RESET + "  " + rgb(P_NET) + "↓ " + TEXT + human_rate(s["net_in"]) + RESET + " "
         + rgb(P_UP) + "↑ " + TEXT + human_rate(s["net_out"]) + RESET,
         graph(n_in, colw, 1, max(n_in + [10e3]), G_NET)[0], muted("peak ↓ %s" % human_rate(max(n_in + [0])))),
    )
    lines = cv.lines() + [""]
    for r in range(3):
        lines.append("  ".join(fit(c[r], colw) for c in cells))
    return panel("Core", lines[:H], W, P_P, muted(info["chip"]))


# ============================================================ the frame around every screen
def header(st, s, temps, info, W):
    """Logo, the three screen names (clickable), then the vital signs."""
    logo = BOLD + rgb(G_LOGO[0]) + "◆ " + "".join(rgb(grad(G_LOGO, i / 3.0)) + ch for i, ch in enumerate("MTOP")) + RESET
    left, x = " " + logo, 8
    st.tabs = []
    for key, num, name in VIEWS:
        label = " %s %s " % (num, name) if W >= 100 or key == st.view else " %s " % num
        left += " "
        x += 1
        if key == st.view:
            left += rgb(P_ACCENT, bg=True) + "\x1b[97m" + BOLD + label + RESET
        else:
            left += rgb(P_MUTED) + label + RESET
        st.tabs.append((x, x + len(label) - 1, key))
        x += len(label)
    th = s["thermal"]
    bits = [(6, TEXT + info["chip"].upper() + RESET)]           # (how much it matters, text)
    if info.get("cores"):
        bits.append((1, muted(info["cores"])))
    if temps.get("cpu") is not None:
        bits.append((8, BOLD + rgb(temp_rgb(temps["cpu"])) + "%d°C" % temps["cpu"] + RESET))
    if st.pw["mac_w"] is not None:
        bits.append((7, BOLD + rgb(P_PWR) + "%.1f W" % st.pw["mac_w"] + RESET))
    elif s.get("has_power", True):
        bits.append((7, BOLD + rgb(P_PWR) + "%.1f W" % s["total_w"] + RESET))
    if th:
        bits.append((4, dot(GOOD if th == "Nominal" else BAD, muted("thermal " + th.lower()))))
    if info.get("uptime"):
        bits.append((2, muted("up " + info["uptime"])))
    bits.append((9, TEXT + time.strftime("%H:%M" if st.resting() else "%H:%M:%S") + RESET))
    while len(bits) > 1 and vlen(left) + 2 + sum(vlen(b) for _, b in bits) + 3 * (len(bits) - 1) > W:
        bits.remove(min(bits, key=lambda b: b[0]))
    right = (rgb(P_BORDER) + " ╱ " + RESET).join(b for _, b in bits) + " "
    return left + " " * max(1, W - vlen(left) - vlen(right)) + right


def footer(st, W):
    """Every button is a key and also a click target. The last action's answer follows them."""
    st.hot = []
    foot, x = " ", 2
    if st.confirm:
        pid, name = st.confirm
        q = " Quit %s (pid %d)? " % (cut(name, 30), pid)
        foot += rgb(BAD, bg=True) + "\x1b[97m" + BOLD + q + RESET + "  "
        x += len(q) + 2
        buttons = [("y", "y", "yes, quit it"), ("n", "n", "cancel")]
    else:
        buttons = [("q", "q", "quit")]
        if st.view == "main":
            buttons += [("ud", "↑↓", "select"), ("k", "k", "quit app"), ("s", "s", "sort CPU/RAM"),
                        ("c", "c", "clear RAM cache")]
        buttons += [("tab", "tab", "next screen"), ("m", "m", "motion " + st.motion)]
    for action, label, txt in buttons:
        w = len(label) + 3 + len(txt)
        st.hot.append((x, x + w - 1, action))
        foot += rgb(P_MUTED) + REV + BOLD + " " + label + " " + RESET + " " + muted(txt) + "  "
        x += w + 2
    if st.msg:
        return foot + BOLD + rgb(P_ACCENT) + st.msg + RESET
    if st.resting():
        return foot + dim("resting: one update every %d s" % REST_EVERY
                          + ("" if st.focus else " while this window is in the background"))
    return foot


_LOGO = {"m": ("#   #", "## ##", "# # #", "#   #", "#   #"), "t": ("#####", "  #  ", "  #  ", "  #  ", "  #  "),
         "o": (" ### ", "#   #", "#   #", "#   #", " ### "), "p": ("#### ", "#   #", "#### ", "#    ", "#    ")}


def intro_lines(t, cols, rows, status, progress):
    """The opening: the name lights up segment by segment, like a meter filling, while sensors load."""
    pix = ["  ".join(_LOGO[c][r] for c in "mtop") for r in range(5)]
    width = len(pix[0])
    lines = [""] * rows
    top = max(0, (rows - 9) // 2)
    if cols < width * 2 + 2 or rows < 9:
        if rows:
            lines[rows // 2] = " " * max(0, (cols - 4) // 2) + BOLD + rgb(G_LOGO[1]) + "mtop" + RESET
        return lines
    for r, row in enumerate(pix):
        out, last = [" " * ((cols - width * 2) // 2)], None
        for c, p in enumerate(row):
            if p != "#":
                out.append("  ")
                continue
            lit = min(1.0, max(0.0, (t - 0.05 - c * 0.022) / 0.18))        # the sweep of light reaches column c
            col = mix(P_SEG, mix(grad(G_LOGO, c / float(width - 1)), WHITE, 0.30 * wave(c, t, 13.0, 0.9, 2) * lit), lit)
            if rgb(col) != last:
                last = rgb(col)
                out.append(last)
            out.append("▊▊")
        lines[top + r] = "".join(out) + RESET
    lines[top + 6] = " " * max(0, (cols - len(status)) // 2) + muted(status)
    lines[top + 7] = " " * max(0, (cols - 26) // 2) + bar(progress, 26, G_LOGO)
    return lines


def render(st, s, mem, pressure, temps, dsk, bat, info, cols, rows):
    W = max(60, min(cols, 160))
    tips = health_check(s, mem, pressure, temps, dsk, bat, st.procs)[:3]
    fixed = 2 + max(1, len(tips))
    head = [header(st, s, temps, info, W)]
    st.rows_hit = (0, 0, 0, 0)
    if st.view == "mac":
        body = mac_view(st, s, temps, bat, dsk, info, W, max(13, min(36, rows - fixed - 2)))
    elif st.view == "core":
        body = core_view(st, s, mem, pressure, temps, dsk, bat, info, W, max(12, min(34, rows - fixed - 2)))
    else:
        body = overview(st, s, mem, pressure, temps, dsk, bat, info, W, rows, fixed)
    tail = []
    if tips:
        for col, tip in tips:
            tail.append(" " + rgb(col) + "● " + BOLD + cut(tip, W - 4) + RESET)
    else:
        tail.append(" " + rgb(GOOD) + "✓ " + BOLD + "All good" + RESET + muted("  your Mac is running normally"))
    tail.append(footer(st, W))
    shown = body[:max(0, rows - len(head) - len(tail))]
    if st.rows_hit[0] + st.rows_hit[1] - 1 > len(head) + len(shown):
        st.rows_hit = (0, 0, 0, 0)          # app list is cut off: not clickable
    out = head + shown + tail
    st.foot_y = len(out)
    return [clip(l, cols) if vlen(l) > cols else l for l in out]


# ============================================================ keys and clicks
MOUSE_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
FOCUS_RE = re.compile(r"\x1b\[([IO])")
VIEW_SAYS = {"main": "Overview", "mac": "Ports & power: what is plugged in, watts in and out, heat",
             "core": "Core: every core in orbit"}
MOTION_SAYS = {"smooth": "motion smooth: animated while you watch",
               "still": "motion still: one update a second, no animation",
               "eco": "motion eco: one update every %d seconds, easiest on your Mac" % REST_EVERY}


def act(action, st, demo):
    """Run one action. Every action answers at once with a message in the footer. False = quit."""
    if st.confirm:
        pid, name = st.confirm
        st.confirm = None
        if action == "y":
            if demo:
                st.msg = "demo mode: %s was not quit" % name
            else:
                try:
                    os.kill(pid, signal.SIGTERM)
                    st.msg = "asked %s to quit" % name
                except Exception as e:
                    st.msg = "could not quit %s: %s" % (name, e)
        else:
            st.msg = "cancelled: %s keeps running" % name
        return True
    if action == "q":
        return False
    order = [v[0] for v in VIEWS]
    if action in ("1", "2", "3", "tab", "back", "p"):
        i = order.index(st.view)
        if action == "tab":
            new = order[(i + 1) % len(order)]
        elif action == "back":
            new = order[(i - 1) % len(order)]
        elif action == "p":
            new = "main" if st.view == "mac" else "mac"
        else:
            new = order[int(action) - 1]
        if new != st.view and st.motion == "smooth" and st.focus:
            st.wipe = [time.time(), 1 if order.index(new) > i else -1, None]
        st.view, st.msg = new, VIEW_SAYS[new]
        return True
    if action == "m":
        st.motion = MOTIONS[(MOTIONS.index(st.motion) + 1) % len(MOTIONS)]
        st.msg = MOTION_SAYS[st.motion]
        return True
    if st.view != "main":                              # the rest belongs to the app list
        return True
    procs = st.sorted_procs()
    if action in ("up", "down"):
        st.sel = max(0, min(len(procs) - 1, st.sel + (1 if action == "down" else -1)))
        if procs:
            st.sel_pid = procs[st.sel][0]
            st.msg = "selected %s (pid %d)" % (procs[st.sel][4], procs[st.sel][0])
    elif action == "s":
        st.sort = "mem" if st.sort == "cpu" else "cpu"
        st.sel, st.sel_pid = 0, None
        st.msg = "sorted by %s" % ("CPU" if st.sort == "cpu" else "RAM")
    elif action == "k":
        if not procs:
            st.msg = "no app selected"
        else:
            pid, user, _, _, name = procs[min(st.sel, len(procs) - 1)]
            me = os.environ.get("SUDO_USER") or os.environ.get("USER")
            if user != me:
                st.msg = "%s is a system process (owner %s): not quitting it" % (name, user)
            else:
                st.confirm = (pid, name)
                st.msg = ""
    elif action == "c":
        if st.purge is not None:
            st.msg = "still clearing cache..."
        elif demo:
            st.msg = "demo mode: nothing cleared"
        else:
            st.purge_before = memory()["cached"]
            st.purge = subprocess.Popen(["purge"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            st.msg = "clearing cache..."
    return True


def click(x, y, st, demo):
    if y == 1 and not st.confirm:
        for i, (x0, x1, key) in enumerate(st.tabs):
            if x0 <= x <= x1:
                return act(str(i + 1), st, demo)
    if y == st.foot_y:
        for x0, x1, action in st.hot:
            if x0 <= x <= x1:
                if action == "ud":
                    action = "up" if x <= x0 + 1 else "down"
                return act(action, st, demo)
    first, count, x0, x1 = st.rows_hit
    if not st.confirm and count and first <= y < first + count and x0 <= x <= x1:
        procs = st.sorted_procs()
        if y - first < len(procs):
            st.sel = y - first
            st.sel_pid = procs[st.sel][0]
            st.msg = "selected %s (pid %d)" % (procs[st.sel][4], procs[st.sel][0])
    return True


KEYS = {"\x1b[A": "up", "\x1bOA": "up", "\x1b[B": "down", "\x1bOB": "down", "\t": "tab", "\x1b[Z": "back",
        "\x1b[C": "tab", "\x1bOC": "tab", "\x1b[D": "back", "\x1bOD": "back"}


def handle_input(data, st, demo):
    """Keys, mouse clicks, the scroll wheel, and the terminal saying its window gained or lost focus.
    Returns False to quit."""
    for m in FOCUS_RE.finditer(data):
        st.focus = m.group(1) == "I"
    data = FOCUS_RE.sub("", data)
    for m in MOUSE_RE.finditer(data):
        button, x, y, kind = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
        if button in (64, 65):
            ok = st.confirm or act("up" if button == 64 else "down", st, demo)
        else:
            ok = click(x, y, st, demo) if kind == "M" and button == 0 else True
        if not ok:
            return False
    data = MOUSE_RE.sub("", data)
    if not data:
        return True
    action = KEYS.get(data) or data[:1].lower()
    if st.confirm:
        return act("y" if action == "y" else "n", st, demo)
    if action not in ("q", "up", "down", "s", "k", "c", "p", "m", "tab", "back", "1", "2", "3"):
        return True
    return act(action, st, demo)


# ============================================================ collecting the numbers
class Sampler:
    """Collects every number mtop shows. Things that change slowly are read less often."""

    def __init__(self, opt):
        self.interval = opt["interval"]
        self.basic = opt["no_sudo"] and not opt["demo"]
        self.demo = Demo() if opt["demo"] else None
        self.local = self.temps_src = self.src = self.net = self.procs = self.smc = None
        self.info = {"chip": "Mac", "cores": "", "smart": "unknown", "uptime": "", "load": None, "ports": None}
        self.bat, self.dsk = None, (0.0, 0.0, 0.0)
        self.last = {"ps": 0.0, "bat": 0.0, "dsk": 0.0, "sample": 0.0, "gpu": 0.0}
        self.next_tick = 0.0
        self.booted = None
        self.note, self.step, self.steps = "Starting", 0, 6
        self.warn = ""
        self.error = None
        self.ready = threading.Event()

    def _say(self, text):
        self.note, self.step = text, self.step + 1

    def setup(self):
        """Everything slow, done once, in the background while the opening plays."""
        try:
            if self.demo:
                self._say("Loading made-up numbers")
                self.info.update(chip="Demo Mac", cores="made-up numbers, not this computer", smart="Verified",
                                 ports=DemoPorts())
                self.booted = time.time() - 273600
                self.step = self.steps - 1
            else:
                self._say("Reading the chip")
                self.local = LocalStats()
                self.info["chip"], self.info["cores"] = chip_info()
                self._say("Finding the sensors")
                self.temps_src = Temps()
                self.smc = self.temps_src.smc
                self._say("Checking the drive")
                self.info["smart"], self.booted = smart_status(), boot_time()
                self.net, self.procs = NetMeter(), Procs()
                self._say("Looking at the ports")
                self.info["ports"] = PortWatch()
                if not self.basic:
                    self._say("Starting the power meter")
                    try:
                        self.src = PowerMetrics(int(self.interval * 1000))
                    except OSError:
                        self.warn = "power readings unavailable on this Mac: basic mode"
                if not self.src:
                    self.next_tick = time.time() + 0.6        # a first look needs a moment to compare against
            self._say("Ready")
        except Exception as e:
            self.error = e
        self.ready.set()

    def refresh_power(self, st):
        """A quick extra look at the power sensors, for the animated screens."""
        if time.time() - st.live_t < 0.25:
            return
        if self.demo:
            sample_power(st, None, self.demo.power(st.h["total_w"][-1] if st.h["total_w"] else 20.0),
                         {1: (5.12, 0.55 + 0.25 * (st.h["gpu"][-1] if st.h["gpu"] else 0.3))})
        else:
            sample_power(st, self.smc)
        st.pw = power_picture(st, self.bat)

    def poll(self, readable, st):
        """Take a new sample if one is due. True when the numbers changed."""
        now = time.time()
        s = None
        if self.src:
            if self.src.fd in readable:
                try:
                    s = self.src.read()
                except RuntimeError:                  # powermetrics missing or refused: carry on without it
                    self.src.close()
                    self.src = None
                    st.msg = "power readings unavailable on this Mac: basic mode"
                    self.next_tick = now
            if s:
                if not s["e_cores"] and not s["p_cores"]:      # Intel: no per-core load in powermetrics
                    s["e_cores"], s["p_cores"] = self.local.split(self.local.cpu())
                if s["gpu"] is None or now - self.last["gpu"] > 3:
                    load, self.last["gpu"] = self.local.gpu(), now    # also measures GPU memory
                    if s["gpu"] is None:
                        s["gpu"] = load
                s["gpu_mem"] = self.local.gpu_mem
        elif now >= self.next_tick:
            self.next_tick = now + self.interval
            s = self.demo.sample() if self.demo else self.local.sample()
        if not s:
            return False
        if self.demo:
            mem, pressure, temps = self.demo.memory(), "normal", self.demo.temps()
            st.procs = self.demo.procs()
            self.bat, self.dsk = self.demo.battery(), (310.0, 494.0, 184.0)
            self.info["load"] = 1.2 + 4 * st.h["p"][-1] if st.h["p"] else 2.0
            sample_power(st, None, self.demo.power(s["total_w"]), {1: (5.12, 0.55 + 0.25 * s["gpu"])})
        else:
            s["net_in"], s["net_out"] = self.net.rates()
            mem, pressure, temps = memory(), memory_pressure(), self.temps_src.read()
            if now - self.last["ps"] > 2:
                st.procs, self.last["ps"] = self.procs.read(), now
            if now - self.last["bat"] > 3:
                self.bat, self.last["bat"] = battery(), now
            if now - self.last["dsk"] > 10:
                self.dsk, self.last["dsk"] = disk_usage(), now
            try:
                self.info["load"] = os.getloadavg()[0]
            except OSError:
                pass
            sample_power(st, self.smc)
        ports = self.info["ports"]
        if self.bat:
            if ports.health is not None:              # macOS's own figure, the one System Settings shows
                self.bat["health"], self.bat["condition"] = ports.health, ports.condition
            if self.bat.get("temp") is None and st.live.get("bat_t"):
                self.bat["temp"] = st.live["bat_t"]
        st.pw = power_picture(st, self.bat)
        log_power(st, ports, now)
        if self.booted:
            self.info["uptime"] = uptime_text(now - self.booted)
        dt = min(now - self.last["sample"], 5 * self.interval) if self.last["sample"] else self.interval
        self.last["sample"] = now
        st.push(s, mem, temps, self.interval if self.demo else dt)
        st.last = (s, mem, pressure, temps)
        return True

    def close(self):
        if self.src:
            self.src.close()


# ============================================================ starting up
USAGE = """mtop - a live picture of what your Mac is doing

  python3 mtop.py              full view (asks for your Mac password once)
  python3 mtop.py --no-sudo    no password: everything except the CPU / GPU watt split and MHz
  python3 mtop.py --demo       made-up numbers, to see what it looks like

  --interval SECONDS           how often to measure (default 1)
  --motion smooth|still|eco    start animated, still, or with one update every 5 seconds
  --view 1|2|3                 start on the Overview, Ports & power, or Core screen
  --color auto|truecolor|256   colour depth (default: detect)
"""


def parse_args(argv):
    opt = {"interval": 1.0, "demo": False, "no_sudo": False, "color": "auto", "frames": 0, "view": "main",
           "motion": "smooth", "from": "", "no_cache": False, "no_intro": False}
    flags = {"--demo": "demo", "--no-sudo": "no_sudo", "--no-cache": "no_cache", "--no-intro": "no_intro"}
    views = {"1": "main", "2": "mac", "3": "core", "main": "main", "overview": "main", "mac": "mac",
             "ports": "mac", "core": "core"}
    i = 0
    while i < len(argv):
        a, value = argv[i], None
        if "=" in a and a.startswith("--"):
            a, value = a.split("=", 1)
        if a in ("-h", "--help"):
            print(USAGE)
            sys.exit(0)
        if a in flags:
            opt[flags[a]] = True
        elif a in ("--interval", "--color", "--frames", "--view", "--motion", "--from"):
            if value is None:
                i += 1
                value = argv[i] if i < len(argv) else ""
            try:
                if a == "--interval":
                    opt["interval"] = min(60.0, max(0.2, float(value)))
                elif a == "--frames":
                    opt["frames"] = int(value)
                elif a == "--view":
                    opt["view"] = views[value.lower()]
                elif a == "--motion" and value in MOTIONS or a == "--color" and value in ("auto", "truecolor", "256"):
                    opt[a[2:]] = value
                elif a == "--from":
                    opt["from"] = value
                else:
                    raise ValueError
            except (ValueError, KeyError):
                sys.exit("mtop: %s does not understand '%s'\n\n%s" % (a, value, USAGE))
        else:
            sys.exit("mtop: unknown option '%s'\n\n%s" % (a, USAGE))
        i += 1
    return opt


def wants_truecolor(mode):
    if mode != "auto":
        return mode == "truecolor"
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return True
    if os.environ.get("TERM_PROGRAM") in ("iTerm.app", "ghostty", "WezTerm", "vscode", "WarpTerminal"):
        return True
    if sys.platform == "darwin":          # Apple's Terminal has 24-bit colour from macOS 26 (Darwin 25)
        try:
            return int(os.uname().release.split(".")[0]) >= 25
        except ValueError:
            pass
    return False


def lean_copy():
    """Path of a ready-compiled copy of this file, made on first run. None if that cannot be done.

    Python turns this file into its own compact form every time it starts, and the leftovers of
    that work stay in memory for as long as mtop runs: about 10 MB. Starting from a saved
    compiled copy skips the work, so mtop needs roughly half the memory. The copy lives in
    ~/Library/Caches/mtop and is rebuilt whenever this file changes.
    """
    try:
        src = os.path.abspath(__file__)
        if not src.endswith(".py") or not sys.executable:
            return None
        info = os.stat(src)
        base = os.path.expanduser("~/Library/Caches/mtop" if sys.platform == "darwin" else "~/.cache/mtop")
        tag = getattr(sys.implementation, "cache_tag", None) or "py%d%d" % sys.version_info[:2]
        path = os.path.join(base, "mtop-%s-%d-%d.pyc" % (tag, info.st_mtime_ns, info.st_size))
        import importlib.util
        if os.path.exists(path):
            with open(path, "rb") as f:
                if f.read(4) == importlib.util.MAGIC_NUMBER:
                    return path
        os.makedirs(base, mode=0o700, exist_ok=True)
        for old in os.listdir(base):
            if old.startswith("mtop-") and old.endswith(".pyc"):
                os.unlink(os.path.join(base, old))
        import py_compile
        tmp = "%s.%d.tmp" % (path, os.getpid())
        py_compile.compile(src, cfile=tmp, doraise=True)
        os.replace(tmp, path)
        if os.geteuid() == 0 and os.environ.get("SUDO_UID"):       # made under sudo: hand it back to you
            for item in (base, path):
                os.chown(item, int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", -1)))
        return path
    except Exception:
        return None


def main():
    global TRUECOLOR
    opt = parse_args(sys.argv[1:])
    TRUECOLOR = wants_truecolor(opt["color"])
    demo = opt["demo"]
    here = opt["from"] or sys.argv[0]
    if not demo and sys.platform != "darwin":
        sys.exit("mtop needs a Mac. Try: python3 %s --demo" % here)
    need_sudo = not demo and not opt["no_sudo"] and os.geteuid() != 0
    lean = None if opt["no_cache"] or opt["from"] else lean_copy()
    if need_sudo or lean:
        cmd = [sys.executable, lean or os.path.abspath(sys.argv[0])] + sys.argv[1:]
        cmd += ["--from", here, "--color", "truecolor" if TRUECOLOR else "256"]    # sudo drops COLORTERM
        if need_sudo:
            print("mtop reads power data, which macOS only gives to an administrator.")
            print("Type your Mac password below (nothing shows while you type), then press Return.")
            print("No password? Press Ctrl-C and run:  python3 %s --no-sudo\n" % here)
            os.execvp("sudo", ["sudo"] + cmd)
        os.execv(sys.executable, cmd)

    st = State()
    st.view, st.motion = opt["view"], opt["motion"]
    sampler = Sampler(opt)
    screen = Screen()
    out = sys.stdout

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    wake_r, wake_w = os.pipe()                         # a window resize wakes the loop through this pipe
    os.set_blocking(wake_w, False)
    os.set_blocking(wake_r, False)
    try:
        signal.set_wakeup_fd(wake_w)
        signal.signal(signal.SIGWINCH, lambda *_: None)
    except (ValueError, AttributeError):
        pass

    tty = sys.stdin.isatty()
    old = None
    if tty:
        import termios
        import tty as ttymod
        old = termios.tcgetattr(sys.stdin)
        ttymod.setcbreak(sys.stdin.fileno())
    out.write("\x1b[?1049h\x1b[?25l\x1b[?7l")              # alt screen, hide cursor, no line wrap
    if tty:
        out.write("\x1b[?1000h\x1b[?1006h\x1b[?1004h")     # report mouse clicks, and window focus
    out.flush()

    def size():
        try:
            ts = os.get_terminal_size(out.fileno())
            return ts.columns, ts.lines
        except (OSError, ValueError):
            return int(os.environ.get("COLUMNS", 120)), int(os.environ.get("LINES", 40))

    def show(lines, **how):
        cols, rows = size()
        data = screen.draw(lines, cols, rows, **how)
        if data:                                       # one burst, so the terminal paints the frame whole
            out.write("\x1b[?2026h" + data + "\x1b[?2026l")
            out.flush()

    def keys(readable):
        """False when the user asked to quit."""
        if wake_r in readable:
            try:
                os.read(wake_r, 64)
            except OSError:
                pass
        if tty and sys.stdin in readable:
            data = os.read(sys.stdin.fileno(), 1024)
            if not data:                               # the keyboard went away: keep showing, stop listening
                watch[:] = [wake_r]
                return True
            return handle_input(data.decode(errors="ignore"), st, demo)
        return True

    def picture():
        s, mem, pressure, temps = st.last
        cols, rows = size()
        return render(st, s, mem, pressure, temps, sampler.dsk, sampler.bat, sampler.info, cols, rows)

    frames = 0
    try:
        # ---- opening: the name lights up while the sensors are found
        intro = tty and not opt["frames"] and not opt["no_intro"]
        threading.Thread(target=sampler.setup, daemon=True).start()
        began = time.time()
        watch = [wake_r] + ([sys.stdin] if tty else [])
        while True:
            fds = watch + ([sampler.src.fd] if sampler.ready.is_set() and sampler.src else [])
            r, _, _ = select.select(fds, [], [], 0.05)
            if not keys(r):
                return
            if sampler.ready.is_set():
                if sampler.error:
                    raise sampler.error
                sampler.poll(r, st)
            t = time.time() - began
            if st.last and (not intro or t >= 0.9):
                break
            if intro:
                cols, rows = size()
                show(intro_lines(t, cols, rows, sampler.note, sampler.step / float(sampler.steps)))
        if sampler.warn and not st.msg:
            st.msg = sampler.warn
        st.msg = st.msg or ""
        if intro and st.motion == "smooth":                # the dashboard opens from the middle, like a lid
            began = time.time()
            while True:
                f = min(1.0, (time.time() - began) / 0.35)
                cols, rows = size()
                half = int((1 - (1 - f) ** 3) * (rows / 2.0 + 1))
                new, was = picture(), intro_lines(9.0, cols, rows, sampler.note, 1.0)
                show([new[i] if abs(i - rows // 2) < half and i < len(new) else "" if abs(i - rows // 2) < half
                      else was[i] for i in range(rows)])
                if f >= 1.0:
                    break
                time.sleep(0.03)
        st.wipe = None

        # ---- the main loop
        ports = sampler.info["ports"]
        dirty, urgent, last_draw, next_frame = True, True, 0.0, 0.0
        was_size = size()
        while True:
            now = time.time()
            waits = []
            if st.moving():
                waits.append(max(0.0, next_frame - now))
            if not sampler.src:
                waits.append(max(0.0, sampler.next_tick - now))
            if urgent:
                waits.append(0.0)
            fds = watch + ([sampler.src.fd] if sampler.src else [])
            r, _, _ = select.select(fds, [], [], min(waits) if waits else None)
            if r and (wake_r in r or (tty and sys.stdin in r)):
                if not keys(r):
                    break
                urgent = True
            if st.purge is not None and st.purge.poll() is not None:
                st.purge = None
                st.msg = "cache cleared: freed %.1f GB" % max(0.0, st.purge_before - memory()["cached"])
                urgent = True
            if sampler.poll(r, st):
                dirty = True
            if ports.watching != (st.view == "mac"):       # look at the ports more often while they are on screen
                ports.watching = st.view == "mac"
                ports.wake.set()
            now = time.time()
            if size() != was_size:
                was_size, urgent = size(), True
            moving = st.moving()
            if moving and now >= next_frame - 0.002:
                if st.view != "main":
                    sampler.refresh_power(st)
            elif not urgent and not (dirty and now - last_draw >= (REST_EVERY if st.resting() else 0.0)):
                continue
            lines = picture()
            if st.wipe:                                    # a new screen sweeps in over the old one
                cols = size()[0]
                if st.wipe[2] is None:
                    st.wipe[2] = screen.snapshot()
                f = min(1.0, (now - st.wipe[0]) / 0.24)
                edge = int((1 - (1 - f) ** 2) * cols)
                show(lines, window=(0, edge) if st.wipe[1] > 0 else (cols - edge, cols), behind=st.wipe[2],
                     edge=BOLD + rgb(P_ACCENT))
                if f >= 1.0:
                    st.wipe = None
                    show(lines)
                next_frame = now + 1 / 30.0
            else:
                show(lines)
                next_frame = now + 1 / 15.0
            dirty, urgent, last_draw = False, False, now
            frames += 1
            if opt["frames"] and frames >= opt["frames"]:
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            out.write("\x1b[0m\x1b[?1004l\x1b[?1006l\x1b[?1000l\x1b[?7h\x1b[?25h\x1b[?1049l")
            out.flush()
        except Exception:
            pass
        if old:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        sampler.close()


if __name__ == "__main__":
    main()
