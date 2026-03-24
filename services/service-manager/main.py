#!/usr/bin/env python3
"""
Service Manager MCP - HTTP API Server

This service provides an HTTP API for the Service Manager MCP,
allowing the Slack bot to call it remotely.
"""

import os
import sys
import asyncio
import logging
import json
from aiohttp import web

# Configure logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.mcp import ServiceManagerMCP


# Global MCP instance
_mcp: ServiceManagerMCP = None


async def health_handler(request):
    """Health check endpoint for K8s liveness probe."""
    return web.json_response({"status": "ok"})


async def ready_handler(request):
    """Readiness check endpoint for K8s readiness probe."""
    global _mcp

    if _mcp is None:
        return web.json_response({"status": "not_ready"}, status=503)

    try:
        healthy = await _mcp.health_check()
        if healthy:
            return web.json_response({"status": "ready"})
        else:
            return web.json_response({"status": "degraded"}, status=503)
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=503)


async def info_handler(request):
    """Return MCP info and available actions."""
    global _mcp

    if _mcp is None:
        return web.json_response({"error": "MCP not initialized"}, status=503)

    return web.json_response({
        "name": _mcp.name,
        "description": _mcp.description,
        "actions": _mcp.get_actions(),
    })


async def execute_handler(request):
    """
    Execute an MCP action.

    POST /execute
    {
        "action": "check-service",
        "parameters": {"role": "mim", "domain": "hyxd"},
        "user_id": "U123",
        "channel_id": "C456"
    }
    """
    global _mcp

    if _mcp is None:
        return web.json_response({"error": "MCP not initialized"}, status=503)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    action = data.get("action")
    if not action:
        return web.json_response({"error": "Missing 'action' field"}, status=400)

    parameters = data.get("parameters", {})
    user_id = data.get("user_id", "")
    channel_id = data.get("channel_id", "")

    logger.info(f"Executing action: {action} with params: {parameters}")

    try:
        result = await _mcp.execute(action, parameters, user_id, channel_id)
        return web.json_response(result.to_dict())
    except Exception as e:
        logger.exception(f"Error executing action {action}: {e}")
        return web.json_response({
            "status": "error",
            "message": str(e),
            "data": {},
        }, status=500)


async def init_app():
    """Initialize the application."""
    global _mcp

    logger.info("Initializing Service Manager MCP...")
    _mcp = ServiceManagerMCP()
    logger.info(f"MCP initialized: {_mcp.name}")

    app = web.Application()

    # Routes
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", ready_handler)
    app.router.add_get("/info", info_handler)
    app.router.add_post("/execute", execute_handler)

    return app


def main():
    """Main entry point."""
    print("=" * 60)
    print("Service Manager MCP")
    print("=" * 60)

    port = int(os.environ.get("PORT", 8081))

    logger.info(f"Starting Service Manager MCP on port {port}...")

    app = asyncio.get_event_loop().run_until_complete(init_app())
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.exception("Fatal error")
        sys.exit(1)
