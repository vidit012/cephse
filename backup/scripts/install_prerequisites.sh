#!/bin/bash
# Install Ceph Prerequisites on Ubuntu 24.04

set -e

echo "=== Installing Ceph Prerequisites ==="

# Update system
echo "Step 1: Updating package lists..."
sudo apt update

# Install Python 3 (should already be installed on Ubuntu 24.04)
echo "Step 2: Installing Python 3..."
sudo apt install -y python3 python3-pip python3-venv

# Systemd (already installed on Ubuntu)
echo "Step 3: Checking systemd..."
systemctl --version

# Install container runtime (Docker)
echo "Step 4: Installing Docker..."
sudo apt install -y docker.io
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker $USER

# Install Podman (alternative container runtime)
echo "Step 5: Installing Podman..."
sudo apt install -y podman

# Install time synchronization (Chrony)
echo "Step 6: Installing Chrony for time synchronization..."
sudo apt install -y chrony
sudo systemctl enable chrony
sudo systemctl start chrony

# Install LVM2
echo "Step 7: Installing LVM2..."
sudo apt install -y lvm2

# Install additional useful tools
echo "Step 8: Installing additional tools..."
sudo apt install -y curl wget gnupg ca-certificates apt-transport-https

echo ""
echo "=== Verifying installations ==="
echo "Python version:"
python3 --version

echo ""
echo "Docker version:"
docker --version

echo ""
echo "Podman version:"
podman --version

echo ""
echo "Chrony status:"
sudo systemctl status chrony --no-pager | head -5

echo ""
echo "LVM version:"
sudo lvm version

echo ""
echo "=== Prerequisites Installation Complete ==="
echo ""
echo "Note: You may need to log out and log back in for Docker group membership to take effect"
echo "Or run: newgrp docker"
echo ""
echo "Next: Run ./ceph_setup.sh to install Ceph"
