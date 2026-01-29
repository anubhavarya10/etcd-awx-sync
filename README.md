# etcd to AWX Inventory Sync

A Python script that synchronizes host information from etcd to AWX (Ansible Tower) inventory with smart filtering and natural language support.

## Features

- **Dynamic Discovery**: Automatically discovers all domains and roles from etcd (no hardcoded lists)
- **Smart Prompt Mode**: Create inventories using natural language (e.g., "mphpp servers for pubwxp")
- **Flexible Filtering**: Filter by domain, role, or both with exact matching
- **Auto Host Groups**: Creates groups based on customer, role, location, and cluster patterns
- **Multiple Auth Methods**: Supports OAuth2, Personal Access Token, and Basic auth
- **Idempotent**: Safe to run multiple times - updates existing hosts

## Current Statistics

| Metric | Count |
|--------|-------|
| Total Hosts | ~2,800+ |
| Domains/Customers | 142 |
| Discovered Roles | 56 |

Top roles: `mphpp` (915), `os` (335), `mim` (286), `www` (241), `mphhos` (140), `ts` (127)

## Quick Start

```bash
# Clone and install
git clone https://github.com/anubhavarya10/etcd-awx-sync.git
cd etcd-awx-sync
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your credentials

# Run
source .env && python3 etcd_to_awx.py --smart
```

## Usage Examples

### Smart Prompt Mode (Interactive)
```bash
python3 etcd_to_awx.py --smart
```
Then type natural language requests:
- `mphpp servers for pubwxp`
- `all ts servers`
- `mim for lolxp domain`
- `pubwxp` (domain only)
- `mphhos` (role only)

### Direct Prompt (Non-Interactive)
```bash
# Combined filter: role + domain
python3 etcd_to_awx.py --prompt "mphpp for pubwxp"

# Role only (all domains)
python3 etcd_to_awx.py --prompt "all mim servers"

# Domain only (all roles)
python3 etcd_to_awx.py --prompt "valxp inventory"
```

### CLI Flags
```bash
# Full sync (all hosts)
python3 etcd_to_awx.py --full

# Filter by domain
python3 etcd_to_awx.py --domain pubwxp

# Filter by role
python3 etcd_to_awx.py --role mphpp

# Combined filter
python3 etcd_to_awx.py --role mphpp --domain pubwxp

# Custom inventory name
python3 etcd_to_awx.py --role ts --domain valxp --inventory-name "ts-valxp-prod"

# List available domains
python3 etcd_to_awx.py --list-domains

# List available roles
python3 etcd_to_awx.py --list-roles
```

### Interactive Menu Mode
```bash
python3 etcd_to_awx.py
```
Choose from:
1. Full sync (all hosts)
2. Domain-specific inventory
3. Role-specific inventory
4. Combined filter (role + domain)

## Configuration

### Environment Variables

```bash
# etcd Configuration
ETCD_SERVER=10.0.25.44
ETCD_PORT=2379
ETCD_PREFIX=/discovery/

# AWX Configuration
AWX_SERVER=10.0.74.5

# AWX Authentication - OAuth2 Resource Owner Password-Based
AWX_CLIENT_ID=your_client_id
AWX_CLIENT_SECRET=your_client_secret
AWX_USERNAME=admin
AWX_PASSWORD=your_password
```

### Authentication Options

| Method | Required Variables |
|--------|-------------------|
| OAuth2 (Recommended) | `AWX_CLIENT_ID`, `AWX_CLIENT_SECRET`, `AWX_USERNAME`, `AWX_PASSWORD` |
| Personal Access Token | `AWX_TOKEN` |
| Basic Auth | `AWX_USERNAME`, `AWX_PASSWORD` |

## Output Example

```
============================================================
etcd to AWX Inventory Sync
============================================================
Using OAuth2 Resource Owner Password-Based authentication

[1] Fetching hosts from etcd...
Connected to etcd at 10.0.25.44:2379

Total hosts found in etcd: 2862
Total domains/customers: 142
Total roles discovered: 56

Parsed prompt: domain=pubwxp, role=mphpp
Inventory name: mphpp-pubwxp

[2] Applying filters...
    Domain filter: pubwxp
    Role filter: mphpp
    Hosts after filtering: 72

[3] Authenticating with AWX...
Successfully obtained OAuth token
Successfully connected to AWX

[4] Getting organization...
Using organization: Default (ID: 1)

[5] Creating inventory 'mphpp-pubwxp'...
Inventory 'mphpp-pubwxp' already exists (ID: 4)

[6] Adding 72 hosts to inventory...
  Progress: 72/72 hosts processed...

[7] Creating groups and assigning hosts...

============================================================
SYNC COMPLETED!
============================================================
Inventory: mphpp-pubwxp (ID: 4)
Hosts synced: 72
Domain filter: pubwxp
Role filter: mphpp

Groups created/updated: 2
  - customer-pubwxp: 72 hosts
  - role-mphpp: 72 hosts
============================================================
```

## Host Groups

The script automatically creates groups based on hostname patterns:

| Group Type | Example | Description |
|------------|---------|-------------|
| Customer | `customer-pubwxp` | Based on etcd path |
| Role | `role-mphpp` | Extracted from hostname |
| Location | `location-bos` | Geographic location |
| Cluster | `cluster-os1` | Infrastructure cluster |
| Server Type | `gen-comp`, `sriov-comp` | Compute type |

## etcd Data Structure

Expected path format:
```
/discovery/<customer>/.../<hostname>/viv_privip     -> private IP
/discovery/<customer>/.../<hostname>/viv_pubip      -> public IP
/discovery/<customer>/.../<hostname>/viv_ipaddresses -> all IPs
```

## Role Extraction

Roles are extracted from hostnames using the pattern `<role>-<domain>-<id>`:
- `mphpp-pubwxp-010103-1.vivox.com` → role: `mphpp`
- `ts-valxp-010101-1.vivox.com` → role: `ts`
- `www5-lionamxp-024901-1.vivox.com` → role: `www`

Role filtering uses **exact match** only (e.g., `mim` won't match `mimmem`).

## Running from AWX

The recommended way to run scheduled syncs is directly from AWX:

1. Create an AWX Project pointing to this repository
2. Create a Job Template using `playbooks/sync_inventory.yml`
3. Add credentials via custom credential type or extra variables
4. Schedule to run twice daily (6 AM and 6 PM)

See **[docs/AWX_SETUP.md](docs/AWX_SETUP.md)** for detailed setup instructions.

### Quick AWX Reference

| Component | Value |
|-----------|-------|
| Repository | `https://github.com/anubhavarya10/etcd-awx-sync.git` |
| Playbook | `playbooks/sync_inventory.yml` |
| Branch | `main` |

## Troubleshooting

### OAuth Token Error
If you see `unauthorized_client`:
- Ensure AWX OAuth app uses "Resource Owner Password-Based" grant type
- Verify all 4 credentials are provided

### No Hosts Found
- Check `ETCD_PREFIX` matches your data structure
- Verify etcd contains `viv_privip` or `viv_pubip` keys

### Role Not Matching
- Role filtering uses exact match only
- Use `--list-roles` to see available roles

## License

MIT
