# Remote Sniffer (Android)

An experimental Android app that acts as a BLE central/host for an original TV remote. It scans for nearby BLE devices, connects to a selected device, looks for the standard HID over GATT service (`0x1812`), reads the HID report map when permitted, subscribes to input report notifications (`0x2A4D`), and exports raw reports with timestamps as JSON.

This is separate from the ESP32 firmware: the ESP32 advertises as a HID peripheral, while this app connects as the central to the remote being investigated. It does not transmit button commands.

## Build

For every app revision, increment both `versionCode` and `versionName` in `app/build.gradle.kts`. The version name appears in the app title, startup session log, and exported JSON; the numeric code is included in the log and export as well.

Open this directory in Android Studio with Android SDK 35 installed, or use Gradle 8.9+ with JDK 17:

```sh
gradle assembleDebug
```

Install `app/build/outputs/apk/debug/app-debug.apk` on an Android phone/tablet with BLE support. Pair/bond the remote in Android Bluetooth settings first, then grant nearby-device permissions, scan, and tap its entry. A GATT connection is not the same as Android's HID input connection: Android may not create an input device or route key events to this app. A successful CCCD write means only that Android accepted subscription setup; reports appear only if the remote sends notifications/indications. Disconnect before connecting to another remote. Use **Export capture JSON** to share a text JSON capture.

## Limits

- The original remote must be discoverable and expose standard BLE HID (`0x1812`) for the current capture path.
- A remote using Bluetooth Classic HID or a proprietary GATT service needs a different implementation.
- Android and the remote control pairing security. The app does not bypass encryption or pairing requirements.
- HID report interpretation depends on the report map and report-reference descriptors. The current decoder only recognizes the common 8-byte keyboard report layout; raw bytes are always retained.
- Android devices differ in BLE central support and permission behavior. Test with the specific phone and remote.
- Some Android versions deny subscriptions to HID Report (`0x2A4D`) from ordinary apps because the operation requires the system-only `BLUETOOTH_PRIVILEGED` permission. This app cannot grant or bypass that restriction; a denial means those reports are unavailable through this capture path. Boot Keyboard Input (`0x2A22`) may still subscribe, but the remote must actually emit reports on it.
