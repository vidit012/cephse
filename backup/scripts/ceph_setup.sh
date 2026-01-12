#!/bin/bash
# Ceph Standalone Installation with Storage Tiering
# For Ubuntu 24.04 - Single Node Setup

set -e

echo "=== Ceph Installation Started ==="

# Update system
echo "Step 1: Updating system..."
sudo apt update
sudo apt upgrade -y

# Install dependencies
echo "Step 2: Installing dependencies..."
sudo apt install -y curl wget apt-transport-https ca-certificates gnupg lsb-release python3 python3-pip docker.io

# Install Cephadm
echo "Step 3: Installing Cephadm..."
curl --silent --remote-name --location https://github.com/ceph/ceph/raw/reef/src/cephadm/cephadm
chmod +x cephadm
sudo mv cephadm /usr/local/bin/

# Add Ceph repository
echo "Step 4: Adding Ceph repository..."
sudo /usr/local/bin/cephadm add-repo --release reef
sudo /usr/local/bin/cephadm install

# Get VM IP
VM_IP=$(hostname -I | awk '{print $1}')
echo "VM IP detected: $VM_IP"

# Bootstrap Ceph cluster
echo "Step 5: Bootstrapping Ceph cluster (this will take 5-10 minutes)..."
sudo cephadm bootstrap \
    --mon-ip $VM_IP \
    --initial-dashboard-user admin \
    --initial-dashboard-password admin123 \
    --allow-fqdn-hostname \
    --single-host-defaults \
    --skip-monitoring-stack

echo "=== Ceph Bootstrap Complete ==="

# Install ceph-common for CLI commands
echo "Step 6: Installing ceph-common..."
sudo apt install -y ceph-common

# Wait for cluster to be ready
echo "Waiting for cluster to stabilize..."
sleep 30

echo "=== Checking cluster status ==="
sudo ceph -s

echo ""
echo "=== Checking available devices ==="
sudo ceph orch device ls

echo ""
echo "=== INSTALLATION COMPLETE ==="
echo "Dashboard URL: https://localhost:8443"
echo "Username: admin"
echo "Password: admin123"
echo ""
echo "Next: Run ceph_create_osds.sh to add storage devices"
