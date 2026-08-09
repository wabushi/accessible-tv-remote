# Accessible TV Remote

A Raspberry Pi-based accessible physical remote for Android TV / Google TV.

The Raspberry Pi presents itself to the TV as a Bluetooth Low Energy (BLE) HID keyboard. Physical switches connected to the Pi's GPIO pins trigger predefined keyboard sequences, allowing complicated remote-control actions to be reduced to a single large physical button press.

The project was developed as an accessibility interface for users who may have difficulty operating a conventional television remote.

## What It Does

Each physical switch is assigned a sequence of keyboard inputs.

The default configuration is:

| BCM GPIO | Sequence |
|---:|---|
| 25 | `1 → 5 → 7` |
| 24 | `6 → 8` |
| 23 | `6 → 9` |
| 27 | `1 → 5` |
| 26 | `0 → 0 → 0` |

The sequences are completely configurable in `tv_remote.py`.

The software also provides:

- Bluetooth LE HID keyboard emulation
- Physical GPIO switch inputs
- Configurable key sequences
- 2-second global input cooldown
- Persistent Bluetooth pairing
- Automatic reconnection after reboot
- Automatic startup using systemd
- Console logging for testing and debugging

## Hardware

The prototype uses:

- A Raspberry Pi with Bluetooth support
- Five physical microswitches/buttons
- Appropriate GPIO wiring
- An Android TV / Google TV device supporting Bluetooth HID keyboards

The physical switches can be placed beneath much larger custom button caps or a 3D-printed interface, allowing the controls themselves to be considerably larger than the electrical switches.

## Software Architecture

This project uses **btferret** for the low-level Bluetooth implementation.

btferret:
https://github.com/petzval/btferret

The relationship is roughly:

```text
Physical button
      │
      ▼
Raspberry Pi GPIO
      │
      ▼
tv_remote.py
      │
      ▼
btferret / btfpy
      │
      ▼
Bluetooth LE HID
      │
      ▼
Android TV / Google TV
```

`tv_remote.py` adapts btferret's HID keyboard functionality and adds the GPIO, accessibility-control, key-sequence, cooldown, pairing-persistence and startup logic required by this project.

## Repository Contents

```text
accessible-tv-remote/
├── tv_remote.py
├── keyboard.txt
├── tv-remote.service
├── README.md
├── LICENSE
└── wiring/
```

The compiled `btfpy.so` module is deliberately **not** included in this repository. It is built locally from btferret for the Raspberry Pi and Python version being used.

---

# Installation

## 1. Install Required Packages

Update the package list:

```bash
sudo apt update
```

Install Git, Python development files, setuptools and GPIO Zero:

```bash
sudo apt install git python3 python3-dev python3-setuptools python3-gpiozero
```

## 2. Install btferret

Clone the upstream btferret repository:

```bash
cd /opt
sudo git clone https://github.com/petzval/btferret.git
```

Give your normal user ownership of the checkout while building:

```bash
sudo chown -R "$USER":"$USER" /opt/btferret
```

Enter the directory:

```bash
cd /opt/btferret
```

Build the Python module:

```bash
python3 btfpymake.py build
```

A successful build creates:

```text
btfpy.so
```

in the btferret directory.

The exact compiled extension generated inside the build directory depends on the Python version and processor architecture. `btfpymake.py` copies the appropriate extension out and names it `btfpy.so`.

## 3. Install Accessible TV Remote

Clone this repository:

```bash
cd /opt
sudo git clone YOUR_REPOSITORY_URL accessible-tv-remote
```

Set normal ownership if desired:

```bash
sudo chown -R "$USER":"$USER" /opt/accessible-tv-remote
```

Copy the locally compiled btferret Python module into the application directory:

```bash
sudo cp /opt/btferret/btfpy.so /opt/accessible-tv-remote/
```

The resulting installation should look like:

```text
/opt/
├── btferret/
│   ├── btfpy.so
│   └── ...
│
└── accessible-tv-remote/
    ├── btfpy.so
    ├── tv_remote.py
    ├── keyboard.txt
    ├── tv-remote.service
    └── ...
```

This keeps the upstream btferret repository separate from the accessibility application.

---

# Bluetooth Configuration

## 4. Find the Raspberry Pi Bluetooth Address

Run:

```bash
bluetoothctl show
```

Find the controller address. It will look similar to:

```text
Controller AA:BB:CC:DD:EE:FF
```

Open:

```bash
nano /opt/accessible-tv-remote/keyboard.txt
```

Replace:

```text
ADDRESS = XX:XX:XX:XX:XX:XX
```

with the Pi's actual Bluetooth controller address.

For example:

```text
DEVICE = My Pi TYPE=Mesh node=1 ADDRESS = AA:BB:CC:DD:EE:FF
```

Do not copy the example address.

---

# Wiring

The application currently expects switches on the following BCM GPIO numbers:

```text
GPIO 25
GPIO 24
GPIO 23
GPIO 27
GPIO 26
```

The software uses **BCM numbering**, not physical header pin numbers.

See the diagram in the `wiring/` directory before connecting the switches.

> Raspberry Pi GPIO uses 3.3 V logic. Do not apply 5 V directly to a GPIO input.

---

# First Test

Before enabling automatic startup, run the application manually.

```bash
cd /opt/accessible-tv-remote
sudo python3 tv_remote.py
```

Root privileges are required because btferret directly accesses the Bluetooth hardware.

A successful startup should eventually display something similar to:

```text
Starting Bluetooth TV accessibility remote...
Initialising btferret...
Waiting for Android TV connection...
Advertising as ... HID device
```

On the Android TV / Google TV device:

1. Open Bluetooth / Remotes & Accessories.
2. Search for a new accessory.
3. Select the HID device.
4. Complete pairing.

The Pi should report a successful connection.

For example:

```text
PAIR OK
TV connected: node 1000
Bluetooth pairing information saved.
GPIO buttons are now active.
```

Test each physical switch and confirm that it performs the expected action.

---

# Persistent Pairing

The application explicitly saves btferret's pairing information after a successful Bluetooth connection.

It also uses a fixed BLE random address.

Both are important for reconnection.

After pairing, **do not casually change the fixed BLE random address in `tv_remote.py`**. The TV associates the saved Bluetooth relationship with the peripheral's identity.

Changing that identity may require the Pi to be paired again.

Once pairing information has been saved, the Android TV device should be able to recognize the remote again after the Raspberry Pi reboots.

---

# Automatic Startup

Once manual operation and reconnection have been verified, install the systemd service.

Copy it:

```bash
sudo cp /opt/accessible-tv-remote/tv-remote.service /etc/systemd/system/
```

Reload systemd:

```bash
sudo systemctl daemon-reload
```

Enable automatic startup:

```bash
sudo systemctl enable tv-remote.service
```

Start it:

```bash
sudo systemctl start tv-remote.service
```

Check its status:

```bash
sudo systemctl status tv-remote.service
```

The service should report:

```text
Active: active (running)
```

To follow the application's log:

```bash
sudo journalctl -u tv-remote.service -f
```

To inspect the log from the current boot:

```bash
sudo journalctl -u tv-remote.service -b --no-pager
```

---

# Important Bluetooth Note

btferret accesses the Bluetooth adapter at a relatively low level and monopolizes the adapter while running.

Do not attempt to run multiple btferret-based applications against the same Bluetooth controller simultaneously.

For example, do not run:

```text
tv_remote.py
```

and btferret's:

```text
keyboard.py
```

at the same time.

During development we also found that immediately terminating and relaunching a btferret application could occasionally leave the Bluetooth controller unavailable.

The supplied systemd service therefore starts the application once during boot rather than continuously attempting rapid process restarts.

If the Bluetooth controller becomes unavailable during development, a Raspberry Pi reboot provides a clean controller reset:

```bash
sudo reboot
```

---

# Changing Button Actions

The button mappings are defined near the beginning of `tv_remote.py`:

```python
SEQUENCES = {
    25: ["1", "5", "7"],
    24: ["6", "8"],
    23: ["6", "9"],
    27: ["1", "5"],
    26: ["0", "0", "0"],
}
```

For example:

```python
24: ["6", "8"]
```

means:

```text
GPIO 24 pressed
       ↓
send "6"
       ↓
send "8"
```

The key sequences can therefore be changed without modifying the Bluetooth implementation.

---

# Troubleshooting

### `ModuleNotFoundError: No module named 'btfpy'`

Verify that:

```text
btfpy.so
```

exists in:

```text
/opt/accessible-tv-remote/
```

If it does not, build btferret and copy the resulting module again.

### `No root permission or Bluetooth (hci0) is off or crashed`

Make sure the program is being run with:

```bash
sudo python3 tv_remote.py
```

Also make sure another btferret process is not already using the adapter.

Check with:

```bash
pgrep -af 'tv_remote.py|keyboard.py|btferret'
```

### `Unable to read local board address`

Check that the Bluetooth address in `keyboard.txt` matches the Raspberry Pi's controller address:

```bash
bluetoothctl show
```

Also verify that another btferret process is not already holding the adapter.

### TV does not reconnect after reboot

First confirm that the TV successfully paired and that the application previously reported:

```text
Bluetooth pairing information saved.
```

Do not change the fixed BLE random address after pairing.

If necessary, forget the HID device from the TV, pair it again, allow the pairing information to be saved, and then perform a controlled Raspberry Pi reboot.

---

# Credits

Bluetooth functionality is based on **btferret**, developed by petzval:

https://github.com/petzval/btferret

btferret provides the Bluetooth library and HID keyboard implementation on which this project is based.

This project adds the application-specific GPIO controls, key-sequence handling, accessibility interface, pairing persistence and systemd integration.

## License

btferret is distributed under the MIT License.

Code derived from btferret remains subject to the upstream copyright and MIT license notice.

This repository's original code is also released under the MIT License. See `LICENSE` for details.
