#!/bin/bash
# Deploy CephFS Storage Tiering System

set -e

echo "=== CephFS Storage Tiering Deployment ==="
echo

# 1. Copy daemon to system location
echo "1. Installing tiering daemon..."
sudo cp cephfs_lc_daemon.py /usr/local/bin/cephfs_lc
sudo chmod +x /usr/local/bin/cephfs_lc

# 2. Create configuration
echo "2. Creating configuration..."
sudo mkdir -p /etc/ceph
sudo tee /etc/ceph/cephfs_lc.conf > /dev/null <<EOF
{
  "mount_point": "/cephfs",
  "scan_interval": 3600,
  "cold_age_days": 30,
  "hot_age_days": 15,
  "hot_pool": "cephfs.tiering.data",
  "cold_pool": "cephfs.tiering.cold",
  "cold_dir": "/cephfs/.tiers/cold",
  "exclude_paths": [
    "/cephfs/.tiers",
    "/cephfs/.snapshot"
  ],
  "enable_promotion": true,
  "enable_demotion": true,
  "log_level": "INFO",
  "log_file": "/var/log/ceph/cephfs_lc.log"
}
EOF

# 3. Create systemd service
echo "3. Creating systemd service..."
sudo tee /etc/systemd/system/cephfs-tiering.service > /dev/null <<EOF
[Unit]
Description=CephFS Storage Tiering Daemon
After=network.target ceph-mds.target
Wants=ceph-mds.target

[Service]
Type=simple
ExecStart=/usr/local/bin/cephfs_lc --config /etc/ceph/cephfs_lc.conf
Restart=on-failure
RestartSec=30
User=root
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 4. Setup log directory
echo "4. Setting up logging..."
sudo mkdir -p /var/log/ceph
sudo touch /var/log/ceph/cephfs_lc.log

# 5. Reload systemd
echo "5. Reloading systemd..."
sudo systemctl daemon-reload

echo
echo "=== Deployment Complete ==="
echo
echo "Start service:  sudo systemctl start cephfs-tiering"
echo "Enable on boot: sudo systemctl enable cephfs-tiering"
echo "Check status:   sudo systemctl status cephfs-tiering"
echo "View logs:      sudo journalctl -u cephfs-tiering -f"
echo "View log file:  sudo tail -f /var/log/ceph/cephfs_lc.log"
echo
echo "Run once (test): sudo /usr/local/bin/cephfs_lc --once"
