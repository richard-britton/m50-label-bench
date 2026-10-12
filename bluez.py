"""Bluetooth through the host's bluetoothd, so the bridge doesn't need the host's network.

Linux only lets processes in the host's network namespace open Bluetooth sockets, whatever
capabilities they have. Instead of opening one itself, the bridge asks bluetoothd (over the
system D-Bus) to connect to the printer's serial port. bluetoothd opens the RFCOMM socket and
passes it back over D-Bus using BlueZ's Profile API. The container only needs /run/dbus mounted.

The D-Bus client here is deliberately small and uses only the standard library: EXTERNAL
authentication, file descriptor passing, method calls, and answering BlueZ's calls to the
profile object.
"""
import os
import socket
import struct
import threading
import time
from urllib.parse import unquote

SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
PROFILE_PATH = "/org/labelbridge/spp"
DEFAULT_BUS = "unix:path=/run/dbus/system_bus_socket"
CONNECT_TIMEOUT = 60.0  # longer than BlueZ's own worst case, so it always finishes an attempt first

APPARMOR_HINT = ("AppArmor is stopping the container from using the host's D-Bus. Keep "
                 "`apparmor:unconfined` under security_opt in docker-compose.yml (see the README).")


class DBusError(Exception):
    def __init__(self, name, text=""):
        super().__init__(f"{name}: {text}" if text else name)
        self.name, self.text = name, text


class BluezError(Exception):
    """Carries one of the bridge's error codes: unreachable, busy, pairing, no_adapter, config, timeout."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Variant:
    __slots__ = ("sig", "value")

    def __init__(self, sig, value):
        self.sig, self.value = sig, value

    def __repr__(self):
        return f"Variant({self.sig!r}, {self.value!r})"


def unwrap(v):
    return v.value if isinstance(v, Variant) else v


# ---------------------------------------------------------------- wire format

_ALIGN = {"y": 1, "b": 4, "n": 2, "q": 2, "i": 4, "u": 4, "x": 8, "t": 8, "d": 8, "h": 4,
          "s": 4, "o": 4, "g": 1, "v": 1, "a": 4, "(": 8, "{": 8}
_FMT = {"y": "B", "b": "I", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q", "t": "Q", "d": "d", "h": "I"}


def _type_end(sig, i):
    c = sig[i]
    if c == "a":
        return _type_end(sig, i + 1)
    if c in "({":
        close = ")" if c == "(" else "}"
        i += 1
        while sig[i] != close:
            i = _type_end(sig, i)
        return i + 1
    if c not in _ALIGN:
        raise ValueError(f"unsupported D-Bus type {c!r} in {sig!r}")
    return i + 1


def split_sig(sig):
    """Split a signature into its complete types: 'oha{sv}' -> ['o', 'h', 'a{sv}']."""
    out, i = [], 0
    while i < len(sig):
        j = _type_end(sig, i)
        out.append(sig[i:j])
        i = j
    return out


class _Writer:
    def __init__(self):
        self.buf = bytearray()

    def pad(self, n):
        self.buf += bytes(-len(self.buf) % n)

    def put(self, t, v):
        c = t[0]
        if c in _FMT:
            self.pad(_ALIGN[c])
            self.buf += struct.pack("<" + _FMT[c], (1 if v else 0) if c == "b" else v)
        elif c in "so":
            data = v.encode()
            self.pad(4)
            self.buf += struct.pack("<I", len(data)) + data + b"\0"
        elif c == "g":
            data = v.encode()
            self.buf += bytes([len(data)]) + data + b"\0"
        elif c == "v":
            self.put("g", v.sig)
            self.put(v.sig, v.value)
        elif c == "(":
            self.pad(8)
            for st, sv in zip(split_sig(t[1:-1]), v):
                self.put(st, sv)
        elif c == "a":
            et = t[1:]
            self.pad(4)
            at = len(self.buf)
            self.buf += bytes(4)
            self.pad(_ALIGN[et[0]])
            start = len(self.buf)
            if et[0] == "{":
                kt, vt = split_sig(et[1:-1])
                for k, val in v.items():
                    self.pad(8)
                    self.put(kt, k)
                    self.put(vt, val)
            elif et == "y":
                self.buf += bytes(v)
            else:
                for item in v:
                    self.put(et, item)
            struct.pack_into("<I", self.buf, at, len(self.buf) - start)
        else:
            raise ValueError(f"unsupported D-Bus type {t!r}")


class _Reader:
    def __init__(self, buf, pos, endian):
        self.buf, self.pos, self.e = buf, pos, endian

    def align(self, n):
        self.pos += -self.pos % n

    def get(self, t):
        c = t[0]
        if c in _FMT:
            self.align(_ALIGN[c])
            fmt = self.e + _FMT[c]
            (v,) = struct.unpack_from(fmt, self.buf, self.pos)
            self.pos += struct.calcsize(fmt)
            return bool(v) if c == "b" else v
        if c in "so":
            n = self.get("u")
            v = self.buf[self.pos:self.pos + n].decode("utf-8", "replace")
            self.pos += n + 1
            return v
        if c == "g":
            n = self.buf[self.pos]
            v = self.buf[self.pos + 1:self.pos + 1 + n].decode("ascii", "replace")
            self.pos += n + 2
            return v
        if c == "v":
            sig = self.get("g")
            return Variant(sig, self.get(sig))
        if c == "(":
            self.align(8)
            return tuple(self.get(st) for st in split_sig(t[1:-1]))
        if c == "a":
            n = self.get("u")
            et = t[1:]
            self.align(_ALIGN[et[0]])
            end = self.pos + n
            if et == "y":
                v = bytes(self.buf[self.pos:end])
                self.pos = end
                return v
            if et[0] == "{":
                kt, vt = split_sig(et[1:-1])
                out = {}
                while self.pos < end:
                    self.align(8)
                    k = self.get(kt)
                    out[k] = self.get(vt)
                return out
            items = []
            while self.pos < end:
                items.append(self.get(et))
            return items
        raise ValueError(f"unsupported D-Bus type {t!r}")


METHOD_CALL, METHOD_RETURN, ERROR, SIGNAL = 1, 2, 3, 4
NO_REPLY_EXPECTED = 0x1
_FIELDS = {1: "path", 2: "interface", 3: "member", 4: "error_name", 5: "reply_serial",
           6: "destination", 7: "sender", 8: "signature", 9: "unix_fds"}
_FIELD_SIG = {1: "o", 2: "s", 3: "s", 4: "s", 5: "u", 6: "s", 7: "s", 8: "g", 9: "u"}


class Message:
    def __init__(self, mtype, serial, flags=0, body=(), **fields):
        self.type, self.serial, self.flags = mtype, serial, flags
        self.body, self.fds = list(body), []
        for name in _FIELDS.values():
            setattr(self, name, fields.get(name))


def encode(mtype, serial, signature="", body=(), flags=0, **fields):
    types = split_sig(signature)
    if len(types) != len(body):
        raise ValueError(f"signature {signature!r} needs {len(types)} values, got {len(body)}")
    w = _Writer()
    for t, v in zip(types, body):
        w.put(t, v)
    fields["signature"] = signature
    header = [(code, Variant(_FIELD_SIG[code], fields[name]))
              for code, name in _FIELDS.items() if fields.get(name)]
    h = _Writer()
    h.buf += struct.pack("<BBBBII", ord("l"), mtype, flags, 1, len(w.buf), serial)
    h.put("a(yv)", header)
    h.pad(8)
    return bytes(h.buf + w.buf)


def _frame_length(buf):
    if len(buf) < 16:
        return None
    e = "<" if buf[0] == ord("l") else ">"
    body_len, _serial, fields_len = struct.unpack_from(e + "III", buf, 4)
    header = 16 + fields_len
    return header + (-header % 8) + body_len


def decode(raw):
    e = "<" if raw[0] == ord("l") else ">"
    r = _Reader(raw, 12, e)
    fields = {}
    for code, var in r.get("a(yv)"):
        if code in _FIELDS:
            fields[_FIELDS[code]] = var.value
    r.align(8)
    body = [r.get(t) for t in split_sig(fields.get("signature") or "")]
    (serial,) = struct.unpack_from(e + "I", raw, 8)
    return Message(raw[1], serial, raw[2], body, **fields)


def _close_fds(fds):
    for fd in fds:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------- connection

def bus_address():
    return os.environ.get("DBUS_SYSTEM_BUS_ADDRESS") or DEFAULT_BUS


def _socket_target(address):
    for entry in address.split(";"):
        kind, _, params = entry.partition(":")
        if kind != "unix":
            continue
        opts = dict(p.split("=", 1) for p in params.split(",") if "=" in p)
        if "path" in opts:
            return unquote(opts["path"])
        if "abstract" in opts:
            return "\0" + unquote(opts["abstract"])
    raise DBusError("org.freedesktop.DBus.Error.BadAddress", f"no unix socket in the D-Bus address {address!r}")


def bus_available():
    """True when the system bus socket is reachable from here (for picking the transport)."""
    try:
        target = _socket_target(bus_address())
    except DBusError:
        return False
    return target.startswith("\0") or os.path.exists(target)


class Bus:
    """One connection to a message bus. A reader thread delivers replies and answers calls to
    exported objects; any thread may make calls."""

    def __init__(self, address=None, timeout=5.0):
        self.address = address or bus_address()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(_socket_target(self.address))
            self._authenticate()
        except BaseException:
            self.sock.close()
            raise
        self.sock.settimeout(None)
        self.alive = True
        self._serial = 0
        self._lock = threading.Lock()
        self._wlock = threading.Lock()
        self._pending = {}
        self._fds = []
        self._objects = {}
        threading.Thread(target=self._read_loop, name="dbus-reader", daemon=True).start()
        try:
            (self.unique_name,) = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                            "org.freedesktop.DBus", "Hello", timeout=timeout)
        except BaseException:
            self.close()
            raise

    # login
    def _authenticate(self):
        uid = str(os.getuid()).encode().hex().encode()
        self.sock.sendall(b"\0AUTH EXTERNAL " + uid + b"\r\n")
        reply = self._auth_line()
        if not reply.startswith(b"OK"):
            raise DBusError("org.freedesktop.DBus.Error.AuthFailed",
                            "the bus refused the login (" + reply.decode("ascii", "replace") + ")")
        self.sock.sendall(b"NEGOTIATE_UNIX_FD\r\n")
        if not self._auth_line().startswith(b"AGREE_UNIX_FD"):
            raise DBusError("org.freedesktop.DBus.Error.NotSupported", "the bus can't pass file descriptors")
        self.sock.sendall(b"BEGIN\r\n")

    def _auth_line(self):
        line = b""
        while not line.endswith(b"\r\n"):
            ch = self.sock.recv(1)
            if not ch:
                raise DBusError("org.freedesktop.DBus.Error.Disconnected", "the bus closed the connection during login")
            line += ch
            if len(line) > 4096:
                raise DBusError("org.freedesktop.DBus.Error.AuthFailed", "unexpected answer from the bus")
        return line[:-2]

    # plumbing
    def _next_serial(self):
        with self._lock:
            self._serial = self._serial % 0xFFFFFFFF + 1
            return self._serial

    def _send(self, data):
        with self._wlock:
            self.sock.sendall(data)

    def _read_loop(self):
        buf = bytearray()
        try:
            while True:
                data, anc, _flags, _addr = self.sock.recvmsg(
                    65536, socket.CMSG_SPACE(32 * 4), getattr(socket, "MSG_CMSG_CLOEXEC", 0))
                for level, kind, cdata in anc:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                        n = len(cdata) // 4
                        self._fds.extend(struct.unpack(f"{n}i", cdata[:n * 4]))
                if not data:
                    break
                buf += data
                while True:
                    size = _frame_length(buf)
                    if size is None or len(buf) < size:
                        break
                    raw = bytes(buf[:size])
                    del buf[:size]
                    msg = decode(raw)
                    count = msg.unix_fds or 0
                    msg.fds, self._fds = self._fds[:count], self._fds[count:]
                    self._dispatch(msg)
        except (OSError, ValueError, IndexError, struct.error):
            pass
        finally:
            self.alive = False
            with self._lock:
                pending, self._pending = self._pending, {}
            for slot in pending.values():
                slot["msg"] = Message(ERROR, 0, body=["lost the connection to the bus"],
                                      error_name="org.freedesktop.DBus.Error.Disconnected")
                slot["event"].set()
            _close_fds(self._fds)
            self._fds = []
            try:
                self.sock.close()
            except OSError:
                pass

    def _dispatch(self, msg):
        if msg.type in (METHOD_RETURN, ERROR):
            with self._lock:
                slot = self._pending.pop(msg.reply_serial, None)
            if slot is None:
                _close_fds(msg.fds)
            else:
                slot["msg"] = msg
                slot["event"].set()
            return
        if msg.type == METHOD_CALL:
            reply = None
            try:
                handler = self._objects.get(msg.path)
                if handler is None:
                    raise DBusError("org.freedesktop.DBus.Error.UnknownObject", f"No object at {msg.path}")
                sig, body = handler(msg) or ("", ())
                reply = encode(METHOD_RETURN, self._next_serial(), sig, body,
                               reply_serial=msg.serial, destination=msg.sender)
            except Exception as e:  # answer with an error rather than leave the caller hanging
                name, text = (e.name, e.text) if isinstance(e, DBusError) else ("org.freedesktop.DBus.Error.Failed", str(e))
                reply = encode(ERROR, self._next_serial(), "s", [text or name], error_name=name,
                               reply_serial=msg.serial, destination=msg.sender)
            if not msg.flags & NO_REPLY_EXPECTED:
                self._send(reply)
        _close_fds(msg.fds)  # anything a handler didn't take

    # public
    def export(self, path, handler):
        """handler(msg) runs on the reader thread and returns None or (signature, body).
        It must not make calls on this bus. To keep a passed descriptor, set msg.fds[i] = None."""
        self._objects[path] = handler

    def call(self, destination, path, interface, member, signature="", body=(), timeout=25.0):
        slot = {"event": threading.Event(), "msg": None}
        serial = self._next_serial()
        with self._lock:
            if not self.alive:
                raise DBusError("org.freedesktop.DBus.Error.Disconnected", "not connected to the bus")
            self._pending[serial] = slot
        try:
            self._send(encode(METHOD_CALL, serial, signature, body, path=path, interface=interface,
                              member=member, destination=destination))
        except OSError as e:
            with self._lock:
                self._pending.pop(serial, None)
            raise DBusError("org.freedesktop.DBus.Error.Disconnected", str(e))
        if not slot["event"].wait(timeout):
            with self._lock:
                self._pending.pop(serial, None)
            raise DBusError("org.freedesktop.DBus.Error.NoReply", f"{member} got no answer within {timeout:g} s")
        reply = slot["msg"]
        _close_fds(reply.fds)  # none of the calls we make expect descriptors back
        if reply.type == ERROR:
            text = reply.body[0] if reply.body and isinstance(reply.body[0], str) else ""
            raise DBusError(reply.error_name or "org.freedesktop.DBus.Error.Failed", text)
        return reply.body

    def close(self):
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


# ---------------------------------------------------------------- BlueZ

def _bluez_error(e, mac=""):
    """Turn a D-Bus or BlueZ error into one of the bridge's error codes, keeping BlueZ's words."""
    name, text = e.name, e.text or ""
    low = f"{name} {text}".lower()
    why = f" (BlueZ: {text or name.rsplit('.', 1)[-1]})"
    if name in ("org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner"):
        return BluezError("no_adapter", "Bluetooth isn't running on the host. Install BlueZ there and run "
                                        "`sudo systemctl enable --now bluetooth`.")
    if name == "org.freedesktop.DBus.Error.AccessDenied":
        if "apparmor" in low:
            return BluezError("config", APPARMOR_HINT)
        return BluezError("config", f"The host's D-Bus won't let the bridge talk to BlueZ ({text}). "
                                    "The container has to run as root, which is the default.")
    if name == "org.freedesktop.DBus.Error.Disconnected":
        return BluezError("unreachable", "Lost the connection to the host's D-Bus. Try again.")
    if name == "org.freedesktop.DBus.Error.NoReply":
        return BluezError("unreachable", "The printer didn't answer in time. It may be at the edge of Bluetooth "
                                         "range of the bridge.")
    if "profile-unavailable" in low:
        return BluezError("pairing", "BlueZ hasn't read the printer's services yet. With the printer on, run "
                                     f"`bluetoothctl connect {mac}` once on the host, then try again "
                                     "(see Pairing in the README)." + why)
    if any(k in low for k in ("authentication", "key-missing", "permission denied", "unknownobject",
                              "doesnotexist", "does not exist")):
        return BluezError("pairing", "The printer and the bridge host need pairing again: "
                                     f"bluetoothctl remove {mac}, then pair (see Pairing in the README)." + why)
    if any(k in low for k in ("inprogress", "in progress", "concurrent")):
        return BluezError("busy", "Bluetooth on the bridge host is still busy with an earlier connection attempt. "
                                  "Try again in a minute; if it doesn't clear, run `sudo systemctl restart bluetooth` "
                                  "on the host." + why)
    if any(k in low for k in ("refused", "busy")):
        return BluezError("busy", "The printer refused the connection. It may be connected to a phone; "
                                  "close the page or the Kodak app there and try again." + why)
    if any(k in low for k in ("notready", "not-powered", "not powered", "no such device", "resource not ready")):
        return BluezError("no_adapter", "The Bluetooth adapter on the bridge host isn't ready. "
                                        "Check `bluetoothctl show` and `rfkill list` there." + why)
    return BluezError("unreachable", "The printer is off, asleep or out of range of the bridge." + why)


def _already_connected(e):
    low = f"{e.name} {e.text}".lower()
    return "alreadyconnected" in low or "already-connected" in low or "already connected" in low


def _adapters(objs):
    return {p: i["org.bluez.Adapter1"] for p, i in objs.items() if "org.bluez.Adapter1" in i}


def _address(dev):
    return str(unwrap(dev.get("Address", ""))).upper()


def _device(objs, mac, name_prefix=""):
    """The printer's BlueZ object as (path, properties), or None.

    With an address, that device. Without one, the paired device whose name starts with
    name_prefix (the M50 calls itself "Kodak Label_XXXX"; its Bluetooth Low Energy twin,
    "Kodak Label_XXXX_iphone", can't be paired and is skipped)."""
    devices = [(p, i["org.bluez.Device1"]) for p, i in objs.items() if "org.bluez.Device1" in i]
    if mac:
        found = [d for d in devices if _address(d[1]) == mac]
    else:
        def named(d):
            name = str(unwrap(d.get("Name") or d.get("Alias") or ""))
            return name.startswith(name_prefix) and not name.endswith("_iphone")
        found = [d for d in devices if named(d[1]) and unwrap(d[1].get("Paired", False))]
        if len({_address(d[1]) for d in found}) > 1:
            names = ", ".join(f"{unwrap(d[1].get('Name'))} ({_address(d[1])})" for d in found)
            raise BluezError("config", f"Several printers are paired: {names}. Set PRINTER_MAC to the one to use.")
    found.sort(key=lambda d: not unwrap(d[1].get("Paired", False)))  # a paired entry first
    return found[0] if found else None


def _not_found(mac, name_prefix):
    if mac:
        return BluezError("pairing", f"The host's Bluetooth doesn't know {mac} yet. Pair it once with bluetoothctl "
                                     "(see Pairing in the README).")
    return BluezError("pairing", f"No paired printer named \"{name_prefix}…\" on the host. Pair it once with "
                                 "bluetoothctl (see Pairing in the README).")


class BluezSpp:
    """Serial-port (RFCOMM) connections to a Bluetooth device, opened by the host's bluetoothd."""

    def __init__(self, log=print, name_prefix="Kodak Label"):
        self.log = log
        self.name_prefix = name_prefix
        self.bus = None
        self.lock = threading.Lock()
        self.waiting = None  # (device path, slot) while a ConnectProfile is in flight

    # bus and profile
    def _connect_bus(self):
        if self.bus and self.bus.alive:
            return self.bus
        address = bus_address()
        try:
            bus = Bus(address)
        except DBusError as e:
            raise _bluez_error(e)
        except OSError as e:
            raise BluezError("config", f"Can't reach the host's D-Bus at {address} ({e.strerror or e}). "
                                       "Mount /run/dbus into the container (see the README).")
        bus.export(PROFILE_PATH, self._on_profile_call)
        self.bus = bus
        return bus

    def _register_profile(self, bus):
        options = {"Name": Variant("s", "M50 label bridge"), "Role": Variant("s", "client"),
                   "AutoConnect": Variant("b", False)}
        try:
            bus.call("org.bluez", "/org/bluez", "org.bluez.ProfileManager1", "RegisterProfile", "osa{sv}",
                     [PROFILE_PATH, SPP_UUID, options], timeout=10)
        except DBusError as e:
            if not e.name.endswith(".AlreadyExists"):
                raise _bluez_error(e)

    def _objects(self, bus):
        try:
            return bus.call("org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", timeout=10)[0]
        except DBusError as e:
            raise _bluez_error(e)

    def _on_profile_call(self, msg):
        # Runs on the bus reader thread: no bus calls from here.
        if msg.interface not in (None, "org.bluez.Profile1"):
            raise DBusError("org.freedesktop.DBus.Error.UnknownInterface", f"Unknown interface {msg.interface}")
        if msg.member == "NewConnection":
            device, index = msg.body[0], msg.body[1]
            waiting = self.waiting
            if not waiting or waiting[0] != device or waiting[1]["fd"] is not None or index >= len(msg.fds):
                raise DBusError("org.bluez.Error.Rejected", "The label bridge isn't expecting this connection.")
            waiting[1]["fd"], msg.fds[index] = msg.fds[index], None
            waiting[1]["event"].set()
            return None
        if msg.member in ("RequestDisconnection", "Release"):
            return None
        raise DBusError("org.freedesktop.DBus.Error.UnknownMethod", f"Unknown method {msg.member}")

    def _quiet(self, bus, path, member, signature="", body=(), timeout=8.0):
        try:
            bus.call("org.bluez", path, "org.bluez.Device1", member, signature, body, timeout=timeout)
        except DBusError:
            pass

    # public
    def state(self, mac):
        """What BlueZ knows about the adapter and the printer, for the status endpoint and the log."""
        try:
            with self.lock:
                objs = self._objects(self._connect_bus())
        except BluezError as e:
            return {"ok": False, "code": e.code, "error": str(e)}
        adapters = [{"name": p.rsplit("/", 1)[-1], "address": unwrap(a.get("Address")),
                     "powered": bool(unwrap(a.get("Powered")))} for p, a in _adapters(objs).items()]
        info = {"ok": True, "adapters": adapters, "printer": None}
        try:
            found = _device(objs, mac.upper(), self.name_prefix)
        except BluezError as e:
            info["printer_error"] = str(e)
            return info
        if found:
            d = found[1]
            uuids = [str(u).lower() for u in (unwrap(d.get("UUIDs")) or [])]
            info["printer"] = {"name": unwrap(d.get("Name")), "address": _address(d),
                               "paired": bool(unwrap(d.get("Paired"))), "connected": bool(unwrap(d.get("Connected"))),
                               "serial_port": SPP_UUID in uuids}
        return info

    def connect(self, mac="", timeout=CONNECT_TIMEOUT):
        """Open a fresh RFCOMM connection to the printer's serial port. Returns (fd, device path).
        Without an address, uses the paired printer found by name."""
        mac = mac.upper()
        with self.lock:
            bus = self._connect_bus()
            self._register_profile(bus)
            objs = self._objects(bus)
            adapters = _adapters(objs)
            if not adapters:
                raise BluezError("no_adapter", "BlueZ on the host has no Bluetooth adapter. "
                                               "Check `bluetoothctl show` there.")
            found = _device(objs, mac, self.name_prefix)
            if not found:
                raise _not_found(mac, self.name_prefix)
            path, dev = found
            mac = _address(dev)
            adapter = unwrap(dev.get("Adapter"))
            if adapter in adapters and not unwrap(adapters[adapter].get("Powered", True)):
                self._power_on(bus, adapter)
            for attempt in (1, 2):
                slot = {"event": threading.Event(), "fd": None}
                self.waiting = (path, slot)
                try:
                    bus.call("org.bluez", path, "org.bluez.Device1", "ConnectProfile", "s", [SPP_UUID],
                             timeout=timeout)
                    if not slot["event"].wait(5):
                        raise BluezError("timeout", "BlueZ connected to the printer but didn't hand the connection over.")
                    fd, slot["fd"] = slot["fd"], None
                    return fd, path
                except DBusError as e:
                    if attempt == 1 and _already_connected(e):
                        # A connection BlueZ still thinks is open (for example after a crash): drop it and retry.
                        self._quiet(bus, path, "DisconnectProfile", "s", [SPP_UUID])
                        time.sleep(0.5)
                        continue
                    raise _bluez_error(e, mac)
                finally:
                    self.waiting = None
                    _close_fds([slot["fd"]])

    def _power_on(self, bus, adapter):
        self.log(f"Bluetooth adapter {adapter.rsplit('/', 1)[-1]} is off; switching it on")
        try:
            bus.call("org.bluez", adapter, "org.freedesktop.DBus.Properties", "Set", "ssv",
                     ["org.bluez.Adapter1", "Powered", Variant("b", True)], timeout=10)
        except DBusError as e:
            raise BluezError("no_adapter", "The Bluetooth adapter is off and couldn't be switched on "
                                           f"({e.text or e.name}). Check `rfkill list` on the host.")

    def release(self, path):
        """Drop the Bluetooth link after the socket is closed, so a phone can connect straight away."""
        bus = self.bus
        if bus and bus.alive and path:
            self._quiet(bus, path, "Disconnect")
