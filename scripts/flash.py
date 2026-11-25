#!/usr/bin/env python3
"""Build, provision, and flash the Accessible TV Remote ESP32-C3 firmware."""

import csv
import json
import os
import readline
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
IDF_PATH = os.environ.get("IDF_PATH")
CONFIG_PATH = ROOT / ".flash_config.json"


def load_config() -> dict[str, str]:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return config if isinstance(config, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config: dict[str, str]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    CONFIG_PATH.chmod(0o600)


def input_with_default(prompt: str, previous: str = "") -> str:
    """Put the saved value in the editable input line when readline is available."""
    if previous:
        readline.set_startup_hook(lambda: readline.insert_text(previous))
    try:
        return input(f"{prompt}: ").strip()
    finally:
        readline.set_startup_hook()


def required_input(prompt: str, previous: str = "") -> str:
    while True:
        value = input_with_default(prompt, previous) or previous
        if value:
            return value
        print("Value cannot be empty.")


def main() -> int:
    if not IDF_PATH or not (Path(IDF_PATH) / "export.sh").exists():
        print("Activate ESP-IDF first (source $IDF_PATH/export.sh).", file=sys.stderr)
        return 2

    config = load_config()
    port = required_input("Serial port (e.g. /dev/ttyACM0 or ttyACM0)", config.get("port", ""))
    if "/" not in port:
        port = f"/dev/{port}"
    ssid = required_input("Wi-Fi SSID", config.get("ssid", ""))
    saved_password = config.get("password", "")
    password_entry = input_with_default(
        "Wi-Fi password (Enter keeps it; ! clears it)", saved_password
    )
    password = saved_password if password_entry == "" and saved_password else ("" if password_entry == "!" else password_entry)
    ble_name = required_input("Bluetooth HID name (max 31 UTF-8 bytes)", config.get("ble_name", ""))
    device_id = ble_name
    backend_url = required_input("Backend URL (e.g. http://192.168.1.20:8080)", config.get("backend_url", "")).rstrip("/")
    if len(ble_name.encode("utf-8")) > 31:
        print("Bluetooth HID name must be at most 31 UTF-8 bytes.", file=sys.stderr)
        return 2
    if not ble_name or not all(char.isascii() and (char.isalnum() or char in "-_") for char in ble_name):
        print("Bluetooth HID name must use only ASCII letters, numbers, - or _ so it can also identify the device.", file=sys.stderr)
        return 2
    if len(ssid.encode("utf-8")) > 32 or len(password.encode("utf-8")) > 64:
        print("Wi-Fi SSID/password exceed ESP-IDF limits.", file=sys.stderr)
        return 2
    if len(backend_url.encode("utf-8")) > 255 or not backend_url.startswith(("http://", "https://")):
        print("Backend URL must start with http:// or https:// and fit in 255 UTF-8 bytes.", file=sys.stderr)
        return 2
    save_config({"port": port, "ssid": ssid, "password": password,
                 "ble_name": ble_name, "backend_url": backend_url})

    idf_py = shutil.which("idf.py")
    if not idf_py:
        print("idf.py not found; activate ESP-IDF first.", file=sys.stderr)
        return 2

    generator = Path(IDF_PATH) / "components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py"
    with tempfile.TemporaryDirectory(prefix="tv-remote-flash-") as temp_dir:
        temp = Path(temp_dir)
        csv_path = temp / "provisioning.csv"
        bin_path = temp / "provisioning.bin"
        with csv_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(["key", "type", "encoding", "value"])
            writer.writerow(["tv_remote", "namespace", "", ""])
            writer.writerow(["ssid", "data", "string", ssid])
            writer.writerow(["password", "data", "string", password])
            writer.writerow(["ble_name", "data", "string", ble_name])
            writer.writerow(["device_id", "data", "string", device_id])
            writer.writerow(["backend_url", "data", "string", backend_url])

        subprocess.run([sys.executable, str(generator), "generate", str(csv_path), str(bin_path), "0x6000"], check=True, cwd=ROOT)
        try:
            subprocess.run([idf_py, "-p", port, "build", "flash"], check=True, cwd=ROOT)
        except subprocess.CalledProcessError as error:
            print(
                f"Build or flash failed (exit code {error.returncode}). "
                f"Check that {port} exists, the board is connected, and no other program is using it.",
                file=sys.stderr,
            )
            return error.returncode

        esptool = Path(IDF_PATH) / "components/esptool_py/esptool/esptool.py"
        try:
            subprocess.run(
                [sys.executable, str(esptool), "--chip", "esp32c3", "--port", port,
                 "write_flash", "0x9000", str(bin_path)],
                check=True,
                cwd=ROOT,
            )
        except subprocess.CalledProcessError as error:
            print(
                f"NVS provisioning flash failed (exit code {error.returncode}). "
                f"The firmware may already be flashed; retry with the full port path {port}.",
                file=sys.stderr,
            )
            return error.returncode

    print(f"Provisioned Wi-Fi, BLE HID name/device ID '{ble_name}', and backend URL; flashed the device.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
