# 🏷️ M50 Label Bench

A label designer and print server for the **Kodak Label Era M50** thermal label printer, because
the official app makes simple things hard.

- **Free-layout designer in the browser.** Text, emoji, images, QR codes and shapes. Drag, resize,
  rotate and overlap them; undo, templates, and a shared library of saved labels.
- **Print from anything** through a small print server (the *bridge*) with a Bluetooth adapter
  near the printer: phones, iPhones, laptops and Home Assistant automations.
- **Or print straight from the browser** over Bluetooth, with no server at all: Chrome on Android,
  or Chrome/Edge on a computer.
- **Home Assistant**: a `Print label` action for dashboards and automations, emoji included.
- **Plays nicely.** The bridge connects only while printing, so the printer stays free for phones.

Unofficial; not affiliated with Kodak or Prinics. Tested with an M50 on firmware VP2.13 and 30 mm
labels.

```
browser ──https──> reverse proxy (login, adds token) ──> bridge ──D-Bus──> bluetoothd ──Bluetooth──> M50
Home Assistant ──http + token──────────────────────────> bridge
Chrome on a phone or computer ──Bluetooth (Web Serial)──────────────────────────────────────────> M50
```

## Contents

- [What you need](#what-you-need)
- [Pairing](#pairing)
- [Running the bridge](#running-the-bridge)
- [HTTPS and logins (reverse proxy)](#https-and-logins-reverse-proxy)
- [Home Assistant](#home-assistant)
- [Using the designer](#using-the-designer)
- [Troubleshooting](#troubleshooting)
- [Configuration](#configuration)
- [API](#api)
- [Development](#development)

## What you need

- A **Kodak Label Era M50**.
- For the print server: a **Linux machine with Bluetooth** within range of where the printer lives
  (a few metres; walls cut it down), running **Docker** and **BlueZ**. Most distributions and
  Raspberry Pi OS have BlueZ already. A cheap USB dongle is fine; a Realtek RTL8761BU one (for
  example TP-Link UB500) works out of the box.
- For printing straight from a device: **Chrome on Android**, or **Chrome or Edge on a computer**,
  with the page served over **HTTPS** (or from `localhost`). iPhones and iPads can't do this, as iOS
  has no Web Serial; they print through the bridge instead.

Home Assistant's own Bluetooth integration only handles Bluetooth Low Energy, while the M50 prints
over Classic Bluetooth. That's why there's a bridge.

## Pairing

Pair the printer with the machine that runs the bridge once. Keep the printer next to it while you
do this, and make sure no phone is connected to the printer (close the Kodak app or the designer).

1. Check the adapter:

   ```bash
   bluetoothctl show              # a controller with "Powered: yes"
   systemctl status bluetooth     # "active (running)"; if missing: sudo apt install bluez
   rfkill list                    # "Soft blocked: yes" means switched off: sudo rfkill unblock bluetooth
   ```

2. Pair:

   ```bash
   bluetoothctl
   # then, inside bluetoothctl:
   power on
   agent on
   default-agent
   scan on                        # wait for "Kodak Label_XXXX" and note its address
   scan off
   pair AA:BB:CC:DD:EE:FF         # answer yes if asked to confirm; if asked for a PIN, try 0000
   info AA:BB:CC:DD:EE:FF         # should say "Paired: yes" and list "UUID: Serial Port"
   disconnect AA:BB:CC:DD:EE:FF
   quit
   ```

   The printer shows up twice: `Kodak Label_XXXX` is the one to pair. `Kodak Label_XXXX_iphone` is
   its Bluetooth Low Energy side, used by the iOS app, and can't be paired.

3. If `info` doesn't list **Serial Port**, the printer hung up before BlueZ could read its services.
   Run `bluetoothctl connect AA:BB:CC:DD:EE:FF` (again if it times out) until `info` lists it. That
   command ends with `br-connection-profile-unavailable`, which is expected.

If `power on` fails with `org.bluez.Error.Failed`, check `rfkill list` first.

If a phone that used to print can't connect after this, forget the printer in the phone's Bluetooth
settings and pair it again there.

## Running the bridge

```bash
git clone https://github.com/richard-britton/m50-label-bench.git
cd m50-label-bench
cp .env.example .env
sed -i "s/^LABEL_BRIDGE_TOKEN=.*/LABEL_BRIDGE_TOKEN=$(openssl rand -hex 24)/" .env
docker compose up -d --build
docker logs label-bridge
```

The log should say `Bluetooth via host bluetoothd over D-Bus`, name your adapter, and report the
printer it found (`printer Kodak Label_XXXX (AA:BB:…) is paired, found by name`). If anything is
missing it says what. With more than one M50 paired, set `PRINTER_MAC` in `.env`.

Try it from the same machine:

```bash
source .env
curl -s -H "X-Label-Token: $LABEL_BRIDGE_TOKEN" "http://localhost:${LABEL_BRIDGE_PORT:-8091}/api/status?fresh=1"
curl -s -H "X-Label-Token: $LABEL_BRIDGE_TOKEN" -H "Content-Type: application/json" \
     -d '{"text":"Hello 👋"}' "http://localhost:${LABEL_BRIDGE_PORT:-8091}/api/print"
```

The designer is at `http://<host>:8091`. Opened directly like this, it needs the token: enter it
under **Printer → Print server settings**. For anything beyond a quick test, put it behind a
reverse proxy (next section), which also gives you HTTPS for printing from phones.

### Why the container looks like this

Linux only lets processes on the host's network open Bluetooth sockets, whatever capabilities they
have. Rather than run on the host network, the bridge asks the host's Bluetooth service
(bluetoothd) to open the connection and pass it over D-Bus. So the container:

- mounts `/run/dbus`, and runs on an ordinary Docker network with a mapped port;
- drops every capability except reading and writing its own files, and can't gain privileges;
- sets `apparmor:unconfined`, because Ubuntu's default Docker AppArmor profile blocks D-Bus. If you
  remove that line and D-Bus is blocked, the log says so.

**Host networking instead.** If you'd rather not mount D-Bus, the bridge can open Bluetooth sockets
itself. Pairing is then optional but the address is required. Replace `ports`, the `/run/dbus`
volume, `cap_drop`/`cap_add` and the AppArmor line with:

```yaml
    network_mode: host
    environment:
      BT_MODE: socket
      PRINTER_MAC: "AA:BB:CC:DD:EE:FF"
      PORT: "8091"            # the bridge listens on the host directly, so pick a free port
```

### Published image

Instead of building, you can use the image the GitHub workflow publishes for x86 and ARM: in
`docker-compose.yml`, replace `build: .` with `image: ghcr.io/richard-britton/m50-label-bench:latest`.

## HTTPS and logins (reverse proxy)

A reverse proxy in front of the bridge does three jobs:

1. **HTTPS**, which browsers require before they'll print over Bluetooth from the page.
2. **A login** (Authelia, Authentik, basic auth, or a VPN such as Tailscale), so only your people
   can print and see the saved labels.
3. **Adds the token** to each request (`X-Label-Token`), so browsers never need it. You won't see
   it in the browser's developer tools; it's added after the browser.

Examples: [`examples/traefik.yml`](examples/traefik.yml) and [`examples/Caddyfile`](examples/Caddyfile).
With nginx:

```nginx
location / {
    proxy_pass http://192.168.1.10:8091;
    proxy_set_header X-Label-Token "your-token";
    proxy_read_timeout 180s;
}
```

Allow up to three minutes per request: a printer at the edge of range can take a minute to
connect. To check the token is being added, sign in and open `https://<your host>/api/status?probe=0`.
It should show `"ok": true`, not `Missing or wrong access token`.

## Home Assistant

1. If you don't use packages yet, add this to `configuration.yaml`:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

2. Copy [`homeassistant/label_printer.yaml`](homeassistant/label_printer.yaml) to
   `config/packages/`, and add to `secrets.yaml`:

   ```yaml
   label_bridge_print_url: http://BRIDGE_HOST:8091/api/print
   label_bridge_token: the same token as in the bridge's .env
   ```

3. Restart Home Assistant and try **Developer tools → Actions → Script: Print label**.

In automations:

```yaml
action: script.print_label
data:
  text: "Soup 🍲\nmade {{ now().strftime('%-d %b') }}"
```

If the printer is off or out of range, a notification says what went wrong.

For dashboards there's a [button card](homeassistant/dashboard-card.yaml) (open the designer, quick
label) and a [how-to card](homeassistant/markdown-card.md) to explain it all to the rest of the
house.

## Using the designer

- **Add things** from the toolbar: text, emoji, images, QR codes, shapes. With a mouse or pen you
  can also drag an emoji straight to the spot you want.
- **Arrange freely.** Drag to move, the corner handle resizes, the top handle rotates. Items can
  overlap; tap a selected item again to pick the one underneath.
- **Drop or paste** an image file or text onto the label.
- **Keyboard:** arrows nudge (Shift for bigger steps), Delete removes, Ctrl+D duplicates,
  Ctrl+Z / Ctrl+Shift+Z undo and redo.
- **Label length** and **templates** are on the Label card. Your work in progress survives a reload.
- **Saved labels** are kept on the print server, so everyone sees the same library. Without a print
  server they stay in that browser.

**Three ways to print**, under **Printer**:

- **Print server**: through the bridge. Works from any device, including iPhones.
- **This device**: straight over Bluetooth from Chrome or Edge. Pair the printer in the device's
  Bluetooth settings first. Some Windows computers only find it with **Bluetooth & devices → Devices
  → Device settings → Bluetooth devices discovery** set to **Advanced**.
- **Whichever works** (default): the print server when it's reachable, otherwise this device. If the
  print server can't reach the printer, the error offers to use this device instead.

The printer accepts one connection at a time. After printing from a device, the page stays
connected for two minutes so the next print is quick; prints through the bridge in that time get
`busy`.

If prints sit off-centre, use **Printer → Calibration**: print the ruler, then adjust **Shift along
the label**. The printer reports a label size of `0mm` until it has measured a label after being
switched on, and the first label can land slightly off while it does.

## Troubleshooting

Most problems are Bluetooth on the host rather than the bridge. In order:

1. **Restart Bluetooth on the host.** This clears BlueZ getting stuck on an old connection attempt,
   for example a `busy` error that doesn't go away. The bridge reconnects by itself.

   ```bash
   sudo systemctl restart bluetooth
   ```

2. **Read the logs.** Lines like `Unable to get M50 label bridge SDP record: Connection timed out`
   or `Host is down` mean the printer is off or the signal is too weak. Move the printer or the
   dongle; a short USB extension cable away from the computer's USB 3 ports helps.

   ```bash
   journalctl -u bluetooth --since "-30min" --no-pager | tail -60
   docker logs label-bridge | tail -20
   ```

3. **Check the pairing** with `bluetoothctl info AA:BB:CC:DD:EE:FF`: `Paired: yes` and
   `UUID: Serial Port` (see [Pairing](#pairing)).

4. **Check the adapter** with `rfkill list` and `bluetoothctl show`.

| Error code | Meaning | Fix |
|---|---|---|
| `unreachable` | Printer off, asleep or out of range | Switch it on; move it or the adapter closer |
| `busy` | Printer connected to a phone, or BlueZ still busy with an earlier attempt | Close the page or Kodak app on the phone; wait a minute; restart Bluetooth |
| `not_ready` | The printer reports a problem (paper, lid) | Check the roll and the lid |
| `pairing` | Not paired, pairing lost, or BlueZ hasn't read the printer's services | [Pairing](#pairing); to start over, `bluetoothctl remove` the printer first |
| `no_adapter` | No working Bluetooth adapter, or bluetoothd isn't running | `bluetoothctl show`, `systemctl status bluetooth`, `rfkill list` |
| `config` | D-Bus not mounted or blocked by AppArmor, or several printers paired | Check `docker-compose.yml`; set `PRINTER_MAC` |
| `timeout` | Label sent but not confirmed | Switch the printer off and on |
| `token` | Wrong or missing access token | Check `.env`, the proxy and Home Assistant's `secrets.yaml` match |

Errors from BlueZ end with its own reason in brackets, for example
`(BlueZ: br-connection-page-timeout)`.

If the page itself shows `can't read web/index.html`, fix the file permissions as the message
says.

## Configuration

Set these in `.env`:

| Setting | Default | What it does |
|---|---|---|
| `LABEL_BRIDGE_TOKEN` | (none) | Access token; blank turns it off (trusted networks only) |
| `LABEL_BRIDGE_PORT` | `8091` | Port on the host |
| `PRINTER_MAC` | (none) | Printer address; blank uses the paired printer named `Kodak Label_…` |
| `TZ` | `Etc/UTC` | Time zone for log timestamps |

And these under `environment` in `docker-compose.yml`, if you need them:

| Setting | Default | What it does |
|---|---|---|
| `DEFAULT_LENGTH_MM` | `30` | Label length for plain text printed through the API |
| `OFFSET_MM` | `0` | Shift along the label for API prints (the page has its own) |
| `BT_MODE` | `auto` | `bluez`, `socket`, or `auto`: sockets when on the host network, otherwise BlueZ |
| `PRINTER_NAME` | `Kodak Label` | Name prefix used to find the printer when `PRINTER_MAC` is blank |
| `RFCOMM_CHANNEL` | `1` | Serial channel, `socket` mode only (BlueZ looks it up itself) |
| `HEAD_DOTS` | `112` | Printhead width in dots |
| `ROTATION` | `cw` | Which way the label is turned on its way to the printhead |
| `PORT`, `BIND` | `8091`, `0.0.0.0` | Where the bridge listens inside the container |
| `FAKE_PRINTER` | `0` | `1` simulates a printer, for trying things out |

## API

All `/api` calls except `/api/health` need the `X-Label-Token` header (or `Authorization: Bearer`)
when a token is set.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | Liveness check |
| GET | `/api/status?probe=1&fresh=1` | Bridge info and the printer's state and battery (`probe=0` skips the printer) |
| POST | `/api/print?copies=1&offset_mm=0` | Print. Body: a PNG, or JSON (below) |
| GET | `/api/render.png?text=…` | Preview how the bridge renders text |
| GET | `/api/labels` | Saved labels |
| PUT / DELETE | `/api/labels/<id>` | Save or delete a label |

JSON print body: `{"text": "Hello 👋", "copies": 1, "length_mm": 30, "align": "center", "invert": false}`,
or `{"image": "<base64 PNG>"}` instead of `text`. Images are scaled to the 112-dot printhead and
dithered. The answer is `{"ok": true, "printed": 1, "printer": {…status…}}`, or
`{"ok": false, "code": "unreachable", "error": "…"}` with a matching HTTP status.

## Development

Run the bridge without a printer, then open `http://localhost:8091`:

```bash
sudo apt install python3-pil python3-fonttools fonts-dejavu-core
FAKE_PRINTER=1 PORT=8091 DATA_DIR=./data python3 bridge.py
```

- The designer is the single file `web/index.html`; edit and reload. To try changes in Docker
  without rebuilding, add `- ./web:/app/web:ro` to the volumes.
- `bluez.py` is a small D-Bus client (standard library only) that gets the printer connection from
  bluetoothd through BlueZ's Profile API.
- How the printer talks is in [docs/PROTOCOL.md](docs/PROTOCOL.md). The designer's
  **Printer → Advanced** section has a console and a decoder for Android Bluetooth logs, handy for
  working out other models.

Tests, also run by GitHub Actions:

```bash
# the bridge over a real dbus-daemon, against a mock bluetoothd (needs dbus, python3-dbus, python3-gi)
python3 tests/test_bluez.py
# the designer in headless Chromium (needs: pip install playwright && playwright install chromium)
python3 tests/test_page.py
```

## Licence

MIT. Fonts downloaded at build time: Archivo and Noto Emoji (SIL Open Font Licence). QR codes use
[qrcode-generator](https://github.com/kazuhikoarase/qrcode-generator) (MIT), loaded from jsDelivr.
Kodak is a trademark of Eastman Kodak Company; this project isn't affiliated with or endorsed by
Kodak or Prinics.
