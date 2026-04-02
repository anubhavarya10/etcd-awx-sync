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
from src.mcps.remote import create_remote_mcp


# Global agent reference for health checks
_agent: SlackMCPAgent = None


async def health_handler(request):
    """Health check endpoint for K8s liveness probe."""
    return web.Response(text="OK", status=200)


async def ready_handler(request):
    """Readiness check endpoint for K8s readiness probe.

    Kept lightweight — only checks that the agent is initialized.
    Remote MCP health is NOT checked here to avoid cascading
    HTTP calls that can exceed the probe timeout.
    """
    global _agent

    if _agent is None:
        return web.Response(text="Agent not initialized", status=503)

    return web.Response(text="Ready", status=200)


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

    # Register remote MCPs from environment
    # Format: REMOTE_MCP_<NAME>=<URL>
    # Example: REMOTE_MCP_SERVICE_MANAGER=http://service-manager:8081
    # Optional timeout: REMOTE_MCP_<NAME>_TIMEOUT=<seconds>
    remote_mcps = []
    for key, value in os.environ.items():
        if key.startswith("REMOTE_MCP_") and not key.endswith("_TIMEOUT"):
            mcp_name = key.replace("REMOTE_MCP_", "").lower().replace("_", "-")
            mcp_url = value
            # Check for per-MCP timeout override
            timeout_key = f"{key}_TIMEOUT"
            timeout = int(os.environ.get(timeout_key, "60"))
            remote_mcps.append((mcp_name, mcp_url, timeout))

    for mcp_name, mcp_url, timeout in remote_mcps:
        try:
            logger.info(f"Connecting to remote MCP: {mcp_name} at {mcp_url} (timeout: {timeout}s)")
            remote_mcp = await create_remote_mcp(mcp_name, mcp_url, timeout=timeout)
            if remote_mcp:
                register_mcp(remote_mcp)
                logger.info(f"Registered remote MCP: {mcp_name}")
            else:
                logger.warning(f"Failed to connect to remote MCP: {mcp_name}")
        except Exception as e:
            logger.error(f"Failed to register remote MCP {mcp_name}: {e}")

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
