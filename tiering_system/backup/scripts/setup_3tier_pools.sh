#!/bin/bash
# Setup 3-tier CephFS storage pools: data → warm → cold

set -e

FSNAME="tiercephfs"
FS_ID=$(ceph fs ls | grep "$FSNAME" | awk '{print $2}' | tr -d ',')

echo "=== Setting up 3-tier storage for CephFS: $FSNAME ==="

# Create three data pools
echo "[1/6] Checking data pool (default, tier 1)..."
ceph osd pool ls | grep "cephfs.${FSNAME}.data" || {
    echo "Creating data pool..."
    ceph osd pool create cephfs.${FSNAME}.data 64
    ceph osd pool set cephfs.${FSNAME}.data size 1
    ceph osd pool set cephfs.${FSNAME}.data min_size 1
}

echo "[2/6] Creating warm pool (tier 2)..."
ceph osd pool create cephfs.${FSNAME}.warm 32 || echo "Pool already exists"
ceph osd pool set cephfs.${FSNAME}.warm size 1
ceph osd pool set cephfs.${FSNAME}.warm min_size 1

echo "[3/6] Checking cold pool (tier 3)..."
ceph osd pool ls | grep "cephfs.${FSNAME}.cold" || {
    echo "Creating cold pool..."
    ceph osd pool create cephfs.${FSNAME}.cold 32
    ceph osd pool set cephfs.${FSNAME}.cold size 1
    ceph osd pool set cephfs.${FSNAME}.cold min_size 1
}

# Add all pools to the filesystem
echo "[4/6] Adding warm pool to filesystem..."
ceph fs add_data_pool $FSNAME cephfs.${FSNAME}.warm || echo "Already added"

echo "[5/6] Adding cold pool to filesystem..."
ceph fs add_data_pool $FSNAME cephfs.${FSNAME}.cold || echo "Already added"

# Verify setup
echo "[6/6] Verifying pool configuration..."
echo ""
echo "Filesystem pools:"
ceph fs ls | grep -A 10 "$FSNAME"
echo ""
echo "Pool details:"
ceph osd pool ls detail | grep "cephfs.${FSNAME}"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "3-Tier Configuration:"
echo "  Data (Tier 1): cephfs.${FSNAME}.data - New files start here (default)"
echo "  Warm (Tier 2): cephfs.${FSNAME}.warm - After 3 minutes"
echo "  Cold (Tier 3): cephfs.${FSNAME}.cold - After 6 minutes total"
echo ""
echo "Data pool is the default for new files (no configuration needed)"
echo ""
