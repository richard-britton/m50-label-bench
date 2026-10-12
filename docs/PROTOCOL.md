# Kodak Label Era M50 print protocol

Worked out from Android Bluetooth logs of the official app, then confirmed by printing. Observed
on firmware VP2.13. Anything marked *unclear* is a best guess.

## Connection

- **Bluetooth Classic, Serial Port Profile.** RFCOMM channel 1, advertised over SDP with the
  standard serial port UUID `00001101-0000-1000-8000-00805f9b34fb`.
- The printer shows up twice when scanning: `Kodak Label_XXXX` (Classic, the one to pair and print
  to) and `Kodak Label_XXXX_iphone` (Bluetooth Low Energy, used by the iOS app; not covered here).
  Each has its own address.
- **One client at a time.** While connected to one device, it refuses others.
- It often hangs up straight after pairing, before the host has read its services; connecting once
  more fixes that. Connections at the edge of range can take tens of seconds to fail.

## Printhead and image

- 203 dpi (8 dots per mm), **112 dots** across the head: 14 bytes per raster line.
- Labels feed lengthways: each raster line runs across the label, and the first line is the
  label's leading (left, as you read it) edge. A 30 mm label is 240 lines. About 29 mm is printable.
- So a label designed in reading orientation (width = length, height = 112 dots) is turned 90°
  clockwise before sending: raster line *n* is label column *n*, and bit *x* in that line is label
  row 111 − *x*.
- 1 bit per dot, most significant bit first, **1 = black**.

## Status

Send:

```
10 ff 70
```

The printer answers in ASCII:

```
M50|Kodak Label_XXXX|<classic address>|<BLE address>|VP2.13|<serial>|<battery %>|<state>|<n>mm
```

- `state` is `ready` when it can print. Other values appear with no paper or the lid open (*not
  catalogued*).
- The last field is the label length it has measured: `0mm` after switching on, until it has fed a
  label, then for example `30mm`.

## Printing one label

```
00 00 00 00 00 00 00 00 00 00 00 00     12 zero bytes (unclear; the app always sends them)
10 ff f1 02                             start of job
1d 76 30 00 xL xH yL yH <data>          ESC/POS "GS v 0" raster image: x = bytes per line (14),
                                        y = number of lines, little-endian; then x × y bytes
1d 0c                                   ESC/POS "GS FF": feed to the start of the next label
10 ff f1 45                             end of job
```

While it works, the printer sends:

| Bytes | Meaning |
|---|---|
| `ff 0b` | printing |
| `aa` | label finished |
| `ff 00` | idle (*unclear*) |
| `ff 40` | seen when it got stuck (*unclear*) |

## Several labels

Send one complete job per label (start, raster, feed, end) and **wait for `aa`** before sending the
next. Checking the status between labels is cheap and catches an empty roll. Without the start and
end framing, or without waiting, the second label stutters and the third doesn't print at all.

## Unknowns

- Print density and speed settings: the app never sent any that we saw.
- The full list of `state` values and notification bytes.
- Whether other Kodak Label Era models use the same commands (the head width will differ). The
  designer's **Printer → Advanced** section can decode an Android Bluetooth HCI log
  (`btsnoop_hci.log`) to help find out; issues with findings are welcome.
