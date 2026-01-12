#!/bin/bash
# CephFS Setup Script for Single-Node Ceph Cluster
# Run this on cephvm

set -e

echo "=== Creating CephFS with 3-Tier Storage (Hot/Warm/Cold) ==="

# Step 1: Create pools for CephFS
echo "Step 1: Creating pools..."

# Metadata pool (small, critical)
sudo ceph osd pool create cephfs_metadata 32 32
sudo ceph osd pool set cephfs_metadata size 1 --yes-i-really-mean-it
sudo ceph osd pool set cephfs_metadata min_size 1

# Hot pool (default data pool)
sudo ceph osd pool create cephfs_hot 64 64
sudo ceph osd pool set cephfs_hot size 1 --yes-i-really-mean-it
sudo ceph osd pool set cephfs_hot min_size 1

# Warm pool (additional data pool)
sudo ceph osd pool create cephfs_warm 64 64
sudo ceph osd pool set cephfs_warm size 1 --yes-i-really-mean-it
sudo ceph osd pool set cephfs_warm min_size 1

# Cold pool (additional data pool with compression)
sudo ceph osd pool create cephfs_cold 64 64
sudo ceph osd pool set cephfs_cold size 1 --yes-i-really-mean-it
sudo ceph osd pool set cephfs_cold min_size 1
sudo ceph osd pool set cephfs_cold compression_mode aggressive
sudo ceph osd pool set cephfs_cold compression_algorithm lz4

echo "Pools created successfully!"

# Step 2: Create the file system
echo "Step 2: Creating CephFS file system..."
sudo ceph fs new cephfs cephfs_metadata cephfs_hot

echo "CephFS created with metadata pool and hot data pool!"

# Step 3: Add additional data pools
echo "Step 3: Adding warm and cold data pools..."
sudo ceph fs add_data_pool cephfs cephfs_warm
sudo ceph fs add_data_pool cephfs cephfs_cold

echo "All data pools added!"

# Step 4: Deploy MDS (Metadata Server) service
echo "Step 4: Deploying MDS service..."

# Option A: Using count placement (deploys on any available host)
sudo ceph orch apply mds cephfs --placement="count:1"

# Option B: Using specific host placement (explicitly on cephvm)
# sudo ceph orch apply mds cephfs --placement="host:cephvm"

# Option C: Using label placement (if you add labels)
# sudo ceph orch host label add cephvm mds
# sudo ceph orch apply mds cephfs --placement="label:mds"

echo "MDS service deployment scheduled!"

# Step 5: Wait for MDS to be active
echo "Step 5: Waiting for MDS to become active..."
for i in {1..30}; do
    if sudo ceph fs status cephfs 2>/dev/null | grep -q "up:active"; then
        echo "MDS is active!"
        break
    fi
    echo "Waiting for MDS... ($i/30)"
    sleep 2
done

# Step 6: Show status
echo ""
echo "=== CephFS Status ==="
sudo ceph fs status cephfs

echo ""
echo "=== Pool List ==="
sudo ceph osd lspools

echo ""
echo "=== MDS Service ==="
sudo ceph orch ps | grep mds

echo ""
echo "=== Setup Complete! ==="
echo ""
echo "To mount CephFS:"
echo "  mkdir -p /mnt/cephfs"
echo "  mount -t ceph cephvm:/ /mnt/cephfs -o name=admin,secret=\$(sudo ceph auth get-key client.admin)"
echo ""
echo "To set default pool for directories:"
echo "  setfattr -n ceph.dir.layout.pool -v cephfs_hot /mnt/cephfs/hot_data"
echo "  setfattr -n ceph.dir.layout.pool -v cephfs_warm /mnt/cephfs/warm_data"
echo "  setfattr -n ceph.dir.layout.pool -v cephfs_cold /mnt/cephfs/cold_data"
