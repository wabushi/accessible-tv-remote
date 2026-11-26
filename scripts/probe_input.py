#!/usr/bin/env python3
"""Step through the candidate codes of a key, reporting live, so you can watch the TV while it runs.

For every candidate it sends the key to the remote through the server, then reports whether the remote
actually delivered it (the server's queue for that device drains when the ESP32 acknowledges a send), and
leaves a pause so you can look at the TV. Type while it runs:

    y + Enter    the TV reacted to the key just sent: stop and print the keys.json entry to use
    r + Enter    send the same key again
    Enter        skip the rest of the wait, go to the next key
    q + Enter    quit

Risky keys (power, sleep, mute, volume) are skipped unless --allow-risky is given.
Standard library only; with pyserial installed, --serial also shows the board's own log lines live.

    python3 scripts/probe_input.py --device HID_HOT
"""

import argparse
import datetime
import json
import queue
import select
import sys
import threading
import time
import urllib.error
import urllib.request

RISKY = {"KB:66", "KB:7F", "KB:80", "KB:81", "CC:30", "CC:32", "CC:E2", "CC:E9", "CC:EA", "CC:CD", "SC:81", "SC:82"}


def now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def request(base, method, path, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data, {"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def device_state(base, device_id):
    for device in request(base, "GET", "/api/devices")["devices"]:
        if device["device_id"] == device_id:
            return device
    return None


def serial_watcher(port):
    """Print the board's send failures live (needs pyserial; opening the port may reset the board)."""
    try:
        import serial  # type: ignore
    except ImportError:
        print("  (pyserial not installed: board log disabled; `pip install pyserial` to enable --serial)")
        return
    try:
        link = serial.Serial(port, 115200, timeout=1)
        link.dtr = False
        link.rts = False
    except Exception as error:  # noqa: BLE001 - report any open failure
        print(f"  (could not open {port}: {error})")
        return
    buffer = b""
    while True:
        buffer += link.read(512)
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            text = line.decode(errors="replace").strip()
            if "Indicate Not Enabled" in text:
                print(f"  {now()}  board: Bluetooth host has not enabled notifications (re-pair the board on the TV)")
            elif "Command not sent" in text:
                print(f"  {now()}  board: could not send (no host, or host not subscribed)")
            elif "BLE HID connected" in text:
                print(f"  {now()}  board: Bluetooth host connected")
            elif "BLE HID disconnected" in text:
                print(f"  {now()}  board: Bluetooth host disconnected")


def read_input(lines):
    for line in sys.stdin:
        lines.put(line.strip().lower())


def wait_for_delivery(base, device_id, timeout):
    """Return seconds until the device's queue is empty, or None if it never drained."""
    start = time.time()
    while time.time() - start < timeout:
        state = device_state(base, device_id)
        if state is None or "pending" not in state:
            return -1.0  # server too old to report the queue
        if state["pending"] == 0:
            return time.time() - start
        time.sleep(0.4)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default="https://abuelatv.erezh.com", help="backend base URL")
    parser.add_argument("--device", required=True, help="device id of the remote paired with the TV, e.g. HID_HOT")
    parser.add_argument("--key", default="INPUT_XIAOMI", help="key whose variants are stepped through")
    parser.add_argument("--interval", type=float, default=8.0, help="seconds to look at the TV after each key")
    parser.add_argument("--start", type=int, default=1, help="1-based candidate number to start at")
    parser.add_argument("--serial", metavar="PORT", help="also show the board's log live, e.g. /dev/ttyACM0")
    parser.add_argument("--allow-risky", action="store_true", help="also send power/mute/volume candidates")
    args = parser.parse_args()
    base = args.server.rstrip("/")

    try:
        keys = request(base, "GET", "/api/keys")["keys"]
    except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
        sys.exit(f"Could not read {base}/api/keys: {error}")
    entry = next((k for k in keys if k["key"] == args.key), None)
    if not entry or not entry.get("variants"):
        sys.exit(f"{args.key} has no variants on the server. Known keys: {[k['key'] for k in keys]}")
    candidates = [v for v in entry["variants"] if args.allow_risky or v["key"].upper() not in RISKY]
    skipped = len(entry["variants"]) - len(candidates)

    state = device_state(base, args.device)
    if state is None:
        sys.exit(f"Device {args.device} is not registered on {base}.")
    print(f"{now()}  {args.device} is {'online' if state['online'] else 'OFFLINE'}; "
          f"{len(candidates)} candidates for {args.key}" + (f" ({skipped} risky skipped)" if skipped else ""))
    if not state["online"]:
        print("  The remote is offline; nothing will be delivered until it polls the server again.")
    if state.get("pending"):
        print(f"  WARNING: {state['pending']} command(s) already waiting in its queue; they will fire first.")
    print("  Type y + Enter when the TV reacts, r to resend, Enter to skip the wait, q to quit.\n")

    if args.serial:
        threading.Thread(target=serial_watcher, args=(args.serial,), daemon=True).start()
    typed = queue.Queue()
    threading.Thread(target=read_input, args=(typed,), daemon=True).start()

    results = []
    index = max(args.start, 1) - 1
    while index < len(candidates):
        candidate = candidates[index]
        label = f"[{index + 1:2}/{len(candidates)}] {candidate['key']:7} {candidate['label']}"
        try:
            request(base, "POST", "/api/commands", {"device_id": args.device, "key": candidate["key"]})
        except (urllib.error.URLError, OSError, ValueError) as error:
            print(f"{now()}  {label}\n          NOT SENT: {error}")
            break
        sent_at = time.time()
        print(f"{now()}  {label}\n          queued ...", end="", flush=True)
        delay = wait_for_delivery(base, args.device, 8)
        if delay is None:
            print(" STUCK: the remote did not deliver it within 8 s.")
            print("          The remote's Bluetooth host is not accepting keys (re-pair it on the TV: forget the device, pair again).")
            print(f"          Clear its queue: curl -X DELETE {base}/api/devices/{args.device}")
            results.append((candidate, "stuck"))
            break
        elif delay < 0:
            print(" sent (this server does not report delivery; deploy the latest server.py for that).")
            results.append((candidate, "queued"))
        else:
            print(f" DELIVERED to the TV in {delay:.1f} s: look at the TV now.")
            results.append((candidate, "delivered"))

        action = None
        while time.time() - sent_at < args.interval:
            try:
                action = typed.get(timeout=0.3)
            except queue.Empty:
                continue
            break
        if action == "q":
            break
        if action == "r":
            continue  # resend the same candidate
        if action == "y":
            usage = candidate["key"]
            prefix, value = usage.split(":")
            page = {"KB": "keyboard", "CC": "consumer", "SC": "system"}[prefix]
            print(f"\n{now()}  Marked as working: {candidate['key']} ({candidate['label']})")
            print("  In backend/keys.json set the key's \"hid\" to this and delete its \"variants\":")
            print(f'    "hid": {{"page": "{page}", "usage": "0x{value.upper()}"}}')
            return
        index += 1

    print(f"\n{now()}  Summary")
    for candidate, outcome in results:
        print(f"  {candidate['key']:7} {candidate['label']:40} {outcome}")
    print("  No candidate was marked as working." if results else "")


if __name__ == "__main__":
    main()
