#!/bin/bash
# Quick command to create warm pool and setup 3-tier system

FSNAME="tiercephfs"

echo "Creating warm pool..."
ceph osd pool create cephfs.${FSNAME}.warm 32
ceph osd pool set cephfs.${FSNAME}.warm size 1
ceph osd pool set cephfs.${FSNAME}.warm min_size 1

echo "Adding warm pool to filesystem..."
ceph fs add_data_pool $FSNAME cephfs.${FSNAME}.warm

echo ""
echo "Verifying configuration..."
ceph fs ls

echo ""
echo "✓ 3-Tier setup complete:"
echo "  - cephfs.tiercephfs.data (Tier 1 - default)"
echo "  - cephfs.tiercephfs.warm (Tier 2 - NEW)"
echo "  - cephfs.tiercephfs.cold (Tier 3 - existing)"
