#!/usr/bin/env python3
"""
Playbook Validator

Validates Ansible playbooks against team standards to ensure uniformity
and compatibility with AWX infrastructure.

Usage:
    python playbook_validator.py playbooks/*.yml
    python playbook_validator.py --strict playbooks/myplaybook.yml

Exit codes:
    0 - All checks passed
    1 - Validation errors found
    2 - Warnings found (non-strict mode passes)
"""

import argparse
import sys
import re
from pathlib import Path
from typing import List, Dict, Any, Tuple
import yaml


# Available collections in our Execution Environment
AVAILABLE_COLLECTIONS = {
    'ansible.builtin',
    'community.general',
    'community.docker',
    'community.crypto',
    'ansible.posix',
}

# Common built-in modules (don't need FQCN)
BUILTIN_MODULES = {
    'apt', 'yum', 'dnf', 'package', 'pip',
    'copy', 'template', 'file', 'lineinfile', 'blockinfile',
    'command', 'shell', 'script', 'raw',
    'service', 'systemd',
    'user', 'group',
    'get_url', 'uri',
    'git', 'unarchive',
    'debug', 'fail', 'assert', 'meta',
    'set_fact', 'include_vars', 'include_tasks', 'import_tasks',
    'include_role', 'import_role',
    'wait_for', 'pause',
    'stat', 'find',
    'fetch', 'slurp',
    'cron',
    'hostname',
    'reboot',
    'gather_facts', 'setup',
    'add_host', 'group_by',
    'async_status',
    'include', 'import_playbook',
}

# Modules that require specific collections
COLLECTION_MODULES = {
    'docker_container': 'community.docker',
    'docker_image': 'community.docker',
    'docker_network': 'community.docker',
    'docker_volume': 'community.docker',
    'docker_compose': 'community.docker',
    'snap': 'community.general',
    'ufw': 'community.general',
    'nmcli': 'community.general',
    'modprobe': 'community.general',
    'sysctl': 'ansible.posix',
    'mount': 'ansible.posix',
    'selinux': 'ansible.posix',
    'openssl_certificate': 'community.crypto',
    'openssl_privatekey': 'community.crypto',
    'x509_certificate': 'community.crypto',
}


class ValidationResult:
    """Holds validation results for a playbook."""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.info: List[str] = []

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    def add_error(self, message: str, line: int = None):
        location = f" (line ~{line})" if line else ""
        self.errors.append(f"ERROR{location}: {message}")

    def add_warning(self, message: str, line: int = None):
        location = f" (line ~{line})" if line else ""
        self.warnings.append(f"WARNING{location}: {message}")

    def add_info(self, message: str):
        self.info.append(f"INFO: {message}")


def load_playbook(file_path: Path) -> Tuple[Any, str]:
    """Load a YAML playbook file."""
    try:
        content = file_path.read_text()
        data = yaml.safe_load(content)
        return data, content
    except yaml.YAMLError as e:
        return None, str(e)
    except Exception as e:
        return None, str(e)


def check_hosts_directive(play: Dict, result: ValidationResult):
    """Check that hosts directive uses correct naming."""
    hosts = play.get('hosts', '')

    if not hosts:
        result.add_error("Play missing 'hosts' directive")
        return

    # Check for prefixed group names
    if hosts.startswith('role-'):
        result.add_error(
            f"hosts: '{hosts}' uses 'role-' prefix. "
            f"Use '{hosts.replace('role-', '')}' instead"
        )

    if hosts.startswith('customer-'):
        result.add_warning(
            f"hosts: '{hosts}' uses 'customer-' prefix. "
            "Consider using the role name or 'all'"
        )


def check_delegation(task: Dict, task_name: str, result: ValidationResult):
    """Check for problematic delegation patterns."""
    delegate_to = task.get('delegate_to', '')
    become = task.get('become', False)

    if delegate_to == 'localhost' and become:
        result.add_error(
            f"Task '{task_name}' delegates to localhost with become: yes. "
            "AWX runner has no sudo. Remove delegate_to or become."
        )


def check_module_availability(task: Dict, task_name: str, result: ValidationResult):
    """Check that task uses available modules."""
    # Find the module being used (first key that's not a known task keyword)
    task_keywords = {
        'name', 'register', 'when', 'loop', 'with_items', 'with_dict',
        'become', 'become_user', 'become_method', 'delegate_to',
        'ignore_errors', 'failed_when', 'changed_when', 'no_log',
        'tags', 'vars', 'environment', 'args', 'notify', 'retries',
        'delay', 'until', 'run_once', 'throttle', 'any_errors_fatal',
        'block', 'rescue', 'always', 'listen', 'check_mode', 'diff',
        'local_action', 'async', 'poll', 'connection', 'debugger',
    }

    module = None
    for key in task.keys():
        if key not in task_keywords:
            module = key
            break

    if not module:
        return

    # Check if it's a FQCN (contains dots)
    if '.' in module:
        parts = module.split('.')
        if len(parts) >= 2:
            collection = f"{parts[0]}.{parts[1]}"
            if collection not in AVAILABLE_COLLECTIONS:
                result.add_error(
                    f"Task '{task_name}' uses module '{module}' from "
                    f"collection '{collection}' which is not installed in EE"
                )
    else:
        # Short module name - check if it needs a collection
        if module in COLLECTION_MODULES:
            required_collection = COLLECTION_MODULES[module]
            result.add_warning(
                f"Task '{task_name}' uses '{module}'. "
                f"Consider using FQCN: '{required_collection}.{module}'"
            )
        elif module not in BUILTIN_MODULES:
            # Unknown module - might be from a collection
            result.add_warning(
                f"Task '{task_name}' uses module '{module}'. "
                "If this is from a collection, use FQCN format."
            )


def check_hardcoded_credentials(content: str, result: ValidationResult):
    """Check for hardcoded credentials or keys."""
    patterns = [
        (r'ansible_ssh_private_key_file:', "Hardcoded SSH key path"),
        (r'ansible_password:', "Hardcoded password"),
        (r'private_key:\s*\|', "Inline private key"),
        (r'-----BEGIN.*PRIVATE KEY-----', "Private key in playbook"),
        (r'password:\s*["\'][^{]', "Possible hardcoded password"),
    ]

    lines = content.split('\n')
    for i, line in enumerate(lines, 1):
        # Skip comments
        if line.strip().startswith('#'):
            continue

        for pattern, message in patterns:
            if re.search(pattern, line, re.IGNORECASE):
                result.add_error(f"{message} found", line=i)


def check_variable_defaults(play: Dict, result: ValidationResult):
    """Check that variables have defaults where appropriate."""
    # This is a basic check - looks for undefined variable patterns
    # A full check would require more context
    pass  # Placeholder for future enhancement


def check_documentation(content: str, result: ValidationResult):
    """Check that playbook has proper documentation."""
    if not content.strip().startswith('---'):
        result.add_warning("Playbook should start with '---'")

    # Check for header comment
    lines = content.split('\n')
    has_description = False

    for i, line in enumerate(lines[:20]):  # Check first 20 lines
        if line.strip().startswith('#') and len(line.strip()) > 5:
            has_description = True
            break
        if line.strip().startswith('- name:'):
            break

    if not has_description:
        result.add_warning(
            "Playbook lacks documentation header. "
            "Add comments describing purpose and required variables."
        )


def extract_tasks(play: Dict) -> List[Dict]:
    """Extract all tasks from a play, including from blocks."""
    tasks = []

    # Direct tasks
    for task in play.get('tasks', []):
        if 'block' in task:
            tasks.extend(task.get('block', []))
            tasks.extend(task.get('rescue', []))
            tasks.extend(task.get('always', []))
        else:
            tasks.append(task)

    # Pre/post tasks
    tasks.extend(play.get('pre_tasks', []))
    tasks.extend(play.get('post_tasks', []))

    # Handlers
    tasks.extend(play.get('handlers', []))

    return tasks


def validate_playbook(file_path: Path) -> ValidationResult:
    """Validate a single playbook against standards."""
    result = ValidationResult(str(file_path))

    # Load playbook
    data, content_or_error = load_playbook(file_path)

    if data is None:
        result.add_error(f"Failed to parse YAML: {content_or_error}")
        return result

    content = content_or_error

    # Check documentation
    check_documentation(content, result)

    # Check for hardcoded credentials
    check_hardcoded_credentials(content, result)

    # Validate each play
    if not isinstance(data, list):
        result.add_error("Playbook should be a list of plays")
        return result

    for play in data:
        if not isinstance(play, dict):
            continue

        # Check hosts directive
        check_hosts_directive(play, result)

        # Check all tasks
        tasks = extract_tasks(play)

        for task in tasks:
            if not isinstance(task, dict):
                continue

            task_name = task.get('name', '<unnamed task>')

            # Check delegation
            check_delegation(task, task_name, result)

            # Check module availability
            check_module_availability(task, task_name, result)

    if not result.has_errors and not result.has_warnings:
        result.add_info("All checks passed!")

    return result


def print_result(result: ValidationResult, verbose: bool = False):
    """Print validation results with formatting."""
    print(f"\n{'='*60}")
    print(f"File: {result.file_path}")
    print('='*60)

    if result.errors:
        print("\n[ERRORS]")
        for error in result.errors:
            print(f"  {error}")

    if result.warnings:
        print("\n[WARNINGS]")
        for warning in result.warnings:
            print(f"  {warning}")

    if verbose and result.info:
        print("\n[INFO]")
        for info in result.info:
            print(f"  {info}")

    # Summary
    status = "FAILED" if result.has_errors else ("WARNINGS" if result.has_warnings else "PASSED")
    print(f"\nStatus: {status}")


def main():
    parser = argparse.ArgumentParser(
        description="Validate Ansible playbooks against team standards"
    )
    parser.add_argument(
        'files',
        nargs='+',
        help='Playbook files to validate'
    )
    parser.add_argument(
        '--strict',
        action='store_true',
        help='Treat warnings as errors'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show all messages including info'
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Output results as JSON'
    )

    args = parser.parse_args()

    all_results = []
    has_errors = False
    has_warnings = False

    for file_pattern in args.files:
        file_path = Path(file_pattern)

        if not file_path.exists():
            print(f"File not found: {file_path}", file=sys.stderr)
            continue

        if not file_path.suffix in ['.yml', '.yaml']:
            continue

        result = validate_playbook(file_path)
        all_results.append(result)

        if result.has_errors:
            has_errors = True
        if result.has_warnings:
            has_warnings = True

        if not args.json:
            print_result(result, args.verbose)

    # Output JSON if requested
    if args.json:
        import json
        output = []
        for r in all_results:
            output.append({
                'file': r.file_path,
                'errors': r.errors,
                'warnings': r.warnings,
                'passed': not r.has_errors and (not args.strict or not r.has_warnings)
            })
        print(json.dumps(output, indent=2))

    # Summary
    if not args.json:
        print("\n" + "="*60)
        print("SUMMARY")
        print("="*60)
        print(f"Files checked: {len(all_results)}")
        print(f"Errors: {sum(len(r.errors) for r in all_results)}")
        print(f"Warnings: {sum(len(r.warnings) for r in all_results)}")

    # Exit code
    if has_errors:
        sys.exit(1)
    elif args.strict and has_warnings:
        sys.exit(2)
    else:
        sys.exit(0)


if __name__ == '__main__':
    main()
