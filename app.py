from flask import Flask, render_template, request, jsonify, send_file
import subprocess
import threading
import time
import csv
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json

app = Flask(__name__)

# Global variables for tracking installation progress
installation_progress = {
    'status': 'idle',
    'total_devices': 0,
    'completed': 0,
    'success': 0,
    'failed': 0,
    'results': [],
    'log_file': None
}

class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    WHITE = '\033[97m'
    RESET = '\033[0m'

def run_adb_command(device_ip, command_parts, adb_path, timeout=60):
    try:
        if device_ip == 'connect':
            cmd = [str(adb_path)] + command_parts
        else:
            cmd = [str(adb_path), '-s', device_ip] + command_parts
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout + result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return f"Command timeout after {timeout}s", 1
    except Exception as e:
        return str(e), 1

def launch_app(device_ip, package_name, adb_path):
    try:
        # Try multiple launch methods
        launcher_activities = [
            f"{package_name}/.MainActivity",
            f"{package_name}/.LauncherActivity", 
            f"{package_name}/.SplashActivity",
            f"{package_name}/.HomeActivity"
        ]
        
        for activity in launcher_activities:
            output, code = run_adb_command(device_ip, ['shell', 'am', 'start', '-n', activity], adb_path, 10)
            if code == 0 and "Error" not in output:
                return True, f"Launched via {activity}"
        
        # Fallback to monkey tool
        output, code = run_adb_command(device_ip, ['shell', 'monkey', '-p', package_name, '-c', 'android.intent.category.LAUNCHER', '1'], adb_path, 15)
        if code == 0:
            return True, "Launched via monkey tool"
            
        return False, "All launch methods failed"
        
    except Exception as e:
        return False, str(e)

def install_on_device(device_ip, config):
    timestamp = datetime.now().isoformat()
    uninstall_verified = "NO"
    install_verified = "NO"
    launch_status = "NO"
    
    try:
        # Connect to device
        connected = False
        for attempt in range(3):
            output, code = run_adb_command('connect', ['connect', device_ip], config['adb_path'], 20)
            if "connected to" in output or "already connected" in output:
                connected = True
                break
            time.sleep(5)
        
        if not connected:
            raise Exception("Failed to establish connection")
        
        time.sleep(2)
        
        # Uninstall old version if specified
        if config['old_package']:
            run_adb_command(device_ip, ['uninstall', config['old_package']], config['adb_path'], 30)
            run_adb_command(device_ip, ['shell', 'pm', 'uninstall', '--user', '0', config['old_package']], config['adb_path'], 30)
            
            output, _ = run_adb_command(device_ip, ['shell', 'pm', 'list', 'packages'], config['adb_path'], 15)
            if config['old_package'] not in output:
                uninstall_verified = "YES"
        
        # Install APK
        apk_size = Path(config['apk_path']).stat().st_size / (1024 * 1024)
        timeout = max(120, int(apk_size * 3))
        
        output, code = run_adb_command(device_ip, ['install', '-r', config['apk_path']], config['adb_path'], timeout)
        
        if "Success" in output:
            install_verified = "YES"
            
            # Launch app if enabled
            if config['auto_launch'] and config['launch_package']:
                time.sleep(3)
                success, launch_msg = launch_app(device_ip, config['launch_package'], config['adb_path'])
                if success:
                    launch_status = "YES"
                    return timestamp, device_ip, "SUCCESS", f"Installed & Launched {config['launch_package']}", uninstall_verified, install_verified, launch_status
                else:
                    return timestamp, device_ip, "PARTIAL", f"Installed but launch failed: {launch_msg}", uninstall_verified, install_verified, launch_status
            else:
                return timestamp, device_ip, "SUCCESS", "Installed", uninstall_verified, install_verified, launch_status
        else:
            raise Exception(f"Install failed: {output.strip()}")
            
    except Exception as e:
        error_msg = str(e).replace('\r\n', ' ').replace('\n', ' ').strip()
        return timestamp, device_ip, "FAILED", error_msg, uninstall_verified, install_verified, launch_status
    
    finally:
        run_adb_command('connect', ['disconnect', device_ip], config['adb_path'], 10)

def run_installation(config):
    global installation_progress
    
    devices = [ip.strip() for ip in config['devices'] if ip.strip()]
    installation_progress = {
        'status': 'running',
        'total_devices': len(devices),
        'completed': 0,
        'success': 0,
        'failed': 0,
        'results': [],
        'log_file': config['log_file']
    }
    
    # Initialize log file
    with open(config['log_file'], 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["Timestamp", "Device", "Status", "Details", "UninstallVerified", "InstallVerified", "LaunchStatus"])
    
    with ThreadPoolExecutor(max_workers=config['max_parallel']) as executor:
        future_to_device = {executor.submit(install_on_device, device, config): device for device in devices}
        
        for future in as_completed(future_to_device):
            device = future_to_device[future]
            try:
                result = future.result()
                installation_progress['results'].append(result)
                installation_progress['completed'] += 1
                
                if result[2] == "SUCCESS":
                    installation_progress['success'] += 1
                else:
                    installation_progress['failed'] += 1
                
                # Write to log
                with open(config['log_file'], 'a', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(result)
                    
            except Exception as e:
                installation_progress['completed'] += 1
                installation_progress['failed'] += 1
    
    installation_progress['status'] = 'completed'

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start_installation', methods=['POST'])
def start_installation():
    global installation_progress
    
    if installation_progress['status'] == 'running':
        return jsonify({'error': 'Installation already in progress'})
    
    data = request.json
    
    # Validate inputs
    if not data.get('devices'):
        return jsonify({'error': 'No devices specified'})
    
    if not data.get('apk_path') or not os.path.exists(data['apk_path']):
        return jsonify({'error': 'APK file not found'})
    
    if not data.get('adb_path') or not os.path.exists(data['adb_path']):
        return jsonify({'error': 'ADB executable not found'})
    
    # Prepare configuration
    config = {
        'devices': data['devices'].split('\n'),
        'apk_path': data['apk_path'],
        'adb_path': data['adb_path'],
        'old_package': data.get('old_package', ''),
        'launch_package': data.get('launch_package', ''),
        'auto_launch': data.get('auto_launch', False),
        'max_parallel': min(int(data.get('max_parallel', 5)), 10),  # Max 10 for safety
        'log_file': f"install_log_{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    }
    
    # Start installation in background thread
    thread = threading.Thread(target=run_installation, args=(config,))
    thread.daemon = True
    thread.start()
    
    return jsonify({'success': True, 'message': 'Installation started'})

@app.route('/progress')
def get_progress():
    return jsonify(installation_progress)

@app.route('/download_log')
def download_log():
    if installation_progress['log_file'] and os.path.exists(installation_progress['log_file']):
        return send_file(installation_progress['log_file'], as_attachment=True)
    return jsonify({'error': 'Log file not found'})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)