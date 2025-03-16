#!/usr/bin/env python3
# houdini_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context, Image
import socket
import json
import asyncio
import select
import logging
import queue
import time
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List

# Configure logging
logging.basicConfig(level=logging.DEBUG, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("HoudiniMCPServer")



@dataclass
class HoudiniConnection:
    host: str
    port: int
    sock: socket.socket = None
    _last_command_executed: bool = False
    
    def connect(self) -> bool:
        """Connect to the Houdini socket server"""
        # Always close any existing connection first
        self.disconnect()
            
        try:
            logger.info(f"Creating new socket connection to {self.host}:{self.port}")
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(30.0)  # Set default timeout
            logger.info(f"Connected successfully to Houdini at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Houdini: {str(e)}")
            if self.sock:
                try:
                    self.sock.close()
                except:
                    pass
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Houdini server"""
        if self.sock:
            logger.info("Closing socket connection to Houdini")
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error closing socket: {str(e)}")
            finally:
                self.sock = None
                self._last_command_executed = False
    
    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Houdini and return the response"""
        # Try to connect if we don't have a valid connection
        if not self.sock:
            if not self.connect():
                raise ConnectionError("Could not connect to Houdini")
        
        command = {
            "type": command_type,
            "params": params or {}
        }
        
        try:
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Add newline delimiter for message framing
            command_json = json.dumps(command) + "\n"
            self.sock.sendall(command_json.encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Use a reasonable timeout
            self.sock.settimeout(30.0)
            
            # Receive data until we find a newline or complete JSON
            buffer = b''
            start_time = time.time()
            max_time = 30.0
            
            while time.time() - start_time < max_time:
                try:
                    chunk = self.sock.recv(16384)
                    if not chunk:
                        if buffer:
                            logger.warning("Connection closed with partial data")
                            break
                        logger.warning("Connection closed by Houdini before receiving any data")
                        # Try to reconnect
                        self.sock = None
                        if self.connect():
                            logger.info("Reconnected to Houdini - retrying command")
                            return self.send_command(command_type, params)
                        raise Exception("Connection closed by Houdini and reconnect failed")
                    
                    buffer += chunk
                    logger.info(f"Received {len(chunk)} bytes, buffer now {len(buffer)} bytes")
                    
                    # Check for newline delimiter
                    if b'\n' in buffer:
                        message, _, _ = buffer.partition(b'\n')
                        logger.info("Found newline delimiter in response")
                        buffer = message  # Use only the part before newline
                        break
                    
                    # Fallback: try to parse as JSON
                    try:
                        json.loads(buffer.decode('utf-8'))
                        logger.info("Complete JSON response received (no newline)")
                        break
                    except json.JSONDecodeError:
                        # Not complete yet, continue
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during receive, but command may have succeeded")
                    # Don't close the socket, just return a result indicating timeout
                    result = {
                        "timeout": True,
                        "message": f"The {command_type} command may have been executed successfully despite timeout"
                    }
                    
                    # Include basic info for object creation
                    if command_type == "create_object":
                        obj_type = params.get("type", "unknown")
                        obj_name = params.get("name", f"{obj_type}_{int(time.time())}")
                        result["type"] = obj_type
                        result["name"] = obj_name
                    
                    return result
                except Exception as e:
                    logger.error(f"Error during receive: {str(e)}")
                    break
            
            # Process the response if we got any data
            if buffer:
                try:
                    response = json.loads(buffer.decode('utf-8'))
                    logger.info("Successfully parsed response JSON")
                    
                    if response.get("status") == "error":
                        logger.error(f"Houdini error: {response.get('message')}")
                        raise Exception(response.get("message", "Unknown error from Houdini"))
                    
                    return response.get("result", {})
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON in response: {str(e)}")
                    logger.error(f"Raw data: {buffer.decode('utf-8', errors='replace')}")
                    raise Exception(f"Invalid response from Houdini: {str(e)}")
            
            # If we get here with no data, we timed out
            logger.error(f"Timeout after {max_time} seconds")
            raise Exception(f"Timeout waiting for Houdini response")
            
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None  # Mark as disconnected
            raise Exception(f"Connection to Houdini lost: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Houdini: {str(e)}")
            # Don't close the socket for all errors
            if isinstance(e, socket.timeout) or isinstance(e, ConnectionError):
                self.sock = None
            raise



@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("HoudiniMCP server starting up")
        
        try:
            houdini = get_houdini_connection()
            logger.info("Successfully connected to Houdini on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Houdini on startup: {str(e)}")
            logger.warning("Make sure the Houdini extension is running before using Houdini resources or tools")
        
        yield {}
    finally:
        global _houdini_connection
        if _houdini_connection:
            logger.info("Disconnecting from Houdini on shutdown")
            _houdini_connection.disconnect()
            _houdini_connection = None
        logger.info("HoudiniMCP server shut down")

# Create the MCP server with lifespan support
mcp = FastMCP(
    "HoudiniMCP",
    description="Houdini integration through the Model Context Protocol",
    lifespan=server_lifespan
)

# Global connection for resources
_houdini_connection = None

def get_houdini_connection():
    """Get or create a persistent Houdini connection"""
    global _houdini_connection
    
    # If we have an existing connection, check if it's still valid
    if _houdini_connection is not None:
        try:
            # Simple check if socket is closed
            if _houdini_connection.sock is None:
                logger.warning("Socket is None, reconnecting...")
                _houdini_connection = None
            # Do NOT close the socket here!
        except Exception as e:
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            _houdini_connection = None
    
    # Create a new connection if needed
    if _houdini_connection is None:
        _houdini_connection = HoudiniConnection(host="localhost", port=9876)
        if not _houdini_connection.connect():
            logger.error("Failed to connect to Houdini")
            _houdini_connection = None
            raise Exception("Could not connect to Houdini. Make sure the Houdini extension is running.")
        logger.info("Created new persistent connection to Houdini")
    
    return _houdini_connection

@mcp.tool()
def get_scene_info(ctx: Context) -> str:
    """Get detailed information about the current Houdini scene"""
    try:
        houdini = get_houdini_connection()
        result = houdini.send_command("get_scene_info")
        
        # Return a formatted JSON representation
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting scene info from Houdini: {str(e)}")
        return f"Error getting scene info: {str(e)}"

@mcp.tool()
def get_object_info(ctx: Context, object_path: str = None, object_name: str = None) -> str:
    """
    Get detailed information about a specific object in the Houdini scene.
    
    Parameters:
    - object_path: The full path of the object (e.g., /obj/geo1)
    - object_name: The name of the object if path is not provided
    """
    try:
        if not object_path and not object_name:
            return "Error: Either object_path or object_name must be provided"
            
        houdini = get_houdini_connection()
        params = {}
        if object_path:
            params["path"] = object_path
        if object_name:
            params["name"] = object_name
            
        result = houdini.send_command("get_object_info", params)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting object info from Houdini: {str(e)}")
        return f"Error getting object info: {str(e)}"

@mcp.tool()
def create_object(
    ctx: Context,
    type: str = "geo",
    name: str = None,
    position: List[float] = None,
    primitive_type: str = "box"
) -> str:
    """
    Create a new object in the Houdini scene.
    
    Parameters:
    - type: Node type to create (e.g., geo, cam, light)
    - name: Optional name for the node
    - position: Optional [x, y, z] position coordinates
    - primitive_type: For geo nodes, the type of primitive to create (box, sphere, grid, etc.)
    """
    try:
        houdini = get_houdini_connection()
        
        params = {
            "type": type,
            "primitive_type": primitive_type
        }
        
        if name:
            params["name"] = name
        if position:
            params["position"] = position
            
        result = houdini.send_command("create_object", params)
        
        return f"Created {type} object: {result.get('name')} at path: {result.get('path')}"
    except Exception as e:
        logger.error(f"Error creating object: {str(e)}")
        return f"Error creating object: {str(e)}"

@mcp.tool()
def modify_object(
    ctx: Context,
    path: str = None,
    name: str = None,
    translate: List[float] = None,
    rotate: List[float] = None,
    scale: List[float] = None,
    visible: bool = None
) -> str:
    """
    Modify an existing object in the Houdini scene.
    
    Parameters:
    - path: Full path of the object to modify
    - name: Name of the object if path not provided
    - translate: Optional [x, y, z] translation values
    - rotate: Optional [x, y, z] rotation values in degrees
    - scale: Optional [x, y, z] scale factors
    - visible: Optional boolean to set visibility
    """
    try:
        if not path and not name:
            return "Error: Either path or name must be provided"
            
        houdini = get_houdini_connection()
        
        params = {}
        if path:
            params["path"] = path
        if name:
            params["name"] = name
        if translate:
            params["translate"] = translate
        if rotate:
            params["rotate"] = rotate
        if scale:
            params["scale"] = scale
        if visible is not None:
            params["visible"] = visible
            
        result = houdini.send_command("modify_object", params)
        
        return f"Modified object: {result.get('name')} at path: {result.get('path')}"
    except Exception as e:
        logger.error(f"Error modifying object: {str(e)}")
        return f"Error modifying object: {str(e)}"

@mcp.tool()
def delete_object(ctx: Context, path: str = None, name: str = None) -> str:
    """
    Delete an object from the Houdini scene.
    
    Parameters:
    - path: Full path of the object to delete
    - name: Name of the object if path not provided
    """
    try:
        if not path and not name:
            return "Error: Either path or name must be provided"
            
        houdini = get_houdini_connection()
        
        params = {}
        if path:
            params["path"] = path
        if name:
            params["name"] = name
            
        result = houdini.send_command("delete_object", params)
        
        return f"Deleted object: {result.get('name')} at path: {result.get('path')}"
    except Exception as e:
        logger.error(f"Error deleting object: {str(e)}")
        return f"Error deleting object: {str(e)}"

@mcp.tool()
def set_material(
    ctx: Context,
    object_path: str = None,
    object_name: str = None,
    material_name: str = None,
    color: List[float] = None
) -> str:
    """
    Set or create a material for an object.
    
    Parameters:
    - object_path: Full path of the object
    - object_name: Name of the object if path not provided
    - material_name: Optional name of the material to use or create
    - color: Optional [R, G, B] color values (0.0-1.0)
    """
    try:
        if not object_path and not object_name:
            return "Error: Either object_path or object_name must be provided"
            
        houdini = get_houdini_connection()
        
        params = {}
        if object_path:
            params["object_path"] = object_path
        if object_name:
            params["object_name"] = object_name
        if material_name:
            params["material_name"] = material_name
        if color:
            params["color"] = color
            
        result = houdini.send_command("set_material", params)
        
        return f"Applied material '{result.get('material')}' to object: {result.get('object')}"
    except Exception as e:
        logger.error(f"Error setting material: {str(e)}")
        return f"Error setting material: {str(e)}"

@mcp.tool()
def execute_houdini_code(ctx: Context, code: str) -> str:
    """
    Execute arbitrary Python code in Houdini.
    
    Parameters:
    - code: The Python code to execute
    """
    try:
        houdini = get_houdini_connection()
        
        result = houdini.send_command("execute_code", {"code": code})
        
        return f"Code executed successfully in Houdini"
    except Exception as e:
        logger.error(f"Error executing code: {str(e)}")
        return f"Error executing code: {str(e)}"

@mcp.tool()
def render_scene(
    ctx: Context,
    output_path: str = None,
    resolution_x: int = None,
    resolution_y: int = None
) -> str:
    """
    Render the current Houdini scene.
    
    Parameters:
    - output_path: Optional path to save the rendered image
    - resolution_x: Optional horizontal resolution
    - resolution_y: Optional vertical resolution
    """
    try:
        houdini = get_houdini_connection()
        
        params = {}
        if output_path:
            params["output_path"] = output_path
        if resolution_x:
            params["resolution_x"] = resolution_x
        if resolution_y:
            params["resolution_y"] = resolution_y
            
        result = houdini.send_command("render_scene", params)
        
        return f"Scene rendered successfully. Output: {result.get('output_path')}, Resolution: {result.get('resolution')}"
    except Exception as e:
        logger.error(f"Error rendering scene: {str(e)}")
        return f"Error rendering scene: {str(e)}"

@mcp.prompt()
def asset_creation_strategy() -> str:
    """Defines the preferred strategy for creating assets in Houdini"""
    return """When creating 3D content in Houdini, follow these guidelines:

    1. Always start by checking the current scene using get_scene_info() to understand the existing context.

    2. For creating new objects:
       - Use create_object() with appropriate parameters
       - For geometry nodes, specify the primitive_type parameter
       - Common primitive types: box, sphere, grid, torus, tube
       
    3. For modifying existing objects:
       - Use modify_object() with the path or name of the object
       - Set translation, rotation, or scale as needed
       
    4. For adding materials:
       - Use set_material() to apply colors or materials
       - Remember that Houdini uses a node-based material system
       
    5. For complex operations or unique Houdini features:
       - Use execute_houdini_code() with custom Python code
       - Take advantage of Houdini's procedural workflow
       
    6. When you need to create a complete scene:
       - Create objects one by one
       - Set up proper hierarchy if needed
       - Apply materials
       - Configure lighting and camera
       - Use render_scene() to generate the final output
    """

# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()