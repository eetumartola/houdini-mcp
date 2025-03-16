#!/usr/bin/env python3
"""
Houdini Socket Communication Diagnostic Tool

This tool tests different communication approaches with the Houdini server
to diagnose issues with socket communication.

Usage:
  python houdini_diagnostic.py [--host HOST] [--port PORT] [--command COMMAND] [--timeout TIMEOUT]

Examples:
  python houdini_diagnostic.py --command "create_object"
  python houdini_diagnostic.py --command "create_object" --timeout 5
"""

import socket
import json
import argparse
import sys
import time
import threading
import traceback

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Houdini Socket Communication Diagnostic Tool")
    parser.add_argument("--host", default="localhost", help="Host where Houdini is running (default: localhost)")
    parser.add_argument("--port", type=int, default=9876, help="Port number (default: 9876)")
    parser.add_argument("--command", default="get_scene_info", help="Command to test (default: get_scene_info)")
    parser.add_argument("--timeout", type=float, default=10.0, help="Timeout in seconds (default: 10.0)")
    parser.add_argument("--method", choices=["basic", "newline", "threaded", "nonblocking"], 
                         default="newline", help="Communication method to test")
    return parser.parse_args()

def test_basic_communication(host, port, command, timeout):
    """Test basic socket communication without any special handling."""
    print(f"\n=== Testing Basic Communication ===")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    try:
        print(f"Connecting to {host}:{port}...")
        sock.connect((host, port))
        print("Connected successfully")
        
        # Prepare command
        cmd_obj = {
            "type": command,
            "params": {
                "type": "light" if command == "create_object" else None,
                "name": f"test_light_{int(time.time())}" if command == "create_object" else None
            }
        }
        cmd_json = json.dumps(cmd_obj)
        
        # Set timeout
        sock.settimeout(timeout)
        
        # Send command
        print(f"Sending command: {cmd_json}")
        sock.sendall(cmd_json.encode('utf-8'))
        print("Command sent, waiting for response...")
        
        # Receive response
        start_time = time.time()
        response_data = b''
        
        try:
            while True:
                if time.time() - start_time > timeout:
                    print(f"Timeout after {timeout} seconds")
                    break
                    
                chunk = sock.recv(8192)
                if not chunk:
                    print("Connection closed by server")
                    break
                    
                response_data += chunk
                print(f"Received {len(chunk)} bytes (total: {len(response_data)} bytes)")
                
                # Try to parse as JSON to see if complete
                try:
                    json.loads(response_data.decode('utf-8'))
                    print("Received complete JSON response")
                    break
                except json.JSONDecodeError:
                    print("JSON incomplete, continuing to receive...")
                    continue
        
        except socket.timeout:
            print(f"Socket timeout after {timeout} seconds")
        
        # Process response
        if response_data:
            try:
                response = json.loads(response_data.decode('utf-8'))
                print("\nResponse received:")
                print(json.dumps(response, indent=2))
            except json.JSONDecodeError as e:
                print(f"Failed to parse response as JSON: {e}")
                print(f"Raw response: {response_data.decode('utf-8', errors='replace')}")
        else:
            print("No response data received")
            
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
    finally:
        sock.close()
        print("Connection closed")

def test_newline_communication(host, port, command, timeout):
    """Test communication with newline-delimited messages."""
    print(f"\n=== Testing Newline-Delimited Communication ===")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    try:
        print(f"Connecting to {host}:{port}...")
        sock.connect((host, port))
        print("Connected successfully")
        
        # Prepare command
        cmd_obj = {
            "type": command,
            "params": {
                "type": "light" if command == "create_object" else None,
                "name": f"test_light_{int(time.time())}" if command == "create_object" else None
            }
        }
        cmd_json = json.dumps(cmd_obj) + "\n"  # Add newline delimiter
        
        # Set timeout
        sock.settimeout(timeout)
        
        # Send command
        print(f"Sending command with newline: {cmd_json.strip()}")
        sock.sendall(cmd_json.encode('utf-8'))
        print("Command sent, waiting for response...")
        
        # Receive response
        start_time = time.time()
        response_data = b''
        
        try:
            while True:
                if time.time() - start_time > timeout:
                    print(f"Timeout after {timeout} seconds")
                    break
                    
                chunk = sock.recv(8192)
                if not chunk:
                    print("Connection closed by server")
                    break
                    
                response_data += chunk
                print(f"Received {len(chunk)} bytes (total: {len(response_data)} bytes)")
                
                # Check for newline delimiter
                if b'\n' in response_data:
                    print("Found newline delimiter")
                    message, _, remaining = response_data.partition(b'\n')
                    if remaining:
                        print(f"Warning: {len(remaining)} bytes after newline")
                    response_data = message
                    break
                
                # Also try to parse as JSON to see if complete
                try:
                    json.loads(response_data.decode('utf-8'))
                    print("Received complete JSON response (no newline)")
                    break
                except json.JSONDecodeError:
                    print("JSON incomplete, continuing to receive...")
                    continue
        
        except socket.timeout:
            print(f"Socket timeout after {timeout} seconds")
        
        # Process response
        if response_data:
            try:
                response = json.loads(response_data.decode('utf-8'))
                print("\nResponse received:")
                print(json.dumps(response, indent=2))
            except json.JSONDecodeError as e:
                print(f"Failed to parse response as JSON: {e}")
                print(f"Raw response: {response_data.decode('utf-8', errors='replace')}")
        else:
            print("No response data received")
            
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
    finally:
        sock.close()
        print("Connection closed")

def receive_in_thread(sock, timeout):
    """Receive data in a separate thread."""
    print("Receiver thread started")
    response_data = b''
    
    try:
        sock.settimeout(timeout)
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                chunk = sock.recv(8192)
                if not chunk:
                    print("Receiver: Connection closed by server")
                    break
                    
                response_data += chunk
                print(f"Receiver: Got {len(chunk)} bytes (total: {len(response_data)} bytes)")
                
                # Try to parse as JSON
                try:
                    json.loads(response_data.decode('utf-8'))
                    print("Receiver: Complete JSON received")
                    break
                except json.JSONDecodeError:
                    continue
                    
            except socket.timeout:
                print("Receiver: Socket timeout")
                break
                
    except Exception as e:
        print(f"Receiver error: {e}")
    
    if response_data:
        try:
            response = json.loads(response_data.decode('utf-8'))
            print("\nResponse received in thread:")
            print(json.dumps(response, indent=2))
        except json.JSONDecodeError as e:
            print(f"Failed to parse response as JSON: {e}")
            print(f"Raw response: {response_data.decode('utf-8', errors='replace')}")
    else:
        print("No response data received in thread")
    
    print("Receiver thread ended")

def test_threaded_communication(host, port, command, timeout):
    """Test communication with separate send and receive threads."""
    print(f"\n=== Testing Threaded Communication ===")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    try:
        print(f"Connecting to {host}:{port}...")
        sock.connect((host, port))
        print("Connected successfully")
        
        # Prepare command
        cmd_obj = {
            "type": command,
            "params": {
                "type": "light" if command == "create_object" else None,
                "name": f"test_light_{int(time.time())}" if command == "create_object" else None
            }
        }
        cmd_json = json.dumps(cmd_obj) + "\n"  # Add newline delimiter
        
        # Start receiver thread before sending
        receiver = threading.Thread(target=receive_in_thread, args=(sock, timeout))
        receiver.daemon = True
        receiver.start()
        
        # Send command in main thread
        print(f"Sending command: {cmd_json.strip()}")
        sock.sendall(cmd_json.encode('utf-8'))
        print("Command sent, receiver thread is waiting for response...")
        
        # Wait for receiver to finish
        receiver.join(timeout + 1)
        print("Main thread continuing after receiver")
            
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
    finally:
        sock.close()
        print("Connection closed")

def test_nonblocking_communication(host, port, command, timeout):
    """Test communication with non-blocking sockets."""
    print(f"\n=== Testing Non-Blocking Communication ===")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    try:
        print(f"Connecting to {host}:{port}...")
        sock.connect((host, port))
        print("Connected successfully")
        
        # Set non-blocking mode
        sock.setblocking(False)
        
        # Prepare command
        cmd_obj = {
            "type": command,
            "params": {
                "type": "light" if command == "create_object" else None,
                "name": f"test_light_{int(time.time())}" if command == "create_object" else None
            }
        }
        cmd_json = json.dumps(cmd_obj) + "\n"  # Add newline delimiter
        cmd_bytes = cmd_json.encode('utf-8')
        
        # Send command
        print(f"Sending command: {cmd_json.strip()}")
        bytes_sent = 0
        while bytes_sent < len(cmd_bytes):
            try:
                sent = sock.send(cmd_bytes[bytes_sent:])
                if sent == 0:
                    print("Socket connection broken during send")
                    break
                bytes_sent += sent
                print(f"Sent {sent} bytes (total: {bytes_sent}/{len(cmd_bytes)})")
            except BlockingIOError:
                print("BlockingIOError during send, retrying...")
                time.sleep(0.1)
            except Exception as e:
                print(f"Error during send: {e}")
                break
        
        print("Command sent, waiting for response...")
        
        # Receive response
        import select
        response_data = b''
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                # Use select to check if socket is readable
                ready, _, _ = select.select([sock], [], [], 0.5)
                
                if ready:
                    try:
                        chunk = sock.recv(8192)
                        if not chunk:
                            print("Connection closed by server")
                            break
                            
                        response_data += chunk
                        print(f"Received {len(chunk)} bytes (total: {len(response_data)} bytes)")
                        
                        # Check for newline delimiter
                        if b'\n' in response_data:
                            print("Found newline delimiter")
                            message, _, remaining = response_data.partition(b'\n')
                            if remaining:
                                print(f"Warning: {len(remaining)} bytes after newline")
                            response_data = message
                            break
                        
                        # Try to parse as JSON
                        try:
                            json.loads(response_data.decode('utf-8'))
                            print("Received complete JSON response (no newline)")
                            break
                        except json.JSONDecodeError:
                            print("JSON incomplete, continuing...")
                    
                    except BlockingIOError:
                        print("BlockingIOError during receive, retrying...")
                else:
                    print("Socket not ready for reading")
                    
            except Exception as e:
                print(f"Error during receive: {e}")
                break
                
            time.sleep(0.1)
            
        # Process response
        if response_data:
            try:
                response = json.loads(response_data.decode('utf-8'))
                print("\nResponse received:")
                print(json.dumps(response, indent=2))
            except json.JSONDecodeError as e:
                print(f"Failed to parse response as JSON: {e}")
                print(f"Raw response: {response_data.decode('utf-8', errors='replace')}")
        else:
            print("No response data received")
            
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
    finally:
        sock.close()
        print("Connection closed")

def main():
    """Main entry point for the diagnostic tool."""
    args = parse_arguments()
    
    print(f"Houdini Socket Communication Diagnostic Tool")
    print(f"Testing {args.method} communication with {args.host}:{args.port}")
    print(f"Command: {args.command}, Timeout: {args.timeout} seconds")
    
    if args.method == "basic":
        test_basic_communication(args.host, args.port, args.command, args.timeout)
    elif args.method == "newline":
        test_newline_communication(args.host, args.port, args.command, args.timeout)
    elif args.method == "threaded":
        test_threaded_communication(args.host, args.port, args.command, args.timeout)
    elif args.method == "nonblocking":
        test_nonblocking_communication(args.host, args.port, args.command, args.timeout)

if __name__ == "__main__":
    main()