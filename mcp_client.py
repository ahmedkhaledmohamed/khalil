"""MCP client — consume external MCP servers as Khalil capabilities.

Khalil already EXPOSES an MCP server (mcp_server.py) for Claude Code.
This module lets Khalil CONSUME external MCP servers, making every MCP
tool in the ecosystem available as a Khalil capability.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("khalil.mcp_client")

MCP_SERVERS_PATH = Path(__file__).parent / "mcp_servers.json"


@dataclass
class MCPServerConfig:
    """Configuration for an external MCP server."""
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


class MCPClient:
    """Client for a single MCP server, connected via stdio transport."""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._session: ClientSession | None = None
        self._context_manager: Any = None
        self._streams_cm: Any = None
        self._read_stream: Any = None
        self._write_stream: Any = None
        self._available = False

    @property
    def is_connected(self) -> bool:
        return self._session is not None and self._available

    async def connect(self):
        """Start the MCP server process and perform initialization handshake."""
        try:
            # Build merged env: inherit current env + config overrides
            merged_env = dict(os.environ)
            merged_env.update(self.config.env)

            server_params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=merged_env if self.config.env else None,
            )

            # stdio_client is an async context manager that yields (read, write) streams
            self._context_manager = stdio_client(server_params)
            streams = await self._context_manager.__aenter__()
            self._read_stream, self._write_stream = streams

            # Create and initialize the session
            self._streams_cm = ClientSession(self._read_stream, self._write_stream)
            self._session = await self._streams_cm.__aenter__()
            await self._session.initialize()

            self._available = True
            log.info("MCP server '%s' connected (%s)", self.config.name, self.config.command)
        except Exception as e:
            log.warning("MCP server '%s' connection failed: %s", self.config.name, e)
            self._available = False
            await self._cleanup()

    async def _cleanup(self):
        """Clean up session and context manager state."""
        if self._session and self._streams_cm:
            try:
                await self._streams_cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._session = None
        self._streams_cm = None

        if self._context_manager:
            try:
                await self._context_manager.__aexit__(None, None, None)
            except Exception:
                pass
        self._context_manager = None
        self._read_stream = None
        self._write_stream = None
        self._available = False

    async def list_tools(self) -> list[dict]:
        """Return available tools with names and descriptions."""
        if not self._session or not self._available:
            return []
        try:
            result = await self._session.list_tools()
            tools = []
            for tool in result.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                    "server": self.config.name,
                })
            return tools
        except Exception as e:
            log.warning("Failed to list tools from '%s': %s", self.config.name, e)
            return []

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Call a tool and return the result as text."""
        if not self._session or not self._available:
            raise ConnectionError(f"MCP server '{self.config.name}' is not connected")
        try:
            result = await self._session.call_tool(name, arguments)
            # Extract text from result content blocks
            parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif hasattr(block, "data"):
                    parts.append(f"[binary data: {getattr(block, 'mimeType', 'unknown')}]")
                else:
                    parts.append(str(block))
            return "\n".join(parts) if parts else "(empty result)"
        except ConnectionError:
            raise
        except Exception as e:
            raise RuntimeError(f"Tool call '{name}' on '{self.config.name}' failed: {e}") from e

    async def reconnect(self):
        """Disconnect and reconnect — use after connection failures."""
        log.info("Reconnecting MCP server '%s'...", self.config.name)
        await self.disconnect()
        await self.connect()

    async def disconnect(self):
        """Cleanly shut down the server process."""
        name = self.config.name
        await self._cleanup()
        log.info("MCP server '%s' disconnected", name)

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()
        return False


class MCPClientManager:
    """Singleton manager for all configured MCP server connections."""

    _instance: "MCPClientManager | None" = None

    def __init__(self):
        self._clients: dict[str, MCPClient] = {}
        self._configs: list[MCPServerConfig] = []
        self._cached_tools: list[dict] = []

    @classmethod
    def get_instance(cls) -> "MCPClientManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset singleton — mainly for testing."""
        cls._instance = None

    # --- Config persistence ---

    @staticmethod
    def load_configs() -> list[MCPServerConfig]:
        """Load server configurations from mcp_servers.json."""
        if not MCP_SERVERS_PATH.exists():
            return []
        try:
            data = json.loads(MCP_SERVERS_PATH.read_text())
            configs = []
            for entry in data.get("servers", []):
                configs.append(MCPServerConfig(
                    name=entry["name"],
                    command=entry["command"],
                    args=entry.get("args", []),
                    env=entry.get("env", {}),
                ))
            return configs
        except Exception as e:
            log.error("Failed to load MCP server configs: %s", e)
            return []

    @staticmethod
    def save_configs(configs: list[MCPServerConfig]):
        """Save server configurations to mcp_servers.json."""
        data = {"servers": [asdict(c) for c in configs]}
        MCP_SERVERS_PATH.write_text(json.dumps(data, indent=2) + "\n")
        log.info("Saved %d MCP server configs to %s", len(configs), MCP_SERVERS_PATH)

    # --- Client lifecycle ---

    async def initialize(self):
        """Load configs and connect to all configured servers."""
        self._configs = self.load_configs()
        if not self._configs:
            log.info("No MCP servers configured (file: %s)", MCP_SERVERS_PATH)
            return

        log.info("Initializing %d MCP server(s)...", len(self._configs))
        for config in self._configs:
            client = MCPClient(config)
            await client.connect()
            self._clients[config.name] = client

        connected = sum(1 for c in self._clients.values() if c.is_connected)
        log.info("MCP clients initialized: %d/%d connected", connected, len(self._configs))

    async def get_client(self, name: str) -> MCPClient | None:
        """Get a client by server name, lazy-connecting on first use."""
        if name in self._clients:
            client = self._clients[name]
            if not client.is_connected:
                await client.connect()
            return client if client.is_connected else None

        # Check if there's a config for this name but no client yet
        for config in self._configs:
            if config.name == name:
                client = MCPClient(config)
                await client.connect()
                self._clients[name] = client
                return client if client.is_connected else None

        return None

    async def get_all_tools(self) -> list[dict]:
        """Aggregate tools from all connected servers."""
        all_tools = []
        for name, client in self._clients.items():
            if client.is_connected:
                tools = await client.list_tools()
                all_tools.extend(tools)
        return all_tools

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> str:
        """Call a tool on a specific server, with one reconnect attempt on failure."""
        client = await self.get_client(server_name)
        if not client:
            return f"Error: MCP server '{server_name}' not found or unavailable."

        try:
            return await client.call_tool(tool_name, arguments)
        except (ConnectionError, RuntimeError) as e:
            log.warning("Tool call failed on '%s', attempting reconnect: %s", server_name, e)
            try:
                await client.reconnect()
                return await client.call_tool(tool_name, arguments)
            except Exception as retry_err:
                log.error("Tool call failed after reconnect on '%s': %s", server_name, retry_err)
                return f"Error: tool '{tool_name}' on '{server_name}' failed after retry: {retry_err}"

    def add_config(self, config: MCPServerConfig):
        """Add a new server config and save."""
        self._configs = [c for c in self._configs if c.name != config.name]
        self._configs.append(config)
        self.save_configs(self._configs)

    def remove_config(self, name: str) -> bool:
        """Remove a server config by name. Returns True if found."""
        before = len(self._configs)
        self._configs = [c for c in self._configs if c.name != name]
        if len(self._configs) < before:
            self.save_configs(self._configs)
            return True
        return False

    def get_server_status(self) -> list[dict]:
        """Get status of all configured servers."""
        statuses = []
        seen = set()
        for name, client in self._clients.items():
            statuses.append({
                "name": name,
                "command": client.config.command,
                "status": "connected" if client.is_connected else "disconnected",
            })
            seen.add(name)
        for config in self._configs:
            if config.name not in seen:
                statuses.append({
                    "name": config.name,
                    "command": config.command,
                    "status": "not started",
                })
        return statuses

    async def shutdown(self):
        """Disconnect all clients."""
        for name, client in self._clients.items():
            try:
                await client.disconnect()
            except Exception as e:
                log.warning("Error disconnecting MCP server '%s': %s", name, e)
        self._clients.clear()
        log.info("All MCP clients shut down")
