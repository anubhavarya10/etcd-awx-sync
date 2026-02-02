"""LLM client for intent parsing and natural language understanding."""

import os
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)


@dataclass
class IntentResult:
    """Result of intent parsing."""
    mcp_name: str
    action: str
    parameters: Dict[str, Any]
    confidence: float
    explanation: str
    raw_response: Optional[str] = None


@dataclass
class LLMResponse:
    """Generic LLM response."""
    content: str
    model: str
    usage: Optional[Dict[str, int]] = None


class BaseLLMClient(ABC):
    """Base class for LLM clients."""

    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Send a completion request to the LLM."""
        pass

    async def parse_intent(
        self,
        user_message: str,
        mcp_context: str,
    ) -> IntentResult:
        """
        Parse user intent using the LLM.

        Args:
            user_message: The user's natural language request
            mcp_context: Context about available MCPs and actions

        Returns:
            IntentResult with extracted intent
        """
        system_prompt = f"""You are an AI assistant that helps route user requests to the correct MCP (Model Context Protocol) and action.

Your job is to:
1. Understand the user's request
2. Identify which MCP should handle it
3. Extract the action and parameters

{mcp_context}

IMPORTANT: Respond ONLY with valid JSON in this exact format:
{{
    "mcp_name": "name of the MCP to use",
    "action": "action name to execute",
    "parameters": {{"param1": "value1", "param2": "value2"}},
    "confidence": 0.95,
    "explanation": "brief explanation of why you chose this"
}}

If you cannot determine the intent or no MCP matches, respond with:
{{
    "mcp_name": "unknown",
    "action": "help",
    "parameters": {{}},
    "confidence": 0.0,
    "explanation": "Could not determine intent"
}}

Do not include any text outside the JSON object."""

        response = await self.complete(
            system_prompt=system_prompt,
            user_message=user_message,
            temperature=0.0,
        )

        try:
            # Parse JSON from response
            content = response.content.strip()
            # Handle potential markdown code blocks
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            data = json.loads(content)

            return IntentResult(
                mcp_name=data.get("mcp_name", "unknown"),
                action=data.get("action", "help"),
                parameters=data.get("parameters", {}),
                confidence=data.get("confidence", 0.0),
                explanation=data.get("explanation", ""),
                raw_response=response.content,
            )
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON: {e}")
            logger.error(f"Raw response: {response.content}")
            return IntentResult(
                mcp_name="unknown",
                action="help",
                parameters={},
                confidence=0.0,
                explanation=f"Failed to parse intent: {str(e)}",
                raw_response=response.content,
            )

    async def generate_response(
        self,
        context: str,
        user_message: str,
    ) -> str:
        """
        Generate a conversational response.

        Args:
            context: Context about the current state
            user_message: User's message

        Returns:
            Generated response text
        """
        system_prompt = f"""You are a helpful Slack bot assistant that manages infrastructure operations.
Be concise and friendly. Use Slack markdown formatting when helpful.

{context}"""

        response = await self.complete(
            system_prompt=system_prompt,
            user_message=user_message,
            temperature=0.7,
        )
        return response.content


class UnityAIClient(BaseLLMClient):
    """
    LLM client for Unity AI.
    Unity AI typically provides an OpenAI-compatible API endpoint.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        model: str = "claude-3-sonnet",
    ):
        self.api_key = api_key or os.environ.get("UNITY_AI_API_KEY")
        self.api_base = api_base or os.environ.get(
            "UNITY_AI_API_BASE",
            "https://api.unity.ai/v1"
        )
        self.model = model or os.environ.get("UNITY_AI_MODEL", "claude-3-sonnet")

        if not self.api_key:
            raise ValueError("UNITY_AI_API_KEY is required")

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Send completion request to Unity AI."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    "temperature": temperature,
                    "max_tokens": 2048,
                },
            )
            response.raise_for_status()
            data = response.json()

            return LLMResponse(
                content=data["choices"][0]["message"]["content"],
                model=data.get("model", self.model),
                usage=data.get("usage"),
            )


class AnthropicClient(BaseLLMClient):
    """LLM client for direct Anthropic API access."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-3-sonnet-20240229",
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-3-sonnet-20240229")
        self.api_base = "https://api.anthropic.com/v1"

        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is required")

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Send completion request to Anthropic API."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.api_base}/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "system": system_prompt,
                    "messages": [
                        {"role": "user", "content": user_message},
                    ],
                    "temperature": temperature,
                    "max_tokens": 2048,
                },
            )
            response.raise_for_status()
            data = response.json()

            return LLMResponse(
                content=data["content"][0]["text"],
                model=data.get("model", self.model),
                usage={
                    "input_tokens": data.get("usage", {}).get("input_tokens"),
                    "output_tokens": data.get("usage", {}).get("output_tokens"),
                },
            )


class MockLLMClient(BaseLLMClient):
    """Mock LLM client for testing without API calls."""

    def __init__(self, default_response: Optional[str] = None):
        self.default_response = default_response

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Return a mock response based on keywords."""
        user_lower = user_message.lower()

        # Status command
        if "status" in user_lower:
            return LLMResponse(
                content=json.dumps({
                    "mcp_name": "etcd-awx-sync",
                    "action": "status",
                    "parameters": {},
                    "confidence": 0.95,
                    "explanation": "User wants to see status"
                }),
                model="mock",
            )

        # Create inventory with role/domain
        if "create" in user_lower or ("for" in user_lower and "xp" in user_lower):
            role = None
            domain = None

            # Known roles
            known_roles = ["mphpp", "mim", "ts", "www", "mphhos", "hamim", "os", "db", "web"]
            for word in user_lower.replace(",", " ").split():
                if word in known_roles:
                    role = word
                if "xp" in word:
                    domain = word

            if role or domain:
                params = {}
                if role:
                    params["role"] = role
                if domain:
                    params["domain"] = domain

                return LLMResponse(
                    content=json.dumps({
                        "mcp_name": "etcd-awx-sync",
                        "action": "create",
                        "parameters": params,
                        "confidence": 0.9,
                        "explanation": f"User wants to create inventory"
                    }),
                    model="mock",
                )

        # Full sync
        if "sync" in user_lower:
            return LLMResponse(
                content=json.dumps({
                    "mcp_name": "etcd-awx-sync",
                    "action": "sync",
                    "parameters": {},
                    "confidence": 0.9,
                    "explanation": "User wants to run a full sync"
                }),
                model="mock",
            )

        # Handle "how many" questions
        if "how many" in user_lower:
            words = user_lower.replace(",", " ").replace("?", "").split()
            known_roles = ["mphpp", "mim", "ts", "www", "mphhos", "hamim", "os", "db", "web", "ngx", "mimmem", "haproxy", "redis", "srouter"]

            role = None
            domain = None
            for word in words:
                if word in known_roles:
                    role = word
                if word.endswith("xp") or word.endswith("xs") or (word.endswith("p") and len(word) > 3 and word not in ["help"]):
                    domain = word

            # "how many mphpp does bnxp have" -> count role in domain
            if role and domain:
                return LLMResponse(
                    content=json.dumps({
                        "mcp_name": "etcd-awx-sync",
                        "action": "count",
                        "parameters": {"role": role, "domain": domain},
                        "confidence": 0.95,
                        "explanation": f"Count {role} servers in {domain}"
                    }),
                    model="mock",
                )

            # "how many hosts does lolxp have" -> total hosts in domain
            if domain and ("host" in user_lower or "server" in user_lower or "total" in user_lower):
                return LLMResponse(
                    content=json.dumps({
                        "mcp_name": "etcd-awx-sync",
                        "action": "count",
                        "parameters": {"domain": domain},
                        "confidence": 0.95,
                        "explanation": f"Count total hosts in {domain}"
                    }),
                    model="mock",
                )

            # "how many domains have ngx" -> count domains with role
            if role and ("domain" in user_lower):
                return LLMResponse(
                    content=json.dumps({
                        "mcp_name": "etcd-awx-sync",
                        "action": "count-domains",
                        "parameters": {"role": role},
                        "confidence": 0.95,
                        "explanation": f"Count domains with {role}"
                    }),
                    model="mock",
                )

        if "list" in user_lower and "domain" in user_lower:
            params = {}
            words = user_lower.replace(",", " ").split()
            known_roles = ["mphpp", "mim", "ts", "www", "mphhos", "hamim", "os", "db", "web", "ngx", "mimmem", "haproxy", "redis", "srouter"]
            for word in words:
                if word in known_roles:
                    params["role"] = word
                    break
            return LLMResponse(
                content=json.dumps({
                    "mcp_name": "etcd-awx-sync",
                    "action": "list-domains",
                    "parameters": params,
                    "confidence": 0.95,
                    "explanation": "User wants to list available domains"
                }),
                model="mock",
            )

        if ("list" in user_lower and "role" in user_lower) or ("roles" in user_lower and ("in" in user_lower or "for" in user_lower or "does" in user_lower or "have" in user_lower)):
            params = {}
            words = user_lower.replace(",", " ").split()
            for word in words:
                if word.endswith("xp") or word.endswith("xs") or (word.endswith("p") and len(word) > 3):
                    if word not in ["help", "xp"]:
                        params["domain"] = word
                        break
            return LLMResponse(
                content=json.dumps({
                    "mcp_name": "etcd-awx-sync",
                    "action": "list-roles",
                    "parameters": params,
                    "confidence": 0.95,
                    "explanation": "User wants to list available roles"
                }),
                model="mock",
            )

        if "help" in user_lower:
            return LLMResponse(
                content=json.dumps({
                    "mcp_name": "unknown",
                    "action": "help",
                    "parameters": {},
                    "confidence": 0.8,
                    "explanation": "User is asking for help"
                }),
                model="mock",
            )

        return LLMResponse(
            content=json.dumps({
                "mcp_name": "unknown",
                "action": "help",
                "parameters": {},
                "confidence": 0.0,
                "explanation": "Could not determine intent from message"
            }),
            model="mock",
        )


def create_llm_client(
    provider: Optional[str] = None,
    **kwargs,
) -> BaseLLMClient:
    """
    Factory function to create an LLM client.

    Args:
        provider: LLM provider ('unity', 'anthropic', 'mock')
        **kwargs: Provider-specific configuration

    Returns:
        Configured LLM client instance
    """
    provider = provider or os.environ.get("LLM_PROVIDER", "unity")

    if provider == "unity":
        return UnityAIClient(**kwargs)
    elif provider == "anthropic":
        return AnthropicClient(**kwargs)
    elif provider == "mock":
        return MockLLMClient(**kwargs)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")
