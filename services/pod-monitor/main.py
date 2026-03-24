#!/usr/bin/env python3
"""
Pod Monitor MCP - HTTP API Server

This service provides an HTTP API for the Pod Monitor MCP,
allowing the Slack bot to call it remotely. Also runs a background
alerter that posts pod health issues to Slack via webhook.
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

from src.mcp import PodMonitorMCP
from src.alerter import PodAlerter


# Global instances
_mcp: PodMonitorMCP = None
_alerter: PodAlerter = None


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
        "action": "list-pods",
        "parameters": {"namespace": "default"},
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


async def alert_resolve_handler(request):
    """Resolve an active pod alert."""
    global _alerter

    if _alerter is None:
        return web.json_response({"error": "Alerter not initialized"}, status=503)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    pod = data.get("pod")
    issue = data.get("issue")
    user_id = data.get("user_id", "")

    if not pod or not issue:
        return web.json_response({"error": "Missing 'pod' or 'issue' field"}, status=400)

    found = _alerter.resolve_alert(pod, issue, user_id)
    if found:
        return web.json_response({"status": "ok", "message": f"Alert resolved for {pod}"})
    else:
        return web.json_response({"status": "not_found", "message": f"No active alert for {pod}: {issue}"}, status=404)


async def alert_pause_handler(request):
    """Pause an active pod alert for a given duration."""
    global _alerter

    if _alerter is None:
        return web.json_response({"error": "Alerter not initialized"}, status=503)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    pod = data.get("pod")
    issue = data.get("issue")
    hours = data.get("hours", 24)
    user_id = data.get("user_id", "")

    if not pod or not issue:
        return web.json_response({"error": "Missing 'pod' or 'issue' field"}, status=400)

    found = _alerter.pause_alert(pod, issue, hours, user_id)
    if found:
        return web.json_response({"status": "ok", "message": f"Alert paused for {hours}h"})
    else:
        return web.json_response({"status": "not_found", "message": f"No active alert for {pod}: {issue}"}, status=404)


async def alert_status_handler(request):
    """Return all active alert states."""
    global _alerter

    if _alerter is None:
        return web.json_response({"error": "Alerter not initialized"}, status=503)

    alerts = _alerter.get_alert_status()
    return web.json_response({"status": "ok", "alerts": alerts})


async def on_startup(app):
    """Start the background alerter on app startup."""
    global _alerter
    if _alerter:
        await _alerter.start()


async def on_cleanup(app):
    """Stop the alerter on app shutdown."""
    global _alerter
    if _alerter:
        await _alerter.stop()


async def init_app():
    """Initialize the application."""
    global _mcp, _alerter

    logger.info("Initializing Pod Monitor MCP...")
    _mcp = PodMonitorMCP()
    logger.info(f"MCP initialized: {_mcp.name}")

    # Initialize alerter (shares the same k8s client)
    _alerter = PodAlerter(_mcp.k8s)
    logger.info("Alerter initialized")

    app = web.Application()

    # Lifecycle hooks
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Routes
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", ready_handler)
    app.router.add_get("/info", info_handler)
    app.router.add_post("/execute", execute_handler)

    # Alert management endpoints (called by slack-mcp-agent button handlers)
    app.router.add_post("/alert/resolve", alert_resolve_handler)
    app.router.add_post("/alert/pause", alert_pause_handler)
    app.router.add_get("/alert/status", alert_status_handler)

    return app


def main():
    """Main entry point."""
    print("=" * 60)
    print("Pod Monitor MCP")
    print("=" * 60)

    port = int(os.environ.get("PORT", 8082))

    logger.info(f"Starting Pod Monitor MCP on port {port}...")

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
