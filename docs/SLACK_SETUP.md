# Slack Integration Setup Guide

This guide walks you through setting up the Slack bot for etcd-awx-sync.

## Prerequisites

- A Slack workspace with admin access
- Python 3.8+ with `slack_bolt` and `slack_sdk` installed
- etcd and AWX credentials configured in `.env`

## Quick Start

1. Create a Slack App at https://api.slack.com/apps
2. Configure OAuth scopes and enable Socket Mode
3. Install the app to your workspace
4. Copy the tokens to your `.env` file
5. Run the bot: `python slack_bot.py`

## Step-by-Step Setup

### 1. Create a Slack App

1. Go to https://api.slack.com/apps
2. Click **Create New App**
3. Choose **From scratch**
4. Name: `Inventory Bot` (or your preference)
5. Select your workspace
6. Click **Create App**

### 2. Configure OAuth Scopes

Navigate to **OAuth & Permissions** in the sidebar.

#### Bot Token Scopes

Add these scopes under **Bot Token Scopes**:

| Scope | Purpose |
|-------|---------|
| `chat:write` | Send messages |
| `chat:write.public` | Send to channels the bot isn't in |
| `commands` | Handle slash commands |
| `im:write` | Send DMs |
| `im:history` | Read DM history (for interactive responses) |
| `users:read` | Read user info |

### 3. Enable Socket Mode

Socket Mode allows the bot to receive events without a public URL.

1. Navigate to **Socket Mode** in the sidebar
2. Toggle **Enable Socket Mode** to ON
3. Create an **App-Level Token**:
   - Click **Generate Token and Scopes**
   - Name: `socket-mode-token`
   - Add scope: `connections:write`
   - Click **Generate**
4. **Copy the token** (starts with `xapp-`) - this is your `SLACK_APP_TOKEN`

### 4. Create Slash Command

Navigate to **Slash Commands** in the sidebar.

Click **Create New Command**:

| Field | Value |
|-------|-------|
| Command | `/inventory` |
| Short Description | `Manage AWX inventories from Slack` |
| Usage Hint | `[sync|create|list-domains|list-roles|help]` |

Click **Save**.

### 5. Enable Interactivity

Navigate to **Interactivity & Shortcuts** in the sidebar.

1. Toggle **Interactivity** to ON
2. The Request URL is not needed for Socket Mode

### 6. Subscribe to Events (Optional)

If you want the bot to respond to @mentions:

1. Navigate to **Event Subscriptions**
2. Toggle **Enable Events** to ON
3. Under **Subscribe to bot events**, add:
   - `app_mention`
   - `message.im`

### 7. Install App to Workspace

1. Navigate to **Install App** in the sidebar
2. Click **Install to Workspace**
3. Review permissions and click **Allow**
4. **Copy the Bot User OAuth Token** (starts with `xoxb-`) - this is your `SLACK_BOT_TOKEN`

### 8. Get Signing Secret

1. Navigate to **Basic Information**
2. Scroll to **App Credentials**
3. **Copy the Signing Secret** - this is your `SLACK_SIGNING_SECRET`

### 9. Get Channel ID for Alerts

1. In Slack, right-click the channel where you want alerts
2. Click **View channel details**
3. Scroll to the bottom and **copy the Channel ID** (starts with `C`)

## Environment Variables

Add these to your `.env` file:

```bash
# Slack Configuration
SLACK_BOT_TOKEN=xoxb-your-bot-token-here
SLACK_APP_TOKEN=xapp-your-app-token-here
SLACK_SIGNING_SECRET=your-signing-secret-here
SLACK_CHANNEL_ID=C0123456789
```

### Token Reference

| Token | Starts With | Source |
|-------|-------------|--------|
| `SLACK_BOT_TOKEN` | `xoxb-` | OAuth & Permissions > Bot User OAuth Token |
| `SLACK_APP_TOKEN` | `xapp-` | Socket Mode > App-Level Token |
| `SLACK_SIGNING_SECRET` | varies | Basic Information > App Credentials |
| `SLACK_CHANNEL_ID` | `C` | Channel details in Slack |

## Running the Bot

### Start the Bot

```bash
# Load environment variables
source .env

# Run the bot
python slack_bot.py
```

### Expected Output

```
============================================================
etcd-awx-sync Slack Bot
============================================================
Using OAuth2 Resource Owner Password-Based authentication

Pre-warming etcd cache...
Cached 2862 hosts, 142 domains, 56 roles

Starting Slack bot in Socket Mode...
```

### Running as a Service (systemd)

Create `/etc/systemd/system/inventory-bot.service`:

```ini
[Unit]
Description=Inventory Slack Bot
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/etcd-awx-sync
EnvironmentFile=/path/to/etcd-awx-sync/.env
ExecStart=/usr/bin/python3 slack_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl enable inventory-bot
sudo systemctl start inventory-bot
```

## Available Commands

| Command | Description |
|---------|-------------|
| `/inventory help` | Show help message |
| `/inventory sync` | Run full sync (all hosts) |
| `/inventory create` | Open interactive dialog |
| `/inventory create <prompt>` | Smart create with natural language |
| `/inventory list-domains` | Show available domains |
| `/inventory list-roles` | Show available roles |

### Smart Prompt Examples

```
/inventory create mphpp for pubwxp
/inventory create all ts servers
/inventory create valxp inventory
/inventory create mim
```

## Using Slack Alerts (CLI)

You can also send Slack alerts from the command line:

```bash
# Send notification on sync completion
python etcd_to_awx.py --prompt "mphpp for pubwxp" --slack-alert

# Or with full sync
python etcd_to_awx.py --full --slack-alert
```

## Troubleshooting

### "SLACK_BOT_TOKEN environment variable is required"

Make sure you've exported the environment variables:

```bash
source .env
```

### "invalid_auth" Error

- Verify your `SLACK_BOT_TOKEN` starts with `xoxb-`
- Ensure the app is installed to your workspace
- Try reinstalling the app from the Slack App settings

### Slash Command Not Working

- Verify the slash command is configured in your Slack App
- Ensure the bot is running and connected
- Check the bot has `commands` scope

### Modal Not Opening

- Ensure `interactivity` is enabled in your Slack App
- Verify Socket Mode is enabled and connected
- Check the `SLACK_APP_TOKEN` is correct

### Messages Not Posting

- Verify `chat:write` scope is added
- For posting to channels, ensure `chat:write.public` scope
- Check the `SLACK_CHANNEL_ID` is correct

### Cache Not Refreshing

The bot caches etcd data for 5 minutes. To force refresh:

1. Wait 5 minutes, or
2. Restart the bot

## Security Notes

- **Never commit `.env` to version control**
- Keep tokens secure and rotate them periodically
- Use the principle of least privilege for OAuth scopes
- Consider using Slack's Enterprise Key Management for sensitive workspaces

## Architecture

```
┌─────────────────┐
│   Slack User    │
│ /inventory sync │
└────────┬────────┘
         │ Slash Command
         ▼
┌─────────────────┐     ┌─────────────────┐
│   Slack API     │◄───►│   Slack Bot     │
│  (Socket Mode)  │     │  (slack_bot.py) │
└─────────────────┘     └────────┬────────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              ┌──────────┐ ┌──────────┐ ┌──────────┐
              │   etcd   │ │   AWX    │ │  Slack   │
              │ (source) │ │  (dest)  │ │ Alerts   │
              └──────────┘ └──────────┘ └──────────┘
```
