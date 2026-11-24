# ASC Remote Hub for Android 0.4.0

Android 11+; no root, ADB or Shizuku. Download the signed APK from your own hub at `/downloads/asc-remote-hub-android.apk`. Open the setup wizard, enable notifications, Accessibility, shared storage and unrestricted battery usage. Enter your own HTTPS server address (for example https://hub.example.org), generate a one-time pairing code in that server's dashboard and confirm pairing. The address is stored alongside the encrypted device token. Unpair before switching servers. Updates from 0.3.0 onward retain pairing; older experimental builds without a stored server address must pair again. Android may require allowing restricted settings for sideloaded apps.

The agent uses outbound HTTPS polling and reconnects automatically. Android background restrictions, Doze and force-stop may interrupt operation. UI control requires an unlocked, lit screen and enabled Accessibility. Password fields are excluded from the UI tree; private app data is not accessible. Use the notification or app to stop remote control.

Tools: machine_info, list_directory, stat_path, android_apps, android_launch, android_ui_tree, android_ui_action, android_gesture, android_file_read, android_file_write.

For node actions use a fresh snapshot and node ID. For gestures use the snapshot and screen dimensions from android_ui_tree. Each stroke has points (screen pixel x/y), duration_ms (1–3000), and optional start_ms (0–1000). One point is a tap or hold; multiple points form a swipe or drag. Up to five strokes can overlap. Refresh the snapshot after each gesture; the agent rejects stale window/orientation information. Gestures can interrupt local touches. The agent reports actual completion or cancellation, not just dispatch acceptance.

Example swipe up:

```json
{"device":"YOUR_DEVICE_ID","snapshot":"FRESH_SNAPSHOT","strokes":[{"points":[{"x":500,"y":1600},{"x":500,"y":600}],"duration_ms":400}]}
```

File transfer uses Base64 chunks of up to 65536 bytes. Offset zero creates a new file and never overwrites an existing one.

Build from source with JDK 17, Android SDK 35 and build-tools 35.0.0 using android/build.sh (mount source at /src, SDK at /sdk, output at /out and a private signing-key directory at /keys). To distribute your own build, replace server/static/downloads/asc-remote-hub-android.apk and rebuild the hub image. Keep signing keys outside the repository. A different signing certificate cannot update an existing installation.

Pairing, UI tree and vertical swipes were verified on a physical Android 17 device. Horizontal gestures, drag and multitouch are implemented but have not been separately device-tested.
