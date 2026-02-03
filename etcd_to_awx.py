#!/usr/bin/env python3
"""
Script to pull IPs and hostnames from etcd and create inventory in AWX.
Supports OAuth2 Resource Owner Password-Based authentication.
Supports filtering by domain (customer) and role.
"""

import etcd3
import requests
import json
import os
import sys
import argparse
import re
from typing import Dict, List, Any, Optional, Tuple, Set

# Configuration - can be overridden by environment variables
ETCD_SERVER = os.environ.get("ETCD_SERVER", "localhost")
ETCD_PORT = int(os.environ.get("ETCD_PORT", 2379))
ETCD_PREFIX = os.environ.get("ETCD_PREFIX", "/discovery/")

AWX_SERVER = os.environ.get("AWX_SERVER", "localhost")

# Authentication options (set ONE of these):
AWX_TOKEN = os.environ.get("AWX_TOKEN")
AWX_CLIENT_ID = os.environ.get("AWX_CLIENT_ID")
AWX_CLIENT_SECRET = os.environ.get("AWX_CLIENT_SECRET")
AWX_USERNAME = os.environ.get("AWX_USERNAME")
AWX_PASSWORD = os.environ.get("AWX_PASSWORD")

# Note: Roles and domains are discovered dynamically from etcd
# No hardcoded lists - the script adapts to whatever is in etcd


def check_required_env_vars():
    """Check that required environment variables are set."""
    has_token = bool(AWX_TOKEN)
    has_oauth_password = bool(AWX_CLIENT_ID and AWX_CLIENT_SECRET and AWX_USERNAME and AWX_PASSWORD)
    has_basic = bool(AWX_USERNAME and AWX_PASSWORD) and not (AWX_CLIENT_ID and AWX_CLIENT_SECRET)

    if not (has_token or has_oauth_password or has_basic):
        print("Error: No AWX authentication configured.")
        print("\nSet ONE of the following:")
        print("\n  Option 1 - Personal Access Token:")
        print("    export AWX_TOKEN='your_token'")
        print("\n  Option 2 - OAuth2 Resource Owner Password-Based:")
        print("    export AWX_CLIENT_ID='your_client_id'")
        print("    export AWX_CLIENT_SECRET='your_client_secret'")
        print("    export AWX_USERNAME='your_username'")
        print("    export AWX_PASSWORD='your_password'")
        print("\n  Option 3 - Basic Username/Password:")
        print("    export AWX_USERNAME='your_username'")
        print("    export AWX_PASSWORD='your_password'")
        sys.exit(1)

    if has_token:
        print("Using Personal Access Token authentication")
    elif has_oauth_password:
        print("Using OAuth2 Resource Owner Password-Based authentication")
    else:
        print("Using Basic Username/Password authentication")


def get_awx_api_url() -> str:
    """Get the base AWX API URL (HTTP)."""
    return f"http://{AWX_SERVER}/api/v2"


def create_awx_session() -> requests.Session:
    """Create an authenticated AWX session."""
    session = requests.Session()
    session.headers.update({'Content-Type': 'application/json'})

    if AWX_TOKEN:
        session.headers.update({'Authorization': f'Bearer {AWX_TOKEN}'})
        return session

    if AWX_CLIENT_ID and AWX_CLIENT_SECRET and AWX_USERNAME and AWX_PASSWORD:
        token_url = f"http://{AWX_SERVER}/api/o/token/"
        data = {
            "grant_type": "password",
            "client_id": AWX_CLIENT_ID,
            "client_secret": AWX_CLIENT_SECRET,
            "username": AWX_USERNAME,
            "password": AWX_PASSWORD
        }
        response = requests.post(token_url, data=data)
        if response.status_code != 200:
            print(f"OAuth token request failed: {response.status_code}")
            print(f"Response: {response.text}")
            # Fall back to Basic Auth if OAuth fails
            print("Falling back to Basic Authentication...")
            session.auth = (AWX_USERNAME, AWX_PASSWORD)
            return session
        token = response.json().get("access_token")
        print("Successfully obtained OAuth token")
        session.headers.update({'Authorization': f'Bearer {token}'})
        return session

    if AWX_USERNAME and AWX_PASSWORD:
        session.auth = (AWX_USERNAME, AWX_PASSWORD)
        return session

    raise Exception("No authentication method configured")


def extract_role_from_hostname(hostname: str) -> Optional[str]:
    """
    Extract the role from a hostname.

    Hostname patterns:
    - mphpp-pubwxp-010103-1.vivox.com -> role: mphpp
    - mphhos-lolxp-010103-1.vivox.com -> role: mphhos
    - hamim-lionamxp-024901-1.vivox.com -> role: hamim
    - ts-valxp-010101-1.vivox.com -> role: ts
    - www5-lionamxp-024901-1.vivox.com -> role: www
    """
    hostname_lower = hostname.lower()

    # Extract first part before the domain/customer
    parts = hostname_lower.split('-')
    if not parts:
        return None

    first_part = parts[0]

    # Remove trailing numbers (e.g., www5 -> www)
    role = re.sub(r'\d+$', '', first_part)

    # Check if it's a known role or return the extracted part
    if role:
        return role

    return None


def get_hosts_from_etcd() -> Tuple[Dict[str, Dict[str, Any]], Set[str], Set[str]]:
    """
    Retrieve hosts from etcd.

    Returns:
        - Dictionary of all hosts
        - Set of all domains/customers found
        - Set of all roles found
    """
    all_hosts = {}
    all_domains = set()
    all_roles = set()

    try:
        client = etcd3.client(host=ETCD_SERVER, port=ETCD_PORT)
        print(f"Connected to etcd at {ETCD_SERVER}:{ETCD_PORT}")

        for value, metadata in client.get_prefix(ETCD_PREFIX):
            if not value or not metadata:
                continue

            key = metadata.key.decode('utf-8')
            value_str = value.decode('utf-8').strip()

            path_parts = key.split('/')
            if len(path_parts) < 4:
                continue

            key_type = path_parts[-1]
            hostname = path_parts[-2]
            customer = path_parts[2]

            if key_type not in ('viv_privip', 'viv_pubip', 'viv_ipaddresses'):
                continue

            if hostname.startswith('viv_') or hostname.startswith('version_'):
                continue

            # Extract role from hostname
            role = extract_role_from_hostname(hostname)

            if hostname not in all_hosts:
                all_hosts[hostname] = {
                    'hostname': hostname,
                    'customer': customer,
                    'role': role,
                    'private_ip': None,
                    'public_ip': None,
                    'all_ips': None
                }
                all_domains.add(customer)
                if role:
                    all_roles.add(role)

            if key_type == 'viv_privip':
                all_hosts[hostname]['private_ip'] = value_str
            elif key_type == 'viv_pubip':
                all_hosts[hostname]['public_ip'] = value_str
            elif key_type == 'viv_ipaddresses':
                all_hosts[hostname]['all_ips'] = value_str

        # Set primary IP
        for hostname, host_info in all_hosts.items():
            host_info['ip'] = host_info['private_ip'] or host_info['public_ip']

        # Filter invalid hosts
        all_hosts = {
            h: info for h, info in all_hosts.items()
            if info['ip'] and info['ip'] not in ('127.0.0.1', 'None', '')
        }

        print(f"\nTotal hosts found in etcd: {len(all_hosts)}")
        print(f"Total domains/customers: {len(all_domains)}")
        print(f"Total roles discovered: {len(all_roles)}")

    except Exception as e:
        print(f"Error connecting to etcd: {e}")
        raise

    return all_hosts, all_domains, all_roles


def filter_hosts(
    hosts: Dict[str, Dict[str, Any]],
    domain_filter: Optional[str] = None,
    role_filter: Optional[str] = None
) -> Dict[str, Dict[str, Any]]:
    """
    Filter hosts by domain and/or role using EXACT MATCHING.

    Args:
        hosts: Dictionary of all hosts
        domain_filter: Filter by domain/customer (e.g., 'pubwxp') - EXACT MATCH
        role_filter: Filter by role (e.g., 'mphpp') - EXACT MATCH

    Returns:
        Filtered dictionary of hosts

    Note:
        Uses exact matching to prevent 'rfxp' from matching 'wrfxp'.
        This is critical for production safety.
    """
    filtered = {}

    for hostname, info in hosts.items():
        # Check domain filter - EXACT MATCH ONLY
        if domain_filter:
            host_domain = info.get('customer', '').lower()
            domain_filter_lower = domain_filter.lower()

            # Exact domain match only (rfxp != wrfxp)
            if host_domain != domain_filter_lower:
                continue

        # Check role filter - EXACT MATCH ONLY
        if role_filter:
            host_role = info.get('role', '').lower() if info.get('role') else ''
            role_filter_lower = role_filter.lower()

            # Exact role match only (mim != mimmem)
            if host_role != role_filter_lower:
                continue

        filtered[hostname] = info

    return filtered


def display_filter_menu(domains: Set[str], roles: Set[str]) -> Tuple[Optional[str], Optional[str], str]:
    """
    Display interactive menu for filtering options.

    Returns:
        - domain_filter: Selected domain or None for all
        - role_filter: Selected role or None for all
        - inventory_name: Name for the inventory
    """
    print("\n" + "=" * 60)
    print("INVENTORY FILTER OPTIONS")
    print("=" * 60)

    print("\nWhat type of inventory would you like to create?")
    print("-" * 40)
    print("1. Full sync (all hosts, all domains, all roles)")
    print("2. Domain-specific inventory (e.g., all hosts for 'pubwxp')")
    print("3. Role-specific inventory (e.g., all 'mphpp' servers)")
    print("4. Combined filter (e.g., 'mphpp' servers for 'pubwxp' domain)")
    print("-" * 40)

    while True:
        choice = input("\nEnter your choice (1-4): ").strip()
        if choice in ['1', '2', '3', '4']:
            break
        print("Invalid choice. Please enter 1, 2, 3, or 4.")

    domain_filter = None
    role_filter = None
    inventory_name = "central inventory"

    if choice == '1':
        # Full sync
        inventory_name = "central inventory"
        print("\n>> Creating full inventory with all hosts")

    elif choice == '2':
        # Domain-specific
        print("\n" + "-" * 40)
        print("Available domains (top 30):")
        sorted_domains = sorted(domains)[:30]
        for i, d in enumerate(sorted_domains, 1):
            print(f"  {i:2}. {d}")
        if len(domains) > 30:
            print(f"  ... and {len(domains) - 30} more")
        print("-" * 40)

        domain_filter = input("\nEnter domain name (e.g., pubwxp): ").strip()
        if domain_filter:
            inventory_name = f"{domain_filter}-inventory"
            print(f"\n>> Creating inventory for domain: {domain_filter}")
        else:
            print("No domain entered. Creating full inventory.")

    elif choice == '3':
        # Role-specific
        print("\n" + "-" * 40)
        print("Available roles (discovered from etcd):")
        sorted_roles = sorted(roles)
        for i, r in enumerate(sorted_roles[:30], 1):
            print(f"  {i:2}. {r}")
        if len(roles) > 30:
            print(f"  ... and {len(roles) - 30} more")
        print("-" * 40)

        role_filter = input("\nEnter role name (e.g., mphpp): ").strip()
        if role_filter:
            inventory_name = f"{role_filter}-all-domains"
            print(f"\n>> Creating inventory for role: {role_filter} (all domains)")
        else:
            print("No role entered. Creating full inventory.")

    elif choice == '4':
        # Combined filter
        print("\n" + "-" * 40)
        print("COMBINED FILTER: Role + Domain")
        print("-" * 40)

        # Get role
        print("\nAvailable roles:", ', '.join(sorted(roles)[:20]))
        if len(roles) > 20:
            print(f"  ... and {len(roles) - 20} more")
        role_filter = input("Enter role name (e.g., mphpp): ").strip()

        # Get domain
        print("\nSample domains:", ', '.join(sorted(domains)[:15]))
        domain_filter = input("Enter domain name (e.g., pubwxp): ").strip()

        if role_filter and domain_filter:
            inventory_name = f"{role_filter}-{domain_filter}"
            print(f"\n>> Creating inventory for role '{role_filter}' in domain '{domain_filter}'")
        elif role_filter:
            inventory_name = f"{role_filter}-all-domains"
            print(f"\n>> Creating inventory for role: {role_filter} (all domains)")
        elif domain_filter:
            inventory_name = f"{domain_filter}-inventory"
            print(f"\n>> Creating inventory for domain: {domain_filter}")
        else:
            print("No filters entered. Creating full inventory.")

    return domain_filter, role_filter, inventory_name


def get_or_create_organization(session: requests.Session) -> int:
    """Get the default organization ID or first available one."""
    url = f"{get_awx_api_url()}/organizations/"
    response = session.get(url)
    response.raise_for_status()

    orgs = response.json().get('results', [])
    if orgs:
        org_id = orgs[0]['id']
        print(f"Using organization: {orgs[0]['name']} (ID: {org_id})")
        return org_id
    else:
        raise Exception("No organizations found in AWX")


def create_inventory(session: requests.Session, org_id: int, inventory_name: str) -> int:
    """Create an inventory in AWX or return existing one."""
    url = f"{get_awx_api_url()}/inventories/"

    response = session.get(url, params={'name': inventory_name})
    response.raise_for_status()

    existing = response.json().get('results', [])
    if existing:
        inv_id = existing[0]['id']
        print(f"Inventory '{inventory_name}' already exists (ID: {inv_id})")
        return inv_id

    payload = {
        "name": inventory_name,
        "description": f"Inventory synced from etcd - {inventory_name}",
        "organization": org_id
    }

    response = session.post(url, json=payload)
    response.raise_for_status()

    inv_id = response.json()['id']
    print(f"Created inventory '{inventory_name}' (ID: {inv_id})")
    return inv_id


def get_host_groups(hostname: str, customer: str = None, role: str = None) -> List[str]:
    """Determine which groups a host belongs to based on hostname patterns."""
    groups = []
    hostname_lower = hostname.lower()

    # Customer group
    if customer:
        groups.append(f"customer-{customer}")

    # Role group
    if role:
        groups.append(f"role-{role}")

    # Server type groups
    if 'gen-comp' in hostname_lower:
        groups.append('gen-comp')
    if 'sriov-comp' in hostname_lower:
        groups.append('sriov-comp')

    # Location groups
    if '-bos-' in hostname_lower or hostname_lower.endswith('-bos.vivox.com'):
        groups.append('location-bos')
    if '-chn-' in hostname_lower:
        groups.append('location-chn')

    # Cluster groups
    if hostname_lower.startswith('os1-'):
        groups.append('cluster-os1')
    if hostname_lower.startswith('os2-'):
        groups.append('cluster-os2')
    if hostname_lower.startswith('os-chn-'):
        groups.append('cluster-os-chn')

    return groups


def get_or_create_group(
    session: requests.Session,
    inventory_id: int,
    group_name: str,
    group_cache: Dict[str, int]
) -> int:
    """Get or create a group in AWX inventory."""
    if group_name in group_cache:
        return group_cache[group_name]

    url = f"{get_awx_api_url()}/groups/"
    response = session.get(url, params={'name': group_name, 'inventory': inventory_id})
    response.raise_for_status()

    existing = response.json().get('results', [])
    if existing:
        group_id = existing[0]['id']
        group_cache[group_name] = group_id
        return group_id

    payload = {
        "name": group_name,
        "inventory": inventory_id,
        "description": f"Auto-generated group for {group_name}"
    }

    response = session.post(url, json=payload)
    response.raise_for_status()

    group_id = response.json()['id']
    group_cache[group_name] = group_id
    print(f"Created group '{group_name}' (ID: {group_id})")
    return group_id


def add_host_to_group(session: requests.Session, group_id: int, host_id: int) -> None:
    """Add a host to a group."""
    url = f"{get_awx_api_url()}/groups/{group_id}/hosts/"
    response = session.get(url, params={'id': host_id})
    response.raise_for_status()

    existing = response.json().get('results', [])
    if any(h['id'] == host_id for h in existing):
        return

    payload = {"id": host_id}
    response = session.post(url, json=payload)
    response.raise_for_status()


def add_hosts_to_inventory(
    session: requests.Session,
    inventory_id: int,
    hosts: Dict[str, Dict[str, Any]]
) -> Dict[str, int]:
    """Add hosts to the AWX inventory and return hostname->id mapping."""
    url = f"{get_awx_api_url()}/hosts/"
    host_id_map = {}
    total = len(hosts)

    for idx, (hostname, host_info) in enumerate(hosts.items(), 1):
        check_response = session.get(
            url,
            params={'name': hostname, 'inventory': inventory_id}
        )
        check_response.raise_for_status()

        existing = check_response.json().get('results', [])
        if existing:
            host_id = existing[0]['id']
            host_id_map[hostname] = host_id

            # Update host variables
            new_variables = {
                'ansible_host': host_info['ip'],
                'private_ip': host_info.get('private_ip'),
                'public_ip': host_info.get('public_ip'),
                'all_ips': host_info.get('all_ips'),
                'customer': host_info.get('customer'),
                'role': host_info.get('role')
            }
            new_variables = {k: v for k, v in new_variables.items() if v is not None}

            update_url = f"{get_awx_api_url()}/hosts/{host_id}/"
            session.patch(update_url, json={"variables": json.dumps(new_variables)})

            if idx % 50 == 0 or idx == total:
                print(f"  Progress: {idx}/{total} hosts processed...")
            continue

        variables = {
            'ansible_host': host_info['ip'],
            'private_ip': host_info.get('private_ip'),
            'public_ip': host_info.get('public_ip'),
            'all_ips': host_info.get('all_ips'),
            'customer': host_info.get('customer'),
            'role': host_info.get('role')
        }
        variables = {k: v for k, v in variables.items() if v is not None}

        payload = {
            "name": hostname,
            "inventory": inventory_id,
            "variables": json.dumps(variables)
        }

        response = session.post(url, json=payload)
        response.raise_for_status()

        host_id = response.json()['id']
        host_id_map[hostname] = host_id

        if idx % 50 == 0 or idx == total:
            print(f"  Progress: {idx}/{total} hosts processed...")

    return host_id_map


def create_groups_and_assign_hosts(
    session: requests.Session,
    inventory_id: int,
    hosts: Dict[str, Dict[str, Any]],
    host_id_map: Dict[str, int]
) -> Dict[str, List[str]]:
    """Create groups based on hostname patterns and assign hosts."""
    group_cache: Dict[str, int] = {}
    group_members: Dict[str, List[str]] = {}

    for hostname, host_info in hosts.items():
        customer = host_info.get('customer')
        role = host_info.get('role')
        groups = get_host_groups(hostname, customer, role)
        host_id = host_id_map.get(hostname)

        if not host_id:
            continue

        for group_name in groups:
            if group_name not in group_members:
                group_members[group_name] = []
            group_members[group_name].append(hostname)

            group_id = get_or_create_group(session, inventory_id, group_name, group_cache)
            add_host_to_group(session, group_id, host_id)

    return group_members


def parse_natural_language_prompt(
    prompt: str,
    available_domains: Set[str],
    available_roles: Set[str]
) -> Tuple[Optional[str], Optional[str], str]:
    """
    Parse a natural language prompt to extract domain and role filters.
    Uses dynamically discovered domains and roles from etcd.

    Examples:
    - "Create inventory for mphpp in pubwxp"
    - "Give me all ts servers"
    - "mphpp for pubwxp domain"
    - "all mim servers across all domains"
    - "pubwxp inventory"
    - "ts servers for valxp"

    Returns:
        - domain_filter
        - role_filter
        - inventory_name
    """
    prompt_lower = prompt.lower().strip()

    # Remove common filler words
    filler_words = [
        'create', 'inventory', 'for', 'give', 'me', 'all', 'the', 'servers',
        'hosts', 'in', 'from', 'domain', 'across', 'domains', 'please',
        'can', 'you', 'i', 'want', 'need', 'get', 'show', 'list', 'of', 'with'
    ]

    words = prompt_lower.replace(',', ' ').replace('-', ' ').split()

    domain_filter = None
    role_filter = None

    # Create lowercase lookup sets for faster matching
    domains_lower = {d.lower(): d for d in available_domains}
    roles_lower = {r.lower(): r for r in available_roles}

    # Find matches for domains and roles from discovered data
    for word in words:
        if word in filler_words:
            continue

        # Check if it's a domain (exact match from discovered domains)
        if word in domains_lower:
            domain_filter = domains_lower[word]

        # Check if it's a role (exact match from discovered roles)
        if word in roles_lower:
            role_filter = roles_lower[word]

    # Build inventory name
    if role_filter and domain_filter:
        inventory_name = f"{role_filter}-{domain_filter}"
    elif role_filter:
        inventory_name = f"{role_filter}-all-domains"
    elif domain_filter:
        inventory_name = f"{domain_filter}-inventory"
    else:
        inventory_name = "central inventory"

    return domain_filter, role_filter, inventory_name


def smart_prompt_mode(available_domains: Set[str], available_roles: Set[str]) -> Tuple[Optional[str], Optional[str], str]:
    """
    Interactive smart prompt mode where user can type natural language requests.
    """
    print("\n" + "=" * 60)
    print("SMART INVENTORY CREATOR")
    print("=" * 60)
    print("\nType your request in natural language. Examples:")
    print("  - 'mphpp servers for pubwxp'")
    print("  - 'all ts servers'")
    print("  - 'mim for lolxp domain'")
    print("  - 'create inventory for valxp'")
    print("  - 'hamim across all domains'")
    print("  - 'pubwxp' (domain only)")
    print("  - 'mphhos' (role only)")
    print("-" * 60)
    print("Type 'list domains' to see available domains")
    print("Type 'list roles' to see available roles")
    print("Type 'help' for more examples")
    print("Type 'exit' to quit")
    print("-" * 60)

    while True:
        try:
            user_input = input("\n>> Enter your request: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting...")
            sys.exit(0)

        if not user_input:
            continue

        input_lower = user_input.lower()

        # Handle special commands
        if input_lower == 'exit' or input_lower == 'quit':
            print("Exiting...")
            sys.exit(0)

        if input_lower == 'list domains' or input_lower == 'domains':
            print("\nAvailable domains (sorted by host count):")
            domain_counts = {}
            # We don't have host counts here, just list alphabetically
            for d in sorted(available_domains)[:40]:
                print(f"  - {d}")
            if len(available_domains) > 40:
                print(f"  ... and {len(available_domains) - 40} more")
            continue

        if input_lower == 'list roles' or input_lower == 'roles':
            print("\nAvailable roles:")
            for r in sorted(available_roles):
                print(f"  - {r}")
            continue

        if input_lower == 'help':
            print("\n" + "=" * 50)
            print("HELP - Smart Inventory Examples")
            print("=" * 50)
            print("\nDomain-specific (all roles for a domain):")
            print("  - 'pubwxp'")
            print("  - 'inventory for valxp'")
            print("  - 'all servers in lolxp'")
            print("\nRole-specific (all domains for a role):")
            print("  - 'mphpp'")
            print("  - 'all ts servers'")
            print("  - 'mim across all domains'")
            print("\nCombined (specific role in specific domain):")
            print("  - 'mphpp for pubwxp'")
            print("  - 'ts servers in valxp'")
            print("  - 'mim lolxp'")
            print("  - 'hamim for lionamxp domain'")
            print("=" * 50)
            continue

        # Parse the natural language input
        domain_filter, role_filter, inventory_name = parse_natural_language_prompt(
            user_input, available_domains, available_roles
        )

        # Show what was parsed
        print("\n" + "-" * 40)
        print("Parsed request:")
        if domain_filter:
            print(f"  Domain: {domain_filter}")
        else:
            print("  Domain: ALL domains")
        if role_filter:
            print(f"  Role: {role_filter}")
        else:
            print("  Role: ALL roles")
        print(f"  Inventory name: {inventory_name}")
        print("-" * 40)

        # Confirm with user
        confirm = input("Proceed with this configuration? (y/n/modify): ").strip().lower()

        if confirm == 'y' or confirm == 'yes':
            return domain_filter, role_filter, inventory_name
        elif confirm == 'modify' or confirm == 'm':
            # Allow manual modification
            new_domain = input(f"  Domain [{domain_filter or 'all'}]: ").strip()
            new_role = input(f"  Role [{role_filter or 'all'}]: ").strip()
            new_name = input(f"  Inventory name [{inventory_name}]: ").strip()

            domain_filter = new_domain if new_domain and new_domain != 'all' else domain_filter
            role_filter = new_role if new_role and new_role != 'all' else role_filter
            inventory_name = new_name if new_name else inventory_name

            print(f"\nFinal configuration:")
            print(f"  Domain: {domain_filter or 'ALL'}")
            print(f"  Role: {role_filter or 'ALL'}")
            print(f"  Inventory: {inventory_name}")

            final_confirm = input("Proceed? (y/n): ").strip().lower()
            if final_confirm == 'y' or final_confirm == 'yes':
                return domain_filter, role_filter, inventory_name
        else:
            print("Let's try again...")
            continue


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Sync hosts from etcd to AWX inventory',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Smart prompt mode (natural language)
  python etcd_to_awx.py --smart
  python etcd_to_awx.py -s

  # Interactive menu mode
  python etcd_to_awx.py

  # Full sync (no filters)
  python etcd_to_awx.py --full

  # Domain-specific inventory
  python etcd_to_awx.py --domain pubwxp

  # Role-specific inventory (all domains)
  python etcd_to_awx.py --role mphpp

  # Combined filter (role + domain)
  python etcd_to_awx.py --role mphpp --domain pubwxp

  # Direct prompt (non-interactive)
  python etcd_to_awx.py --prompt "mphpp servers for pubwxp"

  # Custom inventory name
  python etcd_to_awx.py --domain pubwxp --inventory-name "pubwxp-production"
        """
    )

    parser.add_argument('--domain', '-d', type=str, help='Filter by domain/customer (e.g., pubwxp)')
    parser.add_argument('--role', '-r', type=str, help='Filter by role (e.g., mphpp, mphhos, mim, ts)')
    parser.add_argument('--inventory-name', '-n', type=str, help='Custom inventory name')
    parser.add_argument('--full', '-f', action='store_true', help='Full sync without prompts')
    parser.add_argument('--smart', '-s', action='store_true', help='Smart prompt mode (natural language)')
    parser.add_argument('--prompt', '-p', type=str, help='Direct natural language prompt (non-interactive)')
    parser.add_argument('--list-domains', action='store_true', help='List all available domains')
    parser.add_argument('--list-roles', action='store_true', help='List all available roles')

    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 60)
    print("etcd to AWX Inventory Sync")
    print("=" * 60)

    check_required_env_vars()

    # Step 1: Get hosts from etcd
    print("\n[1] Fetching hosts from etcd...")
    all_hosts, all_domains, all_roles = get_hosts_from_etcd()

    if not all_hosts:
        print("No hosts found in etcd. Exiting.")
        return

    # Handle list commands
    if args.list_domains:
        print("\n" + "=" * 40)
        print("AVAILABLE DOMAINS/CUSTOMERS:")
        print("=" * 40)
        for d in sorted(all_domains):
            count = sum(1 for h in all_hosts.values() if h.get('customer') == d)
            print(f"  {d}: {count} hosts")
        return

    if args.list_roles:
        print("\n" + "=" * 40)
        print("AVAILABLE ROLES:")
        print("=" * 40)
        role_counts = {}
        for h in all_hosts.values():
            r = h.get('role')
            if r:
                role_counts[r] = role_counts.get(r, 0) + 1
        for r, count in sorted(role_counts.items(), key=lambda x: -x[1]):
            print(f"  {r}: {count} hosts")
        return

    # Determine filters
    domain_filter = args.domain
    role_filter = args.role
    inventory_name = args.inventory_name

    # Smart prompt mode
    if args.smart:
        domain_filter, role_filter, inventory_name = smart_prompt_mode(all_domains, all_roles)
    # Direct prompt mode (non-interactive)
    elif args.prompt:
        domain_filter, role_filter, inventory_name = parse_natural_language_prompt(
            args.prompt, all_domains, all_roles
        )
        print(f"\nParsed prompt: domain={domain_filter or 'ALL'}, role={role_filter or 'ALL'}")
        print(f"Inventory name: {inventory_name}")
    # Interactive menu mode if no filters specified and not --full
    elif not args.full and not domain_filter and not role_filter:
        domain_filter, role_filter, inventory_name = display_filter_menu(all_domains, all_roles)
    elif args.full:
        inventory_name = inventory_name or "central inventory"
    else:
        # Build inventory name from filters if not provided
        if not inventory_name:
            if role_filter and domain_filter:
                inventory_name = f"{role_filter}-{domain_filter}"
            elif role_filter:
                inventory_name = f"{role_filter}-all-domains"
            elif domain_filter:
                inventory_name = f"{domain_filter}-inventory"
            else:
                inventory_name = "central inventory"

    # Step 2: Apply filters
    if domain_filter or role_filter:
        print(f"\n[2] Applying filters...")
        if domain_filter:
            print(f"    Domain filter: {domain_filter}")
        if role_filter:
            print(f"    Role filter: {role_filter}")

        filtered_hosts = filter_hosts(all_hosts, domain_filter, role_filter)
        print(f"    Hosts after filtering: {len(filtered_hosts)}")

        if not filtered_hosts:
            print("\nNo hosts match the specified filters. Exiting.")
            return

        hosts = filtered_hosts
    else:
        hosts = all_hosts

    # Step 3: Create AWX session
    print(f"\n[3] Authenticating with AWX...")
    session = create_awx_session()

    try:
        response = session.get(f"{get_awx_api_url()}/ping/")
        response.raise_for_status()
        print("Successfully connected to AWX")
    except Exception as e:
        print(f"Failed to connect to AWX: {e}")
        return

    # Step 4: Get organization
    print("\n[4] Getting organization...")
    org_id = get_or_create_organization(session)

    # Step 5: Create inventory
    print(f"\n[5] Creating inventory '{inventory_name}'...")
    inventory_id = create_inventory(session, org_id, inventory_name)

    # Step 6: Add hosts to inventory
    print(f"\n[6] Adding {len(hosts)} hosts to inventory...")
    host_id_map = add_hosts_to_inventory(session, inventory_id, hosts)

    # Step 7: Create groups and assign hosts
    print("\n[7] Creating groups and assigning hosts...")
    group_members = create_groups_and_assign_hosts(session, inventory_id, hosts, host_id_map)

    # Summary
    print("\n" + "=" * 60)
    print("SYNC COMPLETED!")
    print("=" * 60)
    print(f"Inventory: {inventory_name} (ID: {inventory_id})")
    print(f"Hosts synced: {len(host_id_map)}")
    if domain_filter:
        print(f"Domain filter: {domain_filter}")
    if role_filter:
        print(f"Role filter: {role_filter}")
    print(f"\nGroups created/updated: {len(group_members)}")
    for group_name, members in sorted(group_members.items(), key=lambda x: -len(x[1]))[:15]:
        print(f"  - {group_name}: {len(members)} hosts")
    if len(group_members) > 15:
        print(f"  ... and {len(group_members) - 15} more groups")
    print("=" * 60)


def run_sync(
    domain_filter: str = None,
    role_filter: str = None,
    inventory_name: str = None,
) -> Dict[str, Any]:
    """
    Run sync programmatically (called by Slack MCP agent).

    Args:
        domain_filter: Filter by domain/customer
        role_filter: Filter by role
        inventory_name: Custom inventory name

    Returns:
        Dict with sync results
    """
    import time
    start_time = time.time()

    check_required_env_vars()

    # Get hosts from etcd
    all_hosts, all_domains, all_roles = get_hosts_from_etcd()

    if not all_hosts:
        raise Exception("No hosts found in etcd")

    # Build inventory name if not provided
    if not inventory_name:
        if role_filter and domain_filter:
            inventory_name = f"{role_filter}-{domain_filter}"
        elif role_filter:
            inventory_name = f"{role_filter}-all-domains"
        elif domain_filter:
            inventory_name = f"{domain_filter}-inventory"
        else:
            inventory_name = "central inventory"

    # Apply filters
    if domain_filter or role_filter:
        hosts = filter_hosts(all_hosts, domain_filter, role_filter)
        if not hosts:
            raise Exception(f"No hosts match filters: domain={domain_filter}, role={role_filter}")
    else:
        hosts = all_hosts

    # Create AWX session and sync
    session = create_awx_session()

    # Verify connection
    response = session.get(f"{get_awx_api_url()}/ping/")
    response.raise_for_status()

    # Get organization
    org_id = get_or_create_organization(session)

    # Create inventory
    inventory_id = create_inventory(session, org_id, inventory_name)

    # Add hosts
    host_id_map = add_hosts_to_inventory(session, inventory_id, hosts)

    # Create groups
    group_members = create_groups_and_assign_hosts(session, inventory_id, hosts, host_id_map)

    duration = time.time() - start_time

    return {
        "inventory_name": inventory_name,
        "inventory_id": inventory_id,
        "host_count": len(host_id_map),
        "group_count": len(group_members),
        "duration_seconds": duration,
    }


if __name__ == "__main__":
    main()
