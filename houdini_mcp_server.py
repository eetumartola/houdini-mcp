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
        
        # Start HTTP file server to serve rendered/test images from E:/MCP
        rendered_dir = "E:/MCP"
        if not os.path.exists(rendered_dir):
            os.makedirs(rendered_dir)
        try:
            from file_server import start_file_server
            httpd, http_thread = start_file_server(rendered_dir, port=8000)
            logger.info(f"HTTP file server started on port 8000 serving {rendered_dir}")
        except Exception as e:
            logger.error(f"Failed to start HTTP file server: {str(e)}")
        
        try:
            houdini = get_houdini_connection()
            logger.info("Successfully connected to Houdini on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Houdini on startup: {str(e)}")
            logger.warning("Make sure the Houdini extension is running before using Houdini resources or tools")
        
        yield {}
    finally:
        try:
            httpd.shutdown()
            logger.info("HTTP file server shutdown")
        except Exception as e:
            logger.error(f"Error shutting down HTTP file server: {str(e)}")
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
    image_name: str = None
) -> Any:
    """
    Render the current Houdini scene and return the result as an MCP Image object.
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
                logger.info("Successfully created MCPImage object")
                
                return image
                
            except Exception as img_err:
                logger.error(f"Error handling image data: {str(img_err)}")
                import traceback
                logger.error(traceback.format_exc())
                return {
                    "error": f"Failed to process image data: {str(img_err)}"
                }
        
        # If we got here, we couldn't find valid image data
        return {
            "message": "Scene rendered but no image data was returned",
            "render_info": {
                "output_path": result.get('output_path', 'unknown'),
                "resolution": result.get('resolution', ['unknown', 'unknown']),
                "camera": result.get('camera', 'default'),
                "available_keys": list(result.keys())
            }
        }

    except Exception as e:
        logger.error(f"Error rendering scene: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            "error": f"Error rendering scene: {str(e)}"
        }

@mcp.tool()
def diagnose_image_handling(ctx: Context) -> Any:
    try:
        import tempfile, os, time, base64
        from PIL import Image as PILImage, ImageDraw, ImageFont

        # Create a simple test image with text and shapes
        width, height = 800, 600
        img = PILImage.new("RGB", (width, height), color=(240, 240, 240))
        draw = ImageDraw.Draw(img)
        
        draw.rectangle([(50, 50), (200, 200)], fill=(255, 0, 0), outline=(0, 0, 0))
        draw.ellipse([(300, 100), (500, 300)], fill=(0, 255, 0), outline=(0, 0, 0))
        draw.polygon([(600, 50), (700, 200), (500, 200)], fill=(0, 0, 255), outline=(0, 0, 0))
        
        try:
            font = ImageFont.truetype("arial.ttf", 36)
        except IOError:
            font = ImageFont.load_default()
        draw.text((200, 400), "Houdini MCP Test Image", fill=(0, 0, 0), font=font)
        draw.text((150, 450), "Red square, Green circle, Blue triangle", fill=(0, 0, 0), font=font)
        draw.text((250, 500), f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}", fill=(0, 0, 0), font=font)
        
        # Save the image to a temporary file
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, "houdini_mcp_test_image.jpg")
        img.save(temp_path, format="JPEG", quality=95)
        
        # Open the file and encode the image data in base64
        with open(temp_path, "rb") as f:
            image_data = f.read()
        encoded_image = base64.b64encode(image_data).decode("utf-8")
        
        return {
            "message": "This is a test image created to diagnose image handling. "
                       "If Claude can see this image, it should be able to describe these elements.",
            "image": {
                "data": encoded_image,
                "format": "jpg",
                "filename": os.path.basename(temp_path)
            }
        }
    except Exception as e:
        import traceback
        logger.error(f"Error in diagnose_image_handling: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            "error": str(e),
            "traceback": traceback.format_exc()
        }

@mcp.tool()
def simple_image_diagnostics(ctx: Context) -> Any:
    try:
        import tempfile, os, base64
        from PIL import Image as PILImage, ImageDraw, ImageFont

        # Create a simple test image
        width, height = 400, 300
        img = PILImage.new("RGB", (width, height), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        
        # Draw a red rectangle
        draw.rectangle([(50, 50), (150, 150)], fill=(255, 0, 0))
        
        try:
            font = ImageFont.truetype("arial.ttf", 20)
        except IOError:
            font = ImageFont.load_default()
        draw.text((180, 100), "Test Image", fill=(0, 0, 0), font=font)
        
        # Save the image to a temporary file
        temp_file = os.path.join(tempfile.gettempdir(), "simple_test.jpg")
        img.save(temp_file, "JPEG")
        
        # Read and encode the file to base64
        with open(temp_file, "rb") as f:
            img_data = f.read()
        encoded_image = base64.b64encode(img_data).decode("utf-8")
        
        return {
            "message": "Created a simple test image",
            "image": {
                "data": encoded_image,
                "format": "jpg",
                "filename": os.path.basename(temp_file)
            },
            "file_path": temp_file,
            "file_size": len(img_data),
            "dimensions": f"{width}x{height}"
        }
    except Exception as e:
        import traceback
        logger.error(f"Error in simple_image_diagnostics: {str(e)}")
        logger.error(traceback.format_exc())
        return f"Error creating simple test image: {str(e)}"


def test_render(self):
    """Return a test image to verify image communication is working"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import tempfile, os, time, base64
        
        # Create a simple test image
        width, height = 400, 300
        img = Image.new("RGB", (width, height), color=(240, 240, 240))
        draw = ImageDraw.Draw(img)
        
        draw.rectangle([(50, 50), (150, 150)], fill=(255, 0, 0), outline=(0, 0, 0))
        draw.text((180, 100), "Test Render Image", fill=(0, 0, 0))
        draw.text((180, 150), f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}", fill=(0, 0, 0))
        
        # Save to temporary file
        temp_file = os.path.join(tempfile.gettempdir(), f"test_render_{int(time.time())}.jpg")
        img.save(temp_file, "JPEG")
        
        # Read and encode to base64
        with open(temp_file, "rb") as f:
            image_data = f.read()
        encoded_image_data = base64.b64encode(image_data).decode('utf-8')
        
        return {
            "rendered": True,
            "output_path": temp_file,
            "resolution": ["400", "300"],
            "camera": "test",
            "image_data": encoded_image_data,
            "image_size": len(image_data),
            "render_time": "0s"
        }
    except Exception as e:
        return {"error": f"Failed to create test render: {str(e)}"}
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




@mcp.tool()
def diagnose_image_import(ctx: Context) -> str:
    """
    Diagnoses the correct way to import and use the Image class from MCP.
    """
    import inspect
    import importlib
    import sys
    import traceback
    
    # Output details about the current MCP module structure
    result = {
        "mcp_module_info": {}
    }
    
    try:
        # Check what's in the mcp module
        import mcp
        result["mcp_module_info"]["mcp_dir"] = dir(mcp)
        
        # Check server module
        if hasattr(mcp, "server"):
            result["mcp_module_info"]["server_dir"] = dir(mcp.server)
            
            # Check fastmcp module
            if hasattr(mcp.server, "fastmcp"):
                result["mcp_module_info"]["fastmcp_dir"] = dir(mcp.server.fastmcp)
                
                # Look specifically for image-related classes or functions
                image_related = []
                for name in dir(mcp.server.fastmcp):
                    if "image" in name.lower():
                        image_related.append(name)
                result["mcp_module_info"]["image_related"] = image_related
    except Exception as e:
        result["mcp_module_info"]["error"] = str(e)
    
    # Try various ways to import and create an image
    result["import_attempts"] = []
    
    # Simple test image data
    import tempfile
    import os
    from PIL import Image as PILImage
    
    test_img = PILImage.new("RGB", (100, 100), color=(255, 0, 0))
    test_path = os.path.join(tempfile.gettempdir(), "mcp_test_img.jpg")
    test_img.save(test_path)
    
    # Attempt 1: Direct from mcp.server.fastmcp
    try:
        from mcp.server.fastmcp import Image as Image1
        result["import_attempts"].append({
            "method": "from mcp.server.fastmcp import Image",
            "success": True,
            "type": str(type(Image1)),
            "signature": str(inspect.signature(Image1)) if callable(Image1) else "not callable"
        })
        
        # Try to create an image
        try:
            img1 = Image1(path=test_path)
            result["import_attempts"][0]["instance_created"] = True
        except Exception as e:
            result["import_attempts"][0]["instance_error"] = str(e)
            result["import_attempts"][0]["instance_created"] = False
    except Exception as e:
        result["import_attempts"].append({
            "method": "from mcp.server.fastmcp import Image",
            "success": False,
            "error": str(e)
        })
    
    # Attempt 2: From utilities.types
    try:
        from mcp.server.fastmcp.utilities.types import Image as Image2
        result["import_attempts"].append({
            "method": "from mcp.server.fastmcp.utilities.types import Image",
            "success": True,
            "type": str(type(Image2)),
            "signature": str(inspect.signature(Image2)) if callable(Image2) else "not callable"
        })
        
        # Try to create an image
        try:
            img2 = Image2(path=test_path)
            result["import_attempts"][1]["instance_created"] = True
        except Exception as e:
            result["import_attempts"][1]["instance_error"] = str(e)
            result["import_attempts"][1]["instance_created"] = False
    except Exception as e:
        result["import_attempts"].append({
            "method": "from mcp.server.fastmcp.utilities.types import Image",
            "success": False,
            "error": str(e)
        })
    
    # Attempt 3: Look for a create_image function
    try:
        from mcp.server.fastmcp import create_image
        result["import_attempts"].append({
            "method": "from mcp.server.fastmcp import create_image",
            "success": True,
            "type": str(type(create_image)),
            "signature": str(inspect.signature(create_image)) if callable(create_image) else "not callable"
        })
        
        # Try to create an image
        try:
            img3 = create_image(path=test_path)
            result["import_attempts"][2]["instance_created"] = True
        except Exception as e:
            result["import_attempts"][2]["instance_error"] = str(e)
            result["import_attempts"][2]["instance_created"] = False
    except Exception as e:
        result["import_attempts"].append({
            "method": "from mcp.server.fastmcp import create_image",
            "success": False,
            "error": str(e)
        })
        
    return result

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
       
    7. For rendering:
       - Use render_scene() with use_opengl=True (default) for fast visualization
       - Only use use_opengl=False when photorealistic rendering is required
       - OpenGL rendering is significantly faster and more stable for quick views
       - Set resolution_x and resolution_y for specific output dimensions
       - Specify a camera_path if you want to render from a specific camera
    """

# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()