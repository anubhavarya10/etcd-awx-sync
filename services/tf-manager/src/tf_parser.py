"""Regex-based HCL parser/modifier for Terraform .tf files."""

import re
import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Match integer variable assignments: "  mim                       = 5"
INT_VAR_PATTERN = re.compile(r'^\s+([\w]+)\s*=\s*(\d+)\s*$', re.MULTILINE)

# Match boolean variable assignments: "  s2t_svc                   = false"
BOOL_VAR_PATTERN = re.compile(r'^\s+([\w]+)\s*=\s*(true|false)\s*$', re.MULTILINE)


def parse_variables(content: str) -> Dict[str, object]:
    """
    Parse all key = value assignments from a .tf file.

    Returns a dict of variable_name -> value (int or bool).
    """
    variables: Dict[str, object] = {}

    for match in INT_VAR_PATTERN.finditer(content):
        name = match.group(1)
        value = int(match.group(2))
        variables[name] = value

    for match in BOOL_VAR_PATTERN.finditer(content):
        name = match.group(1)
        value = match.group(2) == "true"
        variables[name] = value

    return variables


def get_variable(content: str, var_name: str) -> Optional[object]:
    """Get the value of a specific variable from .tf content."""
    variables = parse_variables(content)
    return variables.get(var_name)


def set_variable(content: str, var_name: str, new_value: int) -> Tuple[str, int]:
    """
    Set an integer variable to a new value, preserving alignment.

    Returns (modified_content, old_value).
    Raises ValueError if variable not found.
    """
    # Build a pattern that matches this specific variable
    pattern = re.compile(
        rf'^(\s+{re.escape(var_name)}\s*=\s*)\d+(\s*)$',
        re.MULTILINE,
    )

    match = pattern.search(content)
    if not match:
        raise ValueError(f"Variable '{var_name}' not found in file")

    old_value = int(re.search(r'\d+', match.group(0).split('=')[1]).group())
    new_content = pattern.sub(rf'\g<1>{new_value}\2', content)

    logger.info(f"Set {var_name}: {old_value} -> {new_value}")
    return new_content, old_value


def set_bool_variable(content: str, var_name: str, new_value: bool) -> str:
    """
    Set a boolean variable to a new value, preserving alignment.

    Returns modified content.
    Raises ValueError if variable not found.
    """
    val_str = "true" if new_value else "false"
    pattern = re.compile(
        rf'^(\s+{re.escape(var_name)}\s*=\s*)(true|false)(\s*)$',
        re.MULTILINE,
    )

    match = pattern.search(content)
    if not match:
        raise ValueError(f"Boolean variable '{var_name}' not found in file")

    new_content = pattern.sub(rf'\g<1>{val_str}\3', content)
    logger.info(f"Set {var_name}: {match.group(2)} -> {val_str}")
    return new_content


def find_matching_variables(content: str, prefix: str) -> Dict[str, int]:
    """
    Find all integer variables matching a prefix.

    Used for mphpp_ostack_* discovery.
    """
    variables = parse_variables(content)
    return {
        name: value
        for name, value in variables.items()
        if isinstance(value, int) and name.startswith(prefix)
    }
