#!/bin/bash
# Create Ceph OSDs

set -e

echo "=== Checking available devices ==="
sudo ceph orch device ls

echo ""
echo "Do you have unused physical disks? (yes/no)"
read -p "Answer: " has_disks

if [ "$has_disks" = "yes" ]; then
    echo "Adding all available devices as OSDs..."
    sudo ceph orch apply osd --all-available-devices
    echo "Waiting for OSDs to be created..."
    sleep 30
else
    echo "No physical disks detected."
    echo ""
    echo "To add disks to your VM:"
    echo "1. Shut down the VM"
    echo "2. VirtualBox: Settings → Storage → Controller: SATA → Add Hard Disk"
    echo "3. Create 2-3 new disks (20GB each)"
    echo "4. Start VM and run this script again"
    echo ""
    echo "For now, creating a minimal OSD on loop device for testing..."
    
    # Create loop device OSD
    sudo dd if=/dev/zero of=/var/lib/ceph/osd-loop0 bs=1G count=20
    sudo losetup /dev/loop10 /var/lib/ceph/osd-loop0
    sudo ceph orch daemon add osd cephvm:/dev/loop10 || echo "Manual OSD creation may be needed"
fi

echo ""
echo "=== Checking OSD status ==="
sudo ceph osd tree
sudo ceph -s

echo ""
echo "=== OSD Setup Complete ==="
echo "Next: Run ceph_setup_tiering.sh to configure storage tiering"
