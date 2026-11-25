#!/usr/bin/env python3
"""Small single-process HTTP command queue and web remote for the ESP32-C3."""

from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from mimetypes import guess_type
import json
import os
import threading
import time
import uuid
from urllib.parse import unquote, urlsplit

HOST = os.environ.get("TV_REMOTE_HOST", "0.0.0.0")
PORT = int(os.environ.get("TV_REMOTE_PORT", "8080"))
VALID_KEYS = {"UP", "DOWN", "LEFT", "RIGHT", "ENTER", "BACK", "BACK_XIAOMI", *(str(n) for n in range(10))}
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
            and action["key"] in VALID_KEYS
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
    except OSError as error:
        # Keep device polling available if the registry cannot be persisted.
        print(f"Could not save registered devices to {DEVICE_FILE}: {error}")
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def save_recording_locked():
    temporary = RECORDING_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(recorded_actions, indent=2) + "\n")
    os.replace(temporary, RECORDING_FILE)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

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
                           "online": now - record.get("last_seen", 0) < 4}
                          for device_id, record in sorted(devices.items(), key=lambda item: item[1].get("name", item[0]).casefold())]
            return self.send_json(200, {"devices": result})
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
                if not isinstance(key, str) or key not in VALID_KEYS:
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
            if not isinstance(key, str) or key not in VALID_KEYS:
                return self.send_json(400, {"error": "unsupported key"})
            command = {"id": uuid.uuid4().hex, "key": key}
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
                if not isinstance(key, str) or key not in VALID_KEYS:
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
                devices[device_id] = {"name": name, "last_seen": time.time()}
                if new_device:
                    save_devices_locked()
                pending = queues.get(device_id)
                command = pending[0] if pending else None
            return self.send_json(200, {"command": command})

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
            command = {"id": uuid.uuid4().hex, "key": action["key"]}
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
