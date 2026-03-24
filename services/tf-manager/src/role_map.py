"""Mapping from user-facing role names to Terraform variable names."""

from typing import Dict, List, Optional, Tuple


# role -> (tf_variable_prefix, required_feature_flags)
# If tf_variable_prefix is None, the role name is the same as the TF variable.
# For mphpp, the prefix is used with region suffixes (_ostack_bos_1, etc.)
ROLE_MAP: Dict[str, Tuple[Optional[str], List[str]]] = {
    "mim": (None, []),
    "mimmem": (None, []),
    "mphpp": ("mphpp_ostack", []),  # region-specific: mphpp_ostack_bos_1, etc.
    "mphhos": (None, []),
    "ts": (None, []),
    "www5": (None, []),
    "ngx": ("ngxc", []),
    "ngxint": ("ngxc", []),
    "redis": (None, []),
    "mongodb": (None, ["chat_svc"]),
    "tps": (None, ["s2t_svc"]),
    "harjo": (None, ["s2t_svc"]),
    "provnstatdb5": (None, []),
    "srouter": ("ssr_router", ["ssr_svc"]),
    "sdecoder": ("ssr_decoder", ["ssr_svc"]),
    "scapture": ("ssr_capture", ["ssr_svc"]),
    "sconductor": ("ssr_conductor", ["ssr_svc"]),
}

# Known mphpp region suffixes
MPHPP_REGIONS = ["bos_1", "bos_2", "ams_1", "sin_1"]


def get_tf_variable(role: str) -> Optional[str]:
    """
    Get the TF variable name for a role.

    Returns None for mphpp (requires region detection from the .tf file).
    For roles with a different TF name (e.g., ngx -> ngxc), returns the mapped name.
    For roles where the name matches (e.g., mim -> mim), returns the role name.
    """
    role_lower = role.lower()
    if role_lower not in ROLE_MAP:
        return None

    prefix, _ = ROLE_MAP[role_lower]
    if role_lower == "mphpp":
        return None  # Caller must use get_mphpp_variables()
    return prefix if prefix else role_lower


def get_mphpp_variables() -> List[str]:
    """Get all possible mphpp TF variable names."""
    return [f"mphpp_ostack_{region}" for region in MPHPP_REGIONS]


def get_required_feature_flags(role: str) -> List[str]:
    """Get feature flags required for a role to function."""
    role_lower = role.lower()
    if role_lower not in ROLE_MAP:
        return []
    _, flags = ROLE_MAP[role_lower]
    return flags


def is_valid_role(role: str) -> bool:
    """Check if a role is valid."""
    return role.lower() in ROLE_MAP


def list_roles() -> List[str]:
    """List all supported roles."""
    return sorted(ROLE_MAP.keys())
