#!/usr/bin/env python3
# houdini_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context, Image
import socket
import json
import os
import asyncio
import select
import logging
import queue
import base64
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
            
            # Set appropriate timeout based on command type and parameters
            if command_type == "render_scene":
                if params and params.get("use_opengl", True):
                    self.sock.settimeout(60.0)  # 1 minute for OpenGL renders
                    logger.info("Using 60 second timeout for OpenGL render")
                else:
                    self.sock.settimeout(120.0)  # 2 minutes for Mantra renders
                    logger.info("Using 120 second timeout for Mantra render")
            else:
                self.sock.settimeout(30.0)   # Default timeout for other operations
            
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
                    
                    result = response.get("result", {})
                    
                    # Special handling for image data
                    if "image_data" in result:
                        if result["image_data"] is not None:
                            # Log the size of binary data
                            image_size = result.get("image_size", 0)
                            logger.info(f"Image data received: {image_size} bytes")
                            
                            if isinstance(result["image_data"], str):
                                # If it's already a string, assume it's base64 encoded
                                logger.info(f"Image data is already a string (length: {len(result['image_data'])})")
                                # We'll keep it as is - image processing functions will handle decoding
                            else:
                                # If it's binary, encode it to base64
                                import base64
                                try:
                                    encoded_data = base64.b64encode(result["image_data"]).decode('utf-8')
                                    result["image_data"] = encoded_data
                                    logger.info(f"Converted binary image data to base64 (length: {len(encoded_data)})")
                                except Exception as e:
                                    logger.error(f"Error encoding image data: {str(e)}")
                                    # Keep the image data as is if encoding fails
                        else:
                            logger.warning("Image data is None")
                    
                    return result
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
def copy_object(
    ctx: Context,
    source_path: str = None,
    source_name: str = None,
    new_name: str = None,
    position_offset: List[float] = None
) -> str:
    """
    Copy an existing object in the Houdini scene.
    
    Parameters:
    - source_path: Full path of the object to copy
    - source_name: Name of the object if path not provided
    - new_name: Optional name for the new copy
    - position_offset: Optional [x, y, z] offset from the original position
    """
    try:
        if not source_path and not source_name:
            return "Error: Either source_path or source_name must be provided"
            
        houdini = get_houdini_connection()
        
        params = {}
        if source_path:
            params["source_path"] = source_path
        if source_name:
            params["source_name"] = source_name
        if new_name:
            params["new_name"] = new_name
        if position_offset:
            params["position_offset"] = position_offset
            
        result = houdini.send_command("copy_object", params)
        
        return f"Copied object: {result.get('source')} → {result.get('name')} at path: {result.get('path')}"
    except Exception as e:
        logger.error(f"Error copying object: {str(e)}")
        return f"Error copying object: {str(e)}"

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
    resolution_x: int = 640,  # Default to smaller resolution
    resolution_y: int = 480,  # Default to smaller resolution
    camera_path: str = None,
    image_name: str = None
) -> Any:
    """
    Render the current Houdini scene and return the result as an MCP Image object.
    Uses smaller default resolutions (640x480) for faster rendering and display.
    """
    try:
        houdini = get_houdini_connection()

        # Prepare parameters
        params = {}
        if output_path:
            params["output_path"] = output_path
        if resolution_x:
            params["resolution_x"] = resolution_x
        if resolution_y:
            params["resolution_y"] = resolution_y
        if camera_path:
            params["camera_path"] = camera_path
        if image_name:
            params["image_name"] = image_name

        logger.info(f"Sending render_scene command with parameters: {params}")
        result = houdini.send_command("render_scene", params)
        logger.info(f"Received render result keys: {list(result.keys())}")

        # Check if the render was successful
        if "error" in result:
            return {
                "error": result["error"],
                "message": "Rendering failed"
            }

        # Process the image data - it comes as a base64 encoded string in image_data
        if "image_data" in result and result["image_data"]:
            try:
                import tempfile, os, base64
                from mcp.server.fastmcp import Image as MCPImage
                
                # Decode the base64 string to binary data
                img_data = base64.b64decode(result["image_data"])
                
                # Save to a temporary file
                timestamp = int(time.time())
                temp_dir = tempfile.gettempdir()
                img_path = os.path.join(temp_dir, f"houdini_render_{timestamp}.jpg")
                
                with open(img_path, "wb") as f:
                    f.write(img_data)
                
                logger.info(f"Saved rendered image to {img_path} ({len(img_data)} bytes)")
                
                # Create and return an MCP Image object
                image = MCPImage(path=img_path)
                return image
                
            except Exception as img_err:
                logger.error(f"Error handling image data: {str(img_err)}")
                return {
                    "error": f"Failed to process image data: {str(img_err)}"
                }
        
        # If we got here, we couldn't find valid image data
        return {
            "message": "Scene rendered but no image data was returned",
            "render_info": {
                "output_path": result.get('output_path', 'unknown'),
                "resolution": result.get('resolution', ['unknown', 'unknown']),
                "camera": result.get('camera', 'default')
            }
        }

    except Exception as e:
        logger.error(f"Error rendering scene: {str(e)}")
        return {
            "error": f"Error rendering scene: {str(e)}"
        }


@mcp.tool()
def claude_vision_test(ctx: Context) -> Any:
    try:
        import os, time, base64, tempfile
        from PIL import Image as PILImage, ImageDraw, ImageFont
        from mcp.server.fastmcp import Image as MCPImage
        
        # Create and save image as before...
        width, height = 256, 256
        img = PILImage.new("RGB", (width, height), color=(240, 240, 240))
        draw = ImageDraw.Draw(img)
        draw.rectangle([(30, 30), (100, 100)], fill=(0, 0, 255), outline=(0, 0, 0))
        draw.ellipse([(120, 30), (190, 100)], fill=(0, 255, 0), outline=(0, 0, 0))
        verification_code = '715517'
        draw.text((60, 10), "Claude Vision Test", fill=(0, 0, 0))
        draw.text((60, 210), verification_code, fill=(255, 0, 0))
        
        temp_dir = tempfile.gettempdir()
        file_path = os.path.join(temp_dir, f"test_{int(time.time())}.jpg")
        img.save(file_path, format="JPEG", quality=95)
        
        # Create an MCP Image object with the file path
        image = MCPImage(path=file_path)
        
        # Return the image object directly
        return image
        
    except Exception as e:
        import traceback
        logger.error(f"Error in claude_vision_test: {str(e)}")
        logger.error(traceback.format_exc())
        return f"Error creating vision test: {str(e)}"


@mcp.prompt()
def asset_creation_strategy() -> str:
    """Defines the preferred strategy for creating assets in Houdini"""
    return """When creating 3D content in Houdini, follow these guidelines:

    1. Always start by checking the current scene using get_scene_info() to understand the existing context.

    2. For creating new objects:
       - Use create_object() with appropriate parameters
       - For geometry nodes, specify the primitive_type parameter
       - Common primitive types: box, sphere, grid, torus, tube (a cylinder is called a tube in houdini)
       
    3. For modifying existing objects:
       - Use modify_object() with the path or name of the object
       - Set translation, rotation, or scale as needed
       - Use copy_object() to clone existing objects

    4. For adding materials:
       - Use set_material() to apply colors or materials
       - Remember that Houdini uses a node-based material system
       
    5. For complex operations or unique Houdini features:
       - Use execute_houdini_code() with custom Python code, if nothing else works
       - Take advantage of Houdini's procedural workflow
       
    6. When you need to create a complete scene:
       - Create objects one by one
       - Set up proper hierarchy if needed
       - Apply materials
       - Configure lighting and camera
       - Iterate by checking render_scene and really thinking about what you see and if it is correct and good

    7.  Remeber Houdini's coordinate system
       - Right hand system
         * X-axis runs left to right (positive right)
         * Y-axis runs from down to up (positive up)
         * Z-axis runs from back to front (positive towards back)
       - Pay special attention to the orientation of primitives you are adding
       - Primitives such as tubes are natively oriented across Houdini's Z axis

    8. For rendering:
       - Use render_scene() for visualization, very helpful in doublechecking whether things look the way they should
       - Default resolution is 640x480, which is ideal for quick preview renders
       - Specify resolution_x and resolution_y only if you need a specific size
       - Larger resolutions will take longer to render and display
       - You should specify a camera_path if you want to render
       - the camera is pointing at negative Z
       - The rendered image will be automatically returned to Claude for viewing
    """

# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()