#!/usr/bin/env python3
"""mtop - system monitor for every Mac (Apple Silicon and Intel).

Shows CPU / GPU / RAM / temperatures / SSD / battery / network / power,
the apps using the most CPU or RAM, and a health check with advice.

Run:   python3 mtop.py             (asks for your password: power readings need sudo)
Basic: python3 mtop.py --no-sudo   (no password; everything except watts and MHz)
Demo:  python3 mtop.py --demo      (fake data, no sudo, any OS)

Keys:  q quit   up/down select app   k quit selected app   s sort CPU/RAM
       c clear RAM cache
Mouse: click any footer button or an app row; the scroll wheel moves the selection.
       Every key and click answers at once with a message in the footer.

Standard library only. Reads macOS `powermetrics`, the SMC and HID
temperature sensors (read-only), `ps`, `vm_stat`, `ioreg` and `diskutil`.
It never writes to hardware.
"""
import argparse
import ctypes
import ctypes.util
import json
import math
import os
import plistlib
import random
import re
import select
import shutil
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
    key = (c, bg, TRUECOLOR)
    if key not in _esc:
        lead = 48 if bg else 38
        if TRUECOLOR:
            _esc[key] = "\x1b[%d;2;%d;%d;%dm" % ((lead,) + tuple(c))
        else:
            _esc[key] = "\x1b[%d;5;%dm" % (lead, _to256(*c))
    return _esc[key]


def grad(stops, t):
    """Colour at position t (0..1) along a list of colour stops."""
    t = min(1.0, max(0.0, t))
    if len(stops) == 1:
        return stops[0]
    x = t * (len(stops) - 1)
    i = min(int(x), len(stops) - 2)
    a, b, f = stops[i], stops[i + 1], x - i
    return tuple(int(round(a[k] + (b[k] - a[k]) * f)) for k in range(3))


# palette: mid-tone, saturated colours that hold up on dark and light backgrounds
P_MUTED = (140, 140, 150)
P_DIM = (108, 108, 120)
P_BORDER = (92, 95, 115)
P_TRACK = (112, 112, 124)
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
    """Smooth horizontal bar: gradient along its length, eighth-block precision."""
    frac = min(1.0, max(0.0, frac))
    cells = frac * width
    full = int(cells)
    part = int((cells - full) * 8)
    if frac > 0.004 and full == 0 and part == 0:
        part = 1
    out, last = [], None
    for i in range(full + (1 if part and full < width else 0)):
        col = rgb(grad(stops, i / float(max(1, width - 1))))
        if col != last:
            out.append(col)
            last = col
        out.append("█" if i < full else HBAR[part])
    used = full + (1 if part and full < width else 0)
    return "".join(out) + rgb(P_TRACK) + track * (width - used) + RESET


def stacked(segments, total, width):
    """One bar split into coloured segments: [(value, colour), ...] out of `total`."""
    out, last = [], None
    for i in range(width):
        x, acc, col = (i + 0.5) / width * total, 0.0, None
        for value, c in segments:
            acc += value
            if x < acc:
                col = c
                break
        code = rgb(col or P_TRACK)
        if code != last:
            out.append(code)
            last = code
        out.append("█" if col else "░")
    return "".join(out) + RESET


def graph(values, width, height, top, stops, bottom=0.0, invert=False):
    """Filled braille area graph, `height` rows tall, two samples per character.

    Returns a list of `height` strings. With invert=True the area hangs from the top.
    """
    n = width * 2
    vals = list(values)[-n:]
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


def panel(title, lines, width, color=P_ACCENT, right=""):
    inner = width - 4
    extra = vlen(right) + 2 if right else 0
    fill = width - 6 - len(title) - extra
    if fill < 1:
        right, fill = "", max(0, width - 6 - len(title))
    b = rgb(P_BORDER)
    top = (b + "╭─ " + RESET + BOLD + rgb(color) + title + RESET + b + " " + "─" * fill + RESET
           + (" " + right + RESET + " " if right else "") + b + "─╮" + RESET)
    body = [b + "│ " + RESET + fit(l, inner) + b + " │" + RESET for l in lines]
    return [top] + body + [b + "╰" + "─" * (width - 2) + "╯" + RESET]


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


# ============================================================ small helpers
def sh(cmd, timeout=5):
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=timeout).decode(errors="ignore").strip()
    except Exception:
        return ""


def chip_info():
    """(chip name, core summary), both read from this Mac. Nothing is assumed."""
    name = sh(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Mac"
    p = sh(["sysctl", "-n", "hw.perflevel0.logicalcpu"])
    e = sh(["sysctl", "-n", "hw.perflevel1.logicalcpu"])
    displays = sh(["system_profiler", "SPDisplaysDataType"], 15)
    m = re.search(r"Total Number of Cores:\s*(\d+)", displays)
    if e and p:                                   # Apple Silicon: efficiency + performance cores
        parts = [e + "E", p + "P"]
    else:                                         # Intel, or a chip with one kind of core
        n = p or sh(["sysctl", "-n", "hw.physicalcpu"]) or sh(["sysctl", "-n", "hw.logicalcpu"])
        parts = [n + " cores"] if n else []
    if m:
        parts.append(m.group(1) + " GPU")
    else:                                         # Intel: no GPU core count, show the graphics chip's name
        models = [g.strip() for g in re.findall(r"Chipset Model:\s*(.+)", displays)]
        if models:
            parts.append(models[-1])
    return name, " + ".join(parts)


def boot_time():
    m = re.search(r"sec = (\d+)", sh(["sysctl", "-n", "kern.boottime"]))
    return float(m.group(1)) if m else None


def uptime_text(seconds):
    m = int(seconds // 60)
    d, h, m = m // 1440, (m % 1440) // 60, m % 60
    return "%dd %dh" % (d, h) if d else "%dh %02dm" % (h, m) if h else "%dm" % m


def memory():
    """(used, total, swap_used, swap_total, cache, wired, compressed) in GB."""
    total = float(sh(["sysctl", "-n", "hw.memsize"]) or 0)
    vm = sh(["vm_stat"])
    m = re.search(r"page size of (\d+)", vm)
    page = int(m.group(1)) if m else 16384

    def pages(key):
        mm = re.search(re.escape(key) + r":\s+(\d+)", vm)
        return int(mm.group(1)) if mm else 0

    used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * page
    sw = sh(["sysctl", "-n", "vm.swapusage"])

    def mb(key):
        mm = re.search(key + r" = ([\d.]+)M", sw)
        return float(mm.group(1)) if mm else 0.0

    g = 1024.0 ** 3
    return (used / g, total / g, mb("used") / 1024, mb("total") / 1024, pages("File-backed pages") * page / g,
            pages("Pages wired down") * page / g, pages("Pages occupied by compressor") * page / g)


def memory_pressure():
    v = sh(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
    return {"1": "normal", "2": "warning", "4": "critical"}.get(v, "normal")


def disk_usage():
    for path in ("/System/Volumes/Data", "/"):
        try:
            u = shutil.disk_usage(path)
            return u.used / 1e9, u.total / 1e9, u.free / 1e9
        except Exception:
            pass
    return 0.0, 0.0, 0.0


def smart_status():
    m = re.search(r"SMART Status:\s*(.+)", sh(["diskutil", "info", "disk0"]))
    return m.group(1).strip() if m else "unknown"


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
    # macOS Settings shows nominal / design capacity; the raw value reads a little lower
    raw, design = deep("NominalChargeCapacity") or deep("AppleRawMaxCapacity"), deep("DesignCapacity")
    t = deep("Temperature") or deep("VirtualTemperature")
    plugged = val("ExternalConnected") == "Yes"
    charging = val("IsCharging") == "Yes"
    full = val("FullyCharged") == "Yes"
    amps, volts = num("Amperage"), num("Voltage")
    tele, adapter = val("PowerTelemetryData"), val("AdapterDetails") or ""
    kilo = lambda v: v / 1000.0 if v is not None else None
    name = re.search(r'"(?:Name|Description)"="([^"]+)"', adapter)
    return {
        "pct": pct_,
        "health": min(100, math.floor(100 * raw / design)) if raw and design else None,   # rounded down, like macOS
        "cycles": int(num("CycleCount") or 0),
        "temp": t / 100 if t else None,
        "state": "charging" if charging else ("full, plugged in" if full else "plugged in") if plugged else "on battery",
        "plugged": plugged,
        # measured by the Mac's own power chip, in watts / volts / amps
        "in_w": kilo(inner("SystemPowerIn", tele)) if plugged else 0.0,
        "in_v": kilo(inner("SystemVoltageIn", tele)) if plugged else 0.0,
        "in_a": kilo(inner("SystemCurrentIn", tele)) if plugged else 0.0,
        "load_w": kilo(inner("SystemLoad", tele)),
        "bat_w": amps * volts / 1e6 if amps is not None and volts else None,      # + charging, - draining
        "adapter_w": inner("Watts", adapter) if plugged else None,
        "adapter_name": name.group(1) if name and plugged else "",
    }


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


def human_size(b):
    """Drive sizes the way they are printed on the box: 256 GB, 1 TB."""
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if b >= div * 0.95 or unit == "MB":
            v = b / div
            return ("%.0f %s" if v >= 9.95 or abs(v - round(v)) < 0.06 else "%.1f %s") % (v, unit)


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
        speed = "USB 3 · %d Gb/s" % (bps // 10 ** 9) if bps >= 10 ** 9 else "USB 2" if bps >= 4 * 10 ** 8 else \
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
        buses = json.loads(sh(["system_profiler", "-json", "SPThunderboltDataType"], 20) or "{}")
    except ValueError:
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
    """Keeps the list of ports and plugged-in devices fresh without slowing the screen down."""

    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self.model, self.ports, self.usb, self.tb = "", [], [], {}
        self.start()

    def run(self):
        m = re.search(r"Model Name:\s*(.+)", sh(["system_profiler", "SPHardwareDataType"], 15))
        self.model = m.group(1).strip() if m else "Mac"
        seen, tb_at, prev = None, 0.0, {}
        while True:
            try:
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
            time.sleep(1.5)


class DemoPorts:
    model = "MacBook Pro"
    out = {1: (5.12, 0.62)}
    tb = {1: {"speed": "Up to 120 Gb/s", "devices": []}, 2: {"speed": "Up to 120 Gb/s", "devices": []},
          3: {"speed": "Up to 120 Gb/s", "devices": []}}
    usb = [{"name": "Portable SSD", "names": ["Portable SSD"], "ma": 896, "speed": "USB 3 · 10 Gb/s", "bus": 0,
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


def processes():
    """[(pid, user, cpu%, rss_mb, name)]"""
    out = sh(["ps", "-Aceo", "pid=,user=,pcpu=,rss=,comm="])
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5:
            try:
                rows.append((int(parts[0]), parts[1], float(parts[2]), int(parts[3]) / 1024.0, parts[4]))
            except ValueError:
                pass
    return rows


# ============================================================ temperature sensors (read-only)
class SMC:
    """Reads Apple SMC temperature keys through IOKit. Read-only."""

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
    FLT = int.from_bytes(b"flt ", "big")          # Apple Silicon sensors
    SP78 = int.from_bytes(b"sp78", "big")         # Intel sensors
    PREFIXES = ("Tp", "Te", "Tf", "Tg", "TC", "TG")

    def __init__(self):
        io = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
        libc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("c"))
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
        task = ctypes.c_uint32.in_dll(libc, "mach_task_self_").value
        if io.IOServiceOpen(svc, task, 0, ctypes.byref(self.conn)) != 0:
            raise OSError("cannot open AppleSMC")
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

    def port_out(self):
        """{port number: (volts, amps)} measured on each USB-C port by the Mac's own power sensors."""
        out = {}
        for n in range(1, 7):
            a = self._read(int.from_bytes(("D%dJI" % n).encode(), "big"), 4)
            v = self._read(int.from_bytes(("D%dJV" % n).encode(), "big"), 4)
            if a and v:
                out[n] = (struct.unpack("<f", v)[0], struct.unpack("<f", a)[0])
        return out

    def fans(self):
        """Fan speeds in rpm. Empty on a Mac without fans."""
        n = self._read(int.from_bytes(b"FNum", "big"), 1)
        out = []
        for i in range(min(n[0], 8) if n else 0):
            key = int.from_bytes(("F%dAc" % i).encode(), "big")
            size, typ = self._info(key)
            b = self._read(key, size) if size else None
            if not b:
                continue
            if typ == self.FLT and size == 4:
                out.append(struct.unpack("<f", b)[0])
            elif size == 2:
                out.append(int.from_bytes(b, "big") / 4.0)
        return out

    def read(self):
        def vals(keys):
            return [v for v in (self._temp(k, t) for k, t in keys) if v is not None and 5 < v < 130]
        return vals(self.cpu), vals(self.gpu)


class HIDTemps:
    """Reads the HID temperature sensors (SoC die, SSD/NAND, battery). Read-only."""

    def __init__(self):
        io = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
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
                "gpu_max": max(gpu) if gpu else None, "ssd": max(ssd) if ssd else None, "fans": fans}


# ============================================================ usage without sudo
class LocalStats:
    """CPU, GPU, disk and network activity read without administrator rights.

    Works on every Mac. Used for basic mode (--no-sudo), and to fill in whatever
    `powermetrics` does not report on a given model (per-core load on Intel).
    """

    def __init__(self):
        self.n_e = 0                              # efficiency cores come first on Apple Silicon
        if sh(["sysctl", "-n", "hw.nperflevels"]) == "2":
            try:
                self.n_e = int(sh(["sysctl", "-n", "hw.perflevel1.logicalcpu"]))
            except ValueError:
                pass
        self.libc = None
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"))
            libc.mach_host_self.restype = ctypes.c_uint32
            libc.host_processor_info.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_uint32),
                                                 ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32)),
                                                 ctypes.POINTER(ctypes.c_uint32)]
            libc.vm_deallocate.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_size_t]
            self.host = libc.mach_host_self()
            self.task = ctypes.c_uint32.in_dll(libc, "mach_task_self_").value
            self.libc = libc
        except Exception:
            pass
        self.ticks = None
        self.counters = None
        self.gpu_mem = None
        self.cpu()

    def cpu(self):
        """Load of every core (0..1) since the previous call."""
        if not self.libc:
            return []
        n, info, count = ctypes.c_uint32(), ctypes.POINTER(ctypes.c_uint32)(), ctypes.c_uint32()
        if self.libc.host_processor_info(self.host, 2, ctypes.byref(n), ctypes.byref(info), ctypes.byref(count)) != 0:
            return []
        now = [tuple(info[i * 4 + j] for j in range(4)) for i in range(n.value)]    # user, system, idle, nice
        self.libc.vm_deallocate(self.task, ctypes.cast(info, ctypes.c_void_p), count.value * 4)
        prev, self.ticks = self.ticks, now
        if not prev or len(prev) != len(now):
            return [0.0] * len(now)
        out = []
        for a, b in zip(prev, now):
            d = [(y - x) & 0xFFFFFFFF for x, y in zip(a, b)]
            total = sum(d)
            out.append((total - d[2]) / float(total) if total else 0.0)
        return out

    def gpu(self):
        """GPU load (0..1). Also records the memory the GPU is using, in GB, as self.gpu_mem."""
        out = sh(["ioreg", "-r", "-d", "1", "-w0", "-c", "IOAccelerator"])
        mem = [int(v) for v in re.findall(r'"In use system memory"=(\d+)', out)]
        self.gpu_mem = sum(mem) / 1024.0 ** 3 if mem else None
        vals = re.findall(r'"Device Utilization %"=(\d+)', out)
        return min(1.0, max(int(v) for v in vals) / 100.0) if vals else 0.0

    def io(self):
        """(disk read, disk write, net in, net out) in bytes per second since the previous call."""
        disk = sh(["ioreg", "-r", "-d", "1", "-w0", "-c", "IOBlockStorageDriver"])
        rd = sum(int(v) for v in re.findall(r'"Bytes \(Read\)"=(\d+)', disk))
        wr = sum(int(v) for v in re.findall(r'"Bytes \(Write\)"=(\d+)', disk))
        n_in = n_out = 0
        for line in sh(["netstat", "-ibn"]).splitlines():
            parts = line.split()
            if len(parts) >= 10 and "<Link#" in line and not parts[0].startswith("lo"):
                try:
                    n_in, n_out = n_in + int(parts[-5]), n_out + int(parts[-2])
                except ValueError:
                    pass
        now, cur = time.time(), (rd, wr, n_in, n_out)
        prev, self.counters = self.counters, (now, cur)
        if not prev or now <= prev[0]:
            return 0.0, 0.0, 0.0, 0.0
        return tuple(max(0.0, (c - p) / (now - prev[0])) for c, p in zip(cur, prev[1]))

    def sample(self):
        cores = self.cpu()
        rd, wr, n_in, n_out = self.io()
        return {"e_cores": cores[:self.n_e], "p_cores": cores[self.n_e:], "e_mhz": 0, "p_mhz": 0,
                "gpu": self.gpu(), "gpu_mem": self.gpu_mem, "gpu_mhz": 0, "cpu_w": 0.0, "gpu_w": 0.0, "ane_w": 0.0, "total_w": 0.0,
                "thermal": None, "has_power": False,
                "disk_r": rd, "disk_w": wr, "net_in": n_in, "net_out": n_out}


# ============================================================ power / usage source
class PowerMetrics:
    def __init__(self, interval_ms):
        self.interval = interval_ms / 1000.0
        self.proc = subprocess.Popen(
            ["powermetrics", "--samplers", "cpu_power,gpu_power,thermal,disk,network", "-f", "plist",
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
                sample = self.parse(plistlib.loads(raw))
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

        gpu, disk, net = d.get("gpu", {}), d.get("disk", {}), d.get("network", {})
        cpu_w, gpu_w, ane_w = watts("cpu"), watts("gpu"), watts("ane")
        total = float(proc["combined_power"]) / 1000.0 if "combined_power" in proc else cpu_w + gpu_w + ane_w
        return {
            "e_cores": e_cores, "p_cores": p_cores,
            "e_mhz": sum(e_f) / len(e_f) if e_f else 0, "p_mhz": sum(p_f) / len(p_f) if p_f else 0,
            "gpu": 1.0 - float(gpu["idle_ratio"]) if "idle_ratio" in gpu else None,
            "gpu_mhz": gpu.get("freq_hz", 0) if "idle_ratio" in gpu else 0,
            "cpu_w": cpu_w, "gpu_w": gpu_w, "ane_w": ane_w, "total_w": total,
            "thermal": d.get("thermal_pressure", "Nominal"),
            "disk_r": float(disk.get("rbytes_per_s", 0)), "disk_w": float(disk.get("wbytes_per_s", 0)),
            "net_in": float(net.get("ibyte_rate", 0)), "net_out": float(net.get("obyte_rate", 0)),
        }

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


class Demo:
    def __init__(self):
        self.t = 0.0
        self.names = ["Google Chrome Helper", "Xcode", "Python", "Discord", "WindowServer", "Spotify",
                      "Finder", "Code Helper", "kernel_task", "mds_stores", "Safari", "Slack", "Terminal",
                      "Music"]

    def wave(self, k, base, amp):
        return min(1, max(0, base + amp * math.sin(self.t * k) + random.uniform(-0.08, 0.08)))

    def sample(self):
        self.t += 0.35
        e = [self.wave(0.5 + i * 0.1, 0.25, 0.15) for i in range(4)]
        p = [self.wave(0.3 + i * 0.07, 0.5, 0.35) for i in range(8)]
        g = self.wave(0.2, 0.4, 0.4)
        cpu_w, gpu_w = 2 + 28 * sum(p) / 8, 25 * g
        ane_w = 1.5 * self.wave(0.15, 0.2, 0.3)
        return {"e_cores": e, "p_cores": p, "e_mhz": 1733, "p_mhz": 3912, "gpu": g, "gpu_mem": 0.6 + 2.4 * g, "gpu_mhz": 1617,
                "cpu_w": cpu_w, "gpu_w": gpu_w, "ane_w": ane_w, "total_w": cpu_w + gpu_w + ane_w,
                "thermal": "Nominal",
                "disk_r": 4e6 * self.wave(0.4, 0.3, 0.3), "disk_w": 1e6 * self.wave(0.6, 0.3, 0.3),
                "net_in": 2e6 * self.wave(0.3, 0.35, 0.35) ** 2, "net_out": 3e5 * self.wave(0.5, 0.3, 0.3) ** 2}

    def temps(self):
        c = 45 + 40 * self.wave(0.3, 0.5, 0.35)
        return {"cpu": c, "cpu_max": c + 6, "gpu": c - 8, "gpu_max": c - 3, "ssd": 38.0,
                "fans": [1200 + 40 * (c - 45)] * 2}

    def procs(self):
        user = os.environ.get("USER", "you")
        return [(400 + i, "root" if n in ("WindowServer", "kernel_task") else user,
                 max(0.0, 80 * self.wave(0.2 + i * 0.13, 0.4 - i * 0.03, 0.3)), 150 + (i * 397) % 2400, n)
                for i, n in enumerate(self.names)]


# ============================================================ state
class State:
    def __init__(self, n=600):
        keys = ("e", "p", "gpu", "ram", "cpu_w", "gpu_w", "ane_w", "total_w", "t_cpu", "t_gpu",
                "net_in", "net_out")
        self.h = {k: deque(maxlen=n) for k in keys}
        self.peak = {k: 0.0 for k in ("cpu_w", "gpu_w", "ane_w", "total_w")}
        self.sum = dict(self.peak)
        self.n = 0
        self.wh = 0.0
        self.purge = None
        self.purge_before = 0.0
        self.msg = ""
        self.sel = 0
        self.sel_pid = None
        self.rows = 8
        self.sort = "cpu"
        self.confirm = None
        self.view = "main"
        self.procs = []
        self.last = None
        self.hot = []           # clickable footer buttons: (x0, x1, action)
        self.foot_y = 0
        self.rows_hit = (0, 0, 0, 0)   # app rows: (first line, count, x0, x1)

    def push(self, s, mem, temps, dt):
        avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
        self.h["e"].append(avg(s["e_cores"]))
        self.h["p"].append(avg(s["p_cores"]))
        self.h["gpu"].append(s["gpu"])
        self.h["ram"].append(mem[0] / mem[1] if mem[1] else 0)
        self.h["net_in"].append(s["net_in"])
        self.h["net_out"].append(s["net_out"])
        if temps.get("cpu") is not None:
            self.h["t_cpu"].append(temps["cpu"])
        if temps.get("gpu") is not None:
            self.h["t_gpu"].append(temps["gpu"])
        self.n += 1
        for k in self.peak:
            self.h[k].append(s[k])
            self.peak[k] = max(self.peak[k], s[k])
            self.sum[k] += s[k]
        self.wh += s["total_w"] * dt / 3600.0

    def sorted_procs(self):
        key = (lambda r: r[2]) if self.sort == "cpu" else (lambda r: r[3])
        return sorted(self.procs, key=key, reverse=True)[:self.rows]


# ============================================================ advice
def health_check(s, mem, pressure, temps, dsk, bat, procs):
    tips = []
    sw_u = mem[2]
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
    return tips


# ============================================================ ports, power & heat view
class Canvas:
    """A grid of coloured characters, for drawing the Mac's body."""

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


PORT_GLYPH = {"USB-C": "(══)", "MagSafe": "[▪▪]", "HDMI": "◥██◤", "SD": "[▭▭]", "Audio": "(◉)"}
HEAT = [(245, 170, 60), (245, 95, 50), (235, 45, 55)]


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


def mac_view(st, s, temps, bat, info, W, H):
    """The Mac seen from above: ports on its sides, the chip in the middle, heat leaving the case."""
    pw = info["ports"]
    now = time.time()
    cw = W - 4
    cv = Canvas(cw, H)
    model = pw.model or "Mac"
    left, right = port_layout(model, pw.ports)
    laptop, fanless = "Book" in model, "Air" in model
    flow_w = 7 if cw >= 96 else 4
    bw = max(26, min(64, int(cw * 0.40)))
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
    cwid = min(bw - 4, 30)
    cx0 = bx0 + (bw - cwid) // 2
    cy0 = by0 + 3
    chip_edge = rgb(grad([hot, (255, 255, 255)], 0.12 + 0.12 * math.sin(now * (2 + 4 * heat))))
    title = " " + cut(info["chip"], cwid - 6) + " "
    cv.put(cx0, cy0, "╭─" + title + "─" * (cwid - 3 - len(title)) + "╮", chip_edge)
    cv.put(cx0 + 2, cy0, title, BOLD + chip_edge)
    cv.put(cx0, cy0 + 3, "╰" + "─" * (cwid - 2) + "╯", chip_edge)
    cores = s["e_cores"] + s["p_cores"]
    for i, (label, col, t, load) in enumerate((("CPU", P_P, t_cpu, sum(cores) / len(cores) if cores else 0.0),
                                               ("GPU", P_GPU, t_gpu if t_gpu is not None else t_cpu, s["gpu"]))):
        y = cy0 + 1 + i
        cv.put(cx0, y, "│", chip_edge)
        cv.put(cx0 + cwid - 1, y, "│", chip_edge)
        cv.put(cx0 + 2, y, label, BOLD + rgb(col))
        cv.put(cx0 + 7, y, "%3d°C" % t if t is not None else "  n/a", BOLD + rgb(temp_rgb(t or 0)))
        cv.put(cx0 + 14, y, "%3d%%" % round(100 * (load or 0)), TEXT)

    # ---- power summary inside the body
    notes = []
    if bat:
        notes.append((TEXT, "Battery %d%%  %s" % (bat["pct"], bat["state"])))
        if bat.get("in_w"):
            notes.append((BOLD + rgb(P_PWR), "Power in %.1f W  (%.1f V × %.2f A)" % (
                bat["in_w"], bat.get("in_v") or 0, bat.get("in_a") or 0)))
        if bat.get("load_w"):
            notes.append((rgb(P_MUTED), "Whole Mac is using %.1f W" % bat["load_w"]))
        if bat.get("bat_w") and abs(bat["bat_w"]) > 0.2:
            notes.append((rgb(P_MUTED), "Battery %s %.1f W" % ("charging at" if bat["bat_w"] > 0 else "giving", abs(bat["bat_w"]))))
        if bat.get("adapter_w"):
            notes.append((rgb(P_MUTED), "Charger: %d W %s" % (bat["adapter_w"], bat.get("adapter_name") or "")))
    else:
        notes.append((rgb(P_MUTED), "No battery: runs from the wall"))
    if s.get("has_power", True):
        notes.append((rgb(P_MUTED), "Chip alone is using %.1f W" % s["total_w"]))
    for i, (style, text) in enumerate(notes):
        y = cy0 + 5 + i
        if y >= by1:
            break
        text = cut(text.strip(), bw - 4)
        cv.put(bx0 + (bw - len(text)) // 2, y, text, style)

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
        fans = [f for f in temps.get("fans") or [] if f is not None]
        if fans:
            rpm = max(fans)
            msg += "  ·  fans %d rpm" % rpm if rpm > 100 else "  ·  fans resting"
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
    chargers = [p for p in pw.ports if p.get("charge")]
    matched = usb_by_port(pw)
    outs = getattr(pw, "out", {})
    if info.get("smc"):
        try:
            outs = info["smc"].port_out()
        except Exception:
            pass

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
    return panel("This Mac: ports, power & heat", cv.lines(), W, P_PWR, muted(model))


# ============================================================ rendering
def render(st, s, mem, pressure, temps, dsk, bat, info, cols, rows):
    W = max(60, min(cols, 160))
    two = W >= 100
    wl = (W - 1) // 2 if two else W
    wr = W - 1 - wl if two else W
    tips = health_check(s, mem, pressure, temps, dsk, bat, st.procs)[:3]
    fixed = 2 + max(1, len(tips))
    # graph height grows with the terminal: each of the 3 panel rows is 7 + 2h lines tall
    h = max(1, min(4, (rows - fixed - 21) // 6)) if two else 1
    body_lines = 5 + 2 * h

    # ---- header
    logo = BOLD + rgb(G_LOGO[0]) + "◆ " + "".join(rgb(grad(G_LOGO, i / 3.0)) + ch for i, ch in enumerate("mtop")) + RESET
    left = " " + logo + "  " + TEXT + info["chip"] + RESET
    left_full = left + (muted("  " + info["cores"]) if info["cores"] else "")
    sep = dim("  ·  ")
    th = s["thermal"]
    bits = []
    if temps.get("cpu") is not None:
        bits.append(BOLD + rgb(temp_rgb(temps["cpu"])) + "%d°C" % temps["cpu"] + RESET)
    if s.get("has_power", True):
        bits.append(BOLD + rgb(P_PWR) + "%.1f W" % s["total_w"] + RESET)
    if th:
        bits.append(dot(GOOD if th == "Nominal" else BAD, muted("thermal " + th.lower())))
    if info.get("uptime"):
        bits.append(muted("up " + info["uptime"]))
    bits.append(TEXT + time.strftime("%H:%M:%S") + RESET)
    while len(bits) > 2 and vlen(left) + vlen(sep.join(bits)) + 3 > W:
        bits.pop(-2)
    right = sep.join(bits) + " "
    if vlen(left_full) + vlen(right) + 1 <= W:
        left = left_full
    head = [left + " " * max(1, W - vlen(left) - vlen(right)) + right]

    body = []

    def row(a, b):
        body.extend(side_by_side(a, b, wl, wr) if two else a + b)

    def cores_row(cs, avail):
        wide = 2 if len(cs) * 3 - 1 <= avail else 1
        gap = " " if len(cs) * (wide + 1) - 1 <= avail else ""
        return dim("%6s " % "cores") + gap.join(
            rgb(grad(G_LOAD, c)) + VBAR[max(1, int(round(c * 8)))] * wide for c in cs) + RESET

    if st.view == "mac":
        body = mac_view(st, s, temps, bat, info, W, max(13, min(36, rows - len(head) - fixed + 1 - 2)))
        st.rows_hit = (0, 0, 0, 0)
    else:
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

        used, total, sw_u, sw_t, cached, wired, comp = mem
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
            (SEG_APP, "app", app), (SEG_WIRED, "wired", wired), (SEG_COMP, "comp", comp), (SEG_CACHE, "cache", cached))))
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
            sl.append(gauge("Batt", bat["pct"] / 100.0, P_PWR, TEXT + "%3d%%" % bat["pct"], bat["state"], bw, [b_col]))
            detail = []
            if bat["health"]:
                detail.append(muted("health ") + TEXT + "%.0f%%" % bat["health"] + RESET)
            detail.append(TEXT + str(bat["cycles"]) + RESET + muted(" cycles"))
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
        apps_top = len(head) + len(body) + (0 if two else body_lines + 2)
        inner = wl - 4
        gw = inner - 7
        sw = max(6, inner - 37)
        pw = []
        for key, label, color, stops in (("cpu_w", "CPU", P_P, G_P), ("gpu_w", "GPU", P_GPU, G_GPU),
                                         ("ane_w", "ANE", P_E, G_E), ("total_w", "Total", P_PWR, G_PWR)):
            pw.append(BOLD + rgb(color) + "%-5s" % label + RESET + " " + TEXT + "%6.2f W" % s[key] + RESET
                      + muted("  avg %5.2f  pk %5.2f " % (st.sum[key] / max(1, st.n), st.peak[key]))
                      + graph(st.h[key], sw, 1, max(st.peak[key], 0.5), stops)[0])
        if h > 1:
            pw.append("")
        top_w = max(1.0, math.ceil(st.peak["total_w"]))
        pw += graph_rows(st.h["total_w"], gw, body_lines - len(pw) - 1, top_w, G_PWR, hi="%d W" % top_w, lo="0 W")
        energy = "%.1f mWh" % (st.wh * 1000) if st.wh < 1 else "%.2f Wh" % st.wh
        pw.append(muted(" energy used ") + TEXT + energy + RESET + muted(" since start"))

        pw_right = muted("peak ") + TEXT + "%.1f W" % st.peak["total_w"]
        if not s.get("has_power", True):
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

    # ---- health check
    tail = []
    if tips:
        for col, tip in tips:
            tail.append(" " + rgb(col) + "● " + BOLD + cut(tip, W - 4) + RESET)
    else:
        tail.append(" " + rgb(GOOD) + "✓ " + BOLD + "All good" + RESET + muted("  your Mac is running normally"))

    # ---- footer: every button is a key and also a click target
    st.hot = []
    foot, x = " ", 2
    if st.confirm:
        pid, name = st.confirm
        q = " Quit %s (pid %d)? " % (cut(name, 30), pid)
        foot += rgb(BAD, bg=True) + "\x1b[97m" + BOLD + q + RESET + "  "
        x += len(q) + 2
        buttons = (("y", "y", "yes, quit it"), ("n", "n", "cancel"))
    else:
        buttons = (("q", "q", "quit"), ("ud", "↑↓", "select"), ("k", "k", "quit app"), ("s", "s", "sort CPU/RAM"),
                   ("c", "c", "clear RAM cache"), ("p", "p", "main view" if st.view == "mac" else "ports & heat"))
    for action, label, txt in buttons:
        w = len(label) + 3 + len(txt)
        st.hot.append((x, x + w - 1, action))
        foot += rgb(P_MUTED) + REV + BOLD + " " + label + " " + RESET + " " + muted(txt) + "  "
        x += w + 2
    tail.append(foot + BOLD + rgb(P_ACCENT) + st.msg + RESET)

    shown = body[:max(0, rows - len(head) - len(tail))]
    if st.rows_hit[0] + st.rows_hit[1] - 1 > len(head) + len(shown):
        st.rows_hit = (0, 0, 0, 0)          # app list is cut off: not clickable
    out = head + shown + tail
    st.foot_y = len(out)
    return [clip(l, cols) if vlen(l) > cols else l for l in out]


# ============================================================ main loop
MOUSE_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


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
    procs = st.sorted_procs()
    if action == "q":
        return False
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
    elif action == "p":
        st.view = "main" if st.view == "mac" else "mac"
        st.msg = "showing ports, power and heat" if st.view == "mac" else "back to the main view"
    elif action == "c":
        if st.purge is not None:
            st.msg = "still clearing cache..."
        elif demo:
            st.msg = "demo mode: nothing cleared"
        else:
            st.purge_before = memory()[4]
            st.purge = subprocess.Popen(["purge"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            st.msg = "clearing cache..."
    return True


def click(x, y, st, demo):
    if y == st.foot_y:
        for x0, x1, action in st.hot:
            if x0 <= x <= x1:
                if action == "ud":
                    action = "up" if x <= x0 + 1 else "down"
                return act(action, st, demo)
    first, count, x0, x1 = st.rows_hit
    if not st.confirm and count and first <= y < first + count and x0 <= x <= x1:
        st.sel = y - first
        p = st.sorted_procs()[st.sel]
        st.sel_pid = p[0]
        st.msg = "selected %s (pid %d)" % (p[4], p[0])
    return True


def handle_input(data, st, demo):
    """Keys, mouse clicks and the scroll wheel. Returns False to quit."""
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
    if data in ("\x1b[A", "\x1bOA"):
        action = "up"
    elif data in ("\x1b[B", "\x1bOB"):
        action = "down"
    else:
        action = data[:1].lower()
    if st.confirm:
        return act("y" if action == "y" else "n", st, demo)
    if action not in ("q", "up", "down", "s", "k", "c", "p"):
        return True
    return act(action, st, demo)


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


def main():
    global TRUECOLOR
    ap = argparse.ArgumentParser(description="System monitor for every Mac")
    ap.add_argument("--interval", type=float, default=1.0, help="refresh seconds (default 1)")
    ap.add_argument("--demo", action="store_true", help="fake data, no sudo needed")
    ap.add_argument("--no-sudo", action="store_true",
                    help="basic mode: no password, everything except watts and MHz")
    ap.add_argument("--color", choices=("auto", "truecolor", "256"), default="auto",
                    help="colour depth (default: detect)")
    ap.add_argument("--frames", type=int, default=0, help=argparse.SUPPRESS)
    a = ap.parse_args()
    TRUECOLOR = wants_truecolor(a.color)
    basic = a.no_sudo and not a.demo

    if not a.demo:
        if sys.platform != "darwin":
            sys.exit("mtop needs a Mac. Try: python3 mtop.py --demo")
        if os.geteuid() != 0 and not basic:
            print("mtop reads power data, which macOS only gives to an administrator.")
            print("Type your Mac password below (nothing shows while you type), then press Return.")
            print("No password? Press Ctrl-C and run:  python3 %s --no-sudo\n" % sys.argv[0])
            # sudo drops COLORTERM, so pass on what we detected
            os.execvp("sudo", ["sudo", sys.executable] + sys.argv
                      + ["--color", "truecolor" if TRUECOLOR else "256"])

    print("mtop: loading sensors...")
    st = State()
    demo = Demo() if a.demo else None
    local = None if a.demo else LocalStats()
    chip, cores = ("Demo Mac", "fake data, not this computer") if a.demo else chip_info()
    temps_src = None if a.demo else Temps()
    src = None
    if not a.demo and not basic:
        try:
            src = PowerMetrics(int(a.interval * 1000))
        except OSError:
            st.msg = "power readings unavailable on this Mac: basic mode"
    info = {"chip": chip, "cores": cores, "smart": "Verified" if a.demo else smart_status(),
            "uptime": "", "load": None, "ports": DemoPorts() if a.demo else PortWatch(),
            "smc": temps_src.smc if temps_src else None}
    booted = time.time() - 273600 if a.demo else boot_time()
    last = {"ps": 0.0, "bat": 0.0, "dsk": 0.0, "sample": 0.0}
    bat, dsk = None, (0.0, 0.0, 0.0)
    next_tick = time.time()

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)

    tty = sys.stdin.isatty()
    old = None
    if tty:
        import termios
        import tty as ttymod
        old = termios.tcgetattr(sys.stdin)
        ttymod.setcbreak(sys.stdin.fileno())
    sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?7l")       # alt screen, hide cursor, no line wrap
    if tty:
        sys.stdout.write("\x1b[?1000h\x1b[?1006h")           # report mouse clicks
    frames = 0
    try:
        while True:
            fds = [sys.stdin] if tty else []
            anim = 0.05 if st.view == "mac" and st.last else None      # the ports view is animated
            if src:
                r, _, _ = select.select(fds + [src.fd], [], [], anim)
            else:
                wait = max(0.0, next_tick - time.time())
                r, _, _ = select.select(fds, [], [], min(wait, anim) if anim else wait)
            if sys.stdin in r:
                if not handle_input(os.read(sys.stdin.fileno(), 256).decode(errors="ignore"), st, a.demo):
                    break
            if st.purge is not None and st.purge.poll() is not None:
                st.purge = None
                st.msg = "cache cleared: freed %.1f GB" % max(0.0, st.purge_before - memory()[4])

            s = None
            if src:
                if src.fd in r:
                    try:
                        s = src.read()
                    except RuntimeError:              # powermetrics missing or refused: carry on without it
                        src.close()
                        src = None
                        st.msg = "power readings unavailable on this Mac: basic mode"
                if s:
                    if not s["e_cores"] and not s["p_cores"]:      # Intel: no per-core load in powermetrics
                        s["p_cores"] = local.cpu()
                    load = local.gpu()                             # also measures GPU memory
                    if s["gpu"] is None:
                        s["gpu"] = load
                    s["gpu_mem"] = local.gpu_mem
            elif time.time() >= next_tick:
                next_tick = time.time() + a.interval
                s = demo.sample() if a.demo else local.sample()
            if s:
                now = time.time()
                if a.demo:
                    mem, pressure, temps = (14.4 + math.sin(demo.t / 3), 24.0, 0.0, 0.0, 6.2, 2.1, 0.8), "normal", demo.temps()
                    st.procs = demo.procs()
                    bat = {"pct": 87, "health": 94, "cycles": 143, "temp": 31.0, "state": "charging", "plugged": True,
                           "in_w": 58 + 6 * math.sin(demo.t / 2), "in_v": 20.1, "in_a": (58 + 6 * math.sin(demo.t / 2)) / 20.1,
                           "load_w": 30 + 6 * math.sin(demo.t / 2), "bat_w": 28.0, "adapter_w": 96,
                           "adapter_name": "pd charger"}
                    dsk = (310.0, 494.0, 184.0)
                    info["load"] = 1.2 + 4 * st.h["p"][-1] if st.h["p"] else 2.0
                else:
                    mem, pressure, temps = memory(), memory_pressure(), temps_src.read()
                    if now - last["ps"] > 2:
                        st.procs, last["ps"] = processes(), now
                    if now - last["bat"] > 2:
                        bat, last["bat"] = battery(), now
                    if now - last["dsk"] > 10:
                        dsk, last["dsk"] = disk_usage(), now
                    try:
                        info["load"] = os.getloadavg()[0]
                    except OSError:
                        pass
                if booted:
                    info["uptime"] = uptime_text(now - booted)
                dt = min(now - last["sample"], 5 * a.interval) if last["sample"] else a.interval
                last["sample"] = now
                st.push(s, mem, temps, a.interval if a.demo else dt)
                st.last = (s, mem, pressure, temps)
            if st.last is None:
                continue
            s_, mem_, pressure_, temps_ = st.last
            size = shutil.get_terminal_size((120, 40))
            lines = render(st, s_, mem_, pressure_, temps_, dsk, bat, info, size.columns, size.lines)
            # synchronized update: the terminal paints the whole frame at once, no flicker
            sys.stdout.write("\x1b[?2026h\x1b[H" + "\x1b[K\n".join(lines) + "\x1b[K\x1b[J\x1b[?2026l")
            sys.stdout.flush()
            frames += 1
            if a.frames and frames >= a.frames:
                break
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?1006l\x1b[?1000l\x1b[?7h\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if old:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        if src:
            src.close()


if __name__ == "__main__":
    main()
