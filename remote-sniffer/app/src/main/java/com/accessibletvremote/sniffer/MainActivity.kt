package com.accessibletvremote.sniffer

import android.Manifest
import android.app.Activity
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothManager
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.input.InputManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.InputDevice
import android.view.KeyCharacterMap
import android.view.KeyEvent
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

class MainActivity : Activity() {
    private val hidService = UUID.fromString("00001812-0000-1000-8000-00805f9b34fb")
    private val reportMapUuid = UUID.fromString("00002a4b-0000-1000-8000-00805f9b34fb")
    private val reportUuid = UUID.fromString("00002a4d-0000-1000-8000-00805f9b34fb")
    private val bootKeyboardInputUuid = UUID.fromString("00002a22-0000-1000-8000-00805f9b34fb")
    private val batteryService = UUID.fromString("0000180f-0000-1000-8000-00805f9b34fb")
    private val batteryLevelUuid = UUID.fromString("00002a19-0000-1000-8000-00805f9b34fb")
    private val cccdUuid = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    private val reportRefUuid = UUID.fromString("00002908-0000-1000-8000-00805f9b34fb")
    private val devices = linkedMapOf<String, BluetoothDevice>()
    private val captures = mutableListOf<JSONObject>()
    private val sessionLog = mutableListOf<String>()
    // Android GATT permits only one request at a time. Queue report-map reads and
    // CCCD writes so that subscriptions do not get silently dropped.
    private val gattOperations = ArrayDeque<() -> Boolean>()
    private var gattOperationInFlight = false
    private var gattOperationGeneration = 0
    private lateinit var adapter: BluetoothAdapter
    private var scanner: BluetoothLeScanner? = null
    private var gatt: BluetoothGatt? = null
    private var connectingAddress: String? = null
    private lateinit var log: TextView
    private lateinit var deviceList: LinearLayout
    private val handler = Handler(Looper.getMainLooper())
    private val inputManager by lazy { getSystemService(Context.INPUT_SERVICE) as InputManager }
    private val inputDeviceListener = object : InputManager.InputDeviceListener {
        override fun onInputDeviceAdded(deviceId: Int) = recordInputDevice("added", deviceId)
        override fun onInputDeviceChanged(deviceId: Int) = recordInputDevice("changed", deviceId)
        override fun onInputDeviceRemoved(deviceId: Int) = recordInputDevice("removed", deviceId)
    }
    private val dateFormat = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)
    private val appVersion: String by lazy {
        packageManager.getPackageInfo(packageName, 0).versionName ?: "unknown"
    }
    private val appVersionCode: Long by lazy {
        if (Build.VERSION.SDK_INT >= 28) packageManager.getPackageInfo(packageName, 0).longVersionCode
        else legacyVersionCode()
    }

    @Suppress("DEPRECATION")
    private fun legacyVersionCode(): Long = packageManager.getPackageInfo(packageName, 0).versionCode.toLong()

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val device = result.device
            val key = device.address
            devices[key] = device
            runOnUiThread { renderDevices() }
        }
        override fun onScanFailed(errorCode: Int) = append("Scan failed: $errorCode")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val manager = getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
        adapter = manager.adapter
        buildUi()
        append("Remote Sniffer v$appVersion (build $appVersionCode). Pair the remote in Android Bluetooth settings first, then connect here. A BLE GATT connection is separate from Android's HID input connection: this app only logs Android key events if Android creates an input device and routes events to it. Missing input devices/events do not prove the remote sent no buttons.")
        requestBluetoothPermissions()
    }

    override fun onResume() {
        super.onResume()
        inputManager.registerInputDeviceListener(inputDeviceListener, handler)
        InputDevice.getDeviceIds().forEach { recordInputDevice("present", it) }
    }

    override fun onPause() {
        inputManager.unregisterInputDeviceListener(inputDeviceListener)
        super.onPause()
    }

    private fun recordInputDevice(action: String, deviceId: Int) {
        val device = InputDevice.getDevice(deviceId)
        val timestamp = System.currentTimeMillis()
        if (device == null) {
            val entry = JSONObject().put("type", "android_input_device").put("timestamp", timestamp)
                .put("action", action).put("device_id", deviceId).put("present", false)
            captures.add(entry)
            append("${dateFormat.format(Date(timestamp))}  android_input_device $action id=$deviceId (details unavailable)")
            return
        }
        val sources = "0x${device.sources.toString(16)}"
        val keyboard = device.supportsSource(InputDevice.SOURCE_KEYBOARD)
        val dpad = device.supportsSource(InputDevice.SOURCE_DPAD)
        val gamepad = device.supportsSource(InputDevice.SOURCE_GAMEPAD)
        val entry = JSONObject()
            .put("type", "android_input_device")
            .put("timestamp", timestamp)
            .put("action", action)
            .put("device_id", device.id)
            .put("device_name", device.name)
            .put("vendor_id", device.vendorId)
            .put("product_id", device.productId)
            .put("sources", sources)
            .put("keyboard", keyboard)
            .put("dpad", dpad)
            .put("gamepad", gamepad)
            .put("virtual", device.isVirtual)
        captures.add(entry)
        append("${dateFormat.format(Date(timestamp))}  android_input_device $action ${device.name} id=${device.id} vendor=${device.vendorId} product=${device.productId} sources=$sources keyboard=$keyboard dpad=$dpad gamepad=$gamepad virtual=${device.isVirtual}")
    }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (event.deviceId != KeyCharacterMap.VIRTUAL_KEYBOARD) {
            recordAndroidKeyEvent(event)
        }
        return super.dispatchKeyEvent(event)
    }

    private fun recordAndroidKeyEvent(event: KeyEvent) {
        val device = InputDevice.getDevice(event.deviceId)
        val timestamp = System.currentTimeMillis()
        val action = when (event.action) {
            KeyEvent.ACTION_DOWN -> "down"
            KeyEvent.ACTION_UP -> "up"
            KeyEvent.ACTION_MULTIPLE -> "multiple"
            else -> "unknown(${event.action})"
        }
        val keyName = KeyEvent.keyCodeToString(event.keyCode)
        val deviceName = device?.name ?: "unknown"
        val source = "0x${event.source.toString(16)}"
        val entry = JSONObject()
            .put("type", "android_key_event")
            .put("timestamp", timestamp)
            .put("action", action)
            .put("key_code", event.keyCode)
            .put("key_name", keyName)
            .put("repeat_count", event.repeatCount)
            .put("device_id", event.deviceId)
            .put("device_name", deviceName)
            .put("source", source)
        captures.add(entry)
        append("${dateFormat.format(Date(timestamp))}  android_key_event $action $keyName (code ${event.keyCode}, repeat ${event.repeatCount}) from $deviceName [$source]")
    }

    private fun buildUi() {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(20, 20, 20, 20)
            layoutParams = ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
        }
        val title = TextView(this).apply { text = "Remote Sniffer v$appVersion · BLE HID"; textSize = 22f }
        root.addView(title)
        root.addView(Button(this).apply { text = "Scan for remotes"; setOnClickListener { startScan() } })
        root.addView(Button(this).apply { text = "Disconnect remote"; setOnClickListener { disconnectRemote() } })
        root.addView(Button(this).apply { text = "Export capture JSON"; setOnClickListener { exportCapture() } })
        val scanScroll = ScrollView(this)
        deviceList = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        scanScroll.addView(deviceList)
        root.addView(TextView(this).apply { text = "Nearby devices (tap to connect)"; textSize = 16f })
        root.addView(scanScroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 4f))
        root.addView(TextView(this).apply { text = "Capture log"; textSize = 16f })
        val logScroll = ScrollView(this)
        log = TextView(this).apply { setTextIsSelectable(true); textSize = 13f }
        logScroll.addView(log)
        root.addView(logScroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 0.7f))
        setContentView(root)
    }

    private fun requestBluetoothPermissions() {
        val required = if (Build.VERSION.SDK_INT >= 31) arrayOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
        else arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
        val missing = required.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) requestPermissions(missing.toTypedArray(), 100)
    }

    private fun hasScanPermission(): Boolean = if (Build.VERSION.SDK_INT >= 31)
        checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED &&
                checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED
    else checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED

    private fun startScan() {
        if (!hasScanPermission()) { requestBluetoothPermissions(); return }
        if (!adapter.isEnabled) { append("Turn Bluetooth on, then scan again."); return }
        devices.clear(); renderDevices(); scanner = adapter.bluetoothLeScanner
        try {
            scanner?.startScan(scanCallback)
            append("Scanning for BLE devices for 12 seconds…")
            handler.postDelayed({ try { scanner?.stopScan(scanCallback) } catch (_: SecurityException) {}; append("Scan complete.") }, 12_000)
        } catch (e: SecurityException) { append("Bluetooth permission unavailable: ${e.message}") }
    }

    private fun renderDevices() {
        deviceList.removeAllViews()
        devices.forEach { (address, device) ->
            val name = try { device.name ?: "Unnamed device" } catch (_: SecurityException) { "Bluetooth device" }
            deviceList.addView(Button(this).apply {
                text = "$name · $address"
                setOnClickListener { connect(device, name) }
            })
        }
    }

    @Suppress("MissingPermission")
    private fun connect(device: BluetoothDevice, name: String) {
        if (gatt != null || connectingAddress != null) {
            append("Already connecting to or connected with ${connectingAddress ?: gatt?.device?.address ?: "a remote"}. Disconnect it before starting another GATT connection.")
            return
        }
        try {
            scanner?.stopScan(scanCallback)
            append("Connecting to $name (${device.address})…")
            connectingAddress = device.address
            gatt = device.connectGatt(this, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
            if (gatt == null) {
                connectingAddress = null
                append("Android did not create a GATT connection. Pair the remote in Android Bluetooth settings, then retry.")
            }
        } catch (e: SecurityException) { connectingAddress = null; append("Connection permission unavailable: ${e.message}") }
    }

    @Suppress("MissingPermission")
    private fun disconnectRemote() {
        val active = gatt
        if (active == null) {
            append("No GATT connection is active.")
            return
        }
        append("Disconnecting ${active.device.address}…")
        try {
            active.disconnect()
        } catch (e: SecurityException) {
            append("Disconnect permission unavailable: ${e.message}")
        }
    }

    private val gattCallback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (g !== gatt) { g.close(); return }
            if (status != BluetoothGatt.GATT_SUCCESS) {
                append("GATT connection error: $status. Check Android Bluetooth settings for pairing/bonding and retry.")
                connectingAddress = null
                g.close(); gatt = null
                return
            }
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                connectingAddress = g.device.address
                append("Connected; discovering services…")
                g.discoverServices()
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                append("Disconnected. Pairing/bond status: ${if (g.device.bondState == BluetoothDevice.BOND_BONDED) "bonded" else "not bonded"}. If pairing is required, complete it in Android Bluetooth settings and reconnect.")
                g.close(); gatt = null; connectingAddress = null
                gattOperations.clear(); gattOperationInFlight = false; gattOperationGeneration++
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) { append("Service discovery failed: $status"); return }
            append("Discovered ${g.services.size} GATT service(s).")
            g.services.forEach { discovered ->
                append("Service ${discovered.uuid}: ${discovered.characteristics.size} characteristic(s).")
                discovered.characteristics.forEach { characteristic ->
                    val properties = buildList {
                        if (characteristic.properties and BluetoothGattCharacteristic.PROPERTY_READ != 0) add("read")
                        if (characteristic.properties and BluetoothGattCharacteristic.PROPERTY_WRITE != 0) add("write")
                        if (characteristic.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY != 0) add("notify")
                        if (characteristic.properties and BluetoothGattCharacteristic.PROPERTY_INDICATE != 0) add("indicate")
                    }.joinToString(",").ifBlank { "none" }
                    append("  Characteristic ${characteristic.uuid} [$properties]")
                }
            }
            val service = g.getService(hidService)
            if (service == null) { append("No standard HID over GATT service (0x1812) found. Export still includes scan details."); return }
            append("Found HID service 0x1812 with ${service.characteristics.size} characteristic(s).")
            append("Pairing status: ${if (g.device.bondState == BluetoothDevice.BOND_BONDED) "bonded" else "not bonded"}. A successful CCCD write only confirms subscription setup; reports arrive only when the remote sends notifications/indications.")
            val bootKeyboardInput = service.characteristics.firstOrNull { it.uuid == bootKeyboardInputUuid }
            if (bootKeyboardInput == null) {
                append("No HID Boot Keyboard Input characteristic (0x2A22) is exposed.")
            } else if (bootKeyboardInput.properties and (BluetoothGattCharacteristic.PROPERTY_NOTIFY or BluetoothGattCharacteristic.PROPERTY_INDICATE) == 0) {
                append("HID Boot Keyboard Input (0x2A22) is exposed but does not support notifications or indications.")
            }
            if (service.characteristics.none { it.uuid == reportUuid }) {
                append("No HID Report characteristic (0x2A4D) is exposed; button presses cannot be captured on this connection.")
            }
            gattOperations.clear()
            gattOperationInFlight = false
            gattOperationGeneration++
service.characteristics.firstOrNull { characteristic ->
    characteristic.uuid == reportUuid &&
        characteristic.properties and (BluetoothGattCharacteristic.PROPERTY_NOTIFY or BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0
}?.let { characteristic -> enqueueNotification(g, characteristic) }
            bootKeyboardInput?.takeIf { characteristic ->
                characteristic.properties and (BluetoothGattCharacteristic.PROPERTY_NOTIFY or BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0
            }?.let { characteristic -> enqueueNotification(g, characteristic) }
            // Xiaomi remotes may also publish proprietary input on their vendor service.
            g.services.filter { it.uuid != hidService }.forEach { vendorService ->
                vendorService.characteristics.forEach { characteristic ->
                    if (characteristic.properties and (BluetoothGattCharacteristic.PROPERTY_NOTIFY or BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0) {
                        enqueueNotification(g, characteristic)
                    }
                }
            }
            val reportMap = service.characteristics.firstOrNull {
                it.uuid == reportMapUuid && it.properties and BluetoothGattCharacteristic.PROPERTY_READ != 0
            }
            if (reportMap != null) enqueueGattOperation("read HID report map") { g.readCharacteristic(reportMap) }
            runNextGattOperation()
        }

        override fun onDescriptorWrite(g: BluetoothGatt, descriptor: BluetoothGattDescriptor, status: Int) {
            if (descriptor.uuid == cccdUuid) {
                val characteristic = descriptor.characteristic
                val result = if (status == BluetoothGatt.GATT_SUCCESS) "succeeded" else "failed"
                append("CCCD write $result: service ${characteristic.service.uuid}, characteristic ${characteristic.uuid}, GATT status $status.")
            }
            completeGattOperation()
        }

        @Suppress("DEPRECATION")
        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            if (status == BluetoothGatt.GATT_SUCCESS && c.uuid == reportMapUuid) {
                append("Report map: ${c.value?.hex() ?: ""}")
                captures.add(JSONObject().put("type", "report_map").put("timestamp", System.currentTimeMillis()).put("hex", c.value?.hex() ?: ""))
            }
            if (c.uuid == reportMapUuid) completeGattOperation()
        }

        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray, status: Int) {
            if (status == BluetoothGatt.GATT_SUCCESS && c.uuid == reportMapUuid) {
                append("Report map: ${value.hex()}")
                captures.add(JSONObject().put("type", "report_map").put("timestamp", System.currentTimeMillis()).put("hex", value.hex()))
            }
            if (c.uuid == reportMapUuid) completeGattOperation()
        }

        @Suppress("DEPRECATION")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            recordReport(c, c.value ?: byteArrayOf())
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            recordReport(c, value)
        }
    }

    private fun enqueueGattOperation(label: String, operation: () -> Boolean) {
        append("Queued GATT operation: $label")
        gattOperations.addLast {
            append("Starting GATT operation: $label")
            val started = operation()
            if (!started) append("GATT operation did not start: $label")
            started
        }
    }

    private fun runNextGattOperation() {
        if (gattOperationInFlight || gattOperations.isEmpty()) return
        gattOperationInFlight = true
        val generation = ++gattOperationGeneration
        if (!gattOperations.removeFirst().invoke()) {
            gattOperationInFlight = false
            runNextGattOperation()
        } else {
            handler.postDelayed({
                if (gattOperationInFlight && generation == gattOperationGeneration) {
                    append("GATT operation timed out after 5 seconds; continuing queue.")
                    gattOperationInFlight = false
                    runNextGattOperation()
                }
            }, 5_000)
        }
    }

    private fun completeGattOperation() {
        if (gattOperationInFlight) {
            gattOperationInFlight = false
            gattOperationGeneration++
            runNextGattOperation()
        }
    }

    @Suppress("DEPRECATION", "MissingPermission")
    private fun enqueueNotification(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
        val reportRef = c.getDescriptor(reportRefUuid)?.value?.hex() ?: "unavailable"
        append("Notify characteristic ${c.service.uuid}/${c.uuid} (report reference: $reportRef)")
        val descriptor = c.getDescriptor(cccdUuid)
        if (descriptor == null) {
            append("Subscription unavailable: service ${c.service.uuid}, characteristic ${c.uuid} has no CCCD.")
            return
        }
        val value = if (c.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY != 0)
            BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE else BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
        enqueueGattOperation("subscribe to ${c.service.uuid}/${c.uuid} report $reportRef") {
            try {
                append("Requesting local notification registration for ${c.service.uuid}/${c.uuid}.")
                val localEnabled = g.setCharacteristicNotification(c, true)
                append("Local notification registration for ${c.service.uuid}/${c.uuid}: ${if (localEnabled) "succeeded" else "failed"}.")
                if (!localEnabled) {
                    false
                } else if (Build.VERSION.SDK_INT >= 33) {
                    val result = g.writeDescriptor(descriptor, value)
                    append("CCCD write request for ${c.service.uuid}/${c.uuid}: ${if (result == BluetoothGatt.GATT_SUCCESS) "accepted" else "rejected"}, immediate result $result.")
                    result == BluetoothGatt.GATT_SUCCESS
                } else {
                    descriptor.value = value
                    val started = g.writeDescriptor(descriptor)
                    append("CCCD write request for ${c.service.uuid}/${c.uuid}: ${if (started) "accepted" else "rejected"} by Android API.")
                    started
                }
        } catch (e: SecurityException) {
            append(if (c.service.uuid == hidService && (c.uuid == reportUuid || c.uuid == bootKeyboardInputUuid))
                "HID input unavailable: Android denied ${c.uuid} notification setup (BLUETOOTH_PRIVILEGED is system-only for this operation). This app cannot bypass that platform restriction; no reports from this characteristic can be captured here."
                else "Subscription operation threw SecurityException: ${e.message ?: "no detail"}.")
            false
        } catch (e: Exception) {
                append("Subscription operation threw ${e.javaClass.simpleName}: ${e.message ?: "no detail"}.")
                false
            }
        }
    }

    private fun recordReport(c: BluetoothGattCharacteristic, value: ByteArray) {
        val timestamp = System.currentTimeMillis()
        val service = c.service
        val hex = value.hex()
    val isBatteryLevel = service.uuid == batteryService && c.uuid == batteryLevelUuid
    val isHidInput = service.uuid == hidService && (c.uuid == reportUuid || c.uuid == bootKeyboardInputUuid)
    val type = when {
        isBatteryLevel -> "battery_level"
        isHidInput && c.uuid == bootKeyboardInputUuid -> "hid_boot_keyboard_input"
        isHidInput -> "hid_input_report"
        else -> "notification"
    }
    val decoded = when {
        isBatteryLevel && value.isNotEmpty() -> "${value[0].toInt() and 0xff}% battery"
        isHidInput -> decodeKeyboardReport(value)
        else -> ""
    }
    val entry = JSONObject().put("type", type).put("timestamp", timestamp)
            .put("service_uuid", service.uuid.toString()).put("characteristic_uuid", c.uuid.toString())
            .put("hex", hex).put("decoded", decoded)
        captures.add(entry)
    append("${dateFormat.format(Date(timestamp))}  $type ${c.uuid}  $hex${if (decoded.isNotBlank()) "  $decoded" else ""}")
    }

    private fun decodeKeyboardReport(bytes: ByteArray): String {
        if (bytes.size < 8) return ""
        val usageNames = mapOf(0x28 to "Enter", 0x29 to "Escape/Back", 0x4F to "Right", 0x50 to "Left", 0x51 to "Down", 0x52 to "Up", 0x4A to "Home")
        val keys = bytes.drop(2).map { it.toInt() and 0xff }.filter { it != 0 }
        val labels = keys.map { key -> usageNames[key] ?: when (key) {
            in 0x1e..0x26 -> (key - 0x1d).toString()
            0x27 -> "0"
            else -> "usage 0x${key.toString(16)}"
        } }.toMutableList()
        if (bytes[0].toInt() != 0) labels.add("modifier 0x${(bytes[0].toInt() and 0xff).toString(16)}")
        return labels.joinToString("+")
    }

    private fun exportCapture() {
        val report = JSONObject().put("app", "Remote Sniffer").put("app_version", appVersion)
            .put("version_code", appVersionCode).put("exported_at", System.currentTimeMillis())
            .put("device", gatt?.device?.address ?: JSONObject.NULL)
            .put("capture_count", captures.size).put("captures", JSONArray(captures))
            .put("session_log", JSONArray(sessionLog))
        val send = android.content.Intent(android.content.Intent.ACTION_SEND).apply {
            type = "application/json"; putExtra(android.content.Intent.EXTRA_SUBJECT, "Bluetooth remote capture")
            putExtra(android.content.Intent.EXTRA_TEXT, report.toString(2))
        }
        startActivity(android.content.Intent.createChooser(send, "Share capture JSON"))
    }

    private fun append(message: String) = runOnUiThread {
        sessionLog.add("${dateFormat.format(Date())} $message")
        log.append("$message\n")
        (log.parent as? ScrollView)?.post { (log.parent as ScrollView).fullScroll(ScrollView.FOCUS_DOWN) }
    }

    private fun ByteArray.hex() = joinToString("") { "%02X".format(it) }

    override fun onDestroy() {
        try { scanner?.stopScan(scanCallback) } catch (_: Exception) {}
        gatt?.close(); gatt = null; connectingAddress = null
        super.onDestroy()
    }
}
