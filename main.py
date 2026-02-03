#!/usr/bin/env python3
"""
Main entry point for the Slack MCP Agent.
"""

import os
import sys
import asyncio
import logging
from aiohttp import web

# Configure logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Load environment variables from .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src.agent import create_agent_from_env, SlackMCPAgent
from src.mcps import register_mcp
from src.mcps.etcd_awx import EtcdAwxMCP
from src.mcps.awx_playbook import AwxPlaybookMCP


# Global agent reference for health checks
_agent: SlackMCPAgent = None


async def health_handler(request):
    """Health check endpoint for K8s liveness probe."""
    return web.Response(text="OK", status=200)


async def ready_handler(request):
    """Readiness check endpoint for K8s readiness probe."""
    global _agent

    if _agent is None:
        return web.Response(text="Agent not initialized", status=503)

    try:
        health = await _agent.health_check()
        if health["status"] == "healthy":
            return web.Response(text="Ready", status=200)
        else:
            return web.Response(
                text=f"Degraded: {health}",
                status=503
            )
    except Exception as e:
        return web.Response(text=f"Error: {e}", status=503)


async def start_health_server():
    """Start the health check HTTP server."""
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", ready_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("HEALTH_PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info(f"Health check server started on port {port}")


async def main():
    """Main entry point."""
    global _agent

    print("=" * 60)
    print("Slack MCP Agent")
    print("=" * 60)

    # Validate required environment variables
    required_vars = ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    # Check for LLM configuration
    llm_provider = os.environ.get("LLM_PROVIDER", "unity")
    if llm_provider == "unity" and not os.environ.get("UNITY_AI_API_KEY"):
        logger.warning("UNITY_AI_API_KEY not set, falling back to mock LLM client")
        os.environ["LLM_PROVIDER"] = "mock"
    elif llm_provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY not set, falling back to mock LLM client")
        os.environ["LLM_PROVIDER"] = "mock"

    # Register MCPs
    logger.info("Registering MCPs...")

    # Register etcd-awx-sync MCP
    try:
        etcd_awx_mcp = EtcdAwxMCP()
        register_mcp(etcd_awx_mcp)
        logger.info(f"Registered MCP: {etcd_awx_mcp.name}")
    except Exception as e:
        logger.error(f"Failed to register etcd-awx-sync MCP: {e}")
        # Continue without this MCP

    # Register awx-playbook MCP
    try:
        awx_playbook_mcp = AwxPlaybookMCP()
        register_mcp(awx_playbook_mcp)
        logger.info(f"Registered MCP: {awx_playbook_mcp.name}")
    except Exception as e:
        logger.error(f"Failed to register awx-playbook MCP: {e}")
        # Continue without this MCP

    # Create agent
    logger.info("Creating Slack agent...")
    _agent = create_agent_from_env()

    # Start health check server
    await start_health_server()

    # Start the agent
    logger.info("Starting Slack agent...")
    await _agent.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.exception("Fatal error")
        sys.exit(1)
