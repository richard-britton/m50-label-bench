#!/usr/bin/env python3
"""End-to-end test of the bridge's BlueZ mode: a real dbus-daemon, a mock bluetoothd written
with dbus-python, and the bridge talking to it with its own D-Bus client."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRIDGE = ROOT
WORK = tempfile.mkdtemp(prefix="m50-bluez-")  # short path: unix socket paths are limited to 108 bytes
MOCK_PYTHON = os.environ.get("MOCK_PYTHON", "python3")  # needs dbus-python and PyGObject


def _bt_sockets():
    import socket
    try:
        socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM).close()
        return True
    except (AttributeError, OSError):
        return False


# Where this machine can open Bluetooth sockets itself, "auto" would pick them, so force BlueZ mode.
BT_SOCKETS = _bt_sockets()
SOCK = os.path.join(WORK, "bus.sock")
ADDR = "unix:path=" + SOCK
EVENTS = os.path.join(WORK, "events.jsonl")
JOBS = os.path.join(WORK, "jobs")
PORT = 8095
MAC = "00:11:22:33:44:01"  # the mock printer

procs = {}
failures = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        failures.append(what)


def start_bus():
    conf = os.path.join(WORK, "bus.conf")
    with open(conf, "w") as f:
        f.write(f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>custom</type>
  <listen>{ADDR}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow user="*"/>
    <allow own="*"/>
    <allow send_destination="*"/>
    <allow receive_sender="*"/>
  </policy>
</busconfig>
""")
    if os.path.exists(SOCK):
        os.remove(SOCK)
    procs["bus"] = subprocess.Popen(["dbus-daemon", "--config-file", conf, "--nofork", "--nopidfile"])
    for _ in range(50):
        if os.path.exists(SOCK):
            return
        time.sleep(0.05)
    raise SystemExit("dbus-daemon did not start")


def start_mock():
    before = kinds(events()).count("ready")
    procs["mock"] = subprocess.Popen([MOCK_PYTHON, os.path.join(HERE, "mock_bluez.py"), ADDR, EVENTS, JOBS])
    for _ in range(100):
        if kinds(events()).count("ready") > before:
            return
        time.sleep(0.05)
    raise SystemExit("mock bluetoothd did not start")


def start_bridge(port=PORT, extra=None, logname="bridge.log"):
    env = dict(os.environ, PORT=str(port), PRINTER_MAC="", DATA_DIR=os.path.join(WORK, "data"),
               DBUS_SYSTEM_BUS_ADDRESS=ADDR, API_TOKEN="", BT_MODE="bluez" if BT_SOCKETS else "auto")
    env.pop("FAKE_PRINTER", None)  # set in this process for building the reference job
    env.update(extra or {})
    log = open(os.path.join(WORK, logname), "w")
    p = subprocess.Popen([sys.executable, "bridge.py"], cwd=BRIDGE, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
            return p
        except OSError:
            time.sleep(0.1)
    raise SystemExit("bridge did not start: " + open(os.path.join(WORK, logname)).read())


def stop(name):
    p = procs.pop(name, None)
    if p:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()


def events():
    try:
        with open(EVENTS) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def mark():
    return len(events())


def since(n):
    return events()[n:]


def api(method, path, body=None, port=PORT, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def ctl(**kw):
    subprocess.run(["dbus-send", f"--bus={ADDR}", "--print-reply", "--dest=org.bluez", "/",
                    "org.labelbridge.Test.Set", "string:" + json.dumps(kw)], check=True, capture_output=True)


def fd_count(pid):
    return len(os.listdir(f"/proc/{pid}/fd"))


def kinds(evs):
    return [e["ev"] for e in evs]


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(JOBS)
    start_bus()
    start_mock()
    procs["bridge"] = start_bridge()
    time.sleep(0.3)
    blog = open(os.path.join(WORK, "bridge.log")).read()
    print("bridge log:\n" + "".join("    " + line + "\n" for line in blog.splitlines()))

    print("transport")
    check("Bluetooth via host bluetoothd over D-Bus" in blog,
          "startup log names the D-Bus transport" + (" (BT_MODE=bluez)" if BT_SOCKETS else ", picked automatically"))
    check("printer Kodak Label_0001 (00:11:22:33:44:01) is paired, found by name" in blog,
          "startup log reports the printer it found by name (skipping its _iphone twin)")

    print("status")
    n = mark()
    code, st = api("GET", "/api/status?fresh=1")
    check(code == 200 and st["transport"] == "bluez", f"status says transport bluez ({code})")
    check(st.get("printer", {}).get("reachable") and st["printer"].get("battery") == 73, "printer reachable, battery 73")
    check(st["bluez"]["printer"] == {"name": "Kodak Label_0001", "address": MAC, "paired": True, "connected": False,
                                     "serial_port": True}, "bluez details decoded from GetManagedObjects")
    check(st["printer_mac"] == MAC, "status reports the address it found")
    check(st["adapter"] is True, "adapter reported as present and powered")
    ev = since(n)
    reg = [e for e in ev if e["ev"] == "register"]
    check(len(reg) == 1 and reg[0]["role"] == "client" and reg[0]["autoconnect"] is False
          and reg[0]["uuid"] == "00001101-0000-1000-8000-00805f9b34fb", "profile registered as SPP client, no autoconnect")
    check(kinds(ev).count("connect") == 1 and "handed_over" in kinds(ev), "one ConnectProfile, socket handed over")
    check("eof" in kinds(ev) and "disconnect" in kinds(ev), "link closed and Device1.Disconnect called afterwards")

    print("printing")
    n = mark()
    code, res = api("POST", "/api/print", {"text": "Hello 👋", "copies": 2})
    check(code == 200 and res.get("printed") == 2, f"two copies printed ({code} {res})")
    ev = since(n)
    check(kinds(ev).count("job") == 2, "mock printer received two jobs on one connection")
    check(kinds(ev).count("connect") == 1, "one connection for both copies")
    check("disconnect_profile" not in kinds(ev), "no stale-connection recovery was needed")
    jobs = sorted(os.listdir(JOBS))
    sys.path.insert(0, BRIDGE)
    os.environ.update(FAKE_PRINTER="1", DATA_DIR=os.path.join(WORK, "data2"))
    import bridge
    import render
    expect = bridge.build_job(render.render_text("Hello 👋", 30, 112), 112, 0, "cw")
    got = [open(os.path.join(JOBS, j), "rb").read() for j in jobs]
    check(len(got) == 2 and all(g == expect for g in got), f"job bytes identical to the socket path's ({len(expect)} bytes)")

    print("back-to-back prints reuse nothing stale (socket shutdown works through bluetoothd's copy)")
    n = mark()
    for i in range(3):
        code, res = api("POST", "/api/print", {"text": f"Again {i}"})
        check(code == 200, f"print {i + 1} ok ({code} {res.get('error', '')})")
    ev = since(n)
    check("disconnect_profile" not in kinds(ev) and kinds(ev).count("connect") == 3, "three clean connections")

    print("no descriptor leaks")
    pid = procs["bridge"].pid
    before = fd_count(pid)
    for _ in range(8):
        api("GET", "/api/status?fresh=1")
    after = fd_count(pid)
    check(after <= before, f"open fds {before} -> {after} after 8 status checks")

    print("printer not ready")
    ctl(printer_state="no paper")
    code, res = api("POST", "/api/print", {"text": "x"})
    check(code == 409 and res["code"] == "not_ready", f"409 not_ready ({code} {res.get('code')})")
    ctl(printer_state="ready")

    print("printer off (page timeout)")
    ctl(connect_error=["org.bluez.Error.Failed", "br-connection-page-timeout"])
    n = mark()
    t = time.time()
    code, res = api("POST", "/api/print", {"text": "x"})
    check(code == 503 and res["code"] == "unreachable" and "page-timeout" in res["error"],
          f"503 unreachable with BlueZ's reason ({code} {res.get('error')})")
    check(kinds(since(n)).count("connect") == 3, f"retried 3 times ({time.time() - t:.1f} s)")

    print("weak signal: BlueZ takes 25 s to give up (the user's 'busy' cascade)")
    ctl(connect_error=["org.bluez.Error.Failed", "Host is down"], connect_delay=25)
    n = mark()
    t = time.time()
    code, res = api("POST", "/api/print", {"text": "x"})
    took = time.time() - t
    ev = since(n)
    check(code == 503 and res["code"] == "unreachable", f"503 unreachable ({res.get('error')})")
    check(kinds(ev).count("connect") == 1 and "busy" not in kinds(ev), f"waited for BlueZ: one attempt, no busy ({kinds(ev)})")
    check(24 < took < 40, f"answered once BlueZ gave up ({took:.1f} s)")
    ctl(connect_error=None, connect_delay=0.2)
    code, res = api("POST", "/api/print", {"text": "x"})
    check(code == 200, f"next print goes straight through ({code} {res.get('error', '')})")

    print("BlueZ busy with someone else's connect for 6 s")
    ctl(busy_for=6)
    n = mark()
    code, res = api("POST", "/api/print", {"text": "x"})
    ev = since(n)
    check(code == 200 and kinds(ev).count("busy") >= 1, f"waited and printed ({code}, {kinds(ev).count('busy')} busy)")

    print("printer connected to a phone (refused)")
    ctl(connect_error=["org.bluez.Error.Failed", "br-connection-refused"])
    code, res = api("POST", "/api/print", {"text": "x"})
    check(code == 409 and res["code"] == "busy", f"409 busy ({code} {res.get('code')})")

    print("pairing lost")
    ctl(connect_error=["org.bluez.Error.AuthenticationFailed", "Authentication Failed"])
    n = mark()
    code, res = api("POST", "/api/print", {"text": "x"})
    check(code == 503 and res["code"] == "pairing" and "bluetoothctl remove" in res["error"], f"pairing ({res.get('error')})")
    check(kinds(since(n)).count("connect") == 1, "no pointless retries for pairing errors")
    ctl(connect_error=None)

    print("printer never paired")
    ctl(no_device=True)
    code, res = api("POST", "/api/print", {"text": "x"})
    check(code == 503 and res["code"] == "pairing" and "No paired printer named" in res["error"], f"pairing ({res.get('error')})")
    code, st = api("GET", "/api/status?fresh=1")
    check(st["bluez"]["printer"] is None, "status shows the printer as unknown to BlueZ")
    ctl(no_device=False)

    print("adapter switched off")
    ctl(powered=False)
    n = mark()
    code, res = api("POST", "/api/print", {"text": "x"})
    ev = since(n)
    check(code == 200 and any(e["ev"] == "set" and e["prop"] == "Powered" and e["value"] for e in ev),
          f"bridge switched the adapter on and printed ({code})")

    print("BlueZ still thinks an old connection is open")
    ctl(stale=True)
    n = mark()
    code, res = api("POST", "/api/print", {"text": "x"})
    ev = since(n)
    check(code == 200, f"printed after recovering ({code} {res.get('error', '')})")
    check("disconnect_profile" in kinds(ev) and kinds(ev).count("connect") == 2, "DisconnectProfile then a second ConnectProfile")

    print("no adapter")
    ctl(no_adapter=True)
    code, res = api("POST", "/api/print", {"text": "x"})
    check(code == 503 and res["code"] == "no_adapter", f"503 no_adapter ({res.get('error')})")
    ctl(no_adapter=False)

    print("bluetoothd stopped, then started again")
    stop("mock")
    code, res = api("POST", "/api/print", {"text": "x"})
    check(code == 503 and res["code"] == "no_adapter" and "systemctl enable --now bluetooth" in res["error"],
          f"no_adapter: Bluetooth isn't running ({res.get('error')})")
    start_mock()
    code, res = api("POST", "/api/print", {"text": "x"})
    check(code == 200, f"prints again once bluetoothd is back, profile re-registered ({code} {res.get('error', '')})")

    print("D-Bus restarted")
    stop("mock")
    stop("bus")
    code, res = api("POST", "/api/print", {"text": "x"}, timeout=120)
    check(code in (500, 503) and res["code"] in ("config", "unreachable"), f"clear error while the bus is down ({res.get('error')})")
    start_bus()
    start_mock()
    code, res = api("POST", "/api/print", {"text": "x"})
    check(code == 200, f"reconnects to the new bus and prints ({code} {res.get('error', '')})")

    print("two printers paired, no PRINTER_MAC")
    ctl(second_printer=True)
    code, res = api("POST", "/api/print", {"text": "x"})
    check(code == 500 and res["code"] == "config" and "Set PRINTER_MAC" in res["error"], f"asks which one ({res.get('error')})")
    stop("bridge")  # one bridge per host: BlueZ hands connections to whichever registered first
    p = start_bridge(PORT + 3, {"PRINTER_MAC": MAC.lower()}, "bridge4.log")
    code, res = api("POST", "/api/print", {"text": "x"}, port=PORT + 3)
    check(code == 200, f"PRINTER_MAC picks one ({code} {res.get('error', '')})")
    p.terminate()
    p.wait()
    ctl(second_printer=False)
    p = start_bridge(PORT + 3, {"PRINTER_MAC": "00:11:22:33:44:99"}, "bridge5.log")
    code, res = api("POST", "/api/print", {"text": "x"}, port=PORT + 3)
    check(code == 503 and res["code"] == "pairing" and "doesn't know 00:11:22:33:44:99" in res["error"],
          f"unknown PRINTER_MAC ({res.get('error')})")
    p.terminate()
    p.wait()

    print("D-Bus not mounted")
    p = start_bridge(PORT + 1, {"BT_MODE": "bluez", "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/nonexistent/bus"}, "bridge2.log")
    code, res = api("POST", "/api/print", {"text": "x"}, port=PORT + 1)
    check(code == 500 and res["code"] == "config" and "Mount /run/dbus" in res["error"], f"config error ({res.get('error')})")
    log2 = open(os.path.join(WORK, "bridge2.log")).read()
    check("warning: Can't reach the host's D-Bus" in log2, "startup log warns about D-Bus")
    p.terminate()
    p.wait()

    if not BT_SOCKETS:  # only meaningful where Bluetooth sockets can't be opened
        print("auto mode without D-Bus falls back to sockets")
        p = start_bridge(PORT + 2, {"DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/nonexistent/bus", "PRINTER_MAC": MAC}, "bridge3.log")
        code, st = api("GET", "/api/status?probe=0", port=PORT + 2)
        check(st["transport"] == "socket", f"transport socket ({st['transport']})")
        code, res = api("POST", "/api/print", {"text": "x"}, port=PORT + 2)
        check(res["code"] == "no_adapter" and "network_mode: host" in res["error"] and "BT_MODE=bluez" in res["error"],
              f"socket error explains both options ({res.get('error')})")
        p.terminate()
        p.wait()
        p = start_bridge(PORT + 2, {"DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/nonexistent/bus"}, "bridge3.log")
        code, res = api("POST", "/api/print", {"text": "x"}, port=PORT + 2)
        check(res["code"] == "config" and "Set PRINTER_MAC" in res["error"], f"socket mode needs PRINTER_MAC ({res.get('error')})")
        p.terminate()
        p.wait()

    print("\n" + ("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}"))


ok = False
try:
    main()
    ok = not failures
finally:
    for name in list(procs):
        stop(name)
    if ok:
        shutil.rmtree(WORK, ignore_errors=True)
    else:
        print(f"logs kept in {WORK}")
sys.exit(0 if ok else 1)
