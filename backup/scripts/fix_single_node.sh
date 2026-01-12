#!/bin/bash
# Fix Ceph Single Node Warnings

set -e

echo "=== Diagnosing Ceph Cluster ==="

# Check cluster status
echo "Cluster status:"
sudo cephadm shell -- ceph -s

echo ""
echo "=== Checking OSDs ==="
sudo cephadm shell -- ceph osd tree
sudo cephadm shell -- ceph osd stat

echo ""
echo "=== Checking Pools ==="
sudo cephadm shell -- ceph osd pool ls detail

echo ""
echo "=== Checking PG Status ==="
sudo cephadm shell -- ceph pg stat
sudo cephadm shell -- ceph pg dump | grep -E "^PG_STAT|inactive|unclean" | head -20

echo ""
echo "=== Fixing Single Node Configuration ==="

# Enable pool size 1 (required for single node)
echo "Enabling single replica pools..."
sudo cephadm shell -- ceph config set global mon_allow_pool_size_one true

# Get all pools
POOLS=$(sudo cephadm shell -- ceph osd pool ls)

# Set size and min_size to 1 for all pools (single node)
for pool in $POOLS; do
    echo "Fixing pool: $pool"
    sudo cephadm shell -- ceph osd pool set $pool size 1
    sudo cephadm shell -- ceph osd pool set $pool min_size 1
done

echo ""
echo "=== Checking if OSDs exist ==="
OSD_COUNT=$(sudo cephadm shell -- ceph osd stat | grep -oP '\d+(?= osds)')
echo "OSD Count: $OSD_COUNT"

if [ "$OSD_COUNT" -eq 0 ]; then
    echo ""
    echo "WARNING: No OSDs found!"
    echo "You need to add storage devices."
    echo ""
    echo "Check available devices:"
    sudo cephadm shell -- ceph orch device ls
    echo ""
    echo "To add devices, run:"
    echo "  sudo cephadm shell -- ceph orch apply osd --all-available-devices"
    echo ""
    echo "Or to check what devices are available on the system:"
    lsblk
fi

echo ""
echo "=== Waiting for cluster to stabilize ==="
sleep 10

echo ""
echo "=== Final Status ==="
sudo cephadm shell -- ceph -s
sudo cephadm shell -- ceph health detail

echo ""
echo "=== Fix Applied ==="
echo "All pools now set to size=1 and min_size=1 for single node"
echo ""
echo "If you still see warnings:"
echo "1. Make sure you have at least 1 OSD created"
echo "2. Wait a few minutes for PGs to become active"
echo "3. Check: sudo cephadm shell -- ceph pg stat"
