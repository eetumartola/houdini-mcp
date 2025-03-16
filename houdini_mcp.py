import hou
import json
import threading
import socket
import time
import traceback
import os
import shutil
import tempfile
import requests
import select
import base64
import queue
from typing import Dict, Any, Optional, List, Tuple

import logging
# Configure logging
logging.basicConfig(level=logging.DEBUG, 
                  format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("HoudiniMCPServer")
    
class BinaryDataEncoder(json.JSONEncoder):
    """Custom JSON encoder that handles binary data by converting to base64"""
    def default(self, obj):
        import base64
        if isinstance(obj, bytes):
            try:
                # Encode binary data to base64 with clear logging
                encoded = base64.b64encode(obj).decode('utf-8')
                print(f"Encoded {len(obj)} bytes of binary data to {len(encoded)} characters of base64")
                return encoded
            except Exception as e:
                print(f"Error encoding binary data: {str(e)}")
                # Return an empty string rather than failing completely
                return ""
        return super().default(obj)

class HoudiniMCPServer:
    def __init__(self, host='localhost', port=9876):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.clients = {}  # Dictionary of client sockets and their details
        self.lock = threading.Lock()
        
        # Command queue for asynchronous processing
        self.command_queue = queue.Queue()
        
    def start(self):
        """Start the server in non-blocking mode"""
        self.running = True
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.socket.listen(5)
            self.socket.setblocking(False)
            
            # Start command processor thread
            threading.Thread(target=self._command_processor, daemon=True).start()
            
            # Start connection handler thread
            threading.Thread(target=self._connection_handler, daemon=True).start()
            
            print(f"HoudiniMCP server started on {self.host}:{self.port}")
            return True
        except Exception as e:
            print(f"Failed to start server: {str(e)}")
            self.stop()
            return False
    
    def stop(self):
        """Stop the server and clean up connections"""
        self.running = False
        
        # Close all client connections
        with self.lock:
            for client_id in self.clients:
                try:
                    self.clients[client_id]['socket'].close()
                except:
                    pass
            self.clients = {}
        
        # Close server socket
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
        
        print("HoudiniMCP server stopped")
    
    def _connection_handler(self):
        """Thread that handles incoming connections and reads from clients"""
        print("Connection handler thread started")
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        while self.running:
            try:
                # Accept new connections
                try:
                    client_socket, address = self.socket.accept()
                    client_socket.setblocking(False)
                    client_id = f"{address[0]}:{address[1]}:{time.time()}"
                    
                    with self.lock:
                        self.clients[client_id] = {
                            'socket': client_socket,
                            'address': address,
                            'buffer': b'',
                            'last_activity': time.time()
                        }
                    
                    print(f"New client connected: {address}")
                except BlockingIOError:
                    # No new connections
                    pass
                except Exception as e:
                    print(f"Error accepting connection: {str(e)}")
                
                # Check existing clients for data
                with self.lock:
                    clients_to_check = list(self.clients.items())
                
                for client_id, client_info in clients_to_check:
                    try:
                        # Check if client has data
                        readable, _, exceptional = select.select([client_info['socket']], [], [client_info['socket']], 0)
                        
                        if exceptional:
                            # Socket has exception condition
                            raise Exception("Socket exception condition")
                        
                        if readable:
                            # Client has data
                            try:
                                data = client_info['socket'].recv(16384)
                                
                                if not data:
                                    # Client disconnected
                                    print(f"Client disconnected: {client_info['address']}")
                                    with self.lock:
                                        if client_id in self.clients:
                                            try:
                                                self.clients[client_id]['socket'].close()
                                            except:
                                                pass
                                            del self.clients[client_id]
                                    continue
                                
                                # Update client activity timestamp
                                with self.lock:
                                    if client_id in self.clients:
                                        self.clients[client_id]['last_activity'] = time.time()
                                        self.clients[client_id]['buffer'] += data
                                
                                # Process complete messages
                                self._process_client_buffer(client_id)
                                
                            except BlockingIOError:
                                # No data yet
                                pass
                            except ConnectionResetError:
                                # Client closed connection
                                print(f"Connection reset by client: {client_info['address']}")
                                with self.lock:
                                    if client_id in self.clients:
                                        try:
                                            self.clients[client_id]['socket'].close()
                                        except:
                                            pass
                                        del self.clients[client_id]
                            except Exception as e:
                                print(f"Error reading from client {client_info['address']}: {str(e)}")
                                traceback.print_exc()
                                with self.lock:
                                    if client_id in self.clients:
                                        try:
                                            self.clients[client_id]['socket'].close()
                                        except:
                                            pass
                                        del self.clients[client_id]
                    
                    except Exception as e:
                        print(f"Error checking client {client_info['address']}: {str(e)}")
                        with self.lock:
                            if client_id in self.clients:
                                try:
                                    self.clients[client_id]['socket'].close()
                                except:
                                    pass
                                del self.clients[client_id]
                
                # Clean up inactive clients every 60 seconds
                self._cleanup_inactive_clients()
                
                # Short delay to prevent CPU overuse
                time.sleep(0.01)
                
            except Exception as e:
                print(f"Error in connection handler: {str(e)}")
                traceback.print_exc()
                time.sleep(1)  # Prevent rapid failure loops
        
        print("Connection handler thread stopped")
    
    def _process_client_buffer(self, client_id):
        """Process complete messages in client buffer"""
        with self.lock:
            if client_id not in self.clients:
                return
            
            client_info = self.clients[client_id]
            
            # Look for complete messages (delimited by newline)
            while b'\n' in client_info['buffer']:
                message, _, remaining = client_info['buffer'].partition(b'\n')
                client_info['buffer'] = remaining
                
                if message.strip():
                    try:
                        # Parse JSON command
                        command = json.loads(message.decode('utf-8'))
                        cmd_type = command.get('type', '')
                        logger.info(f"Received command: {cmd_type} from {client_info['address']}")
                        
                        # Queue command for processing
                        self.command_queue.put({
                            'client_id': client_id,
                            'command': command,
                            'timestamp': time.time()
                        })
                    except json.JSONDecodeError as e:
                        logger.error(f"Invalid JSON from {client_info['address']}: {e}")
                        # Send error response
                        self._send_response(client_id, {
                            "status": "error",
                            "message": f"Invalid JSON format: {str(e)}"
                        })

    def _command_processor(self):
        """Thread that processes commands from the queue"""
        print("Command processor thread started")
        
        while self.running:
            try:
                # Get command from queue with timeout
                try:
                    command_info = self.command_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                
                client_id = command_info['client_id']
                command = command_info['command']
                
                # Execute command
                try:
                    result = self.execute_command(command)
                    
                    # Send response back to client
                    self._send_response(client_id, result)
                except Exception as e:
                    print(f"Error executing command: {str(e)}")
                    traceback.print_exc()
                    
                    # Send error response
                    self._send_response(client_id, {
                        "status": "error",
                        "message": f"Command execution error: {str(e)}"
                    })
                
                finally:
                    # Mark task as done
                    self.command_queue.task_done()
                
            except Exception as e:
                print(f"Error in command processor: {str(e)}")
                traceback.print_exc()
                time.sleep(1)  # Prevent rapid failure loops
        
        print("Command processor thread stopped")


    def _send_response(self, client_id, response):
        """Send response to client"""
        try:
            with self.lock:
                if client_id not in self.clients:
                    logger.warn(f"Client {client_id} no longer connected, can't send response")
                    return False
                
                client_info = self.clients[client_id]
            
            # Check for binary data in the response
            has_binary = False
            if "result" in response and isinstance(response["result"], dict):
                has_binary = "image_data" in response["result"] and isinstance(response["result"]["image_data"], bytes)
            
            # Prepare response with appropriate encoder
            if has_binary:
                logger.info("Response contains binary data, using custom encoder")
                response_json = json.dumps(response, cls=BinaryDataEncoder) + "\n"
            else:
                response_json = json.dumps(response) + "\n"
            
            response_bytes = response_json.encode('utf-8')
            
            logger.info(f"Sending response ({len(response_bytes)} bytes) to {client_info['address']}")
            
            # Send in a non-blocking way
            try:
                # Set socket to blocking mode for the send operation with a timeout
                client_info['socket'].setblocking(True)
                client_info['socket'].settimeout(5.0)  # 5 second timeout for sending
                
                # Send all data
                total_sent = 0
                while total_sent < len(response_bytes):
                    sent = client_info['socket'].send(response_bytes[total_sent:])
                    if sent == 0:
                        raise ConnectionError("Socket connection broken")
                    total_sent += sent
                    
                # Reset to non-blocking
                client_info['socket'].setblocking(False)
                
                logger.info(f"Response sent successfully to {client_info['address']}")
                return True
                
            except (ConnectionError, BrokenPipeError, socket.timeout) as e:
                logger.warn(f"Client disconnected during send: {str(e)}")
                with self.lock:
                    if client_id in self.clients:
                        try:
                            self.clients[client_id]['socket'].close()
                        except:
                            pass
                        del self.clients[client_id]
                return False
                
            except Exception as e:
                logger.error(f"Error sending response: {str(e)}")
                traceback.print_exc()
                return False
                
        except Exception as e:
            logger.error(f"Error in send_response: {str(e)}")
            traceback.print_exc()
            return False
        
    def _cleanup_inactive_clients(self):
        """Remove clients that haven't been active for a while"""
        try:
            current_time = time.time()
            inactive_timeout = 300  # 5 minutes
            
            with self.lock:
                clients_to_remove = []
                
                for client_id, client_info in self.clients.items():
                    if current_time - client_info['last_activity'] > inactive_timeout:
                        clients_to_remove.append(client_id)
                
                for client_id in clients_to_remove:
                    print(f"Removing inactive client: {self.clients[client_id]['address']}")
                    try:
                        self.clients[client_id]['socket'].close()
                    except:
                        pass
                    del self.clients[client_id]
        
        except Exception as e:
            print(f"Error in cleanup_inactive_clients: {str(e)}")
    
    def execute_command(self, command):
        """Execute a command and return the result"""
        try:
            cmd_type = command.get("type")
            params = command.get("params", {})
            
            # For create_object commands, handle them differently
            if cmd_type == "create_object":
                obj_type = params.get("type", "unknown")
                obj_name = params.get("name", f"{obj_type}_{int(time.time())}")
                
                logger.info(f"Processing create_object for {obj_name}")
                
                # Execute immediately instead of in background
                try:
                    result = self.create_object(
                        type=obj_type, 
                        name=obj_name, 
                        position=params.get("position"), 
                        primitive_type=params.get("primitive_type", "box")
                    )
                    logger.info(f"Creation complete for {obj_name}")
                    return {"status": "success", "result": result}
                except Exception as e:
                    logger.error(f"Error creating object {obj_name}: {str(e)}")
                    return {"status": "error", "message": str(e)}
            
            # For all other commands, process normally
            handlers = {
                "get_scene_info": self.get_scene_info,
                "get_object_info": self.get_object_info,
                "modify_object": self.modify_object,
                "copy_object": self.copy_object,  
                "delete_object": self.delete_object,
                "execute_code": self.execute_code,
                "set_material": self.set_material,
                "render_scene": self.render_scene,
            }
            
            handler = handlers.get(cmd_type)
            if handler:
                try:
                    logger.info(f"Executing handler for {cmd_type}")
                    result = handler(**params)
                    logger.info(f"Handler execution complete for {cmd_type}")
                    return {"status": "success", "result": result}
                except Exception as e:
                    logger.error(f"Error in handler {cmd_type}: {str(e)}")
                    traceback.print_exc()
                    return {"status": "error", "message": str(e)}
            else:
                return {"status": "error", "message": f"Unknown command type: {cmd_type}"}
        except Exception as e:
            logger.error(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}



    def get_scene_info(self):
        """Get information about the current Houdini scene"""
        try:
            print("Getting scene info...")
            
            # Basic scene info
            scene_info = {
                "name": hou.hipFile.basename(),
                "path": hou.hipFile.path(),
                "object_count": 0,
                "objects": [],
            }
            
            # Get root nodes from the obj context (scene objects)
            obj_context = hou.node("/obj")
            if obj_context:
                nodes = obj_context.children()
                scene_info["object_count"] = len(nodes)
                
                # Collect information about each object (limited to first 10)
                for i, node in enumerate(nodes):
                    if i >= 10:  # Limit to 10 objects to avoid overwhelming
                        break
                        
                    obj_info = {
                        "name": node.name(),
                        "type": node.type().name(),
                        "path": node.path(),
                    }
                    
                    # Get transform if available
                    try:
                        if hasattr(node, "worldTransform"):
                            transform = node.worldTransform()
                            translation = transform.extractTranslates()
                            obj_info["position"] = [
                                float(translation[0]),
                                float(translation[1]),
                                float(translation[2])
                            ]
                    except Exception:
                        pass  # Not all nodes have transforms
                        
                    scene_info["objects"].append(obj_info)
            
            print(f"Scene info collected: {len(scene_info.get('objects', []))} objects")
            return scene_info
        except Exception as e:
            print(f"Error in get_scene_info: {str(e)}")
            traceback.print_exc()
            return {"error": str(e)}


    def create_object(self, type="geo", name=None, position=None, primitive_type="box") -> Dict[str, Any]:
        """Create a new object in the scene"""
        try:
            obj_context = hou.node("/obj")
            
            # Generate a name if none provided
            if not name:
                name = f"{type}_node"
            
            # Create the node based on type
            new_node = obj_context.createNode(type, name)
            
            # Set position if provided
            if position and len(position) == 3:
                translate_parm = new_node.parmTuple("t")
                if translate_parm:
                    translate_parm[0].set(float(position[0]))
                    translate_parm[1].set(float(position[1]))
                    translate_parm[2].set(float(position[2]))
                    
            # For geometry nodes, add the appropriate primitives
            if type == "geo":
                geo_node = new_node
                if not geo_node:
                    raise ValueError(f"Failed to create geometry node: {name}")
                
                # Add a primitive inside the geo node based on requested type
                inside_node = geo_node.createNode(primitive_type)
                
                # Connect to output
                output_node = geo_node.createNode("output")
                output_node.setInput(0, inside_node)
                
                # Set display flag on the output node for consistent behavior
                inside_node.setDisplayFlag(False)
                output_node.setDisplayFlag(True)
                
                # Layout the network for cleanliness
                geo_node.layoutChildren()
            
            return {
                "name": new_node.name(),
                "path": new_node.path(),
                "type": new_node.type().name()
            }
        
        except Exception as e:
            print(f"Error in create_object: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to create object: {str(e)}")

    def modify_object(self, path=None, name=None, translate=None, rotate=None, scale=None, visible=None):
        """Modify an existing object in the scene"""
        try:
            # Find the node by path or name
            node = None
            
            if path:
                node = hou.node(path)
            elif name:
                obj_context = hou.node("/obj")
                for child in obj_context.children():
                    if child.name() == name:
                        node = child
                        break
            
            if not node:
                raise ValueError(f"Object not found: {path or name}")
            
            # Apply transformations directly to the object node
            if translate:
                translate_parm = node.parmTuple("t")
                if translate_parm:
                    translate_parm[0].set(float(translate[0]))
                    translate_parm[1].set(float(translate[1]))
                    translate_parm[2].set(float(translate[2]))
            
            if rotate:
                rotate_parm = node.parmTuple("r")
                if rotate_parm:
                    rotate_parm[0].set(float(rotate[0]))
                    rotate_parm[1].set(float(rotate[1]))
                    rotate_parm[2].set(float(rotate[2]))
            
            if scale:
                scale_parm = node.parmTuple("s")
                if scale_parm:
                    scale_parm[0].set(float(scale[0]))
                    scale_parm[1].set(float(scale[1]))
                    scale_parm[2].set(float(scale[2]))
            
            # Handle visibility
            if visible is not None:
                node.setDisplayFlag(visible)
                node.setRenderFlag(visible)
            
            return {
                "name": node.name(),
                "path": node.path(),
                "type": node.type().name(),
                "modified": True
            }
        except Exception as e:
            print(f"Error in modify_object: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to modify object: {str(e)}")


    def delete_object(self, path=None, name=None):
        """Delete an object from the scene"""
        try:
            # Find the node by path or name
            node = None
            
            if path:
                node = hou.node(path)
            elif name:
                # Search in /obj context
                obj_context = hou.node("/obj")
                for child in obj_context.children():
                    if child.name() == name:
                        node = child
                        break
            
            if not node:
                raise ValueError(f"Object not found: {path or name}")
            
            # Store name for return value
            node_name = node.name()
            node_path = node.path()
            
            # Delete the node
            node.destroy()
            
            return {
                "name": node_name,
                "path": node_path,
                "deleted": True
            }
        except Exception as e:
            print(f"Error in delete_object: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to delete object: {str(e)}")

    def get_object_info(self, path=None, name=None):
        """Get detailed information about a specific object"""
        try:
            # Find the node by path or name
            node = None
            
            if path:
                node = hou.node(path)
            elif name:
                obj_context = hou.node("/obj")
                for child in obj_context.children():
                    if child.name() == name:
                        node = child
                        break
            
            if not node:
                raise ValueError(f"Object not found: {path or name}")
            
            # Collect basic node information
            node_info = {
                "name": node.name(),
                "path": node.path(),
                "type": node.type().name(),
                "is_displayed": node.isDisplayFlagSet(),  # Corrected method name
                "is_rendered": node.isRenderFlagSet(),
                "children_count": len(node.children()),
                "parameters": {}
            }
            
            # Get parameter values
            for parm in node.parms():
                try:
                    node_info["parameters"][parm.name()] = parm.eval()
                except Exception:
                    # Skip parameters that can't be evaluated
                    pass
            
            # For geometry nodes, get additional info
            if node.type().name() == "geo":
                try:
                    # Get geometry statistics
                    geo = node.geometry()
                    if geo:
                        node_info["geometry"] = {
                            "point_count": len(geo.points()),
                            "primitive_count": len(geo.prims()),
                            "vertex_count": len(geo.vertices()),
                            "bounds": {
                                "min": [float(v) for v in geo.boundingBox().minvec()],
                                "max": [float(v) for v in geo.boundingBox().maxvec()]
                            }
                        }
                except Exception:
                    pass
            
            return node_info
        except Exception as e:
            print(f"Error in get_object_info: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to get object info: {str(e)}")

    def execute_code(self, code):
        """Execute arbitrary Houdini Python code"""
        try:
            # Create a local namespace for execution
            namespace = {"hou": hou}
            exec(code, namespace)
            return {"executed": True}
        except Exception as e:
            raise Exception(f"Code execution error: {str(e)}")

    def set_material(self, object_path=None, object_name=None, material_name=None, color=None):
        """Set or create a material for an object"""
        try:
            # Find the node by path or name
            node = None
            
            if object_path:
                node = hou.node(object_path)
            elif object_name:
                # Search in /obj context
                obj_context = hou.node("/obj")
                for child in obj_context.children():
                    if child.name() == object_name:
                        node = child
                        break
            
            if not node:
                raise ValueError(f"Object not found: {object_path or object_name}")
            
            # Check if we're dealing with a geometry node
            if node.type().name() != "geo":
                raise ValueError(f"Node {node.name()} is not a geometry node")
            
            # Find or create the material
            mat_context = hou.node("/mat")
            if not mat_context:
                raise ValueError("Material context not found")
            
            material = None
            if material_name:
                # Look for existing material
                for child in mat_context.children():
                    if child.name() == material_name:
                        material = child
                        break
            
            # Create new material if not found
            if not material:
                # Generate a name if none provided
                if not material_name:
                    material_name = f"{node.name()}_material"
                
                # Create a principled shader material
                material = mat_context.createNode("principledshader", material_name)
            
            # Set color if provided
            if color and len(color) >= 3:
                # In Houdini, color is typically set on the basecolor parameter
                base_color = material.parmTuple("basecolor")
                if base_color:
                    base_color[0].set(float(color[0]))
                    base_color[1].set(float(color[1]))
                    base_color[2].set(float(color[2]))
            
            # Assign material to the object
            # In Houdini, we typically add a material SOP inside the geometry node
            material_node = None
            for child in node.children():
                if child.type().name() == "material":
                    material_node = child
                    break
            
            # Find the display node and output node
            display_node = None
            output_node = None
            
            for child in node.children():
                if child.isDisplayFlagSet():
                    display_node = child
                if child.type().name() == "output":
                    output_node = child
            
            if not material_node:
                # Find where to insert the material node
                in_node = None
                
                if output_node and output_node.inputs() and output_node.inputs()[0]:
                    in_node = output_node.inputs()[0]
                
                if output_node:
                    # Create material node
                    material_node = node.createNode("material")
                    
                    # Connect it to the network
                    if in_node:
                        material_node.setInput(0, in_node)
                        output_node.setInput(0, material_node)
                    else:
                        output_node.setInput(0, material_node)
                    
                    # Layout the node for cleanliness
                    node.layoutChildren()
            
            # Set the material path on the material node
            if material_node:
                material_path_parm = material_node.parm("shop_materialpath1")
                if material_path_parm:
                    material_path_parm.set(material.path())
            
            # Update the display flag to show the material
            if display_node:
                display_node.setDisplayFlag(False)
            
            if material_node:
                material_node.setDisplayFlag(True)
            elif output_node:
                output_node.setDisplayFlag(True)
            
            return {
                "object": node.name(),
                "material": material.name(),
                "path": material.path(),
                "color": color if color else None
            }
        except Exception as e:
            print(f"Error in set_material: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to set material: {str(e)}")

    def copy_object(self, source_path=None, source_name=None, new_name=None, position_offset=None):
        """Copy an existing object in the scene"""
        try:
            # Find the source node by path or name
            source_node = None
            
            if source_path:
                source_node = hou.node(source_path)
            elif source_name:
                obj_context = hou.node("/obj")
                for child in obj_context.children():
                    if child.name() == source_name:
                        source_node = child
                        break
            
            if not source_node:
                raise ValueError(f"Source object not found: {source_path or source_name}")
            
            # Get the parent of the source node
            parent_node = source_node.parent()
            
            # Generate a new name if none provided
            if not new_name:
                base_name = source_node.name()
                # Find a unique name
                i = 1
                while True:
                    new_name = f"{base_name}_{i}"
                    if not parent_node.node(new_name):
                        break
                    i += 1
            
            # Copy the node
            new_node = source_node.copyTo(parent_node)
            new_node.setName(new_name)
            
            # Apply position offset if provided
            if position_offset and len(position_offset) == 3:
                # Get current position
                translate_parm = new_node.parmTuple("t")
                if translate_parm:
                    current_pos = [translate_parm[0].eval(), translate_parm[1].eval(), translate_parm[2].eval()]
                    # Apply offset
                    translate_parm[0].set(current_pos[0] + float(position_offset[0]))
                    translate_parm[1].set(current_pos[1] + float(position_offset[1]))
                    translate_parm[2].set(current_pos[2] + float(position_offset[2]))
            
            # Update the internal network if it's a geometry node
            if new_node.type().name() == "geo":
                for child in new_node.children():
                    if child.type().name() == "output":
                        # Ensure the display flag is set correctly on the output node
                        child.setDisplayFlag(True)
            
            # Get the position for reporting
            position = None
            translate_parm = new_node.parmTuple("t")
            if translate_parm:
                position = [
                    translate_parm[0].eval(),
                    translate_parm[1].eval(),
                    translate_parm[2].eval()
                ]
            
            return {
                "name": new_node.name(),
                "path": new_node.path(),
                "type": new_node.type().name(),
                "source": source_node.name(),
                "position": position
            }
        except Exception as e:
            print(f"Error in copy_object: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to copy object: {str(e)}")

    def render_scene(
        self,
        output_path=None,
        resolution_x=None,
        resolution_y=None,
        camera_path=None,
        image_name=None
    ):
        """Render the current scene using OpenGL and properly package image data."""
        try:
            # Existing Houdini rendering logic, simplified here:
            render_node = hou.node("/out/render_mcp_opengl")
            if not render_node:
                render_node = hou.node("/out").createNode("opengl", "render_mcp_opengl")

            # Set output path
            if not output_path:
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                hip_dir = os.path.dirname(hou.hipFile.path())
                render_dir = os.path.join(hip_dir, "renders")
                if not os.path.exists(render_dir):
                    os.makedirs(render_dir)
                output_path = os.path.join(render_dir, f"render_{timestamp}.jpg")
            else:
                output_path = output_path
            
            picture_parm = render_node.parm("picture")
            picture_parm.set(output_path)
            camera_parm = render_node.parm("camera")
            camera_parm.set(camera_path)
            #render_node.parm("picture_format").set(0)  # Set JPEG format

            # Execute render
            render_start = time.time()
            render_node.render()
            render_duration = time.time() - render_start

            # Wait for render completion
            timeout = 10
            start_wait = time.time()
            while not (os.path.exists(output_path) and os.path.getsize(output_path) > 0):
                if time.time() - start_wait > timeout:
                    raise Exception("Render timeout.")
                time.sleep(0.5)

            # Read image and encode to base64
            with open(output_path, "rb") as f:
                image_data = f.read()

            encoded_image_data = base64.b64encode(image_data).decode('utf-8')

            # Construct response with MCP-compatible Image object explicitly
            response = {
                "rendered": True,
                "output_path": output_path,
                "resolution": [resolution_x or "default", resolution_y or "default"],
                "camera": camera_path or "default",
                "image_data": encoded_image_data,
                "image_size": len(image_data),
                "render_time": "unknown"
            }
            '''
            # Use MCP Image class correctly to package image for Claude:
            from mcp.server.fastmcp import Image as MCPImage

            mcp_image = MCPImage(data=base64.b64decode(encoded_image_data), format="jpg")

            response["mcp_image"] = mcp_image  # <-- THIS is critical!
            '''
            return response
            
        except Exception as e:
            traceback.print_exc()
            return {"error": f"Failed to render scene: {str(e)}"}


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


# UI Panel for Houdini
def create_interface():
    """Create a simple UI panel for the MCP server"""
    # This would be implemented differently based on Houdini version
    # and preferred UI approach (Python Panel, Shelf Tool, etc.)
    # For now, we'll just print instructions
    print("HoudiniMCP is available via Python scripting:")
    print("  from houdini_mcp import HoudiniMCPServer")
    print("  server = HoudiniMCPServer()")
    print("  server.start()")

# Global server instance
_mcp_server = None

def start_server(host='localhost', port=9876):
    """Start the MCP server"""
    global _mcp_server
    if _mcp_server is None:
        _mcp_server = HoudiniMCPServer(host, port)
        _mcp_server.start()
        return True
    return False

def stop_server():
    """Stop the MCP server"""
    global _mcp_server
    if _mcp_server is not None:
        _mcp_server.stop()
        _mcp_server = None
        return True
    return False

# Initialize when imported
create_interface()

# For direct execution
if __name__ == "__main__":
    # Start the server automatically when run as a script
    start_server()