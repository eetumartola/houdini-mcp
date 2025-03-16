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
            
            # For render operations, use a much longer timeout
            if command_type == "render_scene":
                self.sock.settimeout(120.0)  # 2 minutes for render operations
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
                                # If it's already a string, it's likely already encoded
                                logger.info("Image data is already a string, length: " + 
                                            f"{len(result['image_data'])}")
                            else:
                                # Convert binary image data to base64 string
                                import base64
                                try:
                                    encoded_data = base64.b64encode(result["image_data"]).decode('utf-8')
                                    result["image_data"] = encoded_data
                                    logger.info(f"Converted image data to base64, length: {len(encoded_data)}")
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
    resolution_x: int = None,
    resolution_y: int = None,
    camera_path: str = None,
    image_name: str = None,
    return_image: bool = True
) -> Any:
    """
    Render the current Houdini scene and optionally return the image.
    
    Parameters:
    - output_path: Optional path to save the rendered image
    - resolution_x: Optional horizontal resolution
    - resolution_y: Optional vertical resolution
    - camera_path: Optional path to the camera to use for rendering
    - image_name: Optional descriptive name for the rendered image
    - return_image: If True, returns the rendered image as part of the response
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
        if camera_path:
            params["camera_path"] = camera_path
        if image_name:
            params["image_name"] = image_name
            
        logger.info(f"Sending render_scene command with parameters: {params}")
        result = houdini.send_command("render_scene", params)
        logger.info(f"Received render result with keys: {list(result.keys())}")
        
        # Check if image data is available and return_image is requested
        if return_image and "image_data" in result and result["image_data"]:
            # Create an Image object from the binary data
            try:
                import base64
                import io
                from PIL import Image as PILImage
                import inspect
                import tempfile
                import os
                
                # Add debugging information about the Image class
                logger.info(f"MCP Image class expects type: {inspect.signature(Image.__init__)}")
                
                # Log the image data length for debugging
                logger.info(f"Processing image data, length: {len(result['image_data'])}")
                
                # Convert binary data to image
                img_data = result["image_data"]
                if isinstance(img_data, str):
                    # If it's a string, assume it's base64 encoded
                    img_data = base64.b64decode(img_data)
                
                logger.info(f"Decoded image data, binary length: {len(img_data)}")
                
                # Create a BytesIO object from the binary data
                img_buffer = io.BytesIO(img_data)
                img_buffer.seek(0)
                
                # Open the image using PIL
                img = PILImage.open(img_buffer)
                logger.info(f"Successfully created PIL Image: format={img.format}, size={img.size}, mode={img.mode}")
                logger.info(f"Current data type: {type(img)}")
                
                # Convert RGBA to RGB if needed
                if img.mode == 'RGBA':
                    img = img.convert('RGB')
                    logger.info("Converted image from RGBA to RGB")
                
                # Since MCP Image expects a file path, save to a temporary file first
                temp_file = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
                temp_path = temp_file.name
                temp_file.close()  # Close the file so we can reopen it
                
                logger.info(f"Saving image to temporary file: {temp_path}")
                img.save(temp_path, format="JPEG", quality=95)
                
                # Now create the MCP Image using the file path
                logger.info(f"Creating MCP Image from path: {temp_path}")
                mcp_image = Image(temp_path)
                
                logger.info("Successfully created MCP Image object")
                
                # Return both the image and render info
                result_obj = {
                    "message": f"Scene rendered successfully. Output: {result.get('output_path')}, Resolution: {result.get('resolution')}",
                    "image": mcp_image
                }
                
                # Schedule the temporary file for deletion
                # We can't delete it immediately as it's still being used
                def cleanup_temp_file():
                    try:
                        if os.path.exists(temp_path):
                            os.unlink(temp_path)
                            logger.info(f"Deleted temporary file: {temp_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete temporary file {temp_path}: {str(e)}")
                
                # Start a thread to clean up after a delay
                import threading
                cleanup_thread = threading.Timer(60.0, cleanup_temp_file)  # Clean up after 60 seconds
                cleanup_thread.daemon = True
                cleanup_thread.start()
                
                return result_obj
            
            except Exception as e:
                logger.error(f"Error processing image data: {str(e)}")
                # Include the traceback for better debugging
                import traceback
                logger.error(traceback.format_exc())
                
                # Fall back to text response if image processing fails
                return f"Scene rendered successfully (but image processing failed: {str(e)}). Output: {result.get('output_path')}, Resolution: {result.get('resolution')}"
        else:
            # Provide information about why the image wasn't returned
            if not return_image:
                logger.info("Image return was disabled by parameter")
                return f"Scene rendered successfully (image return disabled). Output: {result.get('output_path')}, Resolution: {result.get('resolution')}"
            elif "image_data" not in result:
                logger.warning("No image_data found in the render result")
                return f"Scene rendered successfully, but no image data was returned from Houdini. Output: {result.get('output_path')}, Resolution: {result.get('resolution')}"
            elif not result["image_data"]:
                logger.warning("image_data is empty in the render result")
                return f"Scene rendered successfully, but the image data is empty. Output: {result.get('output_path')}, Resolution: {result.get('resolution')}"
            else:
                # This case shouldn't be reached but is here for completeness
                logger.warning("Unknown reason for not processing image")
                return f"Scene rendered successfully. Output: {result.get('output_path')}, Resolution: {result.get('resolution')}"
    except ConnectionError as e:
        logger.error(f"Connection error: {str(e)}")
        return f"Error rendering scene: Connection to Houdini failed: {str(e)}"
    except Exception as e:
        logger.error(f"Error rendering scene: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
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
       - Use copy_object() to clone existing objects

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