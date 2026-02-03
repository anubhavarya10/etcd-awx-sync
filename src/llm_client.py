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
        """Return a mock response based on sentence patterns (dynamic, no hardcoded lists)."""
        user_lower = user_message.lower().strip()
        # Clean up the message - remove punctuation except hyphens
        import re
        words = re.findall(r'[\w-]+', user_lower)

        # Skip common words when looking for role/domain
        skip_words = {
            'how', 'many', 'does', 'have', 'has', 'the', 'a', 'an', 'in', 'for',
            'what', 'which', 'show', 'list', 'get', 'find', 'count', 'total',
            'all', 'are', 'is', 'there', 'do', 'can', 'please', 'me', 'i',
            'want', 'need', 'would', 'like', 'to', 'see', 'hosts', 'servers',
            'host', 'server', 'domain', 'domains', 'role', 'roles', 'inventory',
            'create', 'sync', 'status', 'help', 'with', 'of', 'from', 'and', 'or'
        }

        # Extract potential role/domain - any word that's not a common word
        potential_terms = [w for w in words if w not in skip_words and len(w) >= 2]

        # === PLAYBOOK COMMANDS ===

        # List playbooks
        if ("list" in user_lower or "show" in user_lower) and "playbook" in user_lower:
            return LLMResponse(
                content=json.dumps({
                    "mcp_name": "awx-playbook",
                    "action": "list-playbooks",
                    "parameters": {},
                    "confidence": 0.95,
                    "explanation": "User wants to list available playbooks"
                }),
                model="mock",
            )

        # Show repo config - "show repo", "current repo", "playbook repo"
        if ("show" in user_lower or "current" in user_lower) and "repo" in user_lower:
            return LLMResponse(
                content=json.dumps({
                    "mcp_name": "awx-playbook",
                    "action": "show-repo",
                    "parameters": {},
                    "confidence": 0.95,
                    "explanation": "User wants to see current repo configuration"
                }),
                model="mock",
            )

        # Set repo - "set repo <org/repo> [path <folder>] [branch <branch>]"
        if ("set" in user_lower or "change" in user_lower or "use" in user_lower) and "repo" in user_lower:
            params = {}
            # Find repo (contains /)
            for term in potential_terms:
                if "/" in term:
                    params["repo"] = term
                    break

            # Find path (after "path" keyword)
            if "path" in words:
                path_idx = words.index("path")
                if path_idx + 1 < len(words):
                    params["path"] = words[path_idx + 1]

            # Find branch (after "branch" keyword)
            if "branch" in words:
                branch_idx = words.index("branch")
                if branch_idx + 1 < len(words):
                    params["branch"] = words[branch_idx + 1]

            return LLMResponse(
                content=json.dumps({
                    "mcp_name": "awx-playbook",
                    "action": "set-repo",
                    "parameters": params,
                    "confidence": 0.9,
                    "explanation": f"User wants to set playbook repo: {params}"
                }),
                model="mock",
            )

        # List jobs
        if ("list" in user_lower or "show" in user_lower) and "job" in user_lower:
            params = {}
            if potential_terms:
                params["inventory"] = potential_terms[0]
            return LLMResponse(
                content=json.dumps({
                    "mcp_name": "awx-playbook",
                    "action": "list-jobs",
                    "parameters": params,
                    "confidence": 0.95,
                    "explanation": "User wants to list recent jobs"
                }),
                model="mock",
            )

        # Job status - "job status 123" or "status of job 123"
        if "job" in user_lower and ("status" in user_lower or "check" in user_lower):
            # Look for a job ID (numeric)
            job_id = None
            for term in potential_terms:
                if term.isdigit():
                    job_id = term
                    break
            if job_id:
                return LLMResponse(
                    content=json.dumps({
                        "mcp_name": "awx-playbook",
                        "action": "job-status",
                        "parameters": {"job_id": job_id},
                        "confidence": 0.95,
                        "explanation": f"Check status of job {job_id}"
                    }),
                    model="mock",
                )

        # Job output - "job output 123" or "show output of job 123"
        if "job" in user_lower and ("output" in user_lower or "log" in user_lower or "result" in user_lower):
            job_id = None
            for term in potential_terms:
                if term.isdigit():
                    job_id = term
                    break
            if job_id:
                return LLMResponse(
                    content=json.dumps({
                        "mcp_name": "awx-playbook",
                        "action": "job-output",
                        "parameters": {"job_id": job_id},
                        "confidence": 0.95,
                        "explanation": f"Get output of job {job_id}"
                    }),
                    model="mock",
                )

        # Run playbook - "run <playbook> on <inventory>"
        if "run" in user_lower and ("playbook" in user_lower or ".yml" in user_lower or ".yaml" in user_lower):
            params = {}
            # Look for playbook name (contains .yml or .yaml, or term after "run")
            playbook_name = None
            inventory_name = None

            # Find terms - playbook is usually before "on", inventory after
            on_idx = words.index("on") if "on" in words else None

            for term in potential_terms:
                if ".yml" in term or ".yaml" in term:
                    playbook_name = term
                    break

            # If no .yml found, look for term after "run"
            if not playbook_name:
                run_idx = words.index("run") if "run" in words else None
                if run_idx is not None and run_idx + 1 < len(words):
                    next_term = words[run_idx + 1]
                    if next_term not in skip_words and next_term != "playbook":
                        playbook_name = next_term

            # Find inventory (term after "on")
            if on_idx is not None:
                terms_after_on = [w for w in words[on_idx+1:] if w in potential_terms or "-" in w]
                if terms_after_on:
                    inventory_name = terms_after_on[0]

            if playbook_name:
                params["playbook"] = playbook_name
            if inventory_name:
                params["inventory"] = inventory_name

            if params:
                return LLMResponse(
                    content=json.dumps({
                        "mcp_name": "awx-playbook",
                        "action": "run-playbook",
                        "parameters": params,
                        "confidence": 0.9,
                        "explanation": f"Run playbook: {params}"
                    }),
                    model="mock",
                )

        # === INVENTORY COMMANDS ===

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

        # Full sync
        if "sync" in user_lower and "all" in user_lower:
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

        # Handle "how many" questions - dynamic parsing
        if "how many" in user_lower:
            # Pattern: "how many X does Y have" -> X=role, Y=domain
            # Pattern: "how many hosts does Y have" -> Y=domain (count all)
            # Pattern: "how many domains have X" -> X=role (count domains)

            if "domain" in user_lower and ("have" in user_lower or "has" in user_lower):
                # "how many domains have X" -> count domains with role X
                if potential_terms:
                    return LLMResponse(
                        content=json.dumps({
                            "mcp_name": "etcd-awx-sync",
                            "action": "count-domains",
                            "parameters": {"role": potential_terms[0]},
                            "confidence": 0.95,
                            "explanation": f"Count domains with {potential_terms[0]}"
                        }),
                        model="mock",
                    )

            if ("host" in user_lower or "server" in user_lower) and ("does" in user_lower or "in" in user_lower):
                # "how many hosts does Y have" -> count all hosts in domain Y
                if potential_terms:
                    return LLMResponse(
                        content=json.dumps({
                            "mcp_name": "etcd-awx-sync",
                            "action": "count",
                            "parameters": {"domain": potential_terms[0]},
                            "confidence": 0.95,
                            "explanation": f"Count total hosts in {potential_terms[0]}"
                        }),
                        model="mock",
                    )

            # "how many X does Y have" or "how many X in Y" -> count X (role) in Y (domain)
            if len(potential_terms) >= 2:
                # First term is likely the role, second is likely the domain
                role = potential_terms[0]
                domain = potential_terms[1]
                return LLMResponse(
                    content=json.dumps({
                        "mcp_name": "etcd-awx-sync",
                        "action": "count",
                        "parameters": {"role": role, "domain": domain},
                        "confidence": 0.95,
                        "explanation": f"Count {role} in {domain}"
                    }),
                    model="mock",
                )

            # Single term - could be role or domain, let MCP figure it out
            if len(potential_terms) == 1:
                return LLMResponse(
                    content=json.dumps({
                        "mcp_name": "etcd-awx-sync",
                        "action": "count",
                        "parameters": {"role": potential_terms[0]},
                        "confidence": 0.8,
                        "explanation": f"Count {potential_terms[0]}"
                    }),
                    model="mock",
                )

        # List domains
        if "list" in user_lower and "domain" in user_lower:
            params = {}
            # If there's a term, it might be filtering by role
            if potential_terms:
                params["role"] = potential_terms[0]
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

        # List roles
        if "list" in user_lower and "role" in user_lower:
            params = {}
            # If there's a term, it might be filtering by domain
            if potential_terms:
                params["domain"] = potential_terms[0]
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

        # "roles in X" or "what roles does X have"
        if ("role" in user_lower or "roles" in user_lower) and potential_terms:
            return LLMResponse(
                content=json.dumps({
                    "mcp_name": "etcd-awx-sync",
                    "action": "list-roles",
                    "parameters": {"domain": potential_terms[0]},
                    "confidence": 0.9,
                    "explanation": f"List roles in {potential_terms[0]}"
                }),
                model="mock",
            )

        # Update inventory - handle "update" or "refresh" commands
        if "update" in user_lower or "refresh" in user_lower:
            params = {}

            # Extract inventory name or role/domain from potential terms
            if len(potential_terms) >= 2:
                # Could be "update mim-nwxp" or "update mim for nwxp"
                # Check if first term contains a dash (inventory name)
                if "-" in potential_terms[0]:
                    params["inventory_name"] = potential_terms[0]
                else:
                    params["role"] = potential_terms[0]
                    params["domain"] = potential_terms[1]
            elif len(potential_terms) == 1:
                # Single term - could be inventory name or role
                if "-" in potential_terms[0]:
                    params["inventory_name"] = potential_terms[0]
                else:
                    params["role"] = potential_terms[0]

            return LLMResponse(
                content=json.dumps({
                    "mcp_name": "etcd-awx-sync",
                    "action": "update",
                    "parameters": params,
                    "confidence": 0.9,
                    "explanation": f"Update inventory: {params}"
                }),
                model="mock",
            )

        # Create inventory - handle "X for Y" pattern explicitly
        # Pattern: "mphpp for mt1s" or "create inventory for mphpp in mt1s"
        if "for" in words or "in" in words or "create" in user_lower:
            params = {}

            # Find the position of "for" or "in" to split role and domain
            for_idx = None
            in_idx = None
            if "for" in words:
                for_idx = words.index("for")
            if "in" in words:
                in_idx = words.index("in")

            # Use the first occurrence of "for" or "in" as separator
            sep_idx = for_idx if for_idx is not None else in_idx

            if sep_idx is not None and len(potential_terms) >= 2:
                # Get terms before and after separator
                terms_before_sep = [w for w in words[:sep_idx] if w in potential_terms]
                terms_after_sep = [w for w in words[sep_idx+1:] if w in potential_terms]

                if terms_before_sep and terms_after_sep:
                    params["role"] = terms_before_sep[-1]  # Last term before separator is role
                    params["domain"] = terms_after_sep[0]   # First term after separator is domain
                elif terms_before_sep:
                    params["role"] = terms_before_sep[-1]
                elif terms_after_sep:
                    # If only terms after, could be "create inventory for mphpp"
                    if len(terms_after_sep) >= 2:
                        params["role"] = terms_after_sep[0]
                        params["domain"] = terms_after_sep[1]
                    else:
                        params["role"] = terms_after_sep[0]
            elif len(potential_terms) >= 2:
                # Fallback: first term is role, second is domain
                params["role"] = potential_terms[0]
                params["domain"] = potential_terms[1]
            elif len(potential_terms) == 1:
                params["role"] = potential_terms[0]

            if params:
                explanation = f"Create inventory: role={params.get('role', 'all')}, domain={params.get('domain', 'all')}"
                return LLMResponse(
                    content=json.dumps({
                        "mcp_name": "etcd-awx-sync",
                        "action": "create",
                        "parameters": params,
                        "confidence": 0.9,
                        "explanation": explanation
                    }),
                    model="mock",
                )

        # Sync command
        if "sync" in user_lower:
            return LLMResponse(
                content=json.dumps({
                    "mcp_name": "etcd-awx-sync",
                    "action": "sync",
                    "parameters": {},
                    "confidence": 0.9,
                    "explanation": "User wants to run a sync"
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
