#!/usr/bin/env python3
"""
Script to pull IPs and hostnames from etcd and create inventory in AWX.
Supports OAuth2 Resource Owner Password-Based authentication.
"""

import etcd3
import requests
import json
import os
import sys
from typing import Dict, List, Any

# Configuration - can be overridden by environment variables
ETCD_SERVER = os.environ.get("ETCD_SERVER", "localhost")
ETCD_PORT = int(os.environ.get("ETCD_PORT", 2379))
ETCD_PREFIX = os.environ.get("ETCD_PREFIX", "/discovery/")

AWX_SERVER = os.environ.get("AWX_SERVER", "localhost")

# Authentication options (set ONE of these):
# Option 1: Personal Access Token (recommended)
AWX_TOKEN = os.environ.get("AWX_TOKEN")

# Option 2: OAuth2 Client Credentials
AWX_CLIENT_ID = os.environ.get("AWX_CLIENT_ID")
AWX_CLIENT_SECRET = os.environ.get("AWX_CLIENT_SECRET")

# Option 3: Username/Password
AWX_USERNAME = os.environ.get("AWX_USERNAME")
AWX_PASSWORD = os.environ.get("AWX_PASSWORD")

INVENTORY_NAME = os.environ.get("AWX_INVENTORY_NAME", "central inventory")


def check_required_env_vars():
    """Check that required environment variables are set."""
    # Check if any auth method is configured
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

    # Option 1: Personal Access Token
    if AWX_TOKEN:
        session.headers.update({'Authorization': f'Bearer {AWX_TOKEN}'})
        return session

    # Option 2: OAuth2 Resource Owner Password-Based
    # Requires: client_id, client_secret, username, password
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
            raise Exception("Failed to obtain OAuth token")

        token = response.json().get("access_token")
        print("Successfully obtained OAuth token")
        session.headers.update({'Authorization': f'Bearer {token}'})
        return session

    # Option 3: Basic Username/Password (without OAuth)
    if AWX_USERNAME and AWX_PASSWORD:
        session.auth = (AWX_USERNAME, AWX_PASSWORD)
        return session

    raise Exception("No authentication method configured")


def get_hosts_from_etcd() -> Dict[str, Dict[str, Any]]:
    """
    Retrieve hosts from etcd.

    etcd structure (handles nested paths):
    /discovery/<customer>/.../<hostname>/viv_privip -> private IP
    /discovery/<customer>/.../<hostname>/viv_pubip -> public IP
    /discovery/<customer>/.../<hostname>/viv_ipaddresses -> all IPs

    The hostname is the second-to-last path component before key_type.
    """
    all_hosts = {}

    try:
        client = etcd3.client(host=ETCD_SERVER, port=ETCD_PORT)
        print(f"Connected to etcd at {ETCD_SERVER}:{ETCD_PORT}")

        # Fetch all keys under /discovery/
        for value, metadata in client.get_prefix(ETCD_PREFIX):
            if not value or not metadata:
                continue

            key = metadata.key.decode('utf-8')
            value_str = value.decode('utf-8').strip()

            # Parse the key - structure: /discovery/<customer>/.../<hostname>/<key_type>
            path_parts = key.split('/')

            if len(path_parts) < 4:
                continue

            # Get key_type (last component) and hostname (second to last)
            key_type = path_parts[-1]
            hostname = path_parts[-2]
            customer = path_parts[2]  # Customer is always 3rd component

            # Only process IP-related keys
            if key_type not in ('viv_privip', 'viv_pubip', 'viv_ipaddresses'):
                continue

            # Skip if hostname looks like a key type (not a valid hostname)
            if hostname.startswith('viv_') or hostname.startswith('version_'):
                continue

            # Initialize host entry if not exists
            if hostname not in all_hosts:
                all_hosts[hostname] = {
                    'hostname': hostname,
                    'customer': customer,
                    'private_ip': None,
                    'public_ip': None,
                    'all_ips': None
                }

            # Store the appropriate IP based on key type
            if key_type == 'viv_privip':
                all_hosts[hostname]['private_ip'] = value_str
            elif key_type == 'viv_pubip':
                all_hosts[hostname]['public_ip'] = value_str
            elif key_type == 'viv_ipaddresses':
                all_hosts[hostname]['all_ips'] = value_str

        # Set the primary IP for each host (prefer private, fallback to public)
        for hostname, host_info in all_hosts.items():
            host_info['ip'] = host_info['private_ip'] or host_info['public_ip']

        # Filter out hosts without any IP or with invalid IPs
        all_hosts = {
            h: info for h, info in all_hosts.items()
            if info['ip'] and info['ip'] not in ('127.0.0.1', 'None', '')
        }

        print(f"\nTotal hosts found in etcd: {len(all_hosts)}")

        # Show summary per customer
        customers_summary = {}
        for hostname, info in all_hosts.items():
            cust = info.get('customer', 'unknown')
            customers_summary[cust] = customers_summary.get(cust, 0) + 1

        print("\nHosts per customer:")
        for cust, count in sorted(customers_summary.items(), key=lambda x: -x[1])[:20]:
            print(f"  {cust}: {count} hosts")
        if len(customers_summary) > 20:
            print(f"  ... and {len(customers_summary) - 20} more customers")

    except Exception as e:
        print(f"Error connecting to etcd: {e}")
        raise

    return all_hosts


def get_host_groups(hostname: str, customer: str = None) -> List[str]:
    """
    Determine which groups a host belongs to based on hostname patterns.

    Patterns detected:
    - Customer: group by customer name
    - Server type: gen-comp, sriov-comp, etcd, mphpp
    - Location: bos (Boston), chn (China)
    - Cluster: os1, os2, os-chn
    """
    groups = []
    hostname_lower = hostname.lower()

    # Customer group
    if customer:
        groups.append(f"customer-{customer}")

    # Server type groups
    if 'gen-comp' in hostname_lower:
        groups.append('gen-comp')
    if 'sriov-comp' in hostname_lower:
        groups.append('sriov-comp')
    if hostname_lower.startswith('etcd'):
        groups.append('etcd')
    if 'mphpp' in hostname_lower:
        groups.append('mphpp')

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

    # Combined groups (cluster + type)
    if 'os1-' in hostname_lower and 'sriov-comp' in hostname_lower:
        groups.append('os1-sriov')
    if 'os1-' in hostname_lower and 'gen-comp' in hostname_lower:
        groups.append('os1-gen')
    if 'os2-' in hostname_lower and 'sriov-comp' in hostname_lower:
        groups.append('os2-sriov')
    if 'os2-' in hostname_lower and 'gen-comp' in hostname_lower:
        groups.append('os2-gen')
    if 'os-chn-' in hostname_lower and 'sriov-comp' in hostname_lower:
        groups.append('chn-sriov')
    if 'os-chn-' in hostname_lower and 'gen-comp' in hostname_lower:
        groups.append('chn-gen')

    return groups


def get_or_create_group(
    session: requests.Session,
    inventory_id: int,
    group_name: str,
    group_cache: Dict[str, int]
) -> int:
    """Get or create a group in AWX inventory."""
    # Check cache first
    if group_name in group_cache:
        return group_cache[group_name]

    url = f"{get_awx_api_url()}/groups/"

    # Check if group exists
    response = session.get(url, params={'name': group_name, 'inventory': inventory_id})
    response.raise_for_status()

    existing = response.json().get('results', [])
    if existing:
        group_id = existing[0]['id']
        group_cache[group_name] = group_id
        return group_id

    # Create new group
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

    # Check if host is already in group
    response = session.get(url, params={'id': host_id})
    response.raise_for_status()

    existing = response.json().get('results', [])
    if any(h['id'] == host_id for h in existing):
        return  # Already in group

    # Add host to group
    payload = {"id": host_id}
    response = session.post(url, json=payload)
    response.raise_for_status()


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


def create_inventory(session: requests.Session, org_id: int) -> int:
    """Create an inventory in AWX or return existing one."""
    url = f"{get_awx_api_url()}/inventories/"

    # Check if inventory already exists
    response = session.get(url, params={'name': INVENTORY_NAME})
    response.raise_for_status()

    existing = response.json().get('results', [])
    if existing:
        inv_id = existing[0]['id']
        print(f"Inventory '{INVENTORY_NAME}' already exists (ID: {inv_id})")
        return inv_id

    # Create new inventory
    payload = {
        "name": INVENTORY_NAME,
        "description": "Inventory synced from etcd",
        "organization": org_id
    }

    response = session.post(url, json=payload)
    response.raise_for_status()

    inv_id = response.json()['id']
    print(f"Created inventory '{INVENTORY_NAME}' (ID: {inv_id})")
    return inv_id


def add_hosts_to_inventory(
    session: requests.Session,
    inventory_id: int,
    hosts: Dict[str, Dict[str, Any]]
) -> Dict[str, int]:
    """Add hosts to the AWX inventory and return hostname->id mapping."""
    url = f"{get_awx_api_url()}/hosts/"
    host_id_map = {}

    for hostname, host_info in hosts.items():
        # Check if host already exists in this inventory
        check_response = session.get(
            url,
            params={'name': hostname, 'inventory': inventory_id}
        )
        check_response.raise_for_status()

        existing = check_response.json().get('results', [])
        if existing:
            host_id = existing[0]['id']
            host_id_map[hostname] = host_id

            # Update host variables if needed
            current_vars = existing[0].get('variables', '{}')
            new_variables = {
                'ansible_host': host_info['ip'],
                'private_ip': host_info.get('private_ip'),
                'public_ip': host_info.get('public_ip'),
                'all_ips': host_info.get('all_ips'),
                'customer': host_info.get('customer')
            }
            new_variables = {k: v for k, v in new_variables.items() if v is not None}

            # Update if variables changed
            if json.dumps(new_variables, sort_keys=True) != current_vars:
                update_url = f"{get_awx_api_url()}/hosts/{host_id}/"
                session.patch(update_url, json={"variables": json.dumps(new_variables)})

            continue

        # Build host variables
        variables = {
            'ansible_host': host_info['ip'],
            'private_ip': host_info.get('private_ip'),
            'public_ip': host_info.get('public_ip'),
            'all_ips': host_info.get('all_ips'),
            'customer': host_info.get('customer')
        }

        # Remove None values
        variables = {k: v for k, v in variables.items() if v is not None}

        # Create new host
        payload = {
            "name": hostname,
            "inventory": inventory_id,
            "variables": json.dumps(variables)
        }

        response = session.post(url, json=payload)
        response.raise_for_status()

        host_id = response.json()['id']
        print(f"Added host '{hostname}' with IP {host_info['ip']} (ID: {host_id})")
        host_id_map[hostname] = host_id

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
        groups = get_host_groups(hostname, customer)
        host_id = host_id_map.get(hostname)

        if not host_id:
            continue

        for group_name in groups:
            # Track group members
            if group_name not in group_members:
                group_members[group_name] = []
            group_members[group_name].append(hostname)

            # Create/get group and add host
            group_id = get_or_create_group(session, inventory_id, group_name, group_cache)
            add_host_to_group(session, group_id, host_id)

    return group_members


def main():
    print("=" * 60)
    print("etcd to AWX Inventory Sync")
    print("=" * 60)

    # Check required environment variables
    check_required_env_vars()

    # Step 1: Get hosts from etcd
    print("\n[1] Fetching hosts from etcd...")
    hosts = get_hosts_from_etcd()

    if not hosts:
        print("No hosts found in etcd. Exiting.")
        return

    # Step 2: Create authenticated AWX session
    print("\n[2] Authenticating with AWX...")
    session = create_awx_session()

    # Test connection
    try:
        response = session.get(f"{get_awx_api_url()}/ping/")
        response.raise_for_status()
        print("Successfully connected to AWX")
    except Exception as e:
        print(f"Failed to connect to AWX: {e}")
        return

    # Step 3: Get organization
    print("\n[3] Getting organization...")
    org_id = get_or_create_organization(session)

    # Step 4: Create inventory
    print("\n[4] Creating inventory...")
    inventory_id = create_inventory(session, org_id)

    # Step 5: Add hosts to inventory
    print("\n[5] Adding hosts to inventory...")
    host_id_map = add_hosts_to_inventory(session, inventory_id, hosts)

    # Step 6: Create groups and assign hosts
    print("\n[6] Creating groups and assigning hosts...")
    group_members = create_groups_and_assign_hosts(session, inventory_id, hosts, host_id_map)

    print("\n" + "=" * 60)
    print("Sync completed!")
    print(f"Inventory: {INVENTORY_NAME} (ID: {inventory_id})")
    print(f"Hosts added/found: {len(host_id_map)}")
    print(f"\nGroups created/updated: {len(group_members)}")
    for group_name, members in sorted(group_members.items()):
        print(f"  - {group_name}: {len(members)} hosts")
    print("=" * 60)


if __name__ == "__main__":
    main()
