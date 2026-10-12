#!/usr/bin/env python3
"""Headless browser checks of the label designer against a bridge with a simulated printer.

Covers the editor (add, drag, resize, rotate, undo, emoji, images, shapes, templates, keyboard),
saving to the print server, printing through it and from "this device" (a mock Web Serial port),
and that both print paths send the same dots. Needs: pip install playwright && playwright install chromium
"""
import base64
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp(prefix="m50-page-")
failures = []

# A Web Serial port that answers like an M50, so the "this device" route can be tested headless.
MOCK_SERIAL = r"""
(() => {
  const status = 'M50|Kodak Label_0000|00:00:00:00:00:01|00:00:00:00:00:02|VP2.13|M50000000000|73|ready|0mm';
  let rxCtrl; window.__writes = []; let buf = [];
  const readable = new ReadableStream({ start(c) { rxCtrl = c; } });
  const send = (arr, d) => setTimeout(() => rxCtrl.enqueue(new Uint8Array(arr)), d || 30);
  const writable = new WritableStream({ write(chunk) {
    const a = Array.from(chunk); window.__writes.push(a); buf = buf.concat(a).slice(-16);
    if (a.length === 3 && a[0] === 0x10 && a[1] === 0xff && a[2] === 0x70) send(Array.from(status, ch => ch.charCodeAt(0)));
    if (buf.slice(-6).map(x => x.toString(16).padStart(2, '0')).join(' ') === '1d 0c 10 ff f1 45') { send([0xff, 0x0b], 50); send([0xaa], 500); }
  }});
  const port = { getInfo: () => ({ bluetoothServiceClassId: '00001101-0000-1000-8000-00805f9b34fb' }),
                 open: async () => {}, close: async () => {}, readable, writable };
  window.__granted = false;
  Object.defineProperty(Navigator.prototype, 'serial', { configurable: true, get() { return {
    getPorts: async () => window.__granted ? [port] : [],
    requestPort: async () => { window.__granted = true; return port; },
    addEventListener() {} }; } });
})();
"""


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        failures.append(what)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}/"


def api(method, path, body=None):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method)
    if body is not None:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=20) as resp:
        return resp.read()


def items(pg):
    return pg.evaluate("JSON.parse(localStorage.getItem('labelbench.draft')).design.items")


def start_bridge():
    env = dict(os.environ, FAKE_PRINTER="1", PORT=str(PORT), DATA_DIR=os.path.join(WORK, "data"), API_TOKEN="")
    log = open(os.path.join(WORK, "bridge.log"), "w")
    p = subprocess.Popen([sys.executable, "bridge.py"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            urllib.request.urlopen(BASE + "api/health", timeout=1)
            return p
        except OSError:
            time.sleep(0.1)
    raise SystemExit("bridge did not start")


def run(b):
    errs = []

    def watch(pg):
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append("console: " + m.text)
              if m.type == "error" and "ERR_" not in m.text and "Failed to load resource" not in m.text else None)

    print("editor")
    ctx = b.new_context(viewport={"width": 1180, "height": 900}, color_scheme="light")
    ctx.add_init_script(MOCK_SERIAL)
    pg = ctx.new_page()
    watch(pg)
    pg.goto(BASE)
    pg.wait_for_timeout(3000)
    check("Print server" in pg.inner_text("#pillText"), f"finds the print server ({pg.inner_text('#pillText')})")

    pg.click('[data-add="text"]')
    pg.keyboard.type("Hello")
    pg.wait_for_timeout(900)
    it = items(pg)[-1]
    check(it["type"] == "text" and it["text"] == "Hello", "adds text and types into it")

    box = pg.locator("#overlay").bounding_box()
    s = box["width"] / 240
    cx, cy = box["x"] + it["cx"] * s, box["y"] + it["cy"] * s
    pg.mouse.move(cx, cy); pg.mouse.down(); pg.mouse.move(cx + 30, cy + 10, steps=6); pg.mouse.up()
    pg.wait_for_timeout(300)
    it2 = items(pg)[-1]
    check(it2["cx"] > it["cx"] + 5, f"drags it ({it['cx']:.0f} -> {it2['cx']:.0f})")

    hb = pg.locator(".h-size").bounding_box()
    hx, hy = hb["x"] + hb["width"] / 2, hb["y"] + hb["height"] / 2
    pg.mouse.move(hx, hy); pg.mouse.down(); pg.mouse.move(hx + 40, hy + 15, steps=6); pg.mouse.up()
    pg.wait_for_timeout(300)
    it3 = items(pg)[-1]
    check(it3["w"] > it2["w"], f"resizes it ({it2['w']:.0f} -> {it3['w']:.0f})")

    rb = pg.locator(".h-rot").bounding_box()
    rx, ry = rb["x"] + rb["width"] / 2, rb["y"] + rb["height"] / 2
    pg.mouse.move(rx, ry); pg.mouse.down(); pg.mouse.move(rx + 60, ry + 40, steps=8); pg.mouse.up()
    pg.wait_for_timeout(300)
    check(items(pg)[-1]["rot"] != 0, f"rotates it ({items(pg)[-1]['rot']}°)")

    pg.click("#btnUndo"); pg.click("#btnUndo"); pg.wait_for_timeout(200)
    check(items(pg)[-1]["rot"] == 0 and round(items(pg)[-1]["w"]) == round(it2["w"]), "undo twice")
    pg.click("#btnRedo"); pg.wait_for_timeout(200)
    check(round(items(pg)[-1]["w"]) == round(it3["w"]), "redo once")

    pg.click('[data-drawer="emoji"]')
    pg.click('[data-emoji="🔋"]')
    pg.wait_for_timeout(300)
    check(items(pg)[-1]["type"] == "emoji" and items(pg)[-1]["text"] == "🔋", "tap adds an emoji")
    src = pg.locator('[data-emoji="❄️"]').bounding_box()
    pg.mouse.move(src["x"] + 20, src["y"] + 20); pg.mouse.down()
    pg.mouse.move(box["x"] + 30 * s, box["y"] + 30 * s, steps=12); pg.mouse.up(); pg.wait_for_timeout(300)
    e = items(pg)[-1]
    check(e["text"] == "❄️" and abs(e["cx"] - 30) < 20, f"drag drops an emoji where released ({e['cx']:.0f}, {e['cy']:.0f})")

    photo = os.path.join(WORK, "photo.png")
    Image.radial_gradient("L").resize((160, 120)).convert("RGB").save(photo)
    pg.set_input_files("#fileImg", photo); pg.wait_for_timeout(800)
    check(items(pg)[-1]["type"] == "image", "adds an image from a file")
    pg.evaluate("""async () => {
      const blob = await (await fetch('api/render.png?text=Drop')).blob();
      const file = new File([blob], 'drop.png', {type: 'image/png'});
      const dt = new DataTransfer(); dt.items.add(file);
      const r = document.querySelector('#overlay').getBoundingClientRect();
      document.querySelector('#tape').dispatchEvent(new DragEvent('drop', {dataTransfer: dt,
        clientX: r.left + r.width * 0.8, clientY: r.top + r.height * 0.5, bubbles: true, cancelable: true}));
    }""")
    pg.wait_for_timeout(900)
    check(items(pg)[-1]["type"] == "image" and items(pg)[-1]["cx"] > 150, "dropping an image file places it there")

    pg.click('[data-drawer="shapes"]'); pg.click('[data-shape="frame"]'); pg.wait_for_timeout(300)
    check(items(pg)[0]["type"] == "rect", "a frame goes to the back")

    pg.click("#templates button:nth-child(2)"); pg.wait_for_timeout(600)
    check([i["type"] for i in items(pg)] == ["emoji", "text"], "template replaces the label")

    print("saving and printing through the print server")
    pg.click("#btnSave"); pg.wait_for_timeout(600)
    check("print server" in pg.inner_text("#stageMsg"), f"saves ({pg.inner_text('#stageMsg')})")
    check(len(json.loads(api("GET", "api/labels"))["labels"]) == 1, "the print server has the saved label")
    pg.click("#t-saved"); pg.wait_for_timeout(500)
    check(pg.locator(".saved-card").count() == 1, "saved tab lists it")

    pg.click("#t-design")
    pg.click("#btnPrint"); pg.wait_for_timeout(2500)
    check("Printed 1 label through the print server" in pg.inner_text("#stageMsg"), f"prints ({pg.inner_text('#stageMsg')})")
    job = Image.open(io.BytesIO(urllib.request.urlopen(BASE + "api/last-job.png").read())).convert("L")
    page_png = pg.evaluate("""async () => { document.querySelector('#btnShowImage').click();
      await new Promise(r => setTimeout(r, 300)); const s = document.querySelector('#imgOut').src;
      document.querySelector('#imgDlg').close(); return s; }""")
    page_img = Image.open(io.BytesIO(base64.b64decode(page_png.split(",", 1)[1]))).convert("L")
    diff = sum(1 for a, c in zip(job.tobytes(), page_img.tobytes()) if (a < 128) != (c < 128))
    check(job.size == page_img.size and diff == 0, f"printed dots match the page ({job.size}, {diff} differ)")

    api("POST", "api/fake", {"off": True})
    pg.click("#btnPrint"); pg.wait_for_timeout(6500)
    msg = pg.inner_text("#stageMsg")
    check("out of range" in msg and "Use this device instead" in msg, "printer off: explains and offers this device")
    api("POST", "api/fake", {"off": False})

    pg.click("#t-printer"); pg.wait_for_timeout(300)
    pg.click("#btnBridgeCheck"); pg.wait_for_timeout(1500)
    check("73%" in pg.inner_text("#bridgeInfo"), "printer tab shows battery")

    pg.click("#t-saved"); pg.wait_for_timeout(400)
    d = pg.locator(".saved-card .danger").first
    d.dispatch_event("click"); d.dispatch_event("click"); pg.wait_for_timeout(500)
    check(pg.locator(".saved-card").count() == 0, "deletes a saved label (two taps)")

    print("printing from this device (mock Web Serial)")
    pg.click("#t-printer"); pg.check('input[name="route"][value="phone"]', force=True); pg.wait_for_timeout(200)
    pg.click("#t-design")
    pg.fill("#copies", "2")
    pg.click("#btnPrint"); pg.wait_for_timeout(4000)
    flat = " ".join(f"{x:02x}" for ch in pg.evaluate("window.__writes") for x in ch)
    starts = flat.count("00 00 00 00 00 00 00 00 00 00 00 00 10 ff f1 02 1d 76 30 00 0e 00")
    check(starts == 2 and flat.count("1d 0c 10 ff f1 45") == 2, f"two framed jobs sent ({starts})")

    print("keyboard")
    pg.locator("#overlay").focus()
    n0 = len(items(pg))
    pg.keyboard.press("Control+d"); pg.wait_for_timeout(900)
    n1 = len(items(pg))
    pg.keyboard.press("Delete"); pg.wait_for_timeout(900)
    n2 = len(items(pg))
    pg.keyboard.press("Control+z"); pg.wait_for_timeout(900)
    n3 = len(items(pg))
    check((n1, n2, n3) == (n0 + 1, n0, n0 + 1), f"duplicate, delete, undo ({n0} {n1} {n2} {n3})")
    pg.reload(); pg.wait_for_timeout(2500)
    check(len(items(pg)) == n3, "the draft survives a reload")
    ctx.close()

    print("phone layout")
    ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, color_scheme="dark")
    pg = ctx.new_page()
    watch(pg)
    pg.goto(BASE); pg.wait_for_timeout(2500)
    check(not pg.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"),
          "no sideways scrolling at 390 px")
    ctx.close()

    check(not errs, f"no page errors ({errs or 'none'})")


bridge = start_bridge()
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            run(browser)
        finally:
            browser.close()
finally:
    bridge.terminate()
    bridge.wait()
print("\n" + ("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}"))
if failures:
    print(f"bridge log kept in {WORK}")
else:
    shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if failures else 0)
