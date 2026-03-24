# vops-bot — Security, Access Control & FAQ

This document addresses security concerns, access control design, and common questions about vops-bot raised during cross-team review.

---

## How vops-bot Connects to Infrastructure

A common concern is that vops-bot bypasses the VPN. It does not.

```
┌──────────────┐     Socket Mode      ┌──────────────────┐
│  User Phone  │  ──── Slack API ───► │  slack-mcp-agent  │
│  or Laptop   │     (outbound only)  │  (K8s pod, on VPN)│
└──────────────┘                      └────────┬─────────┘
                                               │
                                    ┌──────────▼──────────┐
                                    │  Internal Network    │
                                    │  (etcd, AWX, SSH,    │
                                    │   TFC, K8s API)      │
                                    └─────────────────────┘
```

- The bot runs **inside the Kubernetes cluster**, on the internal network. It is the bot that holds SSH credentials and talks to AWX, etcd, and Terraform Cloud — not the user.
- Slack Socket Mode means the bot opens an **outbound** websocket to Slack. No inbound ports are exposed. There is no public endpoint to attack.
- The user never touches SSH, never sees a password, never connects to a server directly. They send a Slack message, and the bot executes it using its own credentials.

**This is not a VPN bypass.** The user is not gaining network access to anything. They are sending a text message to a bot that already has access. This is the same model used by PagerDuty runbooks, Slack-integrated CI/CD (GitHub Actions notifications, Jenkins bots), and ChatOps tools across the industry.

---

## Current Security Controls

### What exists today

| Control | Status | Detail |
|---------|--------|--------|
| Slack workspace authentication | Active | Only members of the Unity Slack workspace can interact with the bot. Slack handles authentication (SSO, MFA). |
| Credential isolation | Active | SSH passwords, AWX tokens, GitHub PATs, and Slack tokens are stored in **Kubernetes Secrets**. Users never see or handle these credentials. Today, anyone with SSH access handles the raw password directly. |
| Command logging | Active | Every command is logged with user ID, command text, and timestamp in container logs. These logs are retained by the cluster's log pipeline. |
| Terraform plan-then-apply | Active | `/tf` operations show a plan summary and require an explicit `/tf confirm <run_id>` before any infrastructure change is applied. Users can review what will change before approving. Scaling down (destroying instances) requires a `--confirm` flag as an additional safeguard. |
| Queue deduplication | Active | The playbook queue prevents the same playbook + inventory combination from running twice simultaneously, preventing accidental duplicate executions. |
| Queue cancel ownership | Active | Users can only cancel their own queued requests, not other users' requests. |
| AWX job audit trail | Active | Every playbook run creates an AWX job with a full audit trail — who launched it, when, what playbook, what inventory, what the output was. This is queryable via the AWX UI and API. |
| Terraform Cloud audit trail | Active | Every TFC apply/cancel includes the Slack username in the comment. TFC maintains its own audit log of who applied what and when. |
| Pod alert controls | Active | Alert buttons (Resolve, Pause) update state on the pod-monitor service. Alert actions are posted visibly in Slack threads — the team sees who resolved or paused what. |

### What does NOT exist today (and what we are adding)

| Gap | Risk | Planned Fix |
|-----|------|-------------|
| No channel restriction | Any channel can run any command | Restrict destructive commands to a designated ops channel |
| No user/group restriction | Any Slack user can run any command | Restrict destructive commands to an approved user group (Slack user group or allowlist) |
| No confirmation on service restart/stop | `/svc restart` executes immediately | Add confirmation prompt for `restart`, `stop`, and `start` actions |
| No confirmation on playbook execution | `/awx run` executes immediately | Enable the existing confirmation workflow (the code is already built, just needs to be wired in) |
| No read-only mode | Cannot give a user read-only access | Add a read-only role that allows `check`, `status`, `logs`, `version`, `list` but blocks `restart`, `stop`, `run` |

---

## Addressing Specific Concerns

### "What if someone runs a destructive command from their phone?"

This concern applies to three commands: `/svc restart`, `/svc stop`, and `/awx run`. Here is the current risk and what changes:

**Current state (without vops-bot):** An engineer SSHes into the jump host, then into the target server, and runs `systemctl restart mongooseim`. There is no confirmation prompt, no audit trail beyond shell history on the jump host, and no visibility to the rest of the team. If they make a typo or target the wrong host, nobody knows until something breaks.

**With vops-bot (after planned controls):**
1. The command is typed in Slack — **visible to the entire channel**
2. The bot responds with a confirmation prompt: *"You are about to restart mongooseim on 5 hosts in lionamxp. Confirm or Cancel?"*
3. The user clicks Confirm
4. The action executes, and the result is posted in the same channel
5. The full command, user, timestamp, and outcome are logged

The phone scenario is a net improvement over the current process:
- **Visibility:** The team sees the command in Slack. With SSH, nobody sees anything.
- **Confirmation:** The bot asks "are you sure?" before destructive actions. SSH does not.
- **Audit:** The command is logged with the user's identity. SSH shell history on the target host is easily lost.
- **Scope control:** The bot targets the correct hosts via etcd discovery. With SSH, the engineer has to know which hosts to connect to and can easily miss one or hit the wrong one.

If the concern is specifically about accidental pocket-dials or unintended messages: Slack slash commands require typing `/svc restart mim in lionamxp` — this is not something that happens accidentally. With the confirmation prompt, it requires a second deliberate button click.

### "There is a reason we have to be on VPN to access servers"

The reason for VPN is to prevent unauthorized network access to internal systems. vops-bot does not change this:

- Users on vops-bot **do not get network access** to any server. They cannot SSH, they cannot browse, they cannot port-scan. They can send a predefined set of commands through a bot that is itself on the network.
- The bot's capabilities are **bounded** — it can only do what its code allows. A user cannot run arbitrary shell commands through the bot. They can run `systemctl show`, `systemctl restart`, etc. on services that are mapped in the service map. They cannot, for example, `rm -rf /` or read arbitrary files.
- This is comparable to how PagerDuty runbooks work: the user clicks a button in PagerDuty (which they can access from their phone, outside VPN), and PagerDuty executes a predefined action on the infrastructure via an agent that is on the network.

### "Phones are not company-issued devices — compliance violation?"

This is a Slack access policy question, not a vops-bot question. vops-bot does not change how Slack is accessed or from which devices. If Slack is currently accessible from personal devices, that is an existing policy decision. If Kolide or another MDM tool should restrict Slack access on non-compliant devices, that is an IT/security policy to enforce at the Slack or device level.

What vops-bot **does** change: today, engineers SSH into production servers from their laptops using raw passwords. The SSH password is stored in `.env` files, password managers, or shell history on their machines. If a laptop is compromised, the attacker has the SSH password. With vops-bot, credentials are in Kubernetes Secrets — they never exist on any engineer's device.

### "What happens if something breaks and you're not at a computer?"

Two points:

1. **Read-only commands work fine from a phone.** `/svc check mim in lionamxp` shows you the service status. `/pods unhealthy` shows you what's failing. `/awx job status 285` shows you the job output. These are the commands you would use to assess the situation.

2. **If you need to take action, you still need to understand the situation first.** Whether you're at a laptop SSHing into boxes or using vops-bot from a phone, you need to read logs, check status, and understand what's wrong before acting. vops-bot makes the diagnostic steps faster and easier from a phone. If the fix requires more than a restart, you're going to need a laptop regardless of whether vops-bot exists.

The recommended approach: enable read-only commands for all users, restrict destructive commands to a designated ops channel and user group, and require confirmation prompts for all destructive actions.

### "I just think we should be careful how we use this, and what kinds of things we allow to run"

This is the right approach. The plan is to implement a tiered access model:

| Tier | Commands | Who | Where |
|------|----------|-----|-------|
| **Read-only** | `check`, `status`, `version`, `logs`, `list`, `pods details`, `pods logs`, `pods unhealthy` | All engineers | Any channel |
| **Operational** | `restart`, `start`, `stop`, `run playbook`, `tf add/remove` | Ops team (Slack user group) | Designated ops channel |
| **Global** | `run playbook globally`, `tf confirm` | Ops leads | Designated ops channel + confirmation |

---

## What vops-bot Replaces (Time Savings)

These are real operational workflows and the time they take today:

| Task | Current Process | Time Today | With vops-bot | Time Saved |
|------|----------------|------------|---------------|------------|
| Check MIM status on 5 hosts | SSH to jump host → SSH to each host → run systemctl → repeat 5x | ~5-8 min | `/svc check mim in lionamxp` → results in 3s | ~5-8 min |
| Restart a service on 10 hosts | SSH to each host → run systemctl restart → verify → repeat 10x | ~10-15 min | `/svc restart mim in lionamxp` → done in 5s | ~10-15 min |
| Run a playbook on an inventory | Open AWX UI → find inventory → create/find template → set EE → launch → wait → check output | ~5-10 min | `/awx run playbook on inventory` → streams output | ~5-10 min |
| Check software version across a domain | SSH to each host → check etcd or service version → compile results | ~5-10 min | `/svc version mim in pubwxp` → table of versions | ~5-10 min |
| Scale up 2 servers in a domain | Edit .tf file → git commit/push → open TFC → wait for plan → review → approve → verify IPs | ~15-20 min | `/tf add 2 mphpp to aptus2` → review plan → `/tf confirm` | ~15-20 min |
| Check if any pod is unhealthy | `kubectl get pods` → scan output → `kubectl describe` on each suspect pod | ~3-5 min | Automatic alert in Slack with buttons, or `/pods unhealthy` | Proactive |

For an ops team handling 10-20 of these tasks per day, vops-bot saves **1-3 hours of manual work daily**. The value is not just speed — it's consistency (no typos, no missed hosts), visibility (the team sees every action in Slack), and audit trail (every command is logged with who ran it).

---

## Why ChatOps (Industry Context)

ChatOps is not a new concept. It is a well-established operational pattern used by:

- **GitHub** — Hubot for deployments, chatops for infrastructure management
- **Shopify** — ChatOps for incident response, deploys, and infrastructure scaling
- **Slack** — Uses its own bots for internal infrastructure operations
- **Netflix, Stripe, Datadog** — All use ChatOps patterns for operational tasks

The common pattern is: put operational commands in the chat tool where the team already communicates. This provides built-in visibility (everyone sees what's happening), audit trail (chat history), and reduces context switching (no need to open a separate UI).

vops-bot follows this same proven pattern, adapted to our specific infrastructure (etcd for host discovery, AWX for configuration management, TFC for infrastructure as code).

---

## Next Steps Toward Automation

vops-bot is one step in a larger automation strategy:

1. **Today:** Engineers run commands manually via SSH, AWX UI, TFC UI — no integration, no visibility, no audit trail
2. **vops-bot (current):** Engineers run the same commands via Slack — integrated, visible, audited, faster
3. **Next: Self-service guardrails** — Tiered access, confirmation workflows, read-only mode for broader team access
4. **Next: Automated responses** — Pod alerts that auto-restart crashed services, playbooks that auto-run on schedule, scaling that responds to metrics
5. **Future: LLM-assisted operations** — Natural language incident diagnosis, automated runbook selection, predictive alerting

Each step builds on the previous one. vops-bot provides the foundation: a unified command interface with audit trail and access controls that future automation can build on.

---

## Summary

| Concern | Answer |
|---------|--------|
| VPN bypass | No. The bot is on the VPN. Users talk to Slack, not to servers. |
| Phone access risk | Confirmation prompts prevent accidental actions. Read-only is safe from any device. |
| Non-company devices | This is a Slack access policy, not a vops-bot issue. vops-bot actually removes credentials from engineer devices. |
| Destructive commands | Confirmation workflow planned. Tiered access model separates read-only from operational. |
| What if something breaks remotely | Diagnostic commands work from phone. If the fix needs more than a restart, you need a laptop regardless. |
| Audit trail | Commands logged with user ID. AWX and TFC maintain their own audit logs. Slack chat history provides visibility. |
| Compliance | Bot credentials are in K8s Secrets, not on user devices. All actions are attributable to a specific Slack user. |
