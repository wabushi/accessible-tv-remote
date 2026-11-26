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

The code uses sample GPIO assignments shown below; verify the pinout and strapping pins for the specific board before wiring. GPIO assignment can be changed in `main/main.c`.

The board advertises as a BLE keyboard. Pair it with the TV (or other BLE host) using the configured name. `BACK` sends standard keyboard Escape. The web app also exposes `BACK_XIAOMI`, which sends Keyboard-page usage `0xF1`; community captures report this usage for Back on Xiaomi Bluetooth Remote 2 Pro. That mapping is model/host dependent, so compare both controls on the target TV. The HID descriptor's key array includes usage `0xF1` for this option.

## TV network control investigation

If BLE key translation remains a problem, community implementations describe two possible network paths: Xiaomi MiLink (commonly TCP port 6091) on supported Xiaomi TVs, and Android TV Remote v2 (TLS, pairing required; commonly ports 6466/6467). Device support varies. Identify the TV model and its LAN IP before choosing or probing either path; these protocols are not implemented by this project yet. See [MiTV-Remote protocol notes](https://github.com/EndLess728/MiTV-Remote) for device-specific findings.

## Original Raspberry Pi prototype

The previous implementation is retained during the migration in `tv_remote.py`, `keyboard.txt`, `tv-remote.service`, and `wiring/`. Its Raspberry Pi and btferret installation instructions are no longer the build instructions for this ESP32-C3 firmware.

## License

See [LICENSE](LICENSE).
