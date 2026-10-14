#!/usr/bin/env python3
"""
CTP500 IPP Bridge
=================

Makes a Core Innovations CTP500 ("Stone Printer") Bluetooth thermal printer
show up as a normal printer in Windows.

How it works:
  * This script runs a tiny IPP print server on your own PC (127.0.0.1:631).
  * Windows talks to it with its built-in, Microsoft-signed IPP class driver,
    so no custom driver has to be installed or signed.
  * Windows renders each page to a raster image (PWG Raster) and sends it here.
  * The script crops, scales and dithers each page to the printer's
    384-dot width and sends it over Bluetooth using the CTP500 protocol
    reverse-engineered by voidsshadows and ThirtyThreeDown (Mel).

Requirements: Python 3.9+ on Windows (for Bluetooth RFCOMM sockets) and Pillow.

    pip install pillow

Usage:
    python ctp500_bridge.py serve --mac AA:BB:CC:DD:EE:FF
    python ctp500_bridge.py print --mac AA:BB:CC:DD:EE:FF picture.png
    python ctp500_bridge.py text  --mac AA:BB:CC:DD:EE:FF "Hello there"
    python ctp500_bridge.py find-mac
    python ctp500_bridge.py serve --dry-run out_folder      (no printer needed)

Run "python ctp500_bridge.py serve --help" for every option.
"""

import argparse
import io
import json
import logging
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps
except ImportError:  # pragma: no cover
    sys.exit("Pillow is not installed. Run:  pip install pillow")

__version__ = "1.0.0"

log = logging.getLogger("ctp500")

# --------------------------------------------------------------------------
# Printer constants
# --------------------------------------------------------------------------

PRINTER_WIDTH = 384          # dots across the print head
PRINTER_DPI = 203            # 8 dots/mm
RFCOMM_CHANNEL = 1

CMD_INIT = b"\x1b\x40"                 # ESC @  : reset
CMD_STATUS = b"\x1e\x47\x03"           # status query (38-byte reply)
CMD_START = b"\x1d\x49\xf0\x19"        # "expect print data"
CMD_END = b"\x0a\x0a\x0a\x0a"          # line feeds: push the print out

# Paper sizes offered to Windows. The print head covers 48 mm, so the
# narrow sizes print at true size. Letter/A4 are there so ordinary documents
# can be printed too; they get shrunk (or auto-cropped) to fit the roll.
MEDIA = [
    # (pwg name, width in 1/100 mm, length in 1/100 mm)
    ("om_ctp500-48x30mm_48x30mm", 4800, 3000),
    ("om_ctp500-48x50mm_48x50mm", 4800, 5000),
    ("om_ctp500-48x80mm_48x80mm", 4800, 8000),
    ("om_ctp500-48x100mm_48x100mm", 4800, 10000),
    ("om_ctp500-48x150mm_48x150mm", 4800, 15000),
    ("om_ctp500-48x200mm_48x200mm", 4800, 20000),
    ("na_letter_8.5x11in", 21590, 27940),
    ("iso_a4_210x297mm", 21000, 29700),
]
DEFAULT_MEDIA = "om_ctp500-48x80mm_48x80mm"
RESOLUTIONS = [203, 300]
DEFAULT_RESOLUTION = 300


# ==========================================================================
# 1. Talking to the printer
# ==========================================================================

class PrinterLink:
    """Bluetooth (RFCOMM) connection to the CTP500, or a dry-run folder."""

    def __init__(self, mac=None, dry_run_dir=None, rotate=False,
                 band_height=0, band_delay=0.05):
        self.mac = mac
        self.dry_run_dir = dry_run_dir
        self.rotate = rotate
        self.band_height = band_height
        self.band_delay = band_delay
        self.sock = None
        self.lock = threading.Lock()
        self.last_used = 0.0
        self.dry_count = 0

    # -- connection --------------------------------------------------------
    def connect(self):
        if self.dry_run_dir or self.sock:
            return
        if not hasattr(socket, "AF_BLUETOOTH"):
            raise RuntimeError(
                "This Python build has no Bluetooth socket support. "
                "Use Python 3.9 or newer from python.org on Windows.")
        log.info("Connecting to printer %s ...", self.mac)
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                          socket.BTPROTO_RFCOMM)
        s.settimeout(15)
        try:
            s.connect((self.mac, RFCOMM_CHANNEL))
        except OSError:
            s.close()
            raise
        self.sock = s
        log.info("Connected.")
        try:  # The phone app asks for status first; harmless if ignored.
            s.sendall(CMD_STATUS)
            s.settimeout(2)
            s.recv(64)
        except OSError:
            pass
        finally:
            s.settimeout(30)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
            log.info("Disconnected from printer.")

    def close_if_idle(self, idle_seconds):
        with self.lock:
            if self.sock and time.time() - self.last_used > idle_seconds:
                self.close()

    def _send(self, data):
        self.sock.sendall(data)

    # -- printing ----------------------------------------------------------
    def print_images(self, images, job_label="job"):
        """Print a list of 1-bit, 384-wide PIL images (one per page)."""
        if not images:
            return
        if self.dry_run_dir:
            self._dry_run(images, job_label)
            return
        with self.lock:
            for attempt in (1, 2):
                try:
                    self.connect()
                    self._print_now(images)
                    self.last_used = time.time()
                    return
                except OSError as e:
                    log.warning("Bluetooth error (%s), attempt %d", e, attempt)
                    self.close()
                    if attempt == 2:
                        raise
                    time.sleep(2)

    def _print_now(self, images):
        for im in images:
            if self.rotate:
                im = im.rotate(180)
            self._send(CMD_INIT)
            time.sleep(0.3)
            self._send(CMD_START)
            time.sleep(0.3)
            bands = [im]
            if self.band_height and im.height > self.band_height:
                bands = [im.crop((0, y, im.width,
                                  min(im.height, y + self.band_height)))
                         for y in range(0, im.height, self.band_height)]
            for band in bands:
                self._send(raster_command(band))
                # Give the printer time to burn the lines (~1 ms per line).
                time.sleep(max(self.band_delay, band.height * 0.002))
            self._send(CMD_END)
            time.sleep(0.5)

    def _dry_run(self, images, job_label):
        os.makedirs(self.dry_run_dir, exist_ok=True)
        for im in images:
            self.dry_count += 1
            label = os.path.splitext(job_label)[0] or "job"
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:40]
            path = os.path.join(self.dry_run_dir,
                                f"{self.dry_count:03d}_{safe}.png")
            im.save(path)
            log.info("[dry run] wrote %s (%dx%d)", path, im.width, im.height)


def raster_command(im):
    """GS v 0: print a 1-bit raster image. Black pixels are 1-bits."""
    if im.mode != "1":
        im = im.convert("1")
    if im.width % 8:
        padded = Image.new("1", (im.width + 8 - im.width % 8, im.height), 1)
        padded.paste(im, (0, 0))
        im = padded
    # PIL "1" stores white as 1; the printer wants black as 1.
    data = ImageOps.invert(im.convert("L")).convert("1").tobytes()
    width_bytes = im.width // 8
    return (b"\x1d\x76\x30\x00"
            + struct.pack("<HH", width_bytes, im.height)
            + data)


# ==========================================================================
# 2. Turning pages into printer-ready images
# ==========================================================================

class PageOptions:
    def __init__(self, autocrop=True, dither="auto", trim=True,
                 margin=8, darkness=1.0, max_length_mm=600):
        self.autocrop = autocrop
        self.dither = dither          # "auto", "always", "never"
        self.trim = trim
        self.margin = margin          # blank dots kept around cropped content
        self.darkness = darkness      # >1 darker, <1 lighter
        self.max_length_mm = max_length_mm


def prepare_page(img, opts):
    """Convert any PIL image into a 384-dot-wide 1-bit image, or None if blank."""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    gray = img.convert("L")

    bbox = content_bbox(gray)
    if bbox is None:
        return None  # blank page

    if opts.autocrop:
        l, t, r, b = bbox
        m = int(opts.margin * gray.width / PRINTER_WIDTH)
        gray = gray.crop((max(0, l - m), max(0, t - m),
                          min(gray.width, r + m), min(gray.height, b + m)))
    elif opts.trim:
        # Keep the layout, but cut blank paper off the bottom.
        bottom = min(gray.height, bbox[3] + int(10 * gray.width / PRINTER_WIDTH))
        gray = gray.crop((0, 0, gray.width, bottom))

    # Scale to the head width.
    if gray.width != PRINTER_WIDTH:
        h = max(1, round(gray.height * PRINTER_WIDTH / gray.width))
        gray = gray.resize((PRINTER_WIDTH, h), Image.LANCZOS)

    max_h = int(opts.max_length_mm * PRINTER_DPI / 25.4)
    if gray.height > max_h:
        log.warning("Page is %d dots long; cutting at %d", gray.height, max_h)
        gray = gray.crop((0, 0, PRINTER_WIDTH, max_h))

    if opts.darkness != 1.0:
        f = opts.darkness
        gray = gray.point(lambda v: max(0, min(255, int(255 - (255 - v) * f))))

    use_dither = {"always": True, "never": False}.get(
        opts.dither, looks_like_photo(gray))
    if use_dither:
        return gray.convert("1", dither=Image.FLOYDSTEINBERG)
    return gray.point(lambda v: 255 if v >= 160 else 0).convert(
        "1", dither=Image.NONE)


def content_bbox(gray, threshold=245):
    """Bounding box of non-white pixels."""
    mask = gray.point(lambda v: 255 if v < threshold else 0)
    return mask.getbbox()


def looks_like_photo(gray):
    """True when a noticeable share of pixels are mid-tones (photos, shading)."""
    hist = gray.histogram()
    total = sum(hist) or 1
    mid = sum(hist[40:216])
    return mid / total > 0.08


def text_to_image(text, font_size=24, font_path=None):
    """Render plain text into a 384-wide image with word wrapping."""
    font = load_font(font_size, font_path)
    lines = []
    for para in text.splitlines() or [""]:
        line = ""
        for word in para.split(" "):
            trial = f"{line} {word}" if line else word
            if font.getlength(trial) <= PRINTER_WIDTH - 8 or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    ascent, descent = font.getmetrics()
    lh = ascent + descent + 4
    img = Image.new("L", (PRINTER_WIDTH, max(lh, lh * len(lines) + 8)), 255)
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        d.text((4, 4 + i * lh), ln, fill=0, font=font)
    return img


def load_font(size, font_path=None):
    candidates = [font_path] if font_path else []
    windir = os.environ.get("WINDIR", r"C:\Windows")
    candidates += [
        os.path.join(windir, "Fonts", "segoeui.ttf"),
        os.path.join(windir, "Fonts", "arial.ttf"),
        os.path.join(windir, "Fonts", "lucon.ttf"),
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for c in candidates:
        if c:
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


# ==========================================================================
# 3. Decoding what Windows sends (PWG Raster, PNG, JPEG)
# ==========================================================================

class RasterError(ValueError):
    pass


SUPPORTED_FORMATS = ("image/pwg-raster", "image/png", "image/jpeg",
                     "text/plain", "application/octet-stream")


def format_supported(fmt):
    return (fmt or "application/octet-stream").split(";")[0].strip() \
        in SUPPORTED_FORMATS


def decode_document(data, mime):
    """Return a list of PIL images from a print document."""
    mime = (mime or "application/octet-stream").split(";")[0].strip()
    if not data:
        return []
    if data[:4] == b"RaS2" or mime == "image/pwg-raster":
        return decode_pwg_raster(data)
    is_image = data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff"
    if mime == "text/plain" or (mime == "application/octet-stream"
                                and not is_image):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = None
        if text is not None and "\x00" not in text:
            return [text_to_image(text.replace("\r\n", "\n"))]
    try:
        img = Image.open(io.BytesIO(data))
    except Exception as e:
        raise RasterError(f"Unsupported document format {mime!r}") from e
    pages = []
    try:
        while True:
            img.load()
            pages.append(img.copy())
            img.seek(img.tell() + 1)
    except EOFError:
        pass
    return pages


def decode_pwg_raster(data):
    if data[:4] != b"RaS2":
        raise RasterError("Not a PWG raster stream")
    pos = 4
    pages = []
    while pos + 1796 <= len(data):
        hdr = data[pos:pos + 1796]
        pos += 1796
        u = lambda off: struct.unpack(">I", hdr[off:off + 4])[0]
        width, height = u(372), u(376)
        bpc, bpp, bpl = u(384), u(388), u(392)
        cspace = u(400)
        if width == 0 or height == 0:
            break
        if bpp not in (1, 8, 24) or bpl != (width * bpp + 7) // 8:
            raise RasterError(f"Unsupported raster: {bpp} bits per pixel")
        unit = max(1, bpp // 8)
        # "Fill" colour used by the clear-to-end-of-line code.
        white = b"\x00" if (bpp == 1 or cspace == 3) else b"\xff"
        out = bytearray()
        y = 0
        while y < height:
            if pos >= len(data):
                raise RasterError("Raster data ended early")
            repeat = data[pos] + 1
            pos += 1
            line = bytearray()
            while len(line) < bpl:
                if pos >= len(data):
                    raise RasterError("Raster data ended early")
                c = data[pos]
                pos += 1
                if c == 128:
                    line.extend(white * (bpl - len(line)))
                elif c > 128:
                    n = (257 - c) * unit
                    line.extend(data[pos:pos + n])
                    pos += n
                else:
                    line.extend(data[pos:pos + unit] * (c + 1))
                    pos += unit
            line = bytes(line[:bpl])
            repeat = min(repeat, height - y)
            out.extend(line * repeat)
            y += repeat
        if bpp == 1:
            # 1-bit "black" colour space: 1 = black.
            img = Image.frombytes("1", (width, height), bytes(out), "raw", "1;I")
        elif bpp == 8:
            img = Image.frombytes("L", (width, height), bytes(out))
            if cspace == 3:  # 8-bit black: 255 = black
                img = ImageOps.invert(img)
        else:
            img = Image.frombytes("RGB", (width, height), bytes(out))
        pages.append(img)
    if not pages:
        raise RasterError("No pages found in raster stream")
    return pages


# ==========================================================================
# 4. A small IPP/2.0 implementation
# ==========================================================================

# Value tags
T_UNSUPPORTED, T_UNKNOWN, T_NOVALUE = 0x10, 0x12, 0x13
T_INTEGER, T_BOOLEAN, T_ENUM = 0x21, 0x22, 0x23
T_OCTET, T_DATETIME, T_RESOLUTION, T_RANGE = 0x30, 0x31, 0x32, 0x33
T_BEGCOL, T_TEXTLANG, T_NAMELANG, T_ENDCOL = 0x34, 0x35, 0x36, 0x37
T_TEXT, T_NAME, T_KEYWORD, T_URI, T_URISCHEME = 0x41, 0x42, 0x44, 0x45, 0x46
T_CHARSET, T_LANG, T_MIME, T_MEMBER = 0x47, 0x48, 0x49, 0x4A

# Group tags
G_OPERATION, G_JOB, G_END, G_PRINTER, G_UNSUPPORTED = 1, 2, 3, 4, 5

# Operations
OP_PRINT_JOB, OP_VALIDATE_JOB, OP_CREATE_JOB = 0x02, 0x04, 0x05
OP_SEND_DOCUMENT, OP_CANCEL_JOB, OP_GET_JOB_ATTRS = 0x06, 0x08, 0x09
OP_GET_JOBS, OP_GET_PRINTER_ATTRS = 0x0A, 0x0B
OP_CANCEL_MY_JOBS = 0x39
OP_CLOSE_JOB, OP_IDENTIFY, OP_VALIDATE_DOC = 0x3B, 0x3C, 0x3D
SUPPORTED_OPS = [OP_PRINT_JOB, OP_VALIDATE_JOB, OP_CREATE_JOB,
                 OP_SEND_DOCUMENT, OP_CANCEL_JOB, OP_GET_JOB_ATTRS,
                 OP_GET_JOBS, OP_GET_PRINTER_ATTRS, OP_CANCEL_MY_JOBS,
                 OP_CLOSE_JOB,
                 OP_IDENTIFY, OP_VALIDATE_DOC]

# Status codes
S_OK, S_OK_IGNORED = 0x0000, 0x0001
S_BAD_REQUEST, S_NOT_FOUND, S_NOT_POSSIBLE = 0x0400, 0x0406, 0x0404
S_FORMAT, S_INTERNAL, S_OP_UNSUPPORTED = 0x040A, 0x0500, 0x0501

# Job states
J_PENDING, J_PROCESSING, J_CANCELED, J_ABORTED, J_COMPLETED = 3, 5, 7, 8, 9


class Col(dict):
    """An IPP collection: {member-name: [(tag, value), ...]}"""


def _v(tag, *values):
    return [(tag, v) for v in values]


def parse_ipp(body):
    """Parse an IPP request. Returns (version, op, request_id, groups, data)."""
    if len(body) < 9:
        raise ValueError("short IPP message")
    version = (body[0], body[1])
    op, req_id = struct.unpack(">HI", body[2:8])
    pos = 8
    groups = []          # list of (group_tag, {name: [(tag, value)]})
    current = None
    stack = []           # for collections: (container_dict, member_name)
    last_name = None
    while pos < len(body):
        tag = body[pos]
        pos += 1
        if tag == G_END:
            break
        if tag < 0x10:   # group delimiter
            current = {}
            groups.append((tag, current))
            last_name = None
            continue
        nlen = struct.unpack(">H", body[pos:pos + 2])[0]
        pos += 2
        name = body[pos:pos + nlen].decode("utf-8", "replace")
        pos += nlen
        vlen = struct.unpack(">H", body[pos:pos + 2])[0]
        pos += 2
        raw = body[pos:pos + vlen]
        pos += vlen
        if current is None:
            raise ValueError("attribute outside group")

        target = stack[-1][0] if stack else current

        if tag == T_MEMBER:          # name of the next member in a collection
            stack[-1] = (stack[-1][0], raw.decode("utf-8", "replace"))
            continue
        if stack:
            name = name or stack[-1][1]
            if not name:
                continue
        elif name:
            last_name = name
        else:
            name = last_name

        if tag == T_BEGCOL:
            col = Col()
            target.setdefault(name, []).append((tag, col))
            stack.append((col, None))
            continue
        if tag == T_ENDCOL:
            if stack:
                stack.pop()
            continue
        target.setdefault(name, []).append((tag, decode_value(tag, raw)))
    return version, op, req_id, groups, body[pos:]


def decode_value(tag, raw):
    if tag in (T_INTEGER, T_ENUM) and len(raw) == 4:
        return struct.unpack(">i", raw)[0]
    if tag == T_BOOLEAN and len(raw) == 1:
        return bool(raw[0])
    if tag == T_RANGE and len(raw) == 8:
        return struct.unpack(">ii", raw)
    if tag == T_RESOLUTION and len(raw) == 9:
        return struct.unpack(">iib", raw)
    if tag in (T_TEXTLANG, T_NAMELANG) and len(raw) >= 4:
        llen = struct.unpack(">H", raw[:2])[0]
        tlen = struct.unpack(">H", raw[2 + llen:4 + llen])[0]
        return raw[4 + llen:4 + llen + tlen].decode("utf-8", "replace")
    if 0x40 <= tag <= 0x5F:
        return raw.decode("utf-8", "replace")
    return raw


def encode_value(tag, value):
    if tag in (T_INTEGER, T_ENUM):
        return struct.pack(">i", value)
    if tag == T_BOOLEAN:
        return b"\x01" if value else b"\x00"
    if tag == T_RANGE:
        return struct.pack(">ii", *value)
    if tag == T_RESOLUTION:
        return struct.pack(">iib", *value)
    if tag == T_DATETIME:
        t = time.gmtime(value)
        return struct.pack(">HBBBBBBcBB", t.tm_year, t.tm_mon, t.tm_mday,
                           t.tm_hour, t.tm_min, t.tm_sec, 0, b"+", 0, 0)
    if tag in (T_NOVALUE, T_UNKNOWN, T_UNSUPPORTED):
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def encode_attr(name, values):
    out = bytearray()
    for i, (tag, value) in enumerate(values):
        n = name.encode() if i == 0 else b""
        if tag == T_BEGCOL:
            out += bytes([T_BEGCOL]) + struct.pack(">H", len(n)) + n
            out += b"\x00\x00"
            for mname, mvals in value.items():
                mn = mname.encode()
                out += bytes([T_MEMBER]) + b"\x00\x00"
                out += struct.pack(">H", len(mn)) + mn
                out += encode_attr("", mvals)
            out += bytes([T_ENDCOL]) + b"\x00\x00\x00\x00"
        else:
            v = encode_value(tag, value)
            out += bytes([tag]) + struct.pack(">H", len(n)) + n
            out += struct.pack(">H", len(v)) + v
    return bytes(out)


def build_response(status, req_id, groups, version=(2, 0)):
    out = bytearray(struct.pack(">BBHI", version[0], version[1], status, req_id))
    for gtag, attrs in groups:
        out.append(gtag)
        for name, values in attrs.items():
            if values:
                out += encode_attr(name, values)
    out.append(G_END)
    return bytes(out)


def first(attrs, name, default=None):
    vals = attrs.get(name)
    return vals[0][1] if vals else default


# ==========================================================================
# 5. The print server
# ==========================================================================

class Job:
    def __init__(self, job_id, name, user):
        self.id = job_id
        self.name = name or f"Job {job_id}"
        self.user = user or "anonymous"
        self.state = J_PENDING
        self.reasons = ["none"]
        self.created = int(time.time())
        self.completed = 0
        self.pages = 0
        self.media = None
        self.message = ""


class BridgeServer:
    def __init__(self, link, opts, host="127.0.0.1", port=631,
                 name="CTP500 Thermal Printer", idle_disconnect=60):
        self.link = link
        self.opts = opts
        self.host, self.port = host, port
        self.name = name
        self.idle_disconnect = idle_disconnect
        self.start_time = time.time()
        self.jobs = {}
        self.next_job_id = 1
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        seed = link.mac or "dry-run"
        self.uuid = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                 "ctp500-bridge:" + seed))
        self._busy = False
        self.state_changed = self.start_time

    @property
    def busy(self):
        return self._busy

    @busy.setter
    def busy(self, value):
        if value != self._busy:
            self._busy = value
            self.state_changed = time.time()

    # -- URIs --------------------------------------------------------------
    def printer_uri(self, host_header=None):
        host = host_header or f"localhost:{self.port}"
        return f"ipp://{host}/ipp/print"

    def job_uri(self, job, host_header=None):
        return f"{self.printer_uri(host_header)}/{job.id}"

    # -- printer attributes --------------------------------------------------
    def printer_attributes(self, host_header=None):
        media_names = [m[0] for m in MEDIA]
        media_cols = [(T_BEGCOL, media_col(m)) for m in MEDIA]
        default_col = media_col(next(m for m in MEDIA if m[0] == DEFAULT_MEDIA))
        state = 4 if self.busy else 3
        with self.lock:
            queued = sum(1 for j in self.jobs.values()
                         if j.state in (J_PENDING, J_PROCESSING))
        uri = self.printer_uri(host_header)
        http_uri = "http://" + (host_header or f"localhost:{self.port}") + "/"
        res = [(T_RESOLUTION, (r, r, 3)) for r in RESOLUTIONS]
        return {
            "charset-configured": _v(T_CHARSET, "utf-8"),
            "charset-supported": _v(T_CHARSET, "utf-8"),
            "color-supported": _v(T_BOOLEAN, False),
            "compression-supported": _v(T_KEYWORD, "none"),
            "copies-default": _v(T_INTEGER, 1),
            "copies-supported": _v(T_RANGE, (1, 50)),
            "document-format-default": _v(T_MIME, "application/octet-stream"),
            "document-format-supported": _v(
                T_MIME, *SUPPORTED_FORMATS),
            "finishings-default": _v(T_ENUM, 3),
            "finishings-supported": _v(T_ENUM, 3),
            "generated-natural-language-supported": _v(T_LANG, "en"),
            "identify-actions-default": _v(T_KEYWORD, "display"),
            "identify-actions-supported": _v(T_KEYWORD, "display"),
            "ipp-features-supported": _v(T_KEYWORD, "ipp-everywhere"),
            "ipp-versions-supported": _v(T_KEYWORD, "1.1", "2.0"),
            "job-creation-attributes-supported": _v(
                T_KEYWORD, "copies", "media", "media-col", "orientation-requested",
                "print-color-mode", "print-quality", "printer-resolution",
                "sides", "print-scaling"),
            "job-ids-supported": _v(T_BOOLEAN, True),
            "media-bottom-margin-supported": _v(T_INTEGER, 0),
            "media-left-margin-supported": _v(T_INTEGER, 0),
            "media-right-margin-supported": _v(T_INTEGER, 0),
            "media-top-margin-supported": _v(T_INTEGER, 0),
            "media-col-default": [(T_BEGCOL, default_col)],
            "media-col-ready": [(T_BEGCOL, default_col)],
            "media-col-database": media_cols,
            "media-col-supported": _v(
                T_KEYWORD, "media-bottom-margin", "media-left-margin",
                "media-right-margin", "media-size", "media-source",
                "media-top-margin", "media-type"),
            "media-default": _v(T_KEYWORD, DEFAULT_MEDIA),
            "media-ready": _v(T_KEYWORD, DEFAULT_MEDIA),
            "media-supported": _v(T_KEYWORD, *media_names),
            "media-size-supported": [
                (T_BEGCOL, Col({"x-dimension": _v(T_INTEGER, w),
                                "y-dimension": _v(T_INTEGER, l)}))
                for _, w, l in MEDIA],
            "media-source-supported": _v(T_KEYWORD, "main-roll"),
            "media-type-supported": _v(T_KEYWORD, "labels", "stationery"),
            "multiple-document-jobs-supported": _v(T_BOOLEAN, False),
            "multiple-operation-time-out": _v(T_INTEGER, 60),
            "natural-language-configured": _v(T_LANG, "en"),
            "operations-supported": _v(T_ENUM, *SUPPORTED_OPS),
            "orientation-requested-default": _v(T_ENUM, 3),
            "orientation-requested-supported": _v(T_ENUM, 3, 4),
            "output-bin-default": _v(T_KEYWORD, "face-up"),
            "output-bin-supported": _v(T_KEYWORD, "face-up"),
            "pdl-override-supported": _v(T_KEYWORD, "attempted"),
            "print-color-mode-default": _v(T_KEYWORD, "monochrome"),
            "print-color-mode-supported": _v(T_KEYWORD, "auto", "monochrome"),
            "print-quality-default": _v(T_ENUM, 4),
            "print-quality-supported": _v(T_ENUM, 4),
            "print-scaling-default": _v(T_KEYWORD, "auto"),
            "print-scaling-supported": _v(T_KEYWORD, "auto", "fit", "fill",
                                          "none"),
            "printer-device-id": _v(
                T_TEXT, "MFG:Core Innovations;MDL:CTP500;CMD:PWGRaster;"
                        "CLS:PRINTER;DES:CTP500 thermal printer bridge;"),
            "printer-info": _v(T_TEXT, "CTP500 Bluetooth thermal printer"),
            "printer-is-accepting-jobs": _v(T_BOOLEAN, True),
            "printer-kind": _v(T_KEYWORD, "document", "labels"),
            "printer-location": _v(T_TEXT, "Bluetooth"),
            "printer-make-and-model": _v(T_TEXT, "Core Innovations CTP500"),
            "printer-more-info": _v(T_URI, http_uri),
            "printer-name": _v(T_NAME, self.name),
            "printer-resolution-default": [
                (T_RESOLUTION, (DEFAULT_RESOLUTION, DEFAULT_RESOLUTION, 3))],
            "printer-resolution-supported": res,
            "printer-state": _v(T_ENUM, state),
            "printer-state-message": _v(
                T_TEXT, "Printing" if self.busy else "Ready"),
            "printer-state-reasons": _v(T_KEYWORD, "none"),
            "printer-up-time": _v(T_INTEGER,
                                  max(1, int(time.time() - self.start_time))),
            "printer-uri-supported": _v(T_URI, uri),
            "printer-uuid": _v(T_URI, self.uuid),
            "pages-per-minute": _v(T_INTEGER, 4),
            "preferred-attributes-supported": _v(T_BOOLEAN, False),
            "multiple-operation-time-out-action": _v(T_KEYWORD, "process-job"),
            "overrides-supported": _v(T_KEYWORD, "document-number", "pages"),
            "print-content-optimize-default": _v(T_KEYWORD, "auto"),
            "print-content-optimize-supported": _v(T_KEYWORD, "auto", "photo",
                                                   "text", "graphic"),
            "print-rendering-intent-default": _v(T_KEYWORD, "auto"),
            "print-rendering-intent-supported": _v(T_KEYWORD, "auto"),
            "printer-config-change-date-time": _v(T_DATETIME, self.start_time),
            "printer-config-change-time": _v(T_INTEGER, 1),
            "printer-state-change-date-time": _v(T_DATETIME, self.state_changed),
            "printer-state-change-time": _v(
                T_INTEGER, max(1, int(self.state_changed - self.start_time))),
            "printer-current-time": _v(T_DATETIME, time.time()),
            "printer-geo-location": [(T_UNKNOWN, None)],
            "printer-get-attributes-supported": _v(T_KEYWORD, "document-format"),
            "printer-icons": _v(T_URI, http_uri + "icon.png"),
            "printer-organization": _v(T_TEXT, ""),
            "printer-organizational-unit": _v(T_TEXT, ""),
            "printer-supply": _v(
                T_OCTET, b"index=1;class=supplyThatIsConsumed;type=other;"
                         b"unit=percent;maxcapacity=100;level=-3;"
                         b"colorantname=black;"),
            "printer-supply-description": _v(T_TEXT, "Thermal paper roll"),
            "printer-supply-info-uri": _v(T_URI, http_uri),
            "pwg-raster-document-resolution-supported": res,
            "pwg-raster-document-sheet-back": _v(T_KEYWORD, "normal"),
            "pwg-raster-document-type-supported": _v(
                T_KEYWORD, "black_1", "sgray_8", "srgb_8"),
            "queued-job-count": _v(T_INTEGER, queued),
            "sides-default": _v(T_KEYWORD, "one-sided"),
            "sides-supported": _v(T_KEYWORD, "one-sided"),
            "uri-authentication-supported": _v(T_KEYWORD, "none"),
            "uri-security-supported": _v(T_KEYWORD, "none"),
            "which-jobs-supported": _v(T_KEYWORD, "completed", "not-completed",
                                       "all"),
        }

    def job_attributes(self, job, host_header=None):
        return {
            "job-id": _v(T_INTEGER, job.id),
            "job-uri": _v(T_URI, self.job_uri(job, host_header)),
            "job-printer-uri": _v(T_URI, self.printer_uri(host_header)),
            "job-name": _v(T_NAME, job.name),
            "job-originating-user-name": _v(T_NAME, job.user),
            "job-state": _v(T_ENUM, job.state),
            "job-state-reasons": _v(T_KEYWORD, *job.reasons),
            "job-state-message": _v(T_TEXT, job.message or "OK"),
            "job-printer-up-time": _v(T_INTEGER,
                                      max(1, int(time.time() - self.start_time))),
            "time-at-creation": _v(T_INTEGER, job.created - int(self.start_time)),
            "time-at-processing": _v(T_INTEGER, 0),
            "time-at-completed": (_v(T_INTEGER, job.completed - int(self.start_time))
                                  if job.completed else [(T_NOVALUE, None)]),
            "job-impressions-completed": _v(T_INTEGER, job.pages),
        }

    # -- request handling -----------------------------------------------------
    def handle(self, body, host_header):
        try:
            version, op, req_id, groups, data = parse_ipp(body)
        except Exception as e:
            log.warning("Bad IPP request: %s", e)
            return build_response(S_BAD_REQUEST, 0, [(G_OPERATION, std_op_attrs())])
        if version[0] not in (1, 2):
            return build_response(0x0503, req_id,
                                  [(G_OPERATION, std_op_attrs())], (1, 1))
        if req_id == 0:
            return build_response(S_BAD_REQUEST, 0,
                                  [(G_OPERATION, std_op_attrs())],
                                  version if version[0] in (1, 2) else (2, 0))
        op_attrs = {}
        job_tmpl = {}
        for gtag, attrs in groups:
            if gtag == G_OPERATION:
                op_attrs.update(attrs)
            elif gtag == G_JOB:
                job_tmpl.update(attrs)
        resp_version = version if version[0] in (1, 2) else (2, 0)
        first_names = list(groups[0][1])[:2] if groups else []
        if not groups or groups[0][0] != G_OPERATION or first_names != [
                "attributes-charset", "attributes-natural-language"] or (
                "printer-uri" not in op_attrs and "job-uri" not in op_attrs):
            return build_response(S_BAD_REQUEST, req_id,
                                  [(G_OPERATION, std_op_attrs())], resp_version)
        log.debug("IPP op 0x%04X request %d", op, req_id)
        try:
            status, extra = self.dispatch(op, op_attrs, job_tmpl, data,
                                          host_header)
        except Exception as e:  # never crash the server
            log.exception("Error handling operation 0x%04X", op)
            status, extra = S_INTERNAL, [(G_OPERATION, {
                "status-message": _v(T_TEXT, str(e)[:200])})]
        out_groups = [(G_OPERATION, std_op_attrs())]
        for gtag, attrs in extra:
            if gtag == G_OPERATION:
                out_groups[0][1].update(attrs)
            else:
                out_groups.append((gtag, attrs))
        return build_response(status, req_id, out_groups, resp_version)

    def dispatch(self, op, op_attrs, job_tmpl, data, host):
        if op == OP_GET_PRINTER_ATTRS:
            req = [v for _, v in op_attrs.get("requested-attributes", [])]
            attrs = filter_attrs(self.printer_attributes(host), req)
            return S_OK, [(G_PRINTER, attrs)]

        if op in (OP_VALIDATE_JOB, OP_VALIDATE_DOC):
            fmt = first(op_attrs, "document-format")
            if not format_supported(fmt):
                return S_FORMAT, [(G_OPERATION, {
                    "status-message": _v(T_TEXT, f"Unsupported format {fmt}")})]
            return S_OK, []

        if op == OP_IDENTIFY:
            return S_OK, []

        if op in (OP_PRINT_JOB, OP_CREATE_JOB):
            fmt = first(op_attrs, "document-format")
            if op == OP_PRINT_JOB and not format_supported(fmt):
                return S_FORMAT, [(G_OPERATION, {
                    "status-message": _v(T_TEXT, f"Unsupported format {fmt}")})]
            job = self.new_job(op_attrs, job_tmpl)
            if op == OP_PRINT_JOB:
                self.queue.put((job, data,
                                first(op_attrs, "document-format",
                                      "application/octet-stream")))
            log.info("Job %d received: %r from %s", job.id, job.name, job.user)
            return S_OK, [(G_JOB, self.short_job_attrs(job, host))]

        if op == OP_SEND_DOCUMENT:
            job = self.find_job(op_attrs)
            if not job:
                return S_NOT_FOUND, []
            if "last-document" not in op_attrs:
                return S_BAD_REQUEST, [(G_OPERATION, {
                    "status-message": _v(T_TEXT, "Missing last-document")})]
            if not format_supported(first(op_attrs, "document-format")):
                return S_FORMAT, []
            if data:
                self.queue.put((job, data, first(op_attrs, "document-format",
                                                 "application/octet-stream")))
            elif job.state == J_PENDING:
                self.finish(job, J_COMPLETED, "No document data")
            return S_OK, [(G_JOB, self.short_job_attrs(job, host))]

        if op == OP_CLOSE_JOB:
            job = self.find_job(op_attrs)
            return (S_OK if job else S_NOT_FOUND), []

        if op == OP_CANCEL_JOB:
            job = self.find_job(op_attrs)
            if not job:
                return S_NOT_FOUND, []
            if job.state in (J_COMPLETED, J_CANCELED, J_ABORTED):
                return S_NOT_POSSIBLE, []
            self.finish(job, J_CANCELED, "Canceled by user",
                        reason="job-canceled-by-user")
            return S_OK, []

        if op == OP_CANCEL_MY_JOBS:
            user = first(op_attrs, "requesting-user-name")
            with self.lock:
                mine = [j for j in self.jobs.values()
                        if j.state in (J_PENDING, J_PROCESSING)
                        and (user is None or j.user == user)]
            for j in mine:
                self.finish(j, J_CANCELED, "Canceled by user",
                            reason="job-canceled-by-user")
            return S_OK, []

        if op == OP_GET_JOB_ATTRS:
            job = self.find_job(op_attrs)
            if not job:
                return S_NOT_FOUND, []
            req = [v for _, v in op_attrs.get("requested-attributes", [])]
            return S_OK, [(G_JOB, filter_attrs(self.job_attributes(job, host), req))]

        if op == OP_GET_JOBS:
            which = first(op_attrs, "which-jobs", "not-completed")
            req = [v for _, v in op_attrs.get("requested-attributes", [])] or \
                ["job-id", "job-uri"]
            with self.lock:
                jobs = sorted(self.jobs.values(), key=lambda j: -j.id)
            done = (J_COMPLETED, J_CANCELED, J_ABORTED)
            if which == "completed":
                jobs = [j for j in jobs if j.state in done]
            elif which != "all":
                jobs = [j for j in jobs if j.state not in done]
            return S_OK, [(G_JOB, filter_attrs(self.job_attributes(j, host), req))
                          for j in jobs[:50]]

        return S_OP_UNSUPPORTED, []

    def new_job(self, op_attrs, job_tmpl):
        with self.lock:
            job = Job(self.next_job_id, first(op_attrs, "job-name"),
                      first(op_attrs, "requesting-user-name"))
            self.next_job_id += 1
            self.jobs[job.id] = job
            # Forget old finished jobs.
            if len(self.jobs) > 200:
                for jid in sorted(self.jobs)[:50]:
                    del self.jobs[jid]
        job.media = first(job_tmpl, "media")
        return job

    def find_job(self, op_attrs):
        jid = first(op_attrs, "job-id")
        if jid is None:
            uri = first(op_attrs, "job-uri", "")
            m = re.search(r"/(\d+)$", uri or "")
            jid = int(m.group(1)) if m else None
        with self.lock:
            return self.jobs.get(jid)

    def short_job_attrs(self, job, host):
        a = self.job_attributes(job, host)
        return {k: a[k] for k in ("job-id", "job-uri", "job-state",
                                   "job-state-reasons", "job-state-message")}

    def finish(self, job, state, message="", reason=None):
        job.state = state
        job.message = message
        job.completed = int(time.time())
        job.reasons = [reason or {J_COMPLETED: "job-completed-successfully",
                                  J_CANCELED: "job-canceled-by-user",
                                  J_ABORTED: "aborted-by-system"}[state]]

    # -- background worker ----------------------------------------------------
    def worker(self):
        while True:
            try:
                job, data, fmt = self.queue.get(timeout=5)
            except queue.Empty:
                self.link.close_if_idle(self.idle_disconnect)
                continue
            if job.state == J_CANCELED:
                continue
            job.state = J_PROCESSING
            job.reasons = ["job-printing"]
            self.busy = True
            try:
                pages = decode_document(data, fmt)
                images = []
                for p in pages:
                    if job.state == J_CANCELED:
                        break
                    im = prepare_page(p, self.opts)
                    if im is not None:
                        images.append(im)
                if job.state != J_CANCELED:
                    log.info("Job %d: printing %d page(s)", job.id, len(images))
                    self.link.print_images(images, job.name)
                    job.pages = len(images)
                    self.finish(job, J_COMPLETED, "Printed")
                    log.info("Job %d done.", job.id)
            except Exception as e:
                log.error("Job %d failed: %s", job.id, e)
                self.finish(job, J_ABORTED, str(e)[:200])
            finally:
                self.busy = False

    def serve_forever(self):
        threading.Thread(target=self.worker, daemon=True).start()
        bridge = self

        class Handler(IPPHandler):
            server_bridge = bridge

        httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        httpd.daemon_threads = True
        log.info("CTP500 bridge %s listening on http://%s:%d/ipp/print",
                 __version__, self.host, self.port)
        if self.link.dry_run_dir:
            log.info("DRY RUN: pages are saved to %s instead of printed",
                     os.path.abspath(self.link.dry_run_dir))
        else:
            log.info("Printer Bluetooth address: %s", self.link.mac)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log.info("Stopping.")
        finally:
            httpd.server_close()
            self.link.close()


def media_col(m):
    name, w, l = m
    return Col({
        "media-size": [(T_BEGCOL, Col({"x-dimension": _v(T_INTEGER, w),
                                       "y-dimension": _v(T_INTEGER, l)}))],
        "media-top-margin": _v(T_INTEGER, 0),
        "media-bottom-margin": _v(T_INTEGER, 0),
        "media-left-margin": _v(T_INTEGER, 0),
        "media-right-margin": _v(T_INTEGER, 0),
        "media-source": _v(T_KEYWORD, "main-roll"),
        "media-type": _v(T_KEYWORD, "labels" if w < 10000 else "stationery"),
    })


def std_op_attrs():
    return {"attributes-charset": _v(T_CHARSET, "utf-8"),
            "attributes-natural-language": _v(T_LANG, "en")}


GROUP_KEYWORDS = {"all", "printer-description", "job-template",
                  "job-description", "job-status", "media-col-database"}


def filter_attrs(attrs, requested):
    if not requested or "all" in requested or \
            any(r in ("printer-description", "job-template",
                      "job-description", "job-status") for r in requested):
        return attrs
    wanted = set(requested)
    return {k: v for k, v in attrs.items() if k in wanted}


class IPPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"CTP500-Bridge/{__version__}"
    server_bridge = None  # set by subclass

    def log_message(self, fmt, *args):
        log.debug("HTTP %s - %s", self.address_string(), fmt % args)

    def _read_body(self):
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            body = bytearray()
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    break
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    # Trailers end with an empty line.
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass
                    break
                body += self.rfile.read(size)
                self.rfile.readline(65537)  # CRLF after chunk
            return bytes(body)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_POST(self):
        body = self._read_body()
        if self.headers.get("Content-Type", "").split(";")[0].strip() \
                != "application/ipp":
            self.send_error(400, "Expected application/ipp")
            return
        host = self.headers.get("Host")
        reply = self.server_bridge.handle(body, host)
        self.send_response(200)
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def do_GET(self):
        if self.path.startswith("/icon.png"):
            data = make_icon()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        b = self.server_bridge
        with b.lock:
            jobs = sorted(b.jobs.values(), key=lambda j: -j.id)[:15]
        names = {J_PENDING: "waiting", J_PROCESSING: "printing",
                 J_COMPLETED: "printed", J_CANCELED: "canceled",
                 J_ABORTED: "failed"}
        rows = "".join(
            f"<tr><td>{j.id}</td><td>{_esc(j.name)}</td>"
            f"<td>{names.get(j.state, '?')}</td><td>{_esc(j.message)}</td></tr>"
            for j in jobs) or "<tr><td colspan=4>No jobs yet</td></tr>"
        target = (f"dry run &rarr; {_esc(os.path.abspath(b.link.dry_run_dir))}"
                  if b.link.dry_run_dir else _esc(b.link.mac or ""))
        page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>CTP500 Bridge</title><meta name="viewport" content="width=device-width">
<style>body{{font:15px system-ui,sans-serif;max-width:640px;margin:24px auto;
padding:0 16px;color:#222}}td,th{{padding:4px 8px;border-bottom:1px solid #ddd;
text-align:left}}table{{border-collapse:collapse;width:100%}}code{{background:#eee;
padding:1px 4px}}</style></head><body>
<h1>CTP500 Bridge</h1>
<p>Status: <b>{"printing" if b.busy else "ready"}</b><br>
Printer: <code>{target}</code><br>
Windows printer URL: <code>http://localhost:{b.port}/ipp/print</code></p>
<h2>Recent jobs</h2><table><tr><th>#</th><th>Name</th><th>State</th>
<th>Note</th></tr>{rows}</table></body></html>""".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


_ICON = None


def make_icon():
    """A simple printer icon, drawn once."""
    global _ICON
    if _ICON is None:
        im = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((14, 40, 114, 100), 14, fill=(70, 74, 82, 255))
        d.rectangle((36, 18, 92, 46), fill=(255, 255, 255, 255),
                    outline=(70, 74, 82, 255), width=4)
        d.rectangle((36, 86, 92, 122), fill=(255, 255, 255, 255),
                    outline=(70, 74, 82, 255), width=4)
        for y in (96, 104, 112):
            d.line((44, y, 84, y), fill=(70, 74, 82, 255), width=3)
        d.ellipse((94, 52, 104, 62), fill=(120, 220, 140, 255))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        _ICON = buf.getvalue()
    return _ICON


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


# ==========================================================================
# 6. Finding the printer's Bluetooth address on Windows
# ==========================================================================

def find_mac(name_hint="Stone"):
    """List paired Bluetooth devices via PowerShell and pull out addresses."""
    if os.name != "nt":
        print("find-mac only works on Windows. Double-press the printer's "
              "power button to print its address instead.")
        return []
    ps = ("Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
          "Select-Object FriendlyName,InstanceId | ConvertTo-Json")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=30).stdout
        devices = json.loads(out or "[]")
    except Exception as e:
        print("Could not query Windows:", e)
        return []
    if isinstance(devices, dict):
        devices = [devices]
    found = []
    for d in devices:
        name = d.get("FriendlyName") or ""
        inst = d.get("InstanceId") or ""
        m = re.search(r"(?:DEV_|&)([0-9A-F]{12})(?:_|\b)", inst, re.I)
        if not m:
            continue
        hexaddr = m.group(1).upper()
        mac = ":".join(hexaddr[i:i + 2] for i in range(0, 12, 2))
        found.append((name, mac))
    seen = set()
    for name, mac in found:
        if (name, mac) in seen:
            continue
        seen.add((name, mac))
        mark = "  <-- probably your printer" if name_hint.lower() in name.lower() else ""
        print(f"{mac}  {name}{mark}")
    if not found:
        print("No paired Bluetooth devices found. Pair the printer in "
              "Settings > Bluetooth & devices first, or double-press its "
              "power button to print its address.")
    return found


# ==========================================================================
# 7. Command line
# ==========================================================================

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "ctp500_config.json")


def load_config():
    try:
        with open(config_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def add_common(p):
    p.add_argument("--mac", help="printer Bluetooth address, e.g. "
                                 "AA:BB:CC:DD:EE:FF (saved for next time)")
    p.add_argument("--dry-run", metavar="FOLDER",
                   help="save pages as PNG files instead of printing")
    p.add_argument("--rotate", action="store_true",
                   help="print upside-down (if your prints come out reversed)")
    p.add_argument("--dither", choices=["auto", "always", "never"],
                   default="auto", help="photo dithering (default: auto)")
    p.add_argument("--no-autocrop", action="store_true",
                   help="keep the page's own margins instead of cropping "
                        "to the content")
    p.add_argument("--darkness", type=float, default=1.0,
                   help="1.0 = normal, 1.3 = darker, 0.8 = lighter")
    p.add_argument("--band-height", type=int, default=0,
                   help="send long pages in bands of this many lines "
                        "(try 200 if long prints get cut off)")
    p.add_argument("-v", "--verbose", action="store_true")


def make_link(args):
    cfg = load_config()
    mac = args.mac or cfg.get("mac")
    if args.mac:
        if not MAC_RE.match(args.mac):
            sys.exit(f"'{args.mac}' doesn't look like a Bluetooth address "
                     "(expected AA:BB:CC:DD:EE:FF).")
        mac = args.mac.replace("-", ":").upper()
        if cfg.get("mac") != mac:
            cfg["mac"] = mac
            save_config(cfg)
    if not mac and not args.dry_run:
        sys.exit("No printer address yet. Run with --mac AA:BB:CC:DD:EE:FF "
                 "(use 'find-mac', or double-press the printer's power "
                 "button to print it).")
    return PrinterLink(mac=mac, dry_run_dir=args.dry_run, rotate=args.rotate,
                       band_height=args.band_height)


def make_opts(args):
    return PageOptions(autocrop=not args.no_autocrop, dither=args.dither,
                       darkness=args.darkness)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Use a CTP500 Bluetooth thermal printer from Windows.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the print server for Windows")
    add_common(s)
    s.add_argument("--port", type=int, default=631)
    s.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default: this PC only)")
    s.add_argument("--name", default="CTP500 Thermal Printer")
    s.add_argument("--idle-disconnect", type=int, default=60,
                   help="seconds before dropping an idle Bluetooth link")
    s.add_argument("--log-file", help="also write the log to this file")

    p = sub.add_parser("print", help="print image files directly")
    add_common(p)
    p.add_argument("files", nargs="+")

    t = sub.add_parser("text", help="print a line of text directly")
    add_common(t)
    t.add_argument("text")
    t.add_argument("--size", type=int, default=28, help="font size")

    sub.add_parser("find-mac", help="list paired Bluetooth devices (Windows)")

    args = ap.parse_args(argv)

    # pythonw.exe (no console) has no stderr, so only log there when it exists.
    handlers = [logging.StreamHandler()] if sys.stderr else []
    if getattr(args, "log_file", None):
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    if not handlers:
        handlers.append(logging.NullHandler())
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
        handlers=handlers)

    if args.cmd == "find-mac":
        find_mac()
        return

    link = make_link(args)
    opts = make_opts(args)

    if args.cmd == "serve":
        server = BridgeServer(link, opts, host=args.host, port=args.port,
                              name=args.name,
                              idle_disconnect=args.idle_disconnect)
        try:
            server.serve_forever()
        except OSError as e:
            sys.exit(f"Could not start the server on port {args.port}: {e}\n"
                     "Is the bridge already running? Try another --port.")
        return

    if args.cmd == "print":
        images = []
        for f in args.files:
            with open(f, "rb") as fh:
                data = fh.read()
            for page in decode_document(data, "application/octet-stream"):
                im = prepare_page(page, opts)
                if im is not None:
                    images.append(im)
        link.print_images(images, os.path.basename(args.files[0]))
    elif args.cmd == "text":
        img = text_to_image(args.text.replace("\\n", "\n"), args.size)
        opts.autocrop = False
        opts.dither = "never"
        page = prepare_page(img, opts)
        if page is None:
            sys.exit("Nothing to print.")
        link.print_images([page], "text")
    link.close()
    log.info("Done.")


if __name__ == "__main__":
    main()
