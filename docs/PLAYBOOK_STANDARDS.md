# Playbook Standards for AWX Integration

This document defines the standards and best practices for creating Ansible playbooks that work seamlessly with our AWX and Slack bot infrastructure.

## Table of Contents

- [Overview](#overview)
- [Host Targeting](#host-targeting)
- [Delegation Rules](#delegation-rules)
- [Available Ansible Collections](#available-ansible-collections)
- [SSH and Authentication](#ssh-and-authentication)
- [Variable Management](#variable-management)
- [Playbook Structure Template](#playbook-structure-template)
- [Inventory Requirements](#inventory-requirements)
- [Common Errors and Solutions](#common-errors-and-solutions)
- [Validation Checklist](#validation-checklist)

---

## Overview

Our infrastructure uses:
- **AWX** for Ansible automation
- **Slack bot** for triggering playbooks via natural language
- **Custom Execution Environment** with pre-installed collections
- **Dynamic inventories** created from etcd

Following these standards ensures playbooks run successfully without debugging cycles.

---

## Host Targeting

### Use Simple Role Names

Inventories created via Slack bot automatically create these groups:
- `{role}` - Plain role name (e.g., `tps`, `harjo`, `mim`)
- `role-{role}` - Prefixed role name
- `customer-{domain}` - Customer/domain group

**Always use the plain role name in playbooks:**

```yaml
# CORRECT - Use role name directly
- name: Deploy TPS Service
  hosts: tps

# CORRECT - Use role name
- name: Deploy Harjo Service
  hosts: harjo

# CORRECT - Target all hosts in inventory
- name: Common Setup
  hosts: all

# INCORRECT - Don't use prefixed names
- name: Deploy TPS Service
  hosts: role-tps  # Won't match!
```

### Naming Convention

| Playbook File | `hosts:` Value |
|---------------|----------------|
| `tps-setup.yml` | `tps` |
| `harjo-setup.yml` | `harjo` |
| `mim-setup.yml` | `mim` |
| `mongooseim-setup.yml` | `mongooseim` |
| `common-setup.yml` | `all` |

---

## Delegation Rules

### The Problem

The AWX Execution Environment (runner container) does **NOT** have `sudo` installed. Tasks that delegate to localhost and require privilege escalation will fail.

### Rules

#### Run Package Installs on Target Host

```yaml
# CORRECT - Runs on target host (has sudo)
- name: Install required packages
  apt:
    name:
      - curl
      - docker.io
    state: present
  become: yes

# INCORRECT - Fails because AWX runner has no sudo
- name: Install required packages
  apt:
    name: curl
  delegate_to: localhost
  become: yes
```

#### Delegate to Localhost Only for Non-Privileged Tasks

```yaml
# CORRECT - API calls don't need sudo
- name: Call external API
  uri:
    url: https://api.example.com
    method: GET
  delegate_to: localhost
  register: api_response

# CORRECT - Local file operations (in allowed paths)
- name: Generate temporary config
  template:
    src: config.j2
    dest: /tmp/config.yml
  delegate_to: localhost
  become: no  # Explicitly disable

# CORRECT - Run local script
- name: Run local validation
  command: python3 /tmp/validate.py
  delegate_to: localhost
  become: no
```

#### Summary Table

| Task Type | `delegate_to: localhost` | `become: yes` | Works? |
|-----------|--------------------------|---------------|--------|
| Install packages | No | Yes | Yes |
| Install packages | Yes | Yes | **NO** |
| API calls | Yes | No | Yes |
| Local file ops | Yes | No | Yes |
| Docker on target | No | Yes | Yes |

---

## Available Ansible Collections

The custom Execution Environment includes these collections:

| Collection | Version | Common Modules |
|------------|---------|----------------|
| `ansible.builtin` | (core) | `apt`, `yum`, `copy`, `template`, `command`, `shell` |
| `community.general` | 12.x | `snap`, `ufw`, `nmcli`, `modprobe` |
| `community.docker` | 5.x | `docker_container`, `docker_image`, `docker_network` |
| `community.crypto` | 3.x | `openssl_certificate`, `openssl_privatekey` |
| `ansible.posix` | 2.x | `sysctl`, `mount`, `selinux` |

### Using Collection Modules

```yaml
# CORRECT - Use fully qualified collection name (FQCN)
- name: Install snap package
  community.general.snap:
    name: certbot
    state: present

# CORRECT - Built-in modules don't need FQCN
- name: Install apt package
  apt:
    name: nginx
    state: present

# INCORRECT - Module not in available collections
- name: Manage Azure resource
  azure.azcollection.azure_rm_virtualmachine:  # Not installed!
    name: myvm
```

### Requesting New Collections

If you need a collection not listed above, request it to be added to the Execution Environment:
1. Update `execution-environment/Dockerfile`
2. Add: `ansible-galaxy collection install <collection-name> -p /usr/share/ansible/collections`
3. Rebuild and push the EE image

---

## SSH and Authentication

### How Authentication Works

1. AWX stores SSH credentials (private key)
2. Job templates have credentials attached
3. Inventories set `ansible_user: root`
4. Playbooks use `become: yes` for privilege escalation

### Rules

```yaml
# CORRECT - Let AWX handle authentication
- name: Deploy service
  hosts: myservice
  become: yes  # Escalate on target
  tasks:
    - name: Do something
      command: whoami

# INCORRECT - Don't hardcode credentials
- name: Deploy service
  hosts: myservice
  vars:
    ansible_ssh_private_key_file: /path/to/key  # Don't do this!
    ansible_user: someuser  # Set in inventory, not playbook
```

### Inventory Variables

Inventories created via Slack bot automatically set:
```yaml
ansible_user: root
```

Don't override this in playbooks unless absolutely necessary.

---

## Variable Management

### Always Provide Defaults

```yaml
# CORRECT - Variables with defaults
vars:
  docker_image: "{{ docker_image | default('myapp:latest') }}"
  service_port: "{{ service_port | default(8080) }}"
  config_path: "{{ config_path | default('/etc/myapp') }}"

tasks:
  - name: Deploy container
    community.docker.docker_container:
      name: myapp
      image: "{{ docker_image }}"
      ports:
        - "{{ service_port }}:8080"

# INCORRECT - Undefined variables cause failures
tasks:
  - name: Deploy container
    community.docker.docker_container:
      image: "{{ docker_image }}"  # Fails if not defined!
```

### Document Required Variables

If a variable MUST be provided, document it clearly:

```yaml
# Required Variables:
#   - gcp_project: GCP project ID
#   - docker_tag: Docker image tag to deploy
#
# Optional Variables:
#   - service_port: Port to expose (default: 8080)
#   - replicas: Number of containers (default: 1)

- name: Deploy Application
  hosts: myapp
  vars:
    service_port: "{{ service_port | default(8080) }}"
    replicas: "{{ replicas | default(1) }}"
```

### Variable Precedence

Variables are loaded in this order (later overrides earlier):
1. Role defaults
2. Inventory group_vars
3. Inventory host_vars
4. Playbook vars
5. Extra vars (passed via AWX)

---

## Playbook Structure Template

Use this template for new playbooks:

```yaml
---
# playbooks/<role>-setup.yml
#
# Description: Deploy <Role> service on Ubuntu 22.04
#
# Required Variables:
#   - None (uses defaults)
#
# Optional Variables:
#   - docker_image: Custom image (default: from GCP Artifact Registry)
#   - service_port: Port to expose (default: 8080)
#
# Inventory Requirements:
#   - Must have group named '<role>' with target hosts
#   - Hosts must have ansible_user set (default: root)

- name: Deploy <Role> on Ubuntu 22.04
  hosts: <role>
  become: yes
  gather_facts: yes

  vars:
    # Application settings
    app_name: "<role>"
    service_port: "{{ service_port | default(8080) }}"

    # Docker settings
    docker_registry: "us-east1-docker.pkg.dev/your-project/your-repo"
    docker_image: "{{ docker_image | default(docker_registry + '/' + app_name + ':latest') }}"

    # Paths
    config_dir: "/etc/{{ app_name }}"
    data_dir: "/var/lib/{{ app_name }}"

  tasks:
    # ===================
    # Prerequisites
    # ===================
    - name: Install required packages
      apt:
        name:
          - curl
          - ca-certificates
        state: present
        update_cache: yes

    # ===================
    # Docker Setup
    # ===================
    - name: Ensure Docker is installed
      apt:
        name: docker.io
        state: present

    - name: Start Docker service
      service:
        name: docker
        state: started
        enabled: yes

    # ===================
    # Application Deployment
    # ===================
    - name: Create config directory
      file:
        path: "{{ config_dir }}"
        state: directory
        mode: '0755'

    - name: Pull Docker image
      community.docker.docker_image:
        name: "{{ docker_image }}"
        source: pull

    - name: Deploy container
      community.docker.docker_container:
        name: "{{ app_name }}"
        image: "{{ docker_image }}"
        state: started
        restart_policy: unless-stopped
        ports:
          - "{{ service_port }}:8080"
        volumes:
          - "{{ config_dir }}:/config:ro"
          - "{{ data_dir }}:/data"

    # ===================
    # Verification
    # ===================
    - name: Wait for service to start
      wait_for:
        port: "{{ service_port }}"
        timeout: 60

    - name: Verify service is responding
      uri:
        url: "http://localhost:{{ service_port }}/health"
        status_code: 200
      register: health_check
      retries: 3
      delay: 10
```

---

## Inventory Requirements

### Creating Inventories via Slack Bot

```
create tps inventory for ubuntuqa
```

This creates:
- Inventory: `tps-ubuntuqa`
- Groups: `tps`, `role-tps`, `customer-ubuntuqa`
- Hosts: All hosts matching role `tps` in domain `ubuntuqa`
- Variables: `ansible_user: root`

### Inventory-Playbook Matching

| Slack Command | Inventory Created | Playbook `hosts:` |
|---------------|-------------------|-------------------|
| `create tps inventory for ubuntuqa` | `tps-ubuntuqa` | `tps` |
| `create harjo inventory for ubuntuqa` | `harjo-ubuntuqa` | `harjo` |
| `create mim inventory for prod` | `mim-prod` | `mim` |

---

## Common Errors and Solutions

### Error: "No hosts matched"

**Cause:** Playbook's `hosts:` doesn't match any group in inventory.

**Solution:** Use plain role name (e.g., `tps` not `role-tps`).

### Error: "sudo: command not found"

**Cause:** Task delegated to localhost with `become: yes`.

**Solution:** Remove `delegate_to: localhost` or remove `become: yes`.

### Error: "couldn't resolve module/action"

**Cause:** Module's collection not installed in Execution Environment.

**Solution:**
1. Use FQCN (e.g., `community.general.snap`)
2. If collection not available, request EE update

### Error: "Permission denied" / "UNREACHABLE"

**Cause:** SSH authentication failed.

**Solution:**
1. Verify AWX credential is attached to job template
2. Check inventory has `ansible_user: root`
3. Verify target host accepts the SSH key

### Error: "'variable_name' is undefined"

**Cause:** Required variable not set.

**Solution:** Add default value: `{{ var | default('value') }}`

---

## Validation Checklist

Before committing a playbook, verify:

- [ ] **Hosts:** Uses simple role name (not `role-` prefix)
- [ ] **Delegation:** No `delegate_to: localhost` with `become: yes`
- [ ] **Collections:** All modules from available collections
- [ ] **Variables:** All variables have defaults or documented as required
- [ ] **Credentials:** No hardcoded SSH keys or passwords
- [ ] **Package installs:** Run on target host, not localhost
- [ ] **File paths:** Use variables, not hardcoded paths
- [ ] **Documentation:** Header comment with description and variables

### Quick Test Commands

```bash
# Syntax check
ansible-playbook --syntax-check playbooks/myplaybook.yml

# List hosts that would be targeted
ansible-playbook --list-hosts -i inventory playbooks/myplaybook.yml

# Dry run (check mode)
ansible-playbook --check -i inventory playbooks/myplaybook.yml
```

---

## Version History

| Date | Version | Changes |
|------|---------|---------|
| 2026-02-26 | 1.0 | Initial version |

---

## Questions?

Contact the DevOps team or check the Slack bot help:
```
@vops-bot help
```
