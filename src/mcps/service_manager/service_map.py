"""Role to service mapping for the Service Manager MCP."""

from typing import Optional

# Mapping of roles to their primary service names
# Some services use {domain} placeholder which gets replaced at runtime
ROLE_SERVICE_MAP = {
    "mphpp": "morpheus",
    "mim": "mongooseim",
    "www5": "{domain}_backend_api@6690.service",
    "www": "{domain}_backend_api@6690.service",
    "mphhos": "mongooseim",
    "ts": "{domain}_token_store@666*.service",
    "mimmem": "memcached",
    "provnstatdb5": "postgresql-9.2",
    "ngx": "nginx",
    "redis": "redis",
    "tps": "transcription",
    "harjo": "harjo",
    "hamim": "hamim",
    "haweb": "haweb",
    "srouter": "srouter",
    "sdecoder": "sdecoder",
    "ngxint": "nginx",
    "mongodb": "mongod",
    "scapture": "scapture",
    "ser": "ser",
    "sconductor": "sconductor",
}

# Additional service aliases (role might have multiple services)
ROLE_ADDITIONAL_SERVICES = {
    "mim": ["ejabberd"],
    "ngx": ["nginx.service"],
    "redis": ["redis-server", "redis.service"],
    "mongodb": ["mongodb", "mongod.service"],
}


def get_service_name(role: str, domain: Optional[str] = None) -> str:
    """
    Get the service name for a given role.

    Args:
        role: The role name (e.g., 'mim', 'mphpp', 'ts')
        domain: The domain name, used for services with dynamic names

    Returns:
        The service name to check/manage
    """
    role_lower = role.lower()
    service = ROLE_SERVICE_MAP.get(role_lower)

    if service is None:
        # Default to role name as service name
        return role_lower

    # Replace {domain} placeholder if present
    if "{domain}" in service and domain:
        service = service.replace("{domain}", domain)

    return service


def get_all_services_for_role(role: str, domain: Optional[str] = None) -> list:
    """
    Get all possible service names for a role (primary + alternatives).

    Args:
        role: The role name
        domain: The domain name

    Returns:
        List of service names to try
    """
    role_lower = role.lower()
    services = [get_service_name(role_lower, domain)]

    # Add additional services if defined
    additional = ROLE_ADDITIONAL_SERVICES.get(role_lower, [])
    services.extend(additional)

    return services


def list_supported_roles() -> list:
    """Return list of all supported roles."""
    return sorted(ROLE_SERVICE_MAP.keys())
