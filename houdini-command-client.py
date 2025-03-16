#!/usr/bin/env python3
"""
Houdini MCP Command Client

A utility for manually sending commands to the Houdini MCP server for testing.
This allows you to verify functionality without relying on Claude.

Usage:
  python houdini_command_client.py [--host HOST] [--port PORT]

Examples:
  python houdini_command_client.py
  python houdini_command_client.py --port 9877
"""

import socket
import json
import argparse
import sys
import time
# readline import removed for Windows compatibility

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Houdini MCP Command Client")
    parser.add_argument("--host", default="localhost", help="Host where Houdini is running (default: localhost)")
    parser.add_argument("--port", type=int, default=9876, help="Port number (default: 9876)")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode with verbose logging")
    return parser.parse_args()

def receive_full_response(sock, buffer_size=16384, timeout=30.0, debug=False):
    """Receive the complete response, expecting a newline-delimited JSON message."""
    buffer = b''
    sock.settimeout(timeout)
    start_time = time.time()
    
    try:
        while True:
            # Check if we've exceeded timeout
            if time.time() - start_time > timeout:
                raise socket.timeout("Timeout waiting for complete response")
                
            try:
                chunk = sock.recv(buffer_size)
                if debug:
                    print(f"Received chunk of {len(chunk)} bytes")
                
                if not chunk:
                    if buffer:
                        if debug:
                            print("Connection closed with data in buffer")
                        break
                    else:
                        raise Exception("Connection closed before receiving any data")
                
                buffer += chunk
                
                # Check for newline delimiter
                if b'\n' in buffer:
                    if debug:
                        print(f"Found newline delimiter after {len(buffer)} bytes")
                    message, _, remaining = buffer.partition(b'\n')
                    if remaining and debug:
                        print(f"Warning: {len(remaining)} bytes of data after delimiter")
                    return message
                
                # Alternatively, try to see if we have a complete JSON object
                try:
                    json.loads(buffer.decode('utf-8'))
                    if debug:
                        print(f"Successfully parsed complete JSON ({len(buffer)} bytes)")
                    return buffer
                except json.JSONDecodeError:
                    # Incomplete JSON, continue receiving
                    if debug:
                        print("Incomplete JSON, continuing to receive")
                    continue
                    
            except socket.timeout:
                if debug:
                    print("Socket timeout during receive")
                if buffer:
                    print(f"Timeout but returning partial data ({len(buffer)} bytes)")
                    return buffer
                raise
                
            except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                print(f"Socket error during receive: {str(e)}")
                if buffer:
                    return buffer
                raise
                
    except socket.timeout:
        print(f"Timeout after {timeout} seconds")
        if buffer:
            print(f"Returning partial data ({len(buffer)} bytes)")
            return buffer
        raise
        
    except Exception as e:
        print(f"Error during receive: {str(e)}")
        if buffer:
            return buffer
        raise
    
    return buffer

def send_command(sock, command_type, params=None, debug=False):
    """Send a command to Houdini and return the response."""
    command = {
        "type": command_type,
        "params": params or {}
    }
    
    print(f"Sending command: {json.dumps(command, indent=2)}")
    
    try:
        # Add newline delimiter to ensure proper message framing
        command_json = json.dumps(command) + "\n"
        command_bytes = command_json.encode('utf-8')
        
        # Send command
        total_sent = 0
        while total_sent < len(command_bytes):
            sent = sock.send(command_bytes[total_sent:])
            if sent == 0:
                raise ConnectionError("Socket connection broken")
            total_sent += sent
        
        print(f"Command sent ({total_sent} bytes), waiting for response...")
        
        # Receive response
        response_data = receive_full_response(sock, debug=debug)
        print(f"Received {len(response_data)} bytes of data")
        
        try:
            response = json.loads(response_data.decode('utf-8'))
            
            if response.get("status") == "error":
                print(f"Error: {response.get('message')}")
                return None
            
            return response.get("result", {})
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON response: {str(e)}")
            print(f"Raw response: {response_data.decode('utf-8', errors='replace')}")
            return None
            
    except Exception as e:
        print(f"Error: {str(e)}")
        return None

def connect_to_houdini(host, port, debug=False):
    """Connect to the Houdini socket server."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        print(f"Connecting to Houdini at {host}:{port}...")
        sock.connect((host, port))
        print("Connected successfully!")
        return sock
    except Exception as e:
        print(f"Failed to connect: {str(e)}")
        return None

def print_help():
    """Print help information for the command client."""
    print("\nAvailable Commands:")
    print("  get_scene_info                   - Get information about the current scene")
    print("  get_object_info PATH [NAME]      - Get detailed information for a specific object")
    print("  create_object TYPE [NAME]        - Create a new object")
    print("  modify_object PATH|NAME [PARAMS] - Modify an existing object")
    print("  delete_object PATH|NAME          - Delete an object")
    print("  set_material OBJ MAT [COLOR]     - Apply a material to an object")
    print("  execute_code \"CODE\"            - Execute Python code in Houdini")
    print("  render_scene [PATH] [RES_X] [RES_Y] - Render the current scene")
    print("\nUtility Commands:")
    print("  help                             - Show this help message")
    print("  quit                             - Exit the command client")
    print("\nExample:")
    print('  create_object geo sphere1 {"primitive_type": "sphere"}')
    print('  get_object_info /obj/sphere1')
    print("")

def parse_command(cmd_line):
    """Parse a command line string into command type and parameters."""
    parts = cmd_line.strip().split(maxsplit=1)
    if not parts:
        return None, None
    
    cmd_type = parts[0]
    
    # Parse parameters based on the command type
    params = {}
    
    if len(parts) > 1:
        args = parts[1]
        
        # Handle special cases
        if cmd_type == "get_object_info":
            arg_parts = args.split(maxsplit=1)
            if arg_parts[0].startswith("/"):
                params["path"] = arg_parts[0]
            else:
                params["name"] = arg_parts[0]
        
        elif cmd_type == "create_object":
            arg_parts = args.split(maxsplit=2)
            params["type"] = arg_parts[0]
            
            if len(arg_parts) > 1:
                params["name"] = arg_parts[1]
                
                # Check for additional JSON parameters
                if len(arg_parts) > 2:
                    try:
                        additional_params = json.loads(arg_parts[2])
                        params.update(additional_params)
                    except json.JSONDecodeError:
                        print("Error: Invalid JSON parameter format")
                        return None, None
        
        elif cmd_type == "modify_object":
            arg_parts = args.split(maxsplit=1)
            
            # Determine if the first argument is a path or name
            if arg_parts[0].startswith("/"):
                params["path"] = arg_parts[0]
            else:
                params["name"] = arg_parts[0]
                
            # Parse additional parameters if provided as JSON
            if len(arg_parts) > 1:
                try:
                    additional_params = json.loads(arg_parts[1])
                    params.update(additional_params)
                except json.JSONDecodeError:
                    print("Error: Invalid JSON parameter format")
                    return None, None
        
        elif cmd_type == "delete_object":
            if args.startswith("/"):
                params["path"] = args
            else:
                params["name"] = args
        
        elif cmd_type == "set_material":
            arg_parts = args.split(maxsplit=2)
            
            # First arg could be path or name
            if arg_parts[0].startswith("/"):
                params["object_path"] = arg_parts[0]
            else:
                params["object_name"] = arg_parts[0]
                
            if len(arg_parts) > 1:
                params["material_name"] = arg_parts[1]
                
                # Check for color parameter
                if len(arg_parts) > 2:
                    try:
                        params["color"] = json.loads(arg_parts[2])
                    except json.JSONDecodeError:
                        print("Error: Invalid color format. Use JSON array: [r, g, b]")
                        return None, None
        
        elif cmd_type == "execute_code":
            params["code"] = args
        
        elif cmd_type == "render_scene":
            arg_parts = args.split()
            
            if len(arg_parts) > 0:
                params["output_path"] = arg_parts[0]
            
            if len(arg_parts) > 1:
                try:
                    params["resolution_x"] = int(arg_parts[1])
                except ValueError:
                    print("Error: Resolution must be an integer")
                    return None, None
            
            if len(arg_parts) > 2:
                try:
                    params["resolution_y"] = int(arg_parts[2])
                except ValueError:
                    print("Error: Resolution must be an integer")
                    return None, None
    
    return cmd_type, params

def interactive_mode(sock, debug=False):
    """Run the command client in interactive mode."""
    print("\nHoudini MCP Command Client")
    print("Type 'help' for a list of commands, 'quit' to exit\n")
    
    while True:
        try:
            cmd_line = input("> ").strip()
            
            if not cmd_line:
                continue
            
            if cmd_line.lower() in ["quit", "exit", "q"]:
                break
            
            if cmd_line.lower() in ["help", "h", "?"]:
                print_help()
                continue
            
            cmd_type, params = parse_command(cmd_line)
            if not cmd_type:
                continue
            
            if cmd_type not in ["get_scene_info", "get_object_info", "create_object", 
                                "modify_object", "delete_object", "set_material", 
                                "execute_code", "render_scene"]:
                print(f"Unknown command: {cmd_type}")
                continue
            
            start_time = time.time()
            result = send_command(sock, cmd_type, params, debug=debug)
            end_time = time.time()
            
            if result is not None:
                print("\nResult:")
                print(json.dumps(result, indent=2))
                print(f"\nCommand completed in {end_time - start_time:.2f} seconds")
        
        except KeyboardInterrupt:
            print("\nOperation cancelled")
        except Exception as e:
            print(f"Error: {str(e)}")
            if debug:
                import traceback
                traceback.print_exc()
    
    print("Exiting command client")

def main():
    """Main entry point for the command client."""
    args = parse_arguments()
    
    sock = connect_to_houdini(args.host, args.port, args.debug)
    if sock:
        try:
            interactive_mode(sock, args.debug)
        finally:
            sock.close()
    else:
        print("Failed to connect to Houdini. Make sure the Houdini MCP server is running.")
        sys.exit(1)

if __name__ == "__main__":
    main()