# 🚀 Parallel APK Installer (Multi-Device) – Web UI

A robust, web-based tool for **installing and launching APKs on multiple Android devices at once**. Enter your device IPs, select your APK/ADB files, configure parallelism and launch settings, and deploy apps with zero hassle—all from your browser.

## ✨ Features

- **Web browser UI** (Flask app, no desktop/exe dependency)
- **Multi-device install:** Install APKs on many devices in parallel
- **Old package uninstall**: Removes existing app before new install
- **Auto-launch after install**: Opens the app automatically on every device
- **Flexible configuration:**
  - Device IPs (manual entry, one per line)
  - Max parallel installs
  - ADB and APK file path selection
  - Package to uninstall & launch
- **Live Progress Tracking:** Real-time bar and per-device status
- **Download results CSV** for records and troubleshooting
- **Strong error handling** and robust networking

## 🚦 How To Use

1. **Install Python packages:**  
   ```bash
   pip install flask
   ```

2. **Directory structure:**  
   ```
   apk_installer/
   ├── app.py
   └── templates/
       └── index.html
   ```
   Place your `adb.exe` and your `app-release.apk` in the project directory or wherever you prefer.

3. **Launch the server:**  
   ```bash
   python app.py
   ```

4. **Open your web browser:**  
   Visit [http://localhost:5000](http://localhost:5000)

5. **Fill out the form:**
   - **ADB Path**: e.g., `./platform-tools/adb.exe`
   - **APK Path**: e.g., `./app-release.apk`
   - **Device IPs**: One per line (e.g., `192.168.1.10`)
   - **Other fields**: As needed for your deployment!
   - Hit **“Start Installation”**

6. **Review results & download log**
   - Watch real-time device-by-device progress and results in the browser.
   - Download the detailed log (CSV).

## ⚙️ Configuration Fields

- **ADB Path**: Path to your Android Debug Bridge executable
- **APK File Path**: Absolute or relative path to your APK file
- **Device IP Addresses**: One per line (`10.0.0.2`, `10.0.0.3`, etc.)
- **Max Parallel Installations**: Number of devices to process at once (2–5 recommended for best results)
- **Old Package to Uninstall**: (optional) Package to remove before install
- **Package to Launch After Install**: (optional) Typically the same as the APK's package name
- **Auto-launch app**: Open app on device right after install

## 📝 Details & Troubleshooting

- Your devices must have ADB over TCP enabled (`adb tcpip 5555` then `adb connect ` initially)
- Make sure firewall allows PC  device communication
- ADB path and APK path are validated by the tool
- If you see “Install failed,” check storage, device USB debugging, permissions, and APK compatibility
- If launch fails, try specifying a main activity or use the "monkey" method as a fallback

## 📂 Folder Example

```sh
apk_installer/
├── app.py             # Flask app backend
├── templates/
│   └── index.html     # Web UI frontend
├── platform-tools/
│   └── adb.exe        # Your own copy of adb
├── app-release.apk    # Your APK file
└── install_log_*.csv  # Generated after each run
```
