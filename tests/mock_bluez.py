#!/usr/bin/env python3.12
"""Stand-in for bluetoothd on a private bus, built on dbus-python (an independent D-Bus
implementation). One adapter, the printer, and a fake M50 behind each RFCOMM connection, handed
out through the Profile API the way BlueZ does it (bluetoothd keeps its own copy of the socket).

usage: mock_bluez.py BUS_ADDRESS EVENTS_FILE JOBS_DIR
Control while running: org.labelbridge.Test.Set(json) on object / of org.bluez.
"""
import json
import os
import socket
import struct
import sys
import threading
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

ADDRESS, EVENTS, JOBS = sys.argv[1:4]
SPP = "00001101-0000-1000-8000-00805f9b34fb"
ADAPTER = "/org/bluez/hci0"
DEV = ADAPTER + "/dev_00_11_22_33_44_01"
MAC = "00:11:22:33:44:01"
NAME = "Kodak Label_0001"
STATUS_CMD = b"\x10\xff\x70"

ctl = {"powered": True, "printer_state": "ready"}
conn = {"connected": False, "mine": None, "theirs": None}
profiles = {}


def event(**kw):
    kw["t"] = round(time.time(), 3)
    with open(EVENTS, "a") as f:
        f.write(json.dumps(kw) + "\n")


def err(name, text):
    return dbus.DBusException(text, name=name)


def drop(reason):
    theirs, mine = conn["theirs"], conn["mine"]
    conn.update(theirs=None)
    if theirs:
        theirs.close()
    if mine:
        try:
            mine.shutdown(socket.SHUT_RDWR)  # link gone: both ends see it
        except OSError:
            pass
    event(ev="drop", reason=reason)


def fake_printer(sock):
    buf = b""
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            buf += data
            while True:
                if buf.startswith(STATUS_CMD):
                    buf = buf[3:]
                    line = f"M50|{NAME}|{MAC}|00:11:22:33:44:02|VP2.13|M50000000001|73|{ctl['printer_state']}|0mm"
                    sock.sendall(line.encode())
                    continue
                if len(buf) >= 24 and buf.startswith(bytes(12)):
                    bpl, lines = struct.unpack("<HH", buf[20:24])
                    total = 24 + bpl * lines + 6
                    if len(buf) < total:
                        break
                    job, buf = buf[:total], buf[total:]
                    n = len(os.listdir(JOBS))
                    with open(os.path.join(JOBS, f"job{n:03d}.bin"), "wb") as f:
                        f.write(job)
                    event(ev="job", bytes=len(job), tail=job[-6:].hex())
                    sock.sendall(b"\xff\x0b")
                    time.sleep(0.3)
                    sock.sendall(b"\xaa")
                    continue
                break
    except OSError:
        pass
    finally:
        event(ev="eof")
        sock.close()
        conn.update(connected=False, mine=None)
        GLib.idle_add(lambda: (conn["theirs"] and conn["theirs"].close(), conn.update(theirs=None), False)[2])


class Root(dbus.service.Object):
    @dbus.service.method("org.freedesktop.DBus.ObjectManager", out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        objs = {}
        if ctl.get("no_adapter"):
            return objs
        objs[ADAPTER] = {"org.bluez.Adapter1": {"Address": "00:11:22:33:44:55", "Name": "nas",
                                                "Powered": dbus.Boolean(ctl["powered"]),
                                                "UUIDs": dbus.Array([], signature="s")}}
        if not ctl.get("no_device"):
            objs[DEV] = {
                "org.bluez.Device1": {
                    "Address": MAC, "Name": NAME, "Alias": NAME, "Paired": dbus.Boolean(True),
                    "Connected": dbus.Boolean(conn["connected"]), "Adapter": dbus.ObjectPath(ADAPTER),
                    "UUIDs": dbus.Array([SPP, "00001200-0000-1000-8000-00805f9b34fb"], signature="s"),
                    "RSSI": dbus.Int16(-61), "Class": dbus.UInt32(0x040680),
                    "ManufacturerData": dbus.Dictionary({dbus.UInt16(0x0590): dbus.ByteArray(b"\x01\x02\x03")},
                                                        signature="qv"),
                },
                "org.freedesktop.DBus.Properties": {},
            }
            # the printer's Bluetooth Low Energy twin (never paired) and a stranger
            objs[ADAPTER + "/dev_00_11_22_33_44_02"] = {"org.bluez.Device1": {
                "Address": "00:11:22:33:44:02", "Name": NAME + "_iphone", "Paired": dbus.Boolean(False),
                "Adapter": dbus.ObjectPath(ADAPTER)}}
            objs[ADAPTER + "/dev_11_22_33_44_55_66"] = {"org.bluez.Device1": {
                "Address": "11:22:33:44:55:66", "Name": "Someone's watch", "Paired": dbus.Boolean(True),
                "Adapter": dbus.ObjectPath(ADAPTER)}}
        if ctl.get("second_printer"):
            objs[ADAPTER + "/dev_00_11_22_33_44_03"] = {"org.bluez.Device1": {
                "Address": "00:11:22:33:44:03", "Name": "Kodak Label_0003", "Paired": dbus.Boolean(True),
                "Adapter": dbus.ObjectPath(ADAPTER)}}
        return objs

    @dbus.service.method("org.labelbridge.Test", in_signature="s")
    def Set(self, payload):
        ctl.update(json.loads(payload))
        if "busy_for" in ctl:
            ctl["busy_until"] = time.time() + ctl.pop("busy_for")
        if ctl.pop("stale", False):
            conn["connected"] = True  # BlueZ thinks a connection is still open
        event(ev="ctl", ctl=ctl)


class ProfileManager(dbus.service.Object):
    @dbus.service.method("org.bluez.ProfileManager1", in_signature="osa{sv}", sender_keyword="sender")
    def RegisterProfile(self, path, uuid, opts, sender=None):
        key = (str(sender), str(path))
        if key in profiles:
            raise err("org.bluez.Error.AlreadyExists", "Already Exists")
        profiles[key] = str(uuid).lower()
        event(ev="register", uuid=str(uuid), role=str(opts.get("Role")), autoconnect=bool(opts.get("AutoConnect")),
              name=str(opts.get("Name")))


class Adapter(dbus.service.Object):
    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ssv")
    def Set(self, iface, prop, value):
        event(ev="set", iface=str(iface), prop=str(prop), value=bool(value))
        if prop == "Powered":
            ctl["powered"] = bool(value)


class Device(dbus.service.Object):
    @dbus.service.method("org.bluez.Device1", in_signature="s", async_callbacks=("ok", "fail"))
    def ConnectProfile(self, uuid, ok, fail):
        event(ev="connect", uuid=str(uuid))
        if conn.get("pending") or time.time() < ctl.get("busy_until", 0):
            # BlueZ refuses a new connect while an earlier one to the same device is still running
            event(ev="busy")
            return fail(err("org.bluez.Error.InProgress", "br-connection-busy"))
        if not ctl["powered"]:
            return fail(err("org.bluez.Error.NotReady", "Resource Not Ready"))
        if ctl.get("connect_error"):
            name, text = ctl["connect_error"]
            conn["pending"] = True

            def finish():
                conn["pending"] = False
                event(ev="connect_failed", text=text)
                fail(err(name, text))
                return False
            GLib.timeout_add(int(ctl.get("connect_delay", 0.2) * 1000), finish)
            return
        if conn["connected"]:
            return fail(err("org.bluez.Error.AlreadyConnected", "br-connection-already-connected"))
        target = next(((s, p) for (s, p), u in profiles.items() if u == SPP), None)
        if not target:
            return fail(err("org.bluez.Error.InvalidArguments", "br-connection-profile-unavailable"))
        mine, theirs = socket.socketpair()
        conn.update(connected=True, mine=mine, theirs=theirs)
        threading.Thread(target=fake_printer, args=(mine,), daemon=True).start()

        def done(*_):
            event(ev="handed_over")
            ok()

        def refused(e):
            event(ev="newconnection_error", error=str(e))
            drop("refused")
            fail(err("org.bluez.Error.Failed", "br-connection-refused"))

        obj = BUS.get_object(target[0], target[1], introspect=False)
        obj.NewConnection(dbus.ObjectPath(DEV), dbus.types.UnixFd(theirs.fileno()), dbus.Dictionary({}, signature="sv"),
                          dbus_interface="org.bluez.Profile1", reply_handler=done, error_handler=refused)

    @dbus.service.method("org.bluez.Device1", in_signature="s")
    def DisconnectProfile(self, uuid):
        event(ev="disconnect_profile", connected=conn["connected"])
        if not conn["connected"]:
            raise err("org.bluez.Error.NotConnected", "Not Connected")
        for (s, p), u in profiles.items():
            if u == SPP:
                BUS.get_object(s, p, introspect=False).RequestDisconnection(
                    dbus.ObjectPath(DEV), dbus_interface="org.bluez.Profile1",
                    reply_handler=lambda *a: event(ev="request_disconnection_ok"),
                    error_handler=lambda e: event(ev="request_disconnection_error", error=str(e)))
        drop("disconnect_profile")
        conn["connected"] = False

    @dbus.service.method("org.bluez.Device1")
    def Disconnect(self):
        event(ev="disconnect", connected=conn["connected"])
        drop("disconnect")
        conn["connected"] = False


dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
BUS = dbus.bus.BusConnection(ADDRESS)
BUS.request_name("org.bluez")


def owner_changed(name, old, new):
    # like bluetoothd: a profile goes away when the program that registered it disconnects
    if name.startswith(":") and not new:
        for key in [k for k in profiles if k[0] == name]:
            del profiles[key]
            event(ev="unregister", sender=name)


BUS.add_signal_receiver(owner_changed, "NameOwnerChanged", "org.freedesktop.DBus")
Root(BUS, "/")
ProfileManager(BUS, "/org/bluez")
Adapter(BUS, ADAPTER)
Device(BUS, DEV)
event(ev="ready")
GLib.MainLoop().run()
