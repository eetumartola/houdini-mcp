# HoudiniMCP - Houdini Model Context Protocol Integration

HoudiniMCP connects Houdini to Claude AI through the Model Context Protocol (MCP), allowing Claude to directly interact with and control Houdini. This integration enables prompt-assisted 3D modeling, scene creation, and manipulation within Houdini.

## Features

- **Two-way communication**: Connect Claude AI to Houdini through a socket-based server
- **Object manipulation**: Create, modify, and delete 3D objects in Houdini
- **Material control**: Apply and modify materials and colors
- **Scene inspection**: Get detailed information about the current Houdini scene
- **Code execution**: Run arbitrary Python code in Houdini from Claude
- **Rendering**: Trigger renders directly from Claude

## Components

The system consists of two main components:

1. **Houdini Extension (`houdini_mcp.py`)**: A Python module that creates a socket server within Houdini to receive and execute commands
2. **MCP Server (`houdini_mcp_server.py`)**: A Python server that implements the Model Context Protocol and connects to the Houdini extension

## Installation

### Prerequisites

- Houdini 19.0 or newer
- Python 3.10 or newer

### Claude Desktop Integration

To connect Claude to Houdini:

1. Open Claude Desktop
2. Go to Claude > Settings > Developer > Edit Config
3. Update `claude_desktop_config.json` to include:

```json
{
    "mcpServers": {
        "houdini": {
            "command": "py",
            "args": [
                "E:/code/houdini-mcp/houdini_mcp_server.py"
            ]
        }
    }
}
```
4. Edit the path to match your location for houdini_mcp_server.py

### Installing the Houdini Extension

Copy houdini_mcp.py to your houdini prefs folder python3.11libs/ subfolder

## Usage

### Starting the Connection

1. In Houdini, run the following Python code:
```python
import houdini_mcp
houdini_mcp.start_server()
```

You should see a message confirming that the server has started.

2. Make sure the MCP server is running from Claude.

### Using with Claude

Once the configuration is set up in Claude, and the Houdini extension is running, you will see a hammer icon with tools for the Houdini MCP.

#### Available Tools

- `get_scene_info` - Gets information about the current scene
- `get_object_info` - Gets detailed information for a specific object
- `create_object` - Create a new object with parameters
- `modify_object` - Modify an existing object's properties
- `delete_object` - Remove an object from the scene
- `set_material` - Apply or create materials for objects
- `execute_houdini_code` - Run any Python code in Houdini
- `render_scene` - Render the current scene

### Example Commands

Here are some examples of what you can ask Claude to do:

- "Create a procedural landscape in Houdini"
- "Add a red sphere to the scene and place it above the grid"
- "Create a box and apply a checkerboard texture to it"
- "Get information about the current Houdini scene"
- "Set up a basic lighting rig with three-point lighting"
- "Render the current scene at 1080p resolution"

## Troubleshooting

- **Connection issues**: Make sure the Houdini extension server is running, and the MCP server is configured in Claude.
- **Timeout errors**: Try simplifying your requests or breaking them into smaller steps.
- **Have you tried turning it off and on again?**: If you're still having connection errors, try restarting both Claude and the Houdini server.

## Technical Details

### Communication Protocol

The system uses a simple JSON-based protocol over TCP sockets


### Houdini Implementation Notes

Unlike Blender, Houdini doesn't have a built-in event loop, so we use a separate thread for the socket server. The commands are executed in the main Houdini thread to ensure compatibility with Houdini's threading model.

## Limitations & Security Considerations

- The `execute_houdini_code` tool allows running arbitrary Python code in Houdini, which can be powerful but potentially dangerous. Use with caution in production environments. ALWAYS save your work before using it.
- Complex operations might need to be broken down into smaller steps.
- This implementation has limited error handling - always save your work before using it for complex tasks.

## Acknowledgements

This was originally based on ahujasid's blender-mcp https://github.com/ahujasid/blender-mcp/

## Disclaimer

This is a third-party integration and not made by SideFX Software, the creators of Houdini.
