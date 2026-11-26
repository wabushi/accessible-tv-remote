#!/usr/bin/env python3
"""Small single-process HTTP command queue and web remote for the ESP32-C3."""

from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from mimetypes import guess_type
import hashlib
import ipaddress
import json
import os
import re
import threading
import time
import uuid
from urllib.parse import unquote, urlsplit

HOST = os.environ.get("TV_REMOTE_HOST", "0.0.0.0")
PORT = int(os.environ.get("TV_REMOTE_PORT", "8080"))
KEYS_FILE = Path(__file__).resolve().parent / "keys.json"
# Keys the web app has always had; keys.json can add more (or override these).
CORE_KEYS = {"UP": 0x52, "DOWN": 0x51, "LEFT": 0x50, "RIGHT": 0x4F, "ENTER": 0x28,
             "BACK": 0x29, "BACK_XIAOMI": 0xF1, "0": 0x27,
             **{str(n): 0x1E + n - 1 for n in range(1, 10)}}

# The server owns everything Bluetooth-specific; the ESP32 only forwards raw HID reports.
# Report ids of the HID report map in DEFAULT_PROFILE (and of the firmware's built-in copy of it).
REPORT_IDS = {"keyboard": 1, "consumer": 2, "system": 3}
HID_PAGES = {"keyboard": 0xFF, "consumer": 0x3FF, "system": 0xFF}   # page -> highest usage the report fields hold
RAW_PREFIX = {"KB": "keyboard", "CC": "consumer", "SC": "system"}
PAGE_PREFIX = {page: prefix for prefix, page in RAW_PREFIX.items()}
RAW_KEY_RE = re.compile(r"(KB|CC|SC):([0-9A-Fa-f]{1,4})\Z")
MAX_REPORT_BYTES = 64
DEFAULT_PROFILE = {
    "vid": 0x2717, "pid": 0x32B9,   # the Xiaomi remote's IDs as seen on a MiTV (Vendor_2717_Product_32b9.kl), so the TV applies its own key layout
    "report_map": bytes([0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01, 0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02, 0x95, 0x01, 0x75, 0x08, 0x81, 0x03, 0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x05, 0x07, 0x19, 0x00, 0x2A, 0xFF, 0x00, 0x81, 0x00, 0xC0, 0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x02, 0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00, 0xC0, 0x05, 0x01, 0x09, 0x80, 0xA1, 0x01, 0x85, 0x03, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x19, 0x00, 0x29, 0xFF, 0x75, 0x08, 0x95, 0x01, 0x81, 0x00, 0xC0]),
}
_keys_cache = {"mtime": None, "catalog": {}, "profile": None}


def _parse_hid(entry):
    hid = entry.get("hid") if isinstance(entry, dict) else None
    if not isinstance(hid, dict) or hid.get("page") not in HID_PAGES:
        return None
    try:
        usage = int(hid.get("usage"), 0) if isinstance(hid.get("usage"), str) else hid.get("usage")
    except ValueError:
        return None
    if isinstance(usage, bool) or not isinstance(usage, int) or not 1 <= usage <= HID_PAGES[hid["page"]]:
        return None
    return {"page": hid["page"], "usage": usage}


def reports_for_hid(hid):
    """Raw HID reports for one key press; the firmware sends each, waits hold_ms, then sends zeros (release)."""
    page, usage = hid["page"], hid["usage"]
    if page == "keyboard":
        data = bytes([0, 0, usage, 0, 0, 0, 0, 0])
    elif page == "consumer":
        data = usage.to_bytes(2, "little")
    else:
        data = bytes([usage])
    return [{"id": REPORT_IDS[page], "data": data.hex(), "hold_ms": 50}]


def _parse_reports(value):
    """Validate explicit raw reports from keys.json: [{"id": 1-255, "data": "hex", "hold_ms": 10-2000}]."""
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        return None
    reports = []
    for item in value:
        if not isinstance(item, dict):
            return None
        report_id, data, hold = item.get("id"), item.get("data"), item.get("hold_ms", 50)
        if (isinstance(report_id, bool) or not isinstance(report_id, int) or not 1 <= report_id <= 255
                or not isinstance(data, str) or not data or len(data) % 2 or len(data) > 2 * MAX_REPORT_BYTES
                or isinstance(hold, bool) or not isinstance(hold, int) or not 10 <= hold <= 2000):
            return None
        try:
            bytes.fromhex(data)
        except ValueError:
            return None
        reports.append({"id": report_id, "data": data.lower(), "hold_ms": hold})
    return reports


def _build_profile(override):
    profile = dict(DEFAULT_PROFILE)
    if isinstance(override, dict):
        try:
            vid, pid = int(override.get("vid", profile["vid"]), 0), int(override.get("pid", profile["pid"]), 0)
            report_map = bytes.fromhex(override["report_map_hex"]) if "report_map_hex" in override else profile["report_map"]
            if not (1 <= vid <= 0xFFFF and 1 <= pid <= 0xFFFF and 8 <= len(report_map) <= 512):
                raise ValueError("out of range")
            profile.update(vid=vid, pid=pid, report_map=report_map)
        except (ValueError, TypeError):
            print(f"Ignoring invalid 'profile' in {KEYS_FILE}")
    fingerprint = f"{profile['vid']}:{profile['pid']}:{profile['report_map'].hex()}".encode()
    return {"rev": hashlib.sha256(fingerprint).hexdigest()[:12], "vid": profile["vid"], "pid": profile["pid"],
            "report_map": profile["report_map"].hex()}


def _load_keys():
    """Re-read keys.json when it changes; refreshes both the key catalog and the Bluetooth profile."""
    try:
        mtime = KEYS_FILE.stat().st_mtime_ns
    except OSError:
        mtime = None
    if _keys_cache["mtime"] == mtime and _keys_cache["catalog"]:
        return
    catalog = {name: {"key": name, "label": name, "symbol": "", "group": "core",
                      "reports": reports_for_hid({"page": "keyboard", "usage": usage}), "variants": []}
               for name, usage in CORE_KEYS.items()}
    data = {}
    try:
        data = json.loads(KEYS_FILE.read_text())
        for entry in data["keys"]:
            name = entry.get("key")
            hid = _parse_hid(entry)
            reports = reports_for_hid(hid) if hid else _parse_reports(entry.get("reports"))
            if not isinstance(name, str) or not name or len(name) > 32 or reports is None:
                print(f"Ignoring invalid entry in {KEYS_FILE}: {entry!r}")
                continue
            variants = []
            for variant in entry.get("variants", [])[:64] if isinstance(entry.get("variants"), list) else []:
                variant_hid = _parse_hid(variant)
                if variant_hid is None:
                    print(f"Ignoring invalid variant of {name} in {KEYS_FILE}: {variant!r}")
                    continue
                variants.append({"label": str(variant.get("label", ""))[:40],
                                 "key": f"{PAGE_PREFIX[variant_hid['page']]}:{variant_hid['usage']:X}"})
            catalog[name] = {"key": name, "label": str(entry.get("label", name))[:32],
                             "symbol": str(entry.get("symbol", ""))[:8],
                             "group": str(entry.get("group", "extra"))[:16], "reports": reports,
                             "variants": variants}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        if mtime is not None:
            print(f"Could not read {KEYS_FILE}: {error}")
    _keys_cache.update(mtime=mtime, catalog=catalog,
                       profile=_build_profile(data.get("profile") if isinstance(data, dict) else None))


def key_catalog():
    _load_keys()
    return _keys_cache["catalog"]


def current_profile():
    _load_keys()
    return _keys_cache["profile"]


def reports_for(key):
    """Raw HID reports for a key name (or raw "KB:4A" / "CC:223" / "SC:81"), or None if unknown."""
    if not isinstance(key, str):
        return None
    entry = key_catalog().get(key)
    if entry:
        return entry["reports"]
    match = RAW_KEY_RE.match(key)
    if match:
        hid = {"page": RAW_PREFIX[match.group(1)], "usage": int(match.group(2), 16)}
        if 1 <= hid["usage"] <= HID_PAGES[hid["page"]]:
            return reports_for_hid(hid)
    return None


def valid_key(key):
    return reports_for(key) is not None


def make_command(key):
    return {"id": uuid.uuid4().hex, "key": key, "reports": reports_for(key)}


queues = {}
DEVICE_FILE = Path(__file__).resolve().parent / "devices.json"
RECORDING_FILE = Path(__file__).resolve().parent / "recording.json"
devices = {}
recorded_actions = []
lock = threading.Lock()
queue_changed = threading.Condition(lock)
playback_lock = threading.Lock()
playback_active = False
WEB_ROOT = Path(__file__).resolve().parent / "web" / "dist"

try:
    saved_devices = json.loads(DEVICE_FILE.read_text())
    if isinstance(saved_devices, dict):
        devices.update({key: value for key, value in saved_devices.items()
                        if isinstance(key, str) and isinstance(value, dict)})
except (OSError, json.JSONDecodeError):
    pass

try:
    saved_recording = json.loads(RECORDING_FILE.read_text())
    if isinstance(saved_recording, list):
        recorded_actions = [
            {"device_id": action["device_id"], "key": action["key"]}
            for action in saved_recording
            if isinstance(action, dict)
            and isinstance(action.get("device_id"), str)
            and isinstance(action.get("key"), str)
            and valid_key(action["key"])
            and len(action["device_id"]) <= 31
            and all(c.isascii() and (c.isalnum() or c in "-_") for c in action["device_id"])
        ][:100]
except (OSError, json.JSONDecodeError, KeyError):
    pass


def save_devices_locked():
    temporary = DEVICE_FILE.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(devices, indent=2) + "\n")
        os.replace(temporary, DEVICE_FILE)
        return True
    except OSError as error:
        # Keep device polling available if the registry cannot be persisted.
        print(f"Could not save registered devices to {DEVICE_FILE}: {error}")
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def save_recording_locked():
    temporary = RECORDING_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(recorded_actions, indent=2) + "\n")
    os.replace(temporary, RECORDING_FILE)


# Wi-Fi changes requested from the web UI. In memory only, never persisted: the password is handed to the
# ESP32 exactly once (on its next poll) and then wiped; it is never returned by any GET.
WIFI_PENDING_TTL = 600     # seconds a change may wait for the remote to come online and poll
WIFI_RESULT_TTL = 90       # seconds the remote has to report back after the change was delivered
wifi_changes = {}          # device_id -> {id, ssid, password|None, status, error, updated}


def secure_transport(handler):
    """True when the request did not cross an untrusted network in plain text.

    Behind a reverse proxy that means X-Forwarded-Proto: https (the proxy must set it itself).
    Without a proxy header, only direct connections from loopback/private (LAN) addresses count.
    """
    if handler.headers.get("X-Forwarded-Proto", "").lower() == "https":
        return True
    if handler.headers.get("X-Forwarded-For") or handler.headers.get("X-Real-IP"):
        return False
    try:
        return ipaddress.ip_address(handler.client_address[0]).is_private
    except ValueError:
        return False


def wifi_status_locked(device_id):
    """Public view of a device's Wi-Fi change (never includes the password); expires stale entries."""
    change = wifi_changes.get(device_id)
    if not change:
        return None
    age = time.time() - change["updated"]
    if change["status"] == "pending" and age > WIFI_PENDING_TTL:
        change.update(status="failed", password=None, updated=time.time(),
                      error="The remote did not come online in time. Try again.")
    elif change["status"] == "delivered" and age > WIFI_RESULT_TTL:
        change.update(status="failed", updated=time.time(),
                      error="The remote did not report back. It may still be on its old network; try again.")
    return {"status": change["status"], "ssid": change["ssid"], "error": change.get("error", "")}


STARTED_AT = time.time()
SLOW_REQUEST_SECONDS = 1.0


def wifi_view_locked(device_id, record):
    """wifi_status_locked plus `blocked`: the change is waiting but this remote only reaches us over plain HTTP,
    where passwords are never handed out, so it can never be delivered."""
    status = wifi_status_locked(device_id)
    if status:
        status["blocked"] = status["status"] == "pending" and record.get("secure") is False
    return status


class Handler(BaseHTTPRequestHandler):
    # A client that stalls mid-request (flaky mobile hotspot, half-open connection) can no longer pin a thread forever.
    timeout = 30

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def handle_one_request(self):
        started = time.monotonic()
        super().handle_one_request()
        elapsed = time.monotonic() - started
        if elapsed > SLOW_REQUEST_SECONDS and getattr(self, "requestline", ""):
            # Timing includes reading the request, so a slow client shows up here too: compare with other lines.
            print(f"SLOW {elapsed:.1f}s {self.requestline} from {self.client_address[0]} "
                  f"(threads={threading.active_count()})", flush=True)

    def send_json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16384:
                return None
            return json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            return None

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path == "/api/devices":
            now = time.time()
            with lock:
                result = [{"device_id": device_id,
                           "name": record.get("name", device_id),
                           "online": now - record.get("last_seen", 0) < 4,
                           "pending": len(queues.get(device_id, ())),
                           "secure": record.get("secure"),
                           "wifi": wifi_view_locked(device_id, record)}
                          for device_id, record in sorted(devices.items(), key=lambda item: item[1].get("name", item[0]).casefold())]
            return self.send_json(200, {"devices": result})
        if path == "/api/health":
            return self.send_json(200, {"ok": True, "uptime_s": int(time.time() - STARTED_AT),
                                        "threads": threading.active_count(), "devices": len(devices),
                                        "queued": sum(len(q) for q in list(queues.values()))})
        if path == "/api/keys":
            extras = [{k: entry[k] for k in ("key", "label", "symbol", "group", "variants")}
                      for entry in key_catalog().values() if entry["group"] != "core"]
            return self.send_json(200, {"keys": extras})
        if path == "/api/recording":
            with lock:
                actions = list(recorded_actions)
            return self.send_json(200, {"commands": actions})
        if not (WEB_ROOT / "index.html").is_file():
            return self.send_json(503, {"error": "web app not built; run npm --prefix backend/web run build"})
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        if not target.is_relative_to(WEB_ROOT.resolve()) or not target.is_file():
            # Client-side routes should load the React app; missing asset paths remain 404s.
            if "." not in Path(relative).name:
                target = WEB_ROOT / "index.html"
            else:
                return self.send_json(404, {"error": "not found"})
        if not target.is_file():
            return self.send_json(503, {"error": "web app not built; run npm --prefix backend/web run build"})
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", (guess_type(str(target))[0] or "application/octet-stream") + ("; charset=utf-8" if target.suffix in {".html", ".js", ".css", ".json", ".webmanifest"} else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if target.name == "index.html" or target.name == "Icon.png" else "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self):
        path = unquote(urlsplit(self.path).path)
        if path.startswith("/api/wifi/"):
            target = path[len("/api/wifi/"):]
            with lock:
                removed = wifi_changes.pop(target, None)
            return self.send_json(200 if removed else 404, {"cancelled": bool(removed)})
        prefix = "/api/devices/"
        if not path.startswith(prefix):
            return self.send_json(404, {"error": "not found"})
        device_id = path[len(prefix):]
        if (not device_id or "/" in device_id or len(device_id) > 31
                or not all(c.isascii() and (c.isalnum() or c in "-_") for c in device_id)):
            return self.send_json(400, {"error": "invalid device_id"})
        with lock:
            record = devices.pop(device_id, None)
            if record is None:
                return self.send_json(404, {"error": "device not found"})
            if not save_devices_locked():
                devices[device_id] = record
                return self.send_json(500, {"error": "could not save device registry"})
            queues.pop(device_id, None)
        return self.send_json(200, {"deleted": True, "device_id": device_id})

    def do_POST(self):
        path = unquote(urlsplit(self.path).path)
        body = self.read_json()
        if not isinstance(body, dict):
            return self.send_json(400, {"error": "expected a JSON object"})
        if path == "/api/recording":
            actions = body.get("commands")
            if not isinstance(actions, list) or len(actions) > 100:
                return self.send_json(400, {"error": "commands must be a list of at most 100 actions"})
            validated = []
            for index, action in enumerate(actions, start=1):
                if not isinstance(action, dict):
                    return self.send_json(400, {"error": f"action {index} must be an object"})
                target, key = action.get("device_id"), action.get("key")
                if (not isinstance(target, str) or not target or len(target) > 31
                        or not all(c.isascii() and (c.isalnum() or c in "-_") for c in target)):
                    return self.send_json(400, {"error": f"invalid device_id in action {index}"})
                if not valid_key(key):
                    return self.send_json(400, {"error": f"unsupported key in action {index}"})
                validated.append({"device_id": target, "key": key})
            global recorded_actions
            with lock:
                recorded_actions = validated
                try:
                    save_recording_locked()
                except OSError as error:
                    print(f"Could not save recording to {RECORDING_FILE}: {error}")
                    return self.send_json(500, {"error": "could not save recording"})
            return self.send_json(200, {"saved": len(validated)})

        device_id = body.get("device_id")
        if path != "/api/playback" and (
            not isinstance(device_id, str) or not device_id or len(device_id) > 31
            or not all(c.isascii() and (c.isalnum() or c in "-_") for c in device_id)
        ):
            return self.send_json(400, {"error": "invalid device_id"})

        if path == "/api/commands":
            key = body.get("key")
            if not valid_key(key):
                return self.send_json(400, {"error": "unsupported key"})
            command = make_command(key)
            with lock:
                queues.setdefault(device_id, deque()).append(command)
                queue_changed.notify_all()
            return self.send_json(202, {"queued": True, "command_id": command["id"]})

        if path == "/api/playback":
            commands = body.get("commands")
            if not isinstance(commands, list) or not commands or len(commands) > 100:
                return self.send_json(400, {"error": "commands must contain 1 to 100 actions"})
            validated = []
            for index, item in enumerate(commands, start=1):
                if not isinstance(item, dict):
                    return self.send_json(400, {"error": f"action {index} must be an object"})
                target = item.get("device_id")
                key = item.get("key")
                if (not isinstance(target, str) or not target or len(target) > 31
                        or not all(c.isascii() and (c.isalnum() or c in "-_") for c in target)):
                    return self.send_json(400, {"error": f"invalid device_id in action {index}: {target!r}"})
                if not valid_key(key):
                    return self.send_json(400, {"error": f"unsupported key in action {index}"})
                validated.append({"device_id": target, "key": key})
            global playback_active
            with playback_lock:
                if playback_active:
                    return self.send_json(409, {"error": "a recorded sequence is already playing"})
                playback_active = True
            threading.Thread(target=play_recording, args=(validated,), daemon=True).start()
            return self.send_json(202, {"started": True, "actions": len(validated)})

        if path == "/api/poll":
            with lock:
                name = body.get("name", device_id)
                if not isinstance(name, str) or not name or len(name) > 64:
                    name = device_id
                new_device = device_id not in devices or devices[device_id].get("name") != name
                devices[device_id] = {"name": name, "last_seen": time.time(), "secure": secure_transport(self)}
                if new_device:
                    save_devices_locked()
                pending = queues.get(device_id)
                command = pending[0] if pending else None
                wifi_payload = None
                change = wifi_changes.get(device_id)
                if change and wifi_status_locked(device_id)["status"] == "pending" and secure_transport(self):
                    wifi_payload = {"id": change["id"], "ssid": change["ssid"], "password": change["password"]}
                    change.update(status="delivered", password=None, updated=time.time())
            profile_payload = None
            reported_rev = body.get("profile_rev")
            if isinstance(reported_rev, str):   # firmware that accepts a server-pushed Bluetooth profile
                profile = current_profile()
                if reported_rev != profile["rev"] and body.get("profile_failed") != profile["rev"]:
                    profile_payload = profile
            return self.send_json(200, {"command": command, "wifi": wifi_payload, "profile": profile_payload})

        if path == "/api/wifi":
            ssid, password = body.get("ssid"), body.get("password", "")
            if not secure_transport(self):
                return self.send_json(403, {"error": "Wi-Fi settings can only be changed over HTTPS or a trusted local network"})
            if (not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32
                    or any(ord(c) < 32 or ord(c) == 127 for c in ssid)):
                return self.send_json(400, {"error": "Network name must be 1-32 bytes"})
            if (not isinstance(password, str) or (password and not 8 <= len(password.encode()) <= 63)
                    or any(ord(c) < 32 or ord(c) == 127 for c in password)):
                return self.send_json(400, {"error": "Password must be empty (open network) or 8-63 characters"})
            with lock:
                if device_id not in devices:
                    return self.send_json(404, {"error": "device not found"})
                wifi_changes[device_id] = {"id": uuid.uuid4().hex[:16], "ssid": ssid, "password": password,
                                           "status": "pending", "error": "", "updated": time.time()}
            return self.send_json(202, {"queued": True})

        if path == "/api/wifi/result":
            change_id, ok = body.get("change_id"), body.get("ok")
            with lock:
                change = wifi_changes.get(device_id)
                if change and change["id"] == change_id and change["status"] in ("pending", "delivered"):
                    if ok is True:
                        change.update(status="applied", error="")
                    else:
                        change.update(status="failed", error="The remote could not join that network, so it kept its previous one.")
                    change.update(password=None, updated=time.time())
            return self.send_json(200, {"recorded": True})

        if path == "/api/ack":
            command_id = body.get("command_id")
            with lock:
                pending = queues.get(device_id)
                if pending and pending[0]["id"] == command_id:
                    pending.popleft()
                    queue_changed.notify_all()
            return self.send_json(200, {"acknowledged": True})

        return self.send_json(404, {"error": "not found"})


def play_recording(actions):
    """Deliver one action at a time, waiting for its ESP32 acknowledgement."""
    global playback_active
    try:
        for action in actions:
            command = make_command(action["key"])
            deadline = time.monotonic() + 20
            with queue_changed:
                queue = queues.setdefault(action["device_id"], deque())
                queue.append(command)
                queue_changed.notify_all()
                while any(entry["id"] == command["id"] for entry in queue):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        try:
                            queue.remove(command)
                        except ValueError:
                            pass
                        print(f"Recorded playback stopped: {action['device_id']} did not acknowledge {action['key']}")
                        return
                    queue_changed.wait(remaining)
            time.sleep(0.15)
    finally:
        with playback_lock:
            playback_active = False


if __name__ == "__main__":
    print(f"AbuelaTV server listening on http://{HOST}:{PORT}")
    print("Open this address in a browser on your network.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
