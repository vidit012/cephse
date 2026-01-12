#!/bin/bash
# Setup Ceph Storage Tiering (Cache Tiering)

set -e

echo "=== Setting up Ceph Storage Tiering ==="

# Check cluster status
echo "Checking cluster status..."
sudo ceph -s

# Create storage pools
echo "Step 1: Creating storage pools..."

# Cold pool (backing storage)
sudo ceph osd pool create cold_pool 32 32
sudo ceph osd pool set cold_pool size 1
sudo ceph osd pool set cold_pool min_size 1

# Hot pool (cache tier)
sudo ceph osd pool create hot_pool 32 32
sudo ceph osd pool set hot_pool size 1
sudo ceph osd pool set hot_pool min_size 1

echo "Pools created"

# Setup cache tiering
echo "Step 2: Setting up cache tier..."

# Add hot_pool as cache tier for cold_pool
sudo ceph osd tier add cold_pool hot_pool

# Set cache mode to writeback
sudo ceph osd tier cache-mode hot_pool writeback

# Set hot_pool as overlay for cold_pool
sudo ceph osd tier set-overlay cold_pool hot_pool

echo "Cache tier configured"

# Configure cache parameters
echo "Step 3: Configuring cache parameters..."

# Hit set configuration
sudo ceph osd pool set hot_pool hit_set_type bloom
sudo ceph osd pool set hot_pool hit_set_count 12
sudo ceph osd pool set hot_pool hit_set_period 14400

# Cache size limits
sudo ceph osd pool set hot_pool target_max_bytes 5368709120  # 5GB
sudo ceph osd pool set hot_pool target_max_objects 100000

# Promotion/demotion thresholds
sudo ceph osd pool set hot_pool min_read_recency_for_promote 2
sudo ceph osd pool set hot_pool min_write_recency_for_promote 2
sudo ceph osd pool set hot_pool cache_target_dirty_ratio 0.4
sudo ceph osd pool set hot_pool cache_target_dirty_high_ratio 0.6
sudo ceph osd pool set hot_pool cache_target_full_ratio 0.8

echo "Cache parameters configured"

# Enable RBD application
echo "Step 4: Enabling RBD..."
sudo ceph osd pool application enable cold_pool rbd
sudo ceph osd pool application enable hot_pool rbd

# Create test RBD image
echo "Step 5: Creating test RBD image..."
sudo rbd create test_image --size 5G --pool cold_pool

echo ""
echo "=== Storage Tiering Setup Complete ==="
echo ""
echo "Configuration:"
echo "  Cold Pool: cold_pool (backing storage)"
echo "  Hot Pool: hot_pool (cache - 5GB max)"
echo "  Cache Mode: writeback"
echo "  Test Image: test_image (5GB)"
echo ""
echo "Monitor commands:"
echo "  sudo ceph osd pool stats"
echo "  sudo ceph osd dump | grep tier"
echo "  sudo rbd ls cold_pool"
