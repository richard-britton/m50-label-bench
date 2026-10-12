#!/usr/bin/env python3
"""M50 Label Bench: the bridge (print server).

Prints to a Kodak Label Era M50 over Bluetooth Classic (RFCOMM) using the host's
adapter, serves the label designer page, and keeps a shared library of saved labels.
Unofficial; not affiliated with Kodak or Prinics.

Two ways to reach Bluetooth (BT_MODE):
  bluez   the host's bluetoothd opens the connection and hands it over D-Bus, so the container
          runs on a normal network with a mapped port; it only needs /run/dbus mounted
  socket  the bridge opens the RFCOMM socket itself, which Linux only allows on the host network
  auto    socket when it works, otherwise bluez (the default)

Printer protocol, decoded from the Kodak app:
  status   -> 10 ff 70, answered by "M50|name|mac|mac|firmware|serial|battery|state|..mm"
  one job  -> 12 x 00, 10 ff f1 02, ESC/POS raster (1d 76 30 00, bytes/line, lines, data),
              1d 0c (feed to next label), 10 ff f1 45 (end of job)
  done     -> the printer sends 0xaa when the label has finished

The link is opened for each request and closed straight after, so the printer is
never held on a connection and phones can still use it in between.
"""
import base64
import errno
import hmac
import io
import json
import os
import re
import socket
import struct
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

import bluez
import render

VERSION = "1.3.0"


def env(name, default=""):
    return os.environ.get(name, default).strip()


CFG = {
    "mac": env("PRINTER_MAC").upper(),             # blank: find the paired printer by name (bluez mode)
    "name_prefix": env("PRINTER_NAME", "Kodak Label"),
    "channel": int(env("RFCOMM_CHANNEL", "1")),
    "bind": env("BIND", "0.0.0.0"),
    "port": int(env("PORT", "8091")),
    "token": env("API_TOKEN"),
    "head": int(env("HEAD_DOTS", "112")),
    "length_mm": float(env("DEFAULT_LENGTH_MM", "30")),
    "offset_mm": float(env("OFFSET_MM", "0")),
    "rotation": env("ROTATION", "cw").lower(),
    "web_root": env("WEB_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")),
    "data_dir": env("DATA_DIR", "/data"),
    "fake": env("FAKE_PRINTER", "0") == "1",
    "bt_mode": env("BT_MODE", "auto").lower(),
}

STATUS_CMD = b"\x10\xff\x70"
JOB_START = bytes(12) + b"\x10\xff\xf1\x02"
JOB_END = b"\x1d\x0c\x10\xff\xf1\x45"
DONE = 0xAA
STATUS_RE = re.compile(
    r"([A-Za-z0-9]+)\|([^|]+)\|([0-9A-Fa-f:]{17})\|([0-9A-Fa-f:]{17})\|([^|]+)\|([^|]+)\|(\d+)\|([^|]+)\|(\d+mm)"
)


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


class PrinterError(Exception):
    """An error with a stable code the page and Home Assistant can act on."""

    HTTP = {"unreachable": 503, "no_adapter": 503, "busy": 409, "not_ready": 409, "pairing": 503,
            "timeout": 504, "config": 500, "bad_request": 400}

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- transport

class RfcommLink:
    def __init__(self, mac, channel):
        self.mac, self.channel, self.sock = mac, channel, None

    def open(self, timeout=10.0):
        if not hasattr(socket, "AF_BLUETOOTH"):
            raise PrinterError("no_adapter", "This Python build has no Bluetooth support.")
        try:
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        except OSError as e:
            raise PrinterError("no_adapter", f"Could not create a Bluetooth socket ({e.strerror}). Either run the "
                                             "container with network_mode: host, or mount /run/dbus and set "
                                             "BT_MODE=bluez (see the README).")
        s.settimeout(timeout)
        try:
            s.connect((self.mac, self.channel))
        except OSError as e:
            s.close()
            raise _map_connect_error(e, self.mac)
        self.sock = s

    def send(self, data):
        self.sock.sendall(data)

    def recv(self, timeout):
        self.sock.settimeout(max(0.05, timeout))
        try:
            return self.sock.recv(1024)
        except (socket.timeout, TimeoutError):
            return b""

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None


def _map_connect_error(e, mac):
    no = getattr(e, "errno", None)
    if isinstance(e, (socket.timeout, TimeoutError)) or no in (errno.EHOSTDOWN, errno.EHOSTUNREACH, errno.ETIMEDOUT):
        return PrinterError("unreachable", "The printer is off, asleep or out of range of the bridge.")
    if no == errno.ECONNREFUSED:
        return PrinterError("busy", "The printer refused the connection. It may be connected to a phone; "
                                    "close the page or the Kodak app there and try again.")
    if no in (errno.EACCES, errno.EPERM):
        return PrinterError("pairing", f"The bridge needs to pair with the printer once: bluetoothctl pair {mac}")
    if no in (errno.EBUSY, errno.EALREADY, errno.EINPROGRESS):
        return PrinterError("busy", "The Bluetooth adapter is busy. Try again in a moment.")
    if no in (errno.ENODEV, errno.EADDRNOTAVAIL, errno.ENETDOWN):
        return PrinterError("no_adapter", "No working Bluetooth adapter found on the bridge host.")
    return PrinterError("unreachable", f"Could not reach the printer: {e}")


class BluezLink(RfcommLink):
    """The same link, but opened by the host's bluetoothd and handed over D-Bus."""

    def __init__(self, mac, channel):
        super().__init__(mac, channel)
        self.device = None

    def open(self, timeout=10.0):
        try:
            # BlueZ gives up on its own after up to ~45 s (a service lookup that times out). Waiting
            # that long avoids abandoning an attempt it is still making, which makes the next one "busy".
            fd, self.device = BLUEZ.connect(self.mac, timeout=bluez.CONNECT_TIMEOUT)
        except bluez.BluezError as e:
            raise PrinterError(e.code, str(e))
        self.sock = socket.socket(fileno=fd)
        self.sock.settimeout(timeout)

    def close(self):
        sock, self.sock = self.sock, None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # bluetoothd holds a copy of the socket; this ends the link for both
            except OSError:
                pass
            sock.close()
            BLUEZ.release(self.device)


def choose_transport(cfg):
    if cfg["fake"]:
        return "fake"
    if cfg["bt_mode"] in ("bluez", "socket"):
        return cfg["bt_mode"]
    if hasattr(socket, "AF_BLUETOOTH"):
        try:
            socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM).close()
            return "socket"
        except OSError:
            pass  # not on the host network
    return "bluez" if bluez.bus_available() else "socket"


class FakeLink:
    """Simulated printer for testing without hardware (FAKE_PRINTER=1)."""

    jobs = []
    state = "ready"
    off = False

    def __init__(self, mac, channel):
        self.out = bytearray()
        self.pending = []  # (ready_at, bytes)
        self.buf = bytearray()

    def open(self, timeout=10.0):
        if FakeLink.off:
            time.sleep(0.2)
            raise PrinterError("unreachable", "The printer is off, asleep or out of range of the bridge.")

    def send(self, data):
        now = time.time()
        if data == STATUS_CMD:
            line = f"M50|Kodak Label_0000|00:00:00:00:00:01|00:00:00:00:00:02|VP2.13|M50000000000|73|{FakeLink.state}|0mm"
            self.pending.append((now + 0.03, line.encode()))
            return
        self.buf += data
        if self.buf.endswith(JOB_END):
            FakeLink.jobs.append(bytes(self.buf))
            self.buf.clear()
            self.pending += [(now + 0.05, b"\xff\x0b"), (now + 0.6, b"\xaa")]

    def recv(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            ready = [p for p in self.pending if p[0] <= time.time()]
            if ready:
                self.pending.remove(ready[0])
                return ready[0][1]
            time.sleep(0.02)
        return b""

    def close(self):
        pass


# ---------------------------------------------------------------- printer

def parse_status(text):
    m = STATUS_RE.search(text)
    if not m:
        return None
    return {"model": m.group(1), "name": m.group(2), "firmware": m.group(5),
            "battery": int(m.group(7)), "state": m.group(8).strip(), "size": m.group(9)}


def build_job(img, head, offset_dots=0, rotation="cw"):
    """Turn a 1-bit image (width = label length, height = head dots) into one M50 print job."""
    if head % 8:
        raise PrinterError("config", "HEAD_DOTS must be a multiple of 8.")
    if img.mode != "1":
        img = img.convert("1")
    if img.height != head:
        raise PrinterError("bad_request", f"The image must be {head} dots tall; it is {img.height}.")
    # Each raster line runs across the printhead; the first line is the label's left edge.
    turned = img.transpose(Image.Transpose.ROTATE_270 if rotation != "ccw" else Image.Transpose.ROTATE_90)
    bpl, lines = head // 8, img.width
    data = bytearray(b ^ 0xFF for b in turned.tobytes())  # PIL stores white as 1; the printer wants black as 1
    if offset_dots:
        shift = min(abs(offset_dots), lines) * bpl
        blank = bytes(shift)
        data = (blank + data[:-shift]) if offset_dots > 0 else (data[shift:] + blank)
    return JOB_START + b"\x1d\x76\x30\x00" + struct.pack("<HH", bpl, lines) + bytes(data) + JOB_END


class M50:
    def __init__(self, cfg, transport):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.last = {"at": 0, "status": None, "error": None}
        self.link_cls = {"fake": FakeLink, "socket": RfcommLink, "bluez": BluezLink}[transport]

    def _open(self):
        if not self.cfg["mac"] and self.link_cls is RfcommLink:
            raise PrinterError("config", "Set PRINTER_MAC: the bridge needs the printer's address when it opens "
                                         "Bluetooth sockets itself (BT_MODE=socket).")
        link, start = self.link_cls(self.cfg["mac"], self.cfg["channel"]), time.time()
        for attempt in range(1, 4):  # Bluetooth Classic links often fail on the first try
            try:
                link.open()
                return link
            except PrinterError as e:
                # Retry quick failures; after a slow one (weak signal) another try would only make
                # the request take minutes.
                if e.code in ("config", "no_adapter", "pairing") or attempt == 3 or time.time() - start > 20:
                    raise
                time.sleep(4.0 if e.code == "busy" else 1.5)

    @staticmethod
    def _read_status(link, timeout=3.0):
        link.send(STATUS_CMD)
        buf, end = b"", time.time() + timeout
        while time.time() < end:
            buf += link.recv(end - time.time())
            st = parse_status(buf.decode("latin-1"))
            if st:
                return st
        return None

    @staticmethod
    def _wait_done(link, timeout=25.0):
        end = time.time() + timeout
        while time.time() < end:
            if DONE in link.recv(end - time.time()):
                return True
        return False

    def status(self, max_age=10.0):
        if time.time() - self.last["at"] < max_age:
            return self.last["status"], self.last["error"]
        with self.lock:
            st, err = None, None
            try:
                link = self._open()
                try:
                    st = self._read_status(link)
                    if not st:
                        err = PrinterError("timeout", "The printer connected but did not answer a status check.")
                finally:
                    link.close()
            except PrinterError as e:
                err = e
            self.last = {"at": time.time(), "status": st, "error": err}
            return st, err

    def print_image(self, img, copies=1, offset_mm=None):
        offset = self.cfg["offset_mm"] if offset_mm is None else float(offset_mm)
        job = build_job(img, self.cfg["head"], int(round(offset * render.DPMM)), self.cfg["rotation"])
        printed, st = 0, None
        with self.lock:
            link = self._open()
            try:
                for i in range(copies):
                    st = self._read_status(link)
                    if st and st["state"] != "ready":
                        raise PrinterError("not_ready", f"The printer reports “{st['state']}”. "
                                                        "Check the label roll and the lid.")
                    link.send(job)
                    if not self._wait_done(link):
                        raise PrinterError("timeout", f"Label {i + 1} was sent, but the printer did not confirm it "
                                                      "finished. If it stalls, switch it off and on.")
                    printed += 1
            finally:
                link.close()
                if st:
                    self.last = {"at": time.time(), "status": st, "error": None}
        log(f"printed {printed} label(s), {len(job)} bytes each, {img.width} lines")
        return printed, st


# ---------------------------------------------------------------- saved labels

class LabelStore:
    MAX_LABELS, MAX_BYTES = 300, 2_000_000
    ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "labels.json")
        self.lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f).get("labels", {})
        except FileNotFoundError:
            return {}

    def _save(self, labels):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"labels": labels}, f)
        os.replace(tmp, self.path)

    def list(self):
        with self.lock:
            return sorted(self._load().values(), key=lambda r: r.get("updated", 0), reverse=True)

    def put(self, label_id, record):
        if not self.ID_RE.match(label_id):
            raise PrinterError("bad_request", "Invalid label id.")
        if len(json.dumps(record)) > self.MAX_BYTES:
            raise PrinterError("bad_request", "That label is too large to save. Use a smaller image.")
        with self.lock:
            labels = self._load()
            if label_id not in labels and len(labels) >= self.MAX_LABELS:
                raise PrinterError("bad_request", "The label library is full. Delete some saved labels first.")
            rec = {k: record.get(k) for k in ("name", "thumb", "design")}
            rec.update(id=label_id, updated=int(time.time() * 1000))
            labels[label_id] = rec
            self._save(labels)
            return rec

    def delete(self, label_id):
        with self.lock:
            labels = self._load()
            labels.pop(label_id, None)
            self._save(labels)


# ---------------------------------------------------------------- HTTP

TRANSPORT = choose_transport(CFG)
BLUEZ = bluez.BluezSpp(log=log, name_prefix=CFG["name_prefix"])
PRINTER = M50(CFG, TRANSPORT)
STORE = LabelStore(CFG["data_dir"])
MAX_BODY = 8 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "M50Bridge/" + VERSION

    def log_message(self, fmt, *args):  # keep the container log to prints and errors
        pass

    # helpers
    def _send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _error(self, e):
        code = e.code if isinstance(e, PrinterError) else "internal"
        status = PrinterError.HTTP.get(code, 500)
        self._json(status, {"ok": False, "code": code, "error": str(e)})

    def _authorised(self):
        token = CFG["token"]
        if not token:
            return True
        given = self.headers.get("X-Label-Token") or ""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            given = given or auth[7:].strip()
        return hmac.compare_digest(given.encode(), token.encode())

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise PrinterError("bad_request", "The request is too large.")
        return self.rfile.read(n) if n else b""

    def _route(self):
        u = urlparse(self.path)
        return u.path.rstrip("/") or "/", {k: v[-1] for k, v in parse_qs(u.query).items()}

    # verbs
    def do_OPTIONS(self):
        self._send(204, extra={"Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                               "Access-Control-Allow-Headers": "Content-Type, X-Label-Token, Authorization",
                               "Access-Control-Max-Age": "86400"})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, q = self._route()
        try:
            if not path.startswith("/api"):
                return self._static(path)
            if path == "/api/health":
                return self._json(200, {"ok": True, "version": VERSION})
            if not self._authorised():
                return self._json(401, {"ok": False, "code": "token", "error": "Missing or wrong access token."})
            if path == "/api/status":
                return self._status(q)
            if path == "/api/labels":
                return self._json(200, {"ok": True, "labels": STORE.list()})
            if path == "/api/render.png":
                img = render.render_text(q.get("text", ""), float(q.get("length_mm", CFG["length_mm"])),
                                         CFG["head"], align=q.get("align", "center"),
                                         invert=q.get("invert") in ("1", "true"))
                out = io.BytesIO()
                img.convert("L").save(out, "PNG")
                return self._send(200, out.getvalue(), "image/png")
            if path == "/api/last-job.png" and CFG["fake"] and FakeLink.jobs:
                return self._send(200, _job_preview(FakeLink.jobs[-1]), "image/png")
            self._json(404, {"ok": False, "code": "not_found", "error": "Unknown endpoint."})
        except Exception as e:
            self._fail(e)

    def do_POST(self):
        path, q = self._route()
        try:
            if not self._authorised():
                return self._json(401, {"ok": False, "code": "token", "error": "Missing or wrong access token."})
            if path == "/api/print":
                return self._print(q)
            if path == "/api/fake" and CFG["fake"]:  # test hook: {"off": true, "state": "no paper"}
                body = json.loads(self._body() or b"{}")
                FakeLink.off = bool(body.get("off", FakeLink.off))
                FakeLink.state = body.get("state", FakeLink.state)
                PRINTER.last["at"] = 0
                return self._json(200, {"ok": True})
            self._json(404, {"ok": False, "code": "not_found", "error": "Unknown endpoint."})
        except Exception as e:
            self._fail(e)

    def do_PUT(self):
        path, _ = self._route()
        try:
            if not self._authorised():
                return self._json(401, {"ok": False, "code": "token", "error": "Missing or wrong access token."})
            m = re.fullmatch(r"/api/labels/([^/]+)", path)
            if not m:
                return self._json(404, {"ok": False, "code": "not_found", "error": "Unknown endpoint."})
            rec = STORE.put(m.group(1), json.loads(self._body() or b"{}"))
            self._json(200, {"ok": True, "label": {"id": rec["id"], "updated": rec["updated"]}})
        except Exception as e:
            self._fail(e)

    def do_DELETE(self):
        path, _ = self._route()
        try:
            if not self._authorised():
                return self._json(401, {"ok": False, "code": "token", "error": "Missing or wrong access token."})
            m = re.fullmatch(r"/api/labels/([^/]+)", path)
            if not m:
                return self._json(404, {"ok": False, "code": "not_found", "error": "Unknown endpoint."})
            STORE.delete(m.group(1))
            self._json(200, {"ok": True})
        except Exception as e:
            self._fail(e)

    # handlers
    def _fail(self, e):
        if not isinstance(e, PrinterError):
            log("error: " + "".join(traceback.format_exception_only(type(e), e)).strip())
            e = PrinterError("bad_request", str(e)) if isinstance(e, (ValueError, json.JSONDecodeError)) else e
        else:
            log(f"{e.code}: {e}")
        self._error(e)

    def _static(self, path):
        name = "index.html" if path in ("/", "/index.html") else None
        if path == "/favicon.ico":
            return self._send(204)
        if not name:
            return self._send(404, b"Not found", "text/plain")
        try:
            with open(os.path.join(CFG["web_root"], name), "rb") as f:
                page = f.read()
        except OSError as e:
            msg = _page_problem(e)
            log("error: " + msg)
            return self._send(500, msg.encode(), "text/plain; charset=utf-8")
        self._send(200, page, "text/html; charset=utf-8")

    def _status(self, q):
        info = {"ok": True, "version": VERSION, "printer_mac": CFG["mac"], "head_dots": CFG["head"],
                "default_length_mm": CFG["length_mm"], "fake": CFG["fake"], "transport": TRANSPORT}
        if TRANSPORT == "bluez":
            info["bluez"] = BLUEZ.state(CFG["mac"])
            info["adapter"] = any(a["powered"] for a in info["bluez"].get("adapters", []))
            info["printer_mac"] = CFG["mac"] or (info["bluez"].get("printer") or {}).get("address", "")
        else:
            info["adapter"] = CFG["fake"] or _sysfs_adapter()
        if q.get("probe", "1") in ("1", "true"):
            st, err = PRINTER.status(max_age=0 if q.get("fresh") == "1" else 10)
            info["printer"] = {"reachable": bool(st), **(st or {})}
            if err:
                info["printer"].update(code=err.code, error=str(err))
        self._json(200, info)

    def _print(self, q):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        body = self._body()
        copies = max(1, min(20, int(q.get("copies", 1))))
        offset = q.get("offset_mm")
        if ctype.startswith("image/"):
            img = render.prepare_image(Image.open(io.BytesIO(body)), CFG["head"])
        else:
            req = json.loads(body or b"{}")
            copies = max(1, min(20, int(req.get("copies", copies))))
            offset = req.get("offset_mm", offset)
            if req.get("image"):
                raw = req["image"].split(",", 1)[-1]
                img = render.prepare_image(Image.open(io.BytesIO(base64.b64decode(raw))), CFG["head"],
                                           req.get("length_mm"))
            elif str(req.get("text", "")).strip():
                img = render.render_text(req["text"], float(req.get("length_mm") or CFG["length_mm"]), CFG["head"],
                                         align=req.get("align", "center"), invert=bool(req.get("invert")))
            else:
                raise PrinterError("bad_request", "Send some text, an image, or a PNG body.")
        printed, st = PRINTER.print_image(img, copies, offset)
        self._json(200, {"ok": True, "printed": printed, "printer": st})


def _sysfs_adapter():
    return os.path.isdir("/sys/class/bluetooth") and bool(os.listdir("/sys/class/bluetooth"))


def _page_problem(e):
    if isinstance(e, PermissionError):
        return ("The label bridge can't read web/index.html (permission denied). On the host, in the "
                "label-bridge folder, run: chmod -R a+rX web")
    return f"The label bridge can't read web/index.html ({e.strerror}). Check the ./web volume in docker-compose.yml."


def _startup_checks():
    """Say in the log what's missing, so the first test print isn't the first sign of trouble."""
    try:
        with open(os.path.join(CFG["web_root"], "index.html"), "rb"):
            pass
    except OSError as e:
        log("warning: " + _page_problem(e))
    try:
        fd, tmp = tempfile.mkstemp(dir=CFG["data_dir"], suffix=".check")
        os.close(fd)
        os.remove(tmp)
    except OSError as e:
        log(f"warning: can't write to {CFG['data_dir']} ({e.strerror}); saving labels will fail. "
            "On the host: sudo chown -R root: ./data")
    if TRANSPORT == "socket" and not _sysfs_adapter():
        log("warning: no Bluetooth adapter visible in /sys/class/bluetooth")
    if TRANSPORT != "bluez":
        return
    st = BLUEZ.state(CFG["mac"])
    if not st["ok"]:
        log(f"warning: {st['error']}")
        return
    for a in st["adapters"]:
        log(f"bluez: adapter {a['name']} {a['address']} {'on' if a['powered'] else 'OFF (will be switched on when printing)'}")
    if not st["adapters"]:
        log("warning: BlueZ has no Bluetooth adapter. Check `bluetoothctl show` on the host.")
    p = st["printer"]
    if st.get("printer_error"):
        log("warning: " + st["printer_error"])
    elif not p:
        who = CFG["mac"] or f"a printer named \"{CFG['name_prefix']}…\""
        log(f"warning: the host's Bluetooth doesn't know {who} yet. Pair it once (see Pairing in the README).")
    elif not p["paired"]:
        log(f"warning: {p['address']} is known but not paired. Pair it (see Pairing in the README).")
    elif not p["serial_port"]:
        log(f"warning: {p['address']} is paired, but BlueZ hasn't read its services yet. With the printer on, run "
            f"`bluetoothctl connect {p['address']}` once on the host (see Pairing in the README).")
    else:
        log(f"bluez: printer {p['name'] or p['address']} ({p['address']}) is paired"
            + ("" if CFG["mac"] else ", found by name"))


def _job_preview(job):
    """Decode a captured job back into an image (fake mode only), to check orientation in tests."""
    i = job.find(b"\x1d\x76\x30\x00")
    bpl, lines = struct.unpack("<HH", job[i + 4:i + 8])
    data = bytes(b ^ 0xFF for b in job[i + 8:i + 8 + bpl * lines])
    img = Image.frombytes("1", (bpl * 8, lines), data).transpose(Image.Transpose.ROTATE_90)
    out = io.BytesIO()
    img.convert("L").save(out, "PNG")
    return out.getvalue()


def main():
    how = {"fake": "FAKE PRINTER", "socket": f"own RFCOMM socket, channel {CFG['channel']}",
           "bluez": f"host bluetoothd over D-Bus ({bluez.bus_address()})"}[TRANSPORT]
    printer = CFG["mac"] or (f"paired \"{CFG['name_prefix']}…\"" if TRANSPORT == "bluez" else "(PRINTER_MAC not set)")
    log(f"M50 bridge {VERSION} on {CFG['bind']}:{CFG['port']} · printer {printer}"
        f" · Bluetooth via {how} · token {'on' if CFG['token'] else 'off'}")
    _startup_checks()
    log(f"fonts: text {[os.path.basename(p) for p, _ in render.TEXT_FONTS]}, emoji {[os.path.basename(p) for p, _ in render.EMOJI_FONTS]}")
    ThreadingHTTPServer((CFG["bind"], CFG["port"]), Handler).serve_forever()


if __name__ == "__main__":
    main()
