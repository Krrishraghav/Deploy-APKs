from flask import Flask, render_template, request, jsonify, send_file
import subprocess
import threading
import time
import csv
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from functools import lru_cache

app = Flask(__name__)

installation_progress = {
    'status': 'idle',
    'total_devices': 0,
    'completed': 0,
    'success': 0,
    'failed': 0,
    'results': [],
    'log_file': None
}

# Connection pool for faster operations
active_connections = {}
connection_lock = threading.Lock()

def run_adb_command(device_ip, command_parts, adb_path, timeout=20):
    """Balanced ADB command execution"""
    try:
        if device_ip == 'connect':
            cmd = [str(adb_path)] + command_parts
        else:
            cmd = [str(adb_path), '-s', device_ip] + command_parts
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout + result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return f"Timeout after {timeout}s", 1
    except Exception as e:
        return str(e), 1

def ensure_reliable_connection(device_ip, adb_path, max_retries=2):
    """Balanced connection - fast but reliable"""
    with connection_lock:
        # Quick check if already connected
        if device_ip in active_connections:
            try:
                test = subprocess.run([
                    adb_path, '-s', device_ip, 'shell', 'echo "ping"'
                ], capture_output=True, text=True, timeout=4)
                if test.returncode == 0 and "ping" in test.stdout:
                    return True
                else:
                    active_connections.pop(device_ip, None)
            except:
                active_connections.pop(device_ip, None)
        
        # Connection attempts with proper cleanup
        for attempt in range(max_retries):
            try:
                # Clean disconnect first
                subprocess.run([adb_path, 'disconnect', device_ip], 
                              capture_output=True, timeout=5)
                time.sleep(1)
                
                # Connect
                result = subprocess.run([
                    adb_path, 'connect', device_ip
                ], capture_output=True, text=True, timeout=12)
                
                if "connected" in result.stdout or "already connected" in result.stdout:
                    # Verify connection works
                    time.sleep(1)
                    test = subprocess.run([
                        adb_path, '-s', device_ip, 'shell', 'echo "test_ok"'
                    ], capture_output=True, text=True, timeout=6)
                    
                    if test.returncode == 0 and "test_ok" in test.stdout:
                        active_connections[device_ip] = True
                        return True
                        
            except:
                pass
            
            if attempt < max_retries - 1:
                time.sleep(2)
        
        return False

@lru_cache(maxsize=32)
def get_apk_size(apk_path):
    """Cache APK size calculation"""
    try:
        return Path(apk_path).stat().st_size / (1024 * 1024)
    except:
        return 10

def calculate_install_timeout(apk_size):
    """Calculate ultra-conservative install timeout"""
    
    # Base timeout - minimum 3 minutes
    base_timeout = 180
    
    # Size-based timeout - very generous
    if apk_size <= 10:
        size_timeout = 120  # 2 minutes for small APKs
    elif apk_size <= 50:
        size_timeout = apk_size * 15  # 15 seconds per MB
    elif apk_size <= 100:
        size_timeout = apk_size * 20  # 20 seconds per MB
    else:
        size_timeout = apk_size * 25  # 25 seconds per MB for large APKs
    
    # Network buffer - assume very slow network
    network_buffer = apk_size * 10  # 10 seconds per MB for network transfer
    
    # Device processing buffer
    device_buffer = 120  # Extra 2 minutes for device processing
    
    # Total timeout with all buffers
    total_timeout = base_timeout + size_timeout + network_buffer + device_buffer
    
    return max(300, int(total_timeout))  # Minimum 5 minutes

def check_device_root_status(device_ip, adb_path):
    """Reliable root check with multiple methods"""
    try:
        if not ensure_reliable_connection(device_ip, adb_path):
            return False, "Connection failed"
        
        # Test su 0 format first (your working format)
        result = subprocess.run([
            adb_path, '-s', device_ip, 'shell', 'su 0 echo "ROOT_OK"'
        ], capture_output=True, text=True, timeout=8)
        
        if "ROOT_OK" in result.stdout:
            return True, "Device is rooted (su 0 confirmed)"
        
        # Try su -c format
        result = subprocess.run([
            adb_path, '-s', device_ip, 'shell', 'su -c "echo ROOT_OK"'
        ], capture_output=True, text=True, timeout=8)
        
        if "ROOT_OK" in result.stdout:
            return True, "Device is rooted (su -c confirmed)"
        
        # Check for su binary existence
        which_su = subprocess.run([
            adb_path, '-s', device_ip, 'shell', 'which su'
        ], capture_output=True, text=True, timeout=6)
        
        if which_su.returncode == 0 and which_su.stdout.strip():
            return True, "Device is rooted (su binary found)"
        
        return False, "Device is not rooted"
        
    except subprocess.TimeoutExpired:
        return False, "Connection timeout"
    except Exception as e:
        return False, f"Error: {str(e)[:40]}"

def launch_app_fast(device_ip, package_name, adb_path):
    """Fast app launch with fallbacks"""
    try:
        result = subprocess.run([
            adb_path, '-s', device_ip, 'shell', 'am', 'start', '-n', 
            f"{package_name}/.MainActivity"
        ], capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0 and "Error" not in result.stdout:
            return True, "Launched"
    except:
        pass
    
    try:
        result = subprocess.run([
            adb_path, '-s', device_ip, 'shell', 'am', 'start', '-n', 
            f"{package_name}/.LauncherActivity"
        ], capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0 and "Error" not in result.stdout:
            return True, "Launched via LauncherActivity"
    except:
        pass
    
    try:
        result = subprocess.run([
            adb_path, '-s', device_ip, 'shell', 'monkey', '-p', package_name, 
            '-c', 'android.intent.category.LAUNCHER', '1'
        ], capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0:
            return True, "Launched via monkey"
    except:
        pass
    
    return False, "Launch failed"

def install_on_device_balanced(device_ip, config):
    """Installation with ULTRA-CONSERVATIVE timeout calculation"""
    timestamp = datetime.now().isoformat()
    uninstall_verified = "NO"
    install_verified = "NO"
    launch_status = "NO"
    
    try:
        print(f"Starting installation on {device_ip}")
        
        # Reliable connection with longer timeout
        if not ensure_reliable_connection(device_ip, config['adb_path']):
            raise Exception("Connection failed")
        
        print(f"Connected to {device_ip}")
        
        # Quick uninstall with longer timeout
        if config['old_package']:
            try:
                print(f"Uninstalling {config['old_package']} on {device_ip}")
                subprocess.run([
                    config['adb_path'], '-s', device_ip, 'uninstall', config['old_package']
                ], capture_output=True, timeout=60)  # Increased from 30s
                
                subprocess.run([
                    config['adb_path'], '-s', device_ip, 'shell', 'pm', 'uninstall', 
                    '--user', '0', config['old_package']
                ], capture_output=True, timeout=60)  # Increased from 30s
                
                uninstall_verified = "ATTEMPTED"
            except:
                pass
        
        # ULTRA-CONSERVATIVE TIMEOUT CALCULATION
        apk_size = get_apk_size(config['apk_path'])
        install_timeout = calculate_install_timeout(apk_size)
        
        print(f"APK size: {apk_size:.1f}MB, Install timeout: {install_timeout}s ({install_timeout/60:.1f} minutes) for {device_ip}")
        
        # Install with very long timeout
        result = subprocess.run([
            config['adb_path'], '-s', device_ip, 'install', '-r', '-d', config['apk_path']
        ], capture_output=True, text=True, timeout=install_timeout)
        
        if "Success" in result.stdout:
            install_verified = "YES"
            print(f"Installation successful on {device_ip}")
            
            # Fast app launch
            if config['auto_launch'] and config['launch_package']:
                print(f"Launching app on {device_ip}")
                time.sleep(2)  # Slightly longer wait
                success, launch_msg = launch_app_fast(device_ip, config['launch_package'], config['adb_path'])
                launch_status = "YES" if success else "NO"
                
                if success:
                    print(f"App launched successfully on {device_ip}")
            
            status_msg = "Installed successfully"
            if launch_status == "YES":
                status_msg += " and launched"
            elif config['auto_launch']:
                status_msg += " but launch failed"
            
            return timestamp, device_ip, "SUCCESS", status_msg, uninstall_verified, install_verified, launch_status
        else:
            error_details = f"Install failed: {result.stderr or result.stdout}"
            print(f"Installation failed on {device_ip}: {error_details}")
            raise Exception(error_details)
            
    except subprocess.TimeoutExpired:
        error_msg = f"Install timeout after {install_timeout}s ({install_timeout/60:.1f} minutes)"
        print(f"Timeout error for {device_ip}: {error_msg}")
        return timestamp, device_ip, "FAILED", error_msg, uninstall_verified, install_verified, launch_status
    except Exception as e:
        error_msg = str(e)[:150]
        print(f"Error for {device_ip}: {error_msg}")
        return timestamp, device_ip, "FAILED", error_msg, uninstall_verified, install_verified, launch_status
    finally:
        # Cleanup connection
        try:
            subprocess.run([config['adb_path'], 'disconnect', device_ip], 
                          capture_output=True, timeout=10)
        except:
            pass

def run_installation_balanced(config):
    """Installation runner with reduced parallelism for stability"""
    global installation_progress
    devices = [ip.strip() for ip in config['devices'] if ip.strip()]
    
    installation_progress.update({
        'status': 'running',
        'total_devices': len(devices),
        'completed': 0,
        'success': 0,
        'failed': 0,
        'results': [],
        'log_file': config['log_file']
    })
    
    # Initialize log file
    with open(config['log_file'], 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["Timestamp", "Device", "Status", "Details", "UninstallVerified", "InstallVerified", "LaunchStatus"])
    
    # Reduced parallelism for better stability with longer timeouts
    max_workers = min(config['max_parallel'], len(devices))
    print(f"Starting installation with {max_workers} parallel workers (reduced for stability)")
    
    # Batch logging for speed
    batch_results = []
    batch_size = 3
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_device = {executor.submit(install_on_device_balanced, device, config): device for device in devices}
        
        for future in as_completed(future_to_device):
            try:
                result = future.result()
                installation_progress['results'].append(result)
                installation_progress['completed'] += 1
                
                if result[2] == "SUCCESS":
                    installation_progress['success'] += 1
                else:
                    installation_progress['failed'] += 1
                
                # Batch logging
                batch_results.append(result)
                if len(batch_results) >= batch_size:
                    with open(config['log_file'], 'a', newline='', encoding='utf-8') as f:
                        writer = csv.writer(f)
                        writer.writerows(batch_results)
                    batch_results.clear()
                
                print(f"Completed {installation_progress['completed']}/{installation_progress['total_devices']}: {result[1]} - {result[2]}")
                    
            except Exception as e:
                print(f"Future execution error: {e}")
                installation_progress['completed'] += 1
                installation_progress['failed'] += 1
    
    # Write remaining results
    if batch_results:
        with open(config['log_file'], 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerows(batch_results)
    
    installation_progress['status'] = 'completed'
    print("Installation process completed")
    
    # Cleanup connections
    with connection_lock:
        active_connections.clear()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start_installation', methods=['POST'])
def start_installation():
    global installation_progress
    
    if installation_progress['status'] == 'running':
        return jsonify({'error': 'Installation already in progress'})
    
    data = request.json
    
    if not data.get('devices'):
        return jsonify({'error': 'No devices specified'})
    if not data.get('apk_path') or not os.path.exists(data['apk_path']):
        return jsonify({'error': 'APK file not found'})
    if not data.get('adb_path') or not os.path.exists(data['adb_path']):
        return jsonify({'error': 'ADB executable not found'})
    
    config = {
        'devices': data['devices'].split('\n'),
        'apk_path': data['apk_path'],
        'adb_path': data['adb_path'],
        'old_package': data.get('old_package', ''),
        'launch_package': data.get('launch_package', ''),
        'auto_launch': data.get('auto_launch', False),
        'max_parallel': min(int(data.get('max_parallel', 4)), 6),  # Reduced for stability
        'log_file': f"install_log_{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    }
    
    print(f"Starting installation with config: max_parallel={config['max_parallel']}")
    
    thread = threading.Thread(target=run_installation_balanced, args=(config,))
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

@app.route('/check_root_status', methods=['POST'])
def check_root_status():
    data = request.json
    devices = [ip.strip() for ip in data.get('devices', '').split('\n') if ip.strip()]
    adb_path = data.get('adb_path')
    
    if not devices or not adb_path or not os.path.exists(adb_path):
        return jsonify({'message': 'Missing or invalid ADB path'}), 400
    
    results = []
    rooted_count = 0
    
    # Balanced parallelism for root checking
    with ThreadPoolExecutor(max_workers=min(6, len(devices))) as executor:
        future_to_device = {executor.submit(check_device_root_status, device, adb_path): device for device in devices}
        
        for future in as_completed(future_to_device):
            device = future_to_device[future]
            try:
                is_rooted, message = future.result()
                status_icon = "🔓" if is_rooted else "🔒"
                results.append(f"{status_icon} {device}: {message}")
                if is_rooted:
                    rooted_count += 1
            except Exception as e:
                results.append(f"❌ {device}: Check failed - {str(e)[:40]}")
    
    total_devices = len(devices)
    summary = f"📊 Summary: {rooted_count}/{total_devices} devices are rooted\n\n" + "\n".join(results)
    
    return jsonify({
        'message': summary,
        'rooted_count': rooted_count,
        'total_count': total_devices
    })

@app.route('/set_date', methods=['POST'])
def set_date():
    data = request.json
    devices = [ip.strip() for ip in data.get('devices', '').split('\n') if ip.strip()]
    adb_path = data.get('adb_path')
    dateonly = data.get('date')

    if not devices or not adb_path or not dateonly:
        return jsonify({'message': 'Missing required parameters'}), 400

    try:
        date_obj = datetime.strptime(dateonly, '%Y-%m-%d')
        # Multiple date formats for better compatibility
        date_formats = [
            date_obj.strftime('%Y-%m-%d'),          # 2025-08-01
            date_obj.strftime('%m/%d/%Y'),          # 08/01/2025
            date_obj.strftime('%Y.%m.%d'),          # 2025.08.01
        ]
    except Exception as e:
        return jsonify({'message': f'Invalid date format: {e}'}), 400

    if not os.path.exists(adb_path):
        return jsonify({'message': 'ADB executable not found'}), 400

    def set_date_on_device_reliable(device):
        try:
            # Step 1: Ensure solid connection with retries
            connected = False
            for attempt in range(2):
                try:
                    # Disconnect and reconnect cleanly
                    subprocess.run([adb_path, 'disconnect', device], 
                                  capture_output=True, timeout=5)
                    time.sleep(1)
                    
                    connect_result = subprocess.run([
                        adb_path, 'connect', device
                    ], capture_output=True, text=True, timeout=12)
                    
                    if "connected" in connect_result.stdout or "already connected" in connect_result.stdout:
                        # Test connection
                        time.sleep(1)
                        test = subprocess.run([
                            adb_path, '-s', device, 'shell', 'echo "connection_ok"'
                        ], capture_output=True, text=True, timeout=8)
                        
                        if test.returncode == 0 and "connection_ok" in test.stdout:
                            connected = True
                            break
                except:
                    pass
                
                if attempt < 1:
                    time.sleep(2)
            
            if not connected:
                return f"❌ {device}: Connection failed"
            
            # Step 2: Check root access with multiple methods
            root_available = False
            
            # Try su 0 first (your working format)
            try:
                root_check = subprocess.run([
                    adb_path, '-s', device, 'shell', 'su 0 echo "ROOT_CHECK"'
                ], capture_output=True, text=True, timeout=8)
                
                if root_check.returncode == 0 and "ROOT_CHECK" in root_check.stdout:
                    root_available = True
                    su_format = 'su 0'
            except:
                pass
            
            # Try su -c as fallback
            if not root_available:
                try:
                    root_check = subprocess.run([
                        adb_path, '-s', device, 'shell', 'su -c "echo ROOT_CHECK"'
                    ], capture_output=True, text=True, timeout=8)
                    
                    if root_check.returncode == 0 and "ROOT_CHECK" in root_check.stdout:
                        root_available = True
                        su_format = 'su -c'
                except:
                    pass
            
            if not root_available:
                return f"🔒 {device}: Not rooted or root access denied"
            
            # Step 3: Try multiple date setting methods
            for date_format in date_formats:
                date_commands = []
                
                if su_format == 'su 0':
                    date_commands = [
                        f'su 0 date -s "{date_format}"',
                        f'su 0 date "{date_format}"',
                        f'su 0 toolbox date -s "{date_format}"',
                        f'su 0 busybox date -s "{date_format}"'
                    ]
                else:
                    date_commands = [
                        f'su -c "date -s \\"{date_format}\\""',
                        f'su -c "date \\"{date_format}\\""',
                        f'su -c "toolbox date -s \\"{date_format}\\""',
                        f'su -c "busybox date -s \\"{date_format}\\""'
                    ]
                
                for cmd in date_commands:
                    try:
                        result = subprocess.run([
                            adb_path, '-s', device, 'shell', cmd
                        ], capture_output=True, text=True, timeout=12)
                        
                        if result.returncode == 0 and "invalid" not in result.stderr.lower():
                            # Verify the date was set correctly
                            time.sleep(1)
                            verify = subprocess.run([
                                adb_path, '-s', device, 'shell', 'date'
                            ], capture_output=True, text=True, timeout=8)
                            
                            if verify.returncode == 0:
                                current_date = verify.stdout.strip()
                                # Check if the year matches what we wanted to set
                                if str(date_obj.year) in current_date:
                                    return f"✅ {device}: Date set successfully - {current_date}"
                        
                    except subprocess.TimeoutExpired:
                        continue
                    except:
                        continue
            
            return f"❌ {device}: Date setting failed with all methods"
            
        except Exception as e:
            return f"❌ {device}: Error - {str(e)[:50]}"
        finally:
            # Clean disconnect
            try:
                subprocess.run([adb_path, 'disconnect', device], 
                              capture_output=True, timeout=5)
            except:
                pass

    # Optimal parallelism for balance between speed and reliability
    results = []
    success_count = 0
    
    # Reduced to 5 parallel workers for more stable connections
    with ThreadPoolExecutor(max_workers=min(5, len(devices))) as executor:
        future_results = executor.map(set_date_on_device_reliable, devices)
        
        for result in future_results:
            results.append(result)
            if "✅" in result:
                success_count += 1
    
    summary = f"📊 Date Setting Results: {success_count}/{len(devices)} successful\n\n" + "\n".join(results)
    return jsonify({'message': summary})

@app.route('/test_connections', methods=['POST'])
def test_connections():
    """Reliable connection testing"""
    data = request.json
    devices = [ip.strip() for ip in data.get('devices', '').split('\n') if ip.strip()]
    adb_path = data.get('adb_path')
    
    if not devices or not adb_path or not os.path.exists(adb_path):
        return jsonify({'message': 'Missing or invalid parameters'}), 400
    
    def test_connection_reliable(device):
        try:
            start_time = time.time()
            
            # Clean connection test
            subprocess.run([adb_path, 'disconnect', device], 
                          capture_output=True, timeout=5)
            time.sleep(0.5)
            
            result = subprocess.run([
                adb_path, 'connect', device
            ], capture_output=True, text=True, timeout=10)
            
            if "connected" in result.stdout or "already connected" in result.stdout:
                time.sleep(1)
                test = subprocess.run([
                    adb_path, '-s', device, 'shell', 'echo "connection_test"'
                ], capture_output=True, text=True, timeout=8)
                
                response_time = int((time.time() - start_time) * 1000)
                
                if test.returncode == 0 and "connection_test" in test.stdout:
                    return f"✅ {device}: Connected ({response_time}ms)"
                else:
                    return f"🔄 {device}: Connected but shell test failed ({response_time}ms)"
            else:
                return f"❌ {device}: Connection failed - {result.stdout.strip()}"
                
        except subprocess.TimeoutExpired:
            return f"⏱️ {device}: Timeout"
        except Exception as e:
            return f"❌ {device}: Error - {str(e)[:30]}"
    
    results = []
    connected_count = 0
    
    # Balanced parallel testing
    with ThreadPoolExecutor(max_workers=min(8, len(devices))) as executor:
        future_results = executor.map(test_connection_reliable, devices)
        
        for result in future_results:
            results.append(result)
            if "✅" in result or "🔄" in result:
                connected_count += 1
    
    total_devices = len(devices)
    summary = f"📊 Connection Test: {connected_count}/{total_devices} connected\n\n" + "\n".join(results)
    
    return jsonify({
        'message': summary,
        'connected_count': connected_count,
        'total_count': total_devices
    })

@app.route('/device_info', methods=['POST'])
def get_device_info():
    """Get device information reliably"""
    data = request.json
    devices = [ip.strip() for ip in data.get('devices', '').split('\n') if ip.strip()]
    adb_path = data.get('adb_path')
    
    if not devices or not adb_path or not os.path.exists(adb_path):
        return jsonify({'message': 'Missing or invalid parameters'}), 400
    
    def get_single_device_info(device):
        try:
            if not ensure_reliable_connection(device, adb_path):
                return f"❌ {device}: Connection failed"
            
            # Get device model and Android version
            model_result = subprocess.run([
                adb_path, '-s', device, 'shell', 'getprop ro.product.model'
            ], capture_output=True, text=True, timeout=8)
            
            version_result = subprocess.run([
                adb_path, '-s', device, 'shell', 'getprop ro.build.version.release'
            ], capture_output=True, text=True, timeout=8)
            
            model = model_result.stdout.strip() if model_result.returncode == 0 else "Unknown"
            version = version_result.stdout.strip() if version_result.returncode == 0 else "Unknown"
            
            return f"📱 {device}: {model} (Android {version})"
            
        except Exception as e:
            return f"❌ {device}: Error - {str(e)[:30]}"
    
    # Get device info with balanced parallelism
    with ThreadPoolExecutor(max_workers=min(5, len(devices))) as executor:
        results = list(executor.map(get_single_device_info, devices))
    
    summary = "📱 Device Information:\n\n" + "\n".join(results)
    return jsonify({'message': summary})

if __name__ == '__main__':
    print("Starting APK Installer Server...")
    print("Ultra-Conservative Timeouts Mode: Long timeouts for maximum reliability")
    print("Server will be available at: http://localhost:5000")
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)
