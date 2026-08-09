#!/usr/bin/python3

"""
Accessible Bluetooth TV Remote
==============================

Raspberry Pi GPIO-driven Bluetooth HID remote for Android TV.

Bluetooth HID functionality is based on the btferret project:
https://github.com/petzval/btferret

This application adapts btferret's Bluetooth HID keyboard example
for an accessibility controller operated through large physical
GPIO-connected switches.

btferret is distributed under the MIT License.
See the upstream project for its copyright and license information.
"""

import signal
import time

import btfpy
from gpiozero import Button


# ============================================================
# USER CONFIGURATION
# ============================================================

COOLDOWN_SECONDS = 2.0
KEY_DELAY_SECONDS = 0.15

# BCM GPIO number -> keyboard sequence
SEQUENCES = {
    25: ["1", "5", "7"],
    24: ["6", "8"],
    23: ["6", "9"],
    27: ["1", "5"],
    26: ["0", "0", "0"],
}


# ============================================================
# BLUETOOTH HID DESCRIPTOR
# ============================================================

reportmap = [
    0x05, 0x01,
    0x09, 0x06,
    0xA1, 0x01,
    0x85, 0x01,
    0x05, 0x07,
    0x19, 0xE0,
    0x29, 0xE7,
    0x15, 0x00,
    0x25, 0x01,
    0x75, 0x01,
    0x95, 0x08,
    0x81, 0x02,
    0x95, 0x01,
    0x75, 0x08,
    0x81, 0x01,
    0x95, 0x06,
    0x75, 0x08,
    0x15, 0x00,
    0x25, 0x65,
    0x05, 0x07,
    0x19, 0x00,
    0x29, 0x65,
    0x81, 0x00,
    0xC0,
]

report = [0, 0, 0, 0, 0, 0, 0, 0]

device_name = "HID"

# 0x03C1 = keyboard appearance
appearance = [0xC1, 0x03]

pnp_info = [
    0x02,
    0x6B,
    0x1D,
    0x46,
    0x02,
    0x37,
    0x05,
]

protocol_mode = [0x01]

hid_info = [
    0x01,
    0x11,
    0x00,
    0x02,
]

# Fixed BLE random address.
#
# IMPORTANT:
# Once a TV has been paired, do not change this address.
# The host associates its saved bond with this BLE identity.
random_address = [
    0xD3,
    0x56,
    0xD6,
    0x74,
    0x33,
    0x04,
]


# ============================================================
# GLOBAL STATE
# ============================================================

node = 0
report_index = -1

tv_connected = False
stop_requested = False
bluetooth_initialized = False

last_press_time = 0.0


# ============================================================
# GPIO CONFIGURATION
# ============================================================

# The microswitch modules used by the prototype are active-low:
#
# released -> HIGH
# pressed  -> LOW

buttons = {
    gpio: Button(
        gpio,
        pull_up=True,
        bounce_time=None,
    )
    for gpio in SEQUENCES
}

# Store the initial state so a switch held during boot does not
# immediately transmit its sequence.
previous_states = {
    gpio: button.is_pressed
    for gpio, button in buttons.items()
}


# ============================================================
# CLEAN SHUTDOWN
# ============================================================

def request_stop(signum=None, frame=None):
    """Request that the Bluetooth server terminate cleanly."""

    global stop_requested

    if not stop_requested:
        print(
            "\nShutdown requested...",
            flush=True,
        )

    stop_requested = True


signal.signal(
    signal.SIGINT,
    request_stop,
)

signal.signal(
    signal.SIGTERM,
    request_stop,
)


# ============================================================
# PAIRING INFORMATION
# ============================================================

def save_pairing_information():
    """
    Save btferret's pairing/bond information immediately.

    This is important for devices that must reconnect after the
    Raspberry Pi is rebooted or unexpectedly loses power.
    """

    try:
        result = btfpy.Save_pair_info()

        if result is None:
            print(
                "Bluetooth pairing information saved.",
                flush=True,
            )
        else:
            print(
                f"Bluetooth pairing information saved "
                f"(result={result}).",
                flush=True,
            )

    except Exception as error:
        print(
            "Warning: pairing information could not "
            f"be saved: {error}",
            flush=True,
        )


# ============================================================
# HID KEY TRANSMISSION
# ============================================================

def send_key(key):
    """
    Send one keyboard key press followed immediately by its
    corresponding key-release HID report.
    """

    hid_code = btfpy.Hid_key_code(key)

    if hid_code == 0:
        print(
            f"Unknown key code: {key}",
            flush=True,
        )
        return

    buffer = [
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    ]

    # --------------------------------------------------------
    # Key press
    # --------------------------------------------------------

    buffer[0] = (hid_code >> 8) & 0xFF
    buffer[2] = hid_code & 0xFF

    result = btfpy.Write_ctic(
        node,
        report_index,
        buffer,
        0,
    )

    if result == 0:
        print(
            "Warning: key-press report was not sent",
            flush=True,
        )

    # --------------------------------------------------------
    # Key release
    # --------------------------------------------------------

    buffer[0] = 0
    buffer[2] = 0

    result = btfpy.Write_ctic(
        node,
        report_index,
        buffer,
        0,
    )

    if result == 0:
        print(
            "Warning: key-release report was not sent",
            flush=True,
        )


def send_sequence(gpio):
    """Send the numeric sequence assigned to a GPIO input."""

    sequence = SEQUENCES[gpio]

    sequence_text = " -> ".join(
        sequence
    )

    print(
        f"GPIO {gpio}: PRESSED - "
        f"sending {sequence_text}",
        flush=True,
    )

    for digit in sequence:
        send_key(
            ord(digit)
        )

        time.sleep(
            KEY_DELAY_SECONDS
        )

    print(
        f"GPIO {gpio}: sequence complete",
        flush=True,
    )


# ============================================================
# GPIO POLLING
# ============================================================

def check_buttons():
    """
    Poll all configured GPIO inputs.

    Only a released -> pressed transition triggers a sequence.

    A global cooldown prevents another switch from activating
    while the previous command is being processed.
    """

    global last_press_time

    now = time.monotonic()

    for gpio, button in buttons.items():

        currently_pressed = (
            button.is_pressed
        )

        previously_pressed = (
            previous_states[gpio]
        )

        # ----------------------------------------------------
        # Detect new press
        # ----------------------------------------------------

        if (
            currently_pressed
            and not previously_pressed
        ):

            elapsed = (
                now
                - last_press_time
            )

            # ------------------------------------------------
            # Cooldown
            # ------------------------------------------------

            if elapsed < COOLDOWN_SECONDS:

                remaining = (
                    COOLDOWN_SECONDS
                    - elapsed
                )

                print(
                    f"GPIO {gpio}: ignored - "
                    f"cooldown "
                    f"({remaining:.1f}s remaining)",
                    flush=True,
                )

            # ------------------------------------------------
            # TV disconnected
            # ------------------------------------------------

            elif not tv_connected:

                print(
                    f"GPIO {gpio}: ignored - "
                    "TV is not connected",
                    flush=True,
                )

            # ------------------------------------------------
            # Valid button press
            # ------------------------------------------------

            else:

                last_press_time = now

                send_sequence(
                    gpio
                )

        previous_states[gpio] = (
            currently_pressed
        )


# ============================================================
# BTFERRET SERVER CALLBACK
# ============================================================

def le_callback(
    client_node,
    operation,
    characteristic,
):
    """
    btferret event callback.

    Handles:
      - TV connection
      - TV disconnection
      - GPIO polling timer
      - interactive shutdown commands
    """

    global tv_connected
    global stop_requested

    if stop_requested:
        return btfpy.SERVER_EXIT

    # --------------------------------------------------------
    # Connected
    # --------------------------------------------------------

    if operation == btfpy.LE_CONNECT:

        tv_connected = True

        print(
            f"TV connected: "
            f"node {client_node}",
            flush=True,
        )

        # Save the bond immediately.
        save_pairing_information()

        print(
            "GPIO buttons are now active.",
            flush=True,
        )

    # --------------------------------------------------------
    # Disconnected
    # --------------------------------------------------------

    elif operation == btfpy.LE_DISCONNECT:

        tv_connected = False

        print(
            f"TV disconnected: "
            f"node {client_node}",
            flush=True,
        )

        print(
            "Waiting for reconnection...",
            flush=True,
        )

        # Keep the HID server running.
        return btfpy.SERVER_CONTINUE

    # --------------------------------------------------------
    # Timer
    # --------------------------------------------------------

    elif operation == btfpy.LE_TIMER:

        check_buttons()

        if stop_requested:
            return btfpy.SERVER_EXIT

    # --------------------------------------------------------
    # Terminal controls
    # --------------------------------------------------------

    elif operation == btfpy.LE_KEYPRESS:

        # x
        # X
        # ESC

        if characteristic in (
            ord("x"),
            ord("X"),
            27,
        ):

            print(
                "Terminal stop command received.",
                flush=True,
            )

            stop_requested = True

            return btfpy.SERVER_EXIT

    return btfpy.SERVER_CONTINUE


# ============================================================
# BLUETOOTH CHARACTERISTICS
# ============================================================

def write_characteristic(
    uuid,
    value,
):
    """Find and populate a local BLE characteristic."""

    characteristic_index = (
        btfpy.Find_ctic_index(
            node,
            btfpy.UUID_2,
            uuid,
        )
    )

    if characteristic_index < 0:

        raise RuntimeError(
            "Bluetooth characteristic "
            f"not found: {uuid}"
        )

    result = btfpy.Write_ctic(
        node,
        characteristic_index,
        value,
        0,
    )

    if result == 0:

        raise RuntimeError(
            "Unable to write Bluetooth "
            f"characteristic: {uuid}"
        )


# ============================================================
# BLUETOOTH INITIALIZATION
# ============================================================

def initialize_bluetooth():
    """Initialize the btferret BLE HID keyboard server."""

    global node
    global report_index
    global bluetooth_initialized

    print(
        "Initialising btferret...",
        flush=True,
    )

    if (
        btfpy.Init_blue(
            "keyboard.txt"
        )
        == 0
    ):

        raise RuntimeError(
            "btfpy.Init_blue() failed. "
            "Run this program with sudo and "
            "verify keyboard.txt and btfpy.so "
            "are available."
        )

    bluetooth_initialized = True

    # The local Pi must be node 1 in keyboard.txt.

    if btfpy.Localnode() != 1:

        local_node = (
            btfpy.Localnode()
        )

        detected_address = (
            btfpy.Device_address(
                local_node
            )
        )

        raise RuntimeError(
            "keyboard.txt does not identify "
            "the Pi as node 1. "
            f"Detected address: "
            f"{detected_address}"
        )

    node = (
        btfpy.Localnode()
    )

    # --------------------------------------------------------
    # Find HID Report characteristic
    # --------------------------------------------------------

    report_uuid = [
        0x2A,
        0x4D,
    ]

    report_index = (
        btfpy.Find_ctic_index(
            node,
            btfpy.UUID_2,
            report_uuid,
        )
    )

    if report_index < 0:

        raise RuntimeError(
            "Failed to find the HID "
            "Report1 characteristic."
        )

    # --------------------------------------------------------
    # Populate HID characteristics
    # --------------------------------------------------------

    write_characteristic(
        [0x2A, 0x00],
        device_name,
    )

    write_characteristic(
        [0x2A, 0x01],
        appearance,
    )

    write_characteristic(
        [0x2A, 0x4E],
        protocol_mode,
    )

    write_characteristic(
        [0x2A, 0x4A],
        hid_info,
    )

    write_characteristic(
        [0x2A, 0x4B],
        reportmap,
    )

    write_characteristic(
        [0x2A, 0x4D],
        report,
    )

    write_characteristic(
        [0x2A, 0x50],
        pnp_info,
    )

    # --------------------------------------------------------
    # Stable BLE identity
    # --------------------------------------------------------

    btfpy.Set_le_random_address(
        random_address
    )

    # --------------------------------------------------------
    # Enable keyboard events for manual shutdown
    # --------------------------------------------------------

    btfpy.Keys_to_callback(
        btfpy.KEY_ON,
        0,
    )

    # --------------------------------------------------------
    # Connection timeout
    # --------------------------------------------------------

    btfpy.Set_le_wait(
        20000
    )

    # --------------------------------------------------------
    # Pairing
    # --------------------------------------------------------

    btfpy.Le_pair(
        btfpy.Localnode(),
        btfpy.JUST_WORKS,
        0,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    try:

        print(
            "Starting Bluetooth TV "
            "accessibility remote...",
            flush=True,
        )

        print(
            "GPIO configuration:",
            flush=True,
        )

        for gpio, sequence in SEQUENCES.items():

            print(
                f"  GPIO {gpio}: "
                f"{' -> '.join(sequence)}",
                flush=True,
            )

        print(
            "Global cooldown: "
            f"{COOLDOWN_SECONDS:.1f} seconds",
            flush=True,
        )

        initialize_bluetooth()

        print(
            "Waiting for Android TV connection...",
            flush=True,
        )

        print(
            "Press x, X, Esc, or Ctrl+C "
            "to stop.",
            flush=True,
        )

        # Timer = 1 decisecond = 100 ms.
        #
        # LE_TIMER callbacks are used to poll
        # the GPIO switches.

        btfpy.Le_server(
            le_callback,
            1,
        )

    except KeyboardInterrupt:

        request_stop()

    except Exception as error:

        print(
            f"Fatal error: {error}",
            flush=True,
        )

        raise

    finally:

        print(
            "Releasing GPIO and "
            "Bluetooth resources...",
            flush=True,
        )

        # ----------------------------------------------------
        # GPIO cleanup
        # ----------------------------------------------------

        for button in buttons.values():

            try:
                button.close()

            except Exception:
                pass

        # ----------------------------------------------------
        # Bluetooth cleanup
        # ----------------------------------------------------

        if bluetooth_initialized:

            # Save bond again during an orderly shutdown.
            save_pairing_information()

            try:
                btfpy.Close_all()

            except Exception as error:

                print(
                    "Warning while closing "
                    f"Bluetooth: {error}",
                    flush=True,
                )

        print(
            "Stopped cleanly.",
            flush=True,
        )


if __name__ == "__main__":
    main()
