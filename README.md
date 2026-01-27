# etcd to AWX Inventory Sync

A Python script that synchronizes host information from etcd to AWX (Ansible Tower) inventory.

## Features

- Fetches hosts from etcd with their IP addresses (private, public)
- Creates/updates inventory in AWX
- Automatically creates host groups based on hostname patterns:
  - **Customer groups**: `customer-<name>`
  - **Server type groups**: `gen-comp`, `sriov-comp`, `etcd`, `mphpp`
  - **Location groups**: `location-bos`, `location-chn`
  - **Cluster groups**: `cluster-os1`, `cluster-os2`, `cluster-os-chn`
  - **Combined groups**: `os1-sriov`, `os1-gen`, `os2-sriov`, `os2-gen`, `chn-sriov`, `chn-gen`
- Supports multiple AWX authentication methods
- Idempotent - safe to run multiple times

## Prerequisites

- Python 3.8+
- Access to etcd server
- Access to AWX server with API credentials

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/etcd-awx-sync.git
cd etcd-awx-sync

# Install dependencies
pip install -r requirements.txt
```

## Configuration

1. Copy the example environment file:
```bash
cp .env.example .env
```

2. Edit `.env` with your configuration:
```bash
# etcd Configuration
ETCD_SERVER=10.0.00.00
ETCD_PORT=****
ETCD_PREFIX=/discovery/

# AWX Configuration
AWX_SERVER=10.0.00.0
AWX_INVENTORY_NAME=central inventory

# AWX Authentication (choose one option)
AWX_CLIENT_ID=your_client_id
AWX_CLIENT_SECRET=your_client_secret
AWX_USERNAME=your_username
AWX_PASSWORD=your_password
```

## AWX Authentication Options

### Option 1: Personal Access Token (Recommended)
```bash
export AWX_TOKEN='your_personal_access_token'
```

### Option 2: OAuth2 Resource Owner Password-Based
```bash
export AWX_CLIENT_ID='your_client_id'
export AWX_CLIENT_SECRET='your_client_secret'
export AWX_USERNAME='your_username'
export AWX_PASSWORD='your_password'
```

### Option 3: Basic Username/Password
```bash
export AWX_USERNAME='your_username'
export AWX_PASSWORD='your_password'
```

## Usage

### Manual Run
```bash
# Source your environment file
source .env

# Or export variables directly
export ETCD_SERVER=10.0.00.00
export ETCD_PORT=****
export AWX_SERVER=10.0.00.00
export AWX_CLIENT_ID=your_client_id
export AWX_CLIENT_SECRET=your_client_secret
export AWX_USERNAME=admin
export AWX_PASSWORD=your_password

# Run the sync
python3 etcd_to_awx.py
```

### Scheduled Sync (Cron)

To run the sync twice daily (at 6 AM and 6 PM), add to crontab:

```bash
# Edit crontab
crontab -e

# Add these lines (adjust paths as needed)
0 6 * * * /path/to/etcd-awx-sync/run_sync.sh >> /var/log/etcd-awx-sync.log 2>&1
0 18 * * * /path/to/etcd-awx-sync/run_sync.sh >> /var/log/etcd-awx-sync.log 2>&1
```

Create a wrapper script `run_sync.sh`:
```bash
#!/bin/bash
cd /path/to/etcd-awx-sync
source .env
python3 etcd_to_awx.py
```

## etcd Data Structure

The script expects data in etcd with the following structure:
```
/discovery/<customer>/<subdirectory>/<hostname>/viv_privip  -> private IP
/discovery/<customer>/<subdirectory>/<hostname>/viv_pubip   -> public IP
/discovery/<customer>/<subdirectory>/<hostname>/viv_ipaddresses -> all IPs
```

## Host Variables

Each host in AWX will have the following variables:
- `ansible_host`: Primary IP (prefers private IP)
- `private_ip`: Private IP address
- `public_ip`: Public IP address
- `all_ips`: All IP addresses
- `customer`: Customer name from etcd path

## Output Example

```
============================================================
etcd to AWX Inventory Sync
============================================================
Using OAuth2 Resource Owner Password-Based authentication

[1] Fetching hosts from etcd...
Connected to etcd at 10.00.00.00:0000
Total hosts found in etcd: 360

[2] Authenticating with AWX...
Successfully obtained OAuth token
Successfully connected to AWX

[3] Getting organization...
Using organization: Default (ID: 1)

[4] Creating inventory...
Created inventory 'central inventory' (ID: 3)

[5] Adding hosts to inventory...
Added host 'server1.example.com' with IP 10.0.1.1 (ID: 1)
...

[6] Creating groups and assigning hosts...
Created group 'customer-vx' (ID: 1)
Created group 'gen-comp' (ID: 2)
...

============================================================
Sync completed!
Inventory: central inventory (ID: 3)
Hosts added/found: 360

Groups created/updated: 15
  - cluster-os1: 200 hosts
  - gen-comp: 100 hosts
  - sriov-comp: 260 hosts
  ...
============================================================
```

## Troubleshooting

### OAuth Token Error
If you see `unauthorized_client` error, ensure:
- Your AWX OAuth application uses "Resource Owner Password-Based" grant type
- All 4 credentials are provided: client_id, client_secret, username, password

### Connection Issues
- Verify etcd and AWX servers are reachable
- Check firewall rules for ports **** (etcd) and 80/443 (AWX)

### No Hosts Found
- Verify the `ETCD_PREFIX` matches your etcd data structure
- Check etcd contains data with `viv_privip` or `viv_pubip` keys

## License

MIT
