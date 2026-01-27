# AWX Setup Guide

This guide explains how to set up the etcd-to-AWX sync to run directly from AWX.

## Overview

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│     GitHub      │────▶│   AWX Project   │────▶│  Job Template   │
│   Repository    │     │   (auto-sync)   │     │   (run sync)    │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                                                        │
                                                        ▼
                              ┌─────────────────┐     ┌─────────────────┐
                              │   AWX Inventory │◀────│      etcd       │
                              │ (central inv.)  │     │    (source)     │
                              └─────────────────┘     └─────────────────┘
```

## Step 1: Create Credentials in AWX

### 1.1 Create etcd/AWX Sync Credentials

1. Go to **AWX → Resources → Credentials**
2. Click **Add**
3. Fill in:
   - **Name**: `etcd-awx-sync-creds`
   - **Credential Type**: `Custom Credential Type` (create if needed)
   - Or use **Machine** credential and pass as extra vars

### 1.2 Create Custom Credential Type (Optional but Recommended)

1. Go to **AWX → Administration → Credential Types**
2. Click **Add**
3. Fill in:
   - **Name**: `etcd-awx-sync`
   - **Input Configuration**:
   ```yaml
   fields:
     - id: etcd_server
       type: string
       label: etcd Server
     - id: etcd_port
       type: string
       label: etcd Port
       default: "2379"
     - id: awx_client_id
       type: string
       label: AWX Client ID
     - id: awx_client_secret
       type: string
       label: AWX Client Secret
       secret: true
     - id: awx_username
       type: string
       label: AWX Username
     - id: awx_password
       type: string
       label: AWX Password
       secret: true
   required:
     - etcd_server
     - awx_client_id
     - awx_client_secret
     - awx_username
     - awx_password
   ```
   - **Injector Configuration**:
   ```yaml
   extra_vars:
     etcd_server: '{{ etcd_server }}'
     etcd_port: '{{ etcd_port }}'
     awx_client_id: '{{ awx_client_id }}'
     awx_client_secret: '{{ awx_client_secret }}'
     awx_username: '{{ awx_username }}'
     awx_password: '{{ awx_password }}'
   ```

## Step 2: Create the Project

1. Go to **AWX → Resources → Projects**
2. Click **Add**
3. Fill in:
   - **Name**: `etcd-awx-sync`
   - **Organization**: Select your organization
   - **Source Control Type**: `Git`
   - **Source Control URL**: `https://github.com/anubhavarya10/etcd-awx-sync.git`
   - **Source Control Branch**: `main`
   - **Options**: Check `Clean`, `Update Revision on Launch`
4. Click **Save**
5. Click the **Sync** button to pull the repository

## Step 3: Create the Job Template

1. Go to **AWX → Resources → Templates**
2. Click **Add → Job Template**
3. Fill in:
   - **Name**: `Sync etcd to AWX Inventory`
   - **Job Type**: `Run`
   - **Inventory**: Select any inventory (localhost will be used)
   - **Project**: `etcd-awx-sync`
   - **Playbook**: `playbooks/sync_inventory.yml`
   - **Credentials**: Add your `etcd-awx-sync-creds` credential
   - **Extra Variables** (if not using custom credential type):
   ```yaml
   etcd_server: "10.0.25.44"
   etcd_port: "2379"
   etcd_prefix: "/discovery/"
   awx_server: "10.0.74.5"
   awx_inventory_name: "central inventory"
   awx_client_id: "your_client_id"
   awx_client_secret: "your_client_secret"
   awx_username: "admin"
   awx_password: "your_password"
   ```
4. Click **Save**

## Step 4: Create a Schedule (Twice Daily)

1. Go to your Job Template **Sync etcd to AWX Inventory**
2. Click on **Schedules** tab
3. Click **Add**
4. Create first schedule:
   - **Name**: `Morning Sync (6 AM)`
   - **Start Date/Time**: Today at 06:00
   - **Repeat Frequency**: `Day`
   - **Every**: `1` day
5. Create second schedule:
   - **Name**: `Evening Sync (6 PM)`
   - **Start Date/Time**: Today at 18:00
   - **Repeat Frequency**: `Day`
   - **Every**: `1` day

## Step 5: Test the Sync

1. Go to **AWX → Resources → Templates**
2. Find **Sync etcd to AWX Inventory**
3. Click the **Launch** (rocket) button
4. Monitor the job output

## Alternative: Using Survey for Credentials

Instead of storing credentials in extra vars, you can create a Survey:

1. Edit the Job Template
2. Go to **Survey** tab
3. Click **Add**
4. Create questions for each credential:
   - AWX Client ID (required)
   - AWX Client Secret (required, encrypt: yes)
   - AWX Username (required)
   - AWX Password (required, encrypt: yes)
5. Enable the Survey

## Verification

After running the sync:

1. Go to **AWX → Resources → Inventories**
2. Find **central inventory**
3. Click on **Hosts** tab - you should see all synced hosts
4. Click on **Groups** tab - you should see auto-created groups

## Troubleshooting

### Job Fails with Module Not Found
Ensure the execution environment has the required Python packages. You may need to:
1. Create a custom Execution Environment with etcd3 and requests
2. Or use a container with these packages pre-installed

### Authentication Errors
- Verify AWX OAuth app is set to "Resource Owner Password-Based"
- Check all 4 credentials are provided (client_id, client_secret, username, password)

### No Hosts Found
- Verify etcd server is reachable from AWX
- Check the ETCD_PREFIX matches your data structure

## Quick Reference

| Item | Value |
|------|-------|
| Repository | https://github.com/anubhavarya10/etcd-awx-sync.git |
| Playbook | playbooks/sync_inventory.yml |
| Branch | main |
