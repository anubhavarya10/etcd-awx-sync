FROM quay.io/ansible/awx-ee:latest

USER root

# Install required Ansible collections
RUN ansible-galaxy collection install \
    community.general \
    community.docker \
    community.crypto \
    ansible.posix \
    --force

# Install additional Python packages
RUN pip3 install --no-cache-dir \
    docker>=6.0.0 \
    jmespath>=1.0.0 \
    netaddr>=0.8.0

USER 1000
