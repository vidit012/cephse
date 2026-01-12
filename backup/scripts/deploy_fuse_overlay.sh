#!/bin/bash
# Deploy FUSE Overlay for CephFS Transparent Tiering

set -e

echo "=== CephFS FUSE Overlay Deployment ==="
echo

# Check if running on VM
if [ ! -d "/cephfs" ]; then
    echo "Error: /cephfs not found. Is CephFS mounted?"
    exit 1
fi

# 1. Install FUSE dependencies
echo "1. Installing FUSE dependencies..."
sudo apt-get update -qq
sudo apt-get install -y fuse3 libfuse3-dev python3-pip

# Install Python FUSE library
sudo pip3 install fusepy

# 2. Copy FUSE overlay to system
echo "2. Installing FUSE overlay..."
sudo cp cephfs_fuse_overlay.py /usr/local/bin/cephfs_fuse_overlay
sudo chmod +x /usr/local/bin/cephfs_fuse_overlay

# 3. Create mount point
echo "3. Creating overlay mount point..."
sudo mkdir -p /cephfs-overlay

# 4. Configure FUSE to allow other users
echo "4. Configuring FUSE permissions..."
if ! grep -q "^user_allow_other" /etc/fuse.conf 2>/dev/null; then
    echo "user_allow_other" | sudo tee -a /etc/fuse.conf > /dev/null
fi

# 5. Install systemd service
echo "5. Installing systemd service..."
sudo cp cephfs-fuse-overlay.service /etc/systemd/system/
sudo systemctl daemon-reload

# 6. Create log directory
echo "6. Setting up logging..."
sudo mkdir -p /var/log/ceph
sudo touch /var/log/ceph/cephfs_fuse.log

echo
echo "=== Deployment Complete ==="
echo
echo "Test manually first:"
echo "  sudo /usr/local/bin/cephfs_fuse_overlay /cephfs /cephfs-overlay -f --allow-other"
echo
echo "In another terminal:"
echo "  ls /cephfs-overlay/"
echo "  cat /cephfs-overlay/test.txt"
echo
echo "Enable as service:"
echo "  sudo systemctl enable cephfs-fuse-overlay"
echo "  sudo systemctl start cephfs-fuse-overlay"
echo
echo "Check status:"
echo "  sudo systemctl status cephfs-fuse-overlay"
echo "  mount | grep cephfs"
echo
echo "View logs:"
echo "  sudo journalctl -u cephfs-fuse-overlay -f"
echo "  sudo tail -f /var/log/ceph/cephfs_fuse.log"
echo
echo "Unmount:"
echo "  sudo systemctl stop cephfs-fuse-overlay"
echo "  # OR: sudo fusermount -u /cephfs-overlay"
