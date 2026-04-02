# VPN Enforcement Plan

## Goal

Before allowing `/awx run` (playbook execution) and `/svc restart` (service restart), verify the requesting user is connected to VPN. If not on VPN, reject the action with a message.

## Approach: VPN Verification API

The user confirmed there is an existing internal API that can check VPN status by username/email.

## Implementation Plan

### 1. VPN Client Module (`vpn/vpn_check.py`)

```python
import aiohttp
import logging

logger = logging.getLogger(__name__)

class VPNChecker:
    """Check if a Slack user is connected to VPN via internal API."""

    def __init__(self, api_url: str, api_token: str = None):
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token

    async def is_user_on_vpn(self, email: str) -> bool:
        """Query the VPN verification API for user's connection status."""
        headers = {}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api_url}/check",  # adjust endpoint as needed
                    params={"email": email},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("connected", False)
                    else:
                        logger.warning(f"VPN API returned {resp.status}")
                        return False
        except Exception as e:
            logger.error(f"VPN check failed: {e}")
            # Fail-open or fail-closed? Decide based on risk tolerance.
            # Default: fail-closed (deny if can't verify)
            return False
```

### 2. Integration Points in `src/agent.py`

#### a. Resolve Slack user_id to email
```python
user_info = await client.users_info(user=user_id)
email = user_info["user"]["profile"].get("email", "")
```

#### b. Check VPN before restart (in `_initiate_restart_confirmation`)
```python
if not await self.vpn_checker.is_user_on_vpn(email):
    await client.chat_postMessage(
        channel=channel_id,
        text=f":no_entry: <@{user_id}> You must be connected to VPN to restart services.",
    )
    return
```

#### c. Check VPN before AWX playbook run (in `_process_message` or the awx-playbook MCP)
Same pattern -- check before `route_action("awx-playbook", "run-playbook", ...)`.

### 3. Configuration

Add to `k8s/configmap.yaml`:
```yaml
VPN_CHECK_API_URL: "https://vpn-check.internal.example.com/api/v1"
```

Add to K8s secrets (via `deploy.sh`):
```
VPN_CHECK_API_TOKEN=<token>
```

Add to `.env.example`:
```
VPN_CHECK_API_URL=https://vpn-check.internal.example.com/api/v1
VPN_CHECK_API_TOKEN=your-vpn-api-token
```

### 4. Agent Init

In `SlackMCPAgent.__init__`:
```python
vpn_api_url = os.environ.get("VPN_CHECK_API_URL")
if vpn_api_url:
    from vpn.vpn_check import VPNChecker
    self.vpn_checker = VPNChecker(vpn_api_url, os.environ.get("VPN_CHECK_API_TOKEN"))
else:
    self.vpn_checker = None  # VPN check disabled
```

### 5. Protected Actions

| Command | Action | VPN Required |
|---------|--------|-------------|
| `/svc restart` | All roles | Yes |
| `/svc start` | All roles | Yes |
| `/svc stop` | All roles | Yes |
| `/awx run` | Playbook execution | Yes |
| `/svc check` | Read-only | No |
| `/svc logs` | Read-only | No |
| `/svc version` | Read-only | No |
| `/awx list` | Read-only | No |
| `/awx job status` | Read-only | No |
| `/tf *` | TBD | TBD |
| `/pods *` | Read-only | No |

### 6. User Experience

When VPN check fails:
```
:no_entry: @username You must be connected to VPN to perform this action.
Connect to VPN and try again.
```

## Status: Paused

Waiting for:
- VPN API endpoint URL and authentication details
- Confirmation of which actions should be protected
- Decision on fail-open vs fail-closed when API is unreachable
