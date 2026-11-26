# Accessible TV Remote

Firmware for an accessible physical remote built around an ESP32-C3. The device reads five active-low switches and sends the same keyboard sequences as the original Raspberry Pi prototype over Bluetooth LE HID.

## Current migration scope

This branch ports the existing physical button mappings to ESP32-C3 firmware and adds a Wi-Fi command path. The device advertises as a BLE keyboard, polls the backend using outbound HTTP or HTTPS POST requests, and sends received commands to its paired host as key presses.

| Button | Original Pi BCM pin | ESP32-C3 sample GPIO | Keyboard sequence |
|---|---:|---:|---|
| 1 | 25 | 0 | `1 → 5 → 7` |
| 2 | 24 | 1 | `6 → 8` |
| 3 | 23 | 2 | `6 → 9` |
| 4 | 27 | 3 | `1 → 5` |
| 5 | 26 | 4 | `0 → 0 → 0` |

GPIO values are example assignments, not header pin numbers. Check the pinout and boot-strapping constraints for the exact ESP32-C3 board before wiring. Each switch should connect its GPIO to ground when pressed; firmware enables the internal pull-up. Use 3.3 V logic only.

## Build and provision

Activate ESP-IDF using Espressif's setup instructions, then from this directory run:

```bash
python3 scripts/flash.py
```

The script prompts for the serial port, Wi-Fi SSID/password, Bluetooth HID name, and backend base URL, then builds and flashes the firmware and provisions its NVS partition. The Bluetooth HID name is also the ESP32's device ID, so give every board a unique name; press Enter to reuse the saved name when reflashing the same board. The script remembers values in the git-ignored `.flash_config.json` file; press Enter to reuse a displayed value. The saved Wi-Fi password is displayed in the prompt so it can be reviewed; press Enter to reuse it, or enter `!` to clear it (useful for an open Wi-Fi network). The config file is restricted to the current user's permissions, but it stores the Wi-Fi password in plain text. Temporary provisioning files are removed when the script exits. Settings are also stored in the ESP32's NVS partition (not encrypted by this setup). To build without provisioning, use `idf.py build`.

## Backend remote

Build the React remote once, then run the Python server on a machine reachable by the ESP32:

```bash
npm --prefix backend/web install
npm --prefix backend/web run build
python3 backend/server.py
```

Open `https://<server-host>/` when the server is behind HTTPS (or `http://<server-address>:8080/` on a trusted local network). Each ESP32 registers on its first successful poll and appears as a selectable tab; the tab's green dot indicates that the device has polled recently. Commands are queued separately for the selected device. The browser provides arrows, OK/Enter, Back, and digits 0–9. `backend/devices.json` stores registered device names across server restarts and is generated locally. Delete a registration with `DELETE /api/devices/<device_id>`; this removes it from the in-memory and persisted registry, and returns 404 if it is not registered. The browser queues a key with `POST /api/commands`; each ESP32 polls `POST /api/poll` every 700 ms and acknowledges a key at `POST /api/ack` after sending it. Command queues are in memory and cleared if the server restarts. Provision the server's base URL when flashing each board. The current firmware validates HTTPS certificates using ESP-IDF's certificate bundle. Rebuild and reflash boards running older firmware before using an HTTPS URL; otherwise their polls can fail TLS validation and they will not register. The server has no login or command authentication, so use an appropriately protected network and deployment.

### Adding buttons without reflashing

The ESP32 is a pipe: it pairs over Bluetooth and sends whatever HID reports the server tells it to. The server owns everything Bluetooth-specific, so changing keys, usages, the HID report map or the USB vendor/product ID never needs a reflash.

- **Commands.** Each command the ESP32 polls carries raw reports, for example `{"id": "...", "key": "INPUT_XIAOMI", "reports": [{"id": 1, "data": "0000730000000000", "hold_ms": 50}]}`. The firmware sends each report, waits `hold_ms`, sends an all-zero release of the same length, and acknowledges. It has no key tables and no usage limits.
- **Keys.** [`backend/keys.json`](backend/keys.json) defines the extra keys. An entry is either `"hid": {"page": "keyboard" | "consumer" | "system", "usage": "0x35"}` (the server builds the report) or explicit `"reports": [{"id", "data" (hex), "hold_ms"}]` for anything else. Entries with `"group": "extra"` appear in the TV controls grid, `"group": "pad"` in the top-left cell of the direction pad. The server picks up edits without a restart and the web app on the next load. Raw usages also work with no file edit through `POST /api/commands`: `KB:4A`, `CC:223` or `SC:B5` (keyboard, consumer, system-control usage, hex).
- **Variants.** A key can list `"variants"` (candidate usages); its web button sends the current candidate on tap and switches to the next on a long press (or Shift+Enter), remembering the choice in the browser. The Xiaomi input button uses this because no documented HID code for it exists: cycle until the TV shows its input menu, then put that code in the key's `hid` and remove `variants`.
- **Bluetooth profile.** The server also pushes the Bluetooth identity: USB vendor/product ID and the HID report map (report ids 1 keyboard, 2 consumer, 3 system control). The default is in `DEFAULT_PROFILE` in `backend/server.py` (Xiaomi Remote 2 Pro IDs `0x2717`/`0x32B8`) and can be overridden with a `"profile": {"vid", "pid", "report_map_hex"}` object in `keys.json`. When a board reports a different revision in its poll, the server sends the new profile; the board stores it in NVS and restarts once to apply it. A stored profile that crashes the board is dropped after 3 boots and reported back, so the server does not resend that revision. After a profile change **forget the board on the TV and pair it again**; Bluetooth hosts cache the report map and IDs.
- **What still needs a flash:** only things the firmware itself does, such as the five physical switches' compiled-in key sequences, or a new kind of command beyond HID reports. Older firmware that predates this protocol (names only, no `reports`) cannot use any of it; flash the current firmware once.

### Changing a remote's Wi-Fi from the web app

Click the already-selected device tab (it shows a gear) to open the Wi-Fi overlay, enter the new network name and password, and press *Change Wi-Fi*. `POST /api/wifi` queues the change; the ESP32 receives it once in its next poll response (`"wifi": {"id", "ssid", "password"}`), tries the new network first, and keeps it only if it gets an IP and can reach the backend through it (then it saves it to NVS). Otherwise it restores the previous network, so a typo cannot strand the board, and reports the outcome to `POST /api/wifi/result`. The overlay shows pending / trying / connected / failed. The password is held in server memory only until delivery, is never written to disk or returned by any GET, and an undelivered change expires after 10 minutes.

Because it carries a password, the server only accepts and delivers Wi-Fi changes over HTTPS (`X-Forwarded-Proto: https` from the reverse proxy) or from a direct loopback/private-LAN connection. A board that can reach the backend only over plain HTTP (see below) therefore cannot be reconfigured remotely. The server still has no login, so anyone who can open the web app can change a remote's Wi-Fi; deploy it behind appropriate access control.

### Boards on networks that only allow port 80

Some networks let the ESP32 open outbound TCP connections on port 80 only; connects to 443 (and every other port) time out with `esp-tls: select() timeout` / `ESP_ERR_HTTP_CONNECT` before TLS starts, even though a PC on the same network reaches the HTTPS server. [`deploy/nginx/`](deploy/nginx/) contains an nginx site that serves only `POST /api/poll` and `POST /api/ack` over plain HTTP on port 80 and redirects everything else (web UI, `/api/commands`, recording, playback, device deletion) to HTTPS. Deploy it, then flash those boards with the plain base URL, for example `http://abuelatv.erezh.com` (no port, no redirect). Boards on unrestricted networks can keep using the `https://` URL. Plain-HTTP device traffic is unencrypted and unauthenticated: it exposes device IDs and key names, and anyone on the path could forge or replay polls and acknowledgements.

The code uses sample GPIO assignments shown below; verify the pinout and strapping pins for the specific board before wiring. GPIO assignment can be changed in `main/main.c`.

The board advertises as a BLE keyboard. Pair it with the TV (or other BLE host) using the configured name. `BACK` sends standard keyboard Escape. The web app also exposes `BACK_XIAOMI`, which sends Keyboard-page usage `0xF1`; community captures report this usage for Back on Xiaomi Bluetooth Remote 2 Pro. That mapping is model/host dependent, so compare both controls on the target TV. The HID descriptor's key array includes usage `0xF1` for this option.

## TV network control investigation

If BLE key translation remains a problem, community implementations describe two possible network paths: Xiaomi MiLink (commonly TCP port 6091) on supported Xiaomi TVs, and Android TV Remote v2 (TLS, pairing required; commonly ports 6466/6467). Device support varies. Identify the TV model and its LAN IP before choosing or probing either path; these protocols are not implemented by this project yet. See [MiTV-Remote protocol notes](https://github.com/EndLess728/MiTV-Remote) for device-specific findings.

## Original Raspberry Pi prototype

The previous implementation is retained during the migration in `tv_remote.py`, `keyboard.txt`, `tv-remote.service`, and `wiring/`. Its Raspberry Pi and btferret installation instructions are no longer the build instructions for this ESP32-C3 firmware.

## License

See [LICENSE](LICENSE).
