# Ceph Storage Tiering - Complete Implementation Plan

## Current Status

**Cluster State:**
- ✅ Ceph cluster: HEALTH_OK
- ✅ 1 Monitor (cephvm)
- ✅ 1 Manager (cephvm.jqliiv)
- ✅ 3 OSDs: All HDD class (20GB each, ~60GB total)
- ✅ Monitoring stack: Grafana, Prometheus, Alertmanager
- ❌ RGW (RADOS Gateway): NOT DEPLOYED
- ❌ Object storage pools: NOT CREATED
- Only 1 pool exists: .mgr (internal management pool)

## What We Need to Create

### Phase 1: Deploy RGW (RADOS Gateway)
1. Deploy RGW service
2. Create RGW system pools (metadata, data, index)
3. Create user credentials
4. Verify S3 API access

### Phase 2: Create Storage Tiers (Pools)
1. Hot pool (rgw.hot.data) - OSD 0,1
2. Cold pool (rgw.cold.data) - OSD 2
3. Configure storage classes mapping

### Phase 3: Setup Access Tracking
1. Install Python dependencies
2. Setup RocksDB
3. Deploy Lua hooks for RGW
4. Create access tracking service

### Phase 4: Build Tiering Engine
1. Promotion engine (cold → hot)
2. Demotion engine (hot → cold)
3. Policy configuration
4. Systemd services

### Phase 5: Testing & Validation
1. Create S3 buckets
2. Upload test objects
3. Monitor access tracking
4. Test automatic tiering
5. Verify transparency

---

## Detailed Implementation Steps

### PHASE 1: Deploy RGW

#### Step 1.1: Deploy RGW Service
```bash
# Deploy RGW with specific placement
sudo cephadm shell -- ceph orch apply rgw mystore \
  --placement="1 cephvm" \
  --port=8000
```

#### Step 1.2: Wait for RGW to Deploy
```bash
# Check RGW status
sudo cephadm shell -- ceph orch ls
# Wait until: rgw.mystore shows 1/1 running

# Verify RGW is running
sudo cephadm shell -- ceph -s
# Should show: rgw: 1 daemon active
```

#### Step 1.3: Create RGW User
```bash
# Create admin user for S3 access
sudo cephadm shell -- radosgw-admin user create \
  --uid=admin \
  --display-name="Admin User" \
  --access-key=ADMIN123 \
  --secret-key=ADMINSECRET123

# Create regular user for testing
sudo cephadm shell -- radosgw-admin user create \
  --uid=testuser \
  --display-name="Test User" \
  --access-key=USER123 \
  --secret-key=USERSECRET123
```

#### Step 1.4: Test S3 Access
```bash
# Install boto3 on host
pip3 install boto3

# Test S3 connection (Python)
python3 -c "
import boto3
s3 = boto3.client('s3',
    endpoint_url='http://localhost:8000',
    aws_access_key_id='USER123',
    aws_secret_access_key='USERSECRET123')
print(s3.list_buckets())
"
```

---

### PHASE 2: Create Storage Tier Pools

#### Step 2.1: Create Hot Tier Pool
```bash
# Create hot pool (smaller, uses OSD 0,1)
sudo cephadm shell -- ceph osd pool create rgw.hot.data 32 32
sudo cephadm shell -- ceph osd pool set rgw.hot.data size 1 --yes-i-really-mean-it
sudo cephadm shell -- ceph osd pool set rgw.hot.data min_size 1
sudo cephadm shell -- ceph osd pool application enable rgw.hot.data rgw
```

#### Step 2.2: Create Cold Tier Pool
```bash
# Create cold pool (larger, uses OSD 2)
sudo cephadm shell -- ceph osd pool create rgw.cold.data 32 32
sudo cephadm shell -- ceph osd pool set rgw.cold.data size 1 --yes-i-really-mean-it
sudo cephadm shell -- ceph osd pool set rgw.cold.data min_size 1
sudo cephadm shell -- ceph osd pool application enable rgw.cold.data rgw
```

#### Step 2.3: Create CRUSH Rules (Optional - for OSD separation)
```bash
# Create rule to prefer OSD 0,1 for hot pool
sudo cephadm shell -- ceph osd crush rule create-replicated hot_rule default host hdd

# Create rule to prefer OSD 2 for cold pool
sudo cephadm shell -- ceph osd crush rule create-replicated cold_rule default host hdd

# Apply rules (optional for now)
```

#### Step 2.4: Configure RGW Storage Classes
```bash
# Create placement targets file
cat > /tmp/zonegroup-placement.json << 'EOF'
{
  "placement_pools": [
    {
      "key": "default-placement",
      "val": {
        "storage_classes": {
          "STANDARD": {
            "data_pool": "rgw.hot.data"
          },
          "GLACIER": {
            "data_pool": "rgw.cold.data"
          }
        }
      }
    }
  ]
}
EOF

# Copy to VM and apply
scp -P 2224 /tmp/zonegroup-placement.json cephvm@localhost:/tmp/

# Apply configuration
sudo cephadm shell -- radosgw-admin zonegroup get > /tmp/zonegroup.json
# Manual edit required to add placement targets
# Then: radosgw-admin zonegroup set < /tmp/zonegroup-modified.json
# Then: radosgw-admin period update --commit
```

---

### PHASE 3: Setup Access Tracking

#### Step 3.1: Install Dependencies (on VM)
```bash
# SSH to VM
ssh -p 2224 cephvm@localhost

# Install Python packages
sudo apt update
sudo apt install -y python3-pip python3-rocksdb
pip3 install boto3 rocksdb python-dateutil
```

#### Step 3.2: Create RocksDB Directory
```bash
sudo mkdir -p /var/lib/ceph/access_tracking
sudo chown cephvm:cephvm /var/lib/ceph/access_tracking
```

#### Step 3.3: Setup Lua Hooks (Advanced - requires RGW restart)
```bash
# Create Lua script directory
sudo mkdir -p /etc/ceph/lua

# Create access tracking Lua script
sudo tee /etc/ceph/lua/access_tracker.lua << 'EOF'
-- Track object access in RocksDB
function request(request_id)
  local bucket = Request.Bucket.Name
  local key = Request.Object.Name
  local access_key = Request.HTTP.Headers["authorization"]
  
  -- Check if admin key (skip tracking for migrations)
  if access_key and access_key:match("ADMIN123") then
    RGWDebugLog("Admin access - skipping tracking")
    return
  end
  
  -- Track user access
  if Request.HTTP.Method == "GET" or Request.HTTP.Method == "HEAD" then
    RGWDebugLog("Tracking access: " .. bucket .. "/" .. key)
    -- Call external service to update RocksDB
    -- (This requires additional setup)
  end
end
EOF

# Note: Full Lua integration requires RGW restart and additional configuration
```

---

### PHASE 4: Build Tiering Engine

#### Step 4.1: Create Access Tracker Service
```python
# /home/cephvm/access_tracker.py
import rocksdb
import json
import time
from datetime import datetime

class AccessTracker:
    def __init__(self, db_path="/var/lib/ceph/access_tracking/db"):
        self.db = rocksdb.DB(db_path, rocksdb.Options(create_if_missing=True))
    
    def track_access(self, bucket, key, operation):
        """Record object access"""
        db_key = f"{bucket}:{key}".encode()
        
        # Get existing metadata or create new
        try:
            metadata = json.loads(self.db.get(db_key))
        except:
            metadata = {
                "created": time.time(),
                "access_count": 0
            }
        
        # Update access time
        metadata["last_user_access"] = time.time()
        metadata["access_count"] += 1
        metadata["last_operation"] = operation
        
        self.db.put(db_key, json.dumps(metadata).encode())
    
    def get_metadata(self, bucket, key):
        """Get object metadata"""
        db_key = f"{bucket}:{key}".encode()
        try:
            return json.loads(self.db.get(db_key))
        except:
            return None
```

#### Step 4.2: Create Tiering Engine
```python
# /home/cephvm/tiering_engine.py
import boto3
import rocksdb
import json
import time
from datetime import datetime, timedelta

class CephTieringEngine:
    def __init__(self):
        # Admin S3 client (for migrations)
        self.admin_s3 = boto3.client('s3',
            endpoint_url='http://localhost:8000',
            aws_access_key_id='ADMIN123',
            aws_secret_access_key='ADMINSECRET123')
        
        # RocksDB for access tracking
        self.db = rocksdb.DB("/var/lib/ceph/access_tracking/db",
                            rocksdb.Options(create_if_missing=True))
        
        # Policy thresholds (days)
        self.DEMOTE_THRESHOLD = 30
        self.PROMOTE_THRESHOLD = 1
    
    def scan_and_tier(self):
        """Main tiering loop"""
        print(f"[{datetime.now()}] Starting tiering scan...")
        
        # Iterate through all tracked objects
        it = self.db.iteritems()
        it.seek_to_first()
        
        demoted = 0
        promoted = 0
        
        for key, value in it:
            bucket_key = key.decode()
            metadata = json.loads(value)
            
            bucket, obj_key = bucket_key.split(':', 1)
            
            # Calculate age
            last_access = metadata.get('last_user_access', metadata.get('created'))
            age_days = (time.time() - last_access) / 86400
            
            current_class = metadata.get('storage_class', 'STANDARD')
            
            # Demotion logic
            if age_days > self.DEMOTE_THRESHOLD and current_class == 'STANDARD':
                if self.migrate_to_cold(bucket, obj_key, metadata):
                    demoted += 1
            
            # Promotion logic
            elif age_days < self.PROMOTE_THRESHOLD and current_class == 'GLACIER':
                if self.migrate_to_hot(bucket, obj_key, metadata):
                    promoted += 1
        
        print(f"Scan complete: {demoted} demoted, {promoted} promoted")
    
    def migrate_to_cold(self, bucket, key, metadata):
        """Demote object to cold tier"""
        try:
            # Preserve original access time
            original_access = metadata.get('last_user_access')
            
            # Perform S3 COPY with admin credentials
            self.admin_s3.copy_object(
                CopySource={'Bucket': bucket, 'Key': key},
                Bucket=bucket,
                Key=key,
                StorageClass='GLACIER',
                MetadataDirective='COPY'
            )
            
            # Update metadata - preserve user access time
            metadata['storage_class'] = 'GLACIER'
            metadata['last_migration'] = time.time()
            metadata['last_user_access'] = original_access
            
            db_key = f"{bucket}:{key}".encode()
            self.db.put(db_key, json.dumps(metadata).encode())
            
            print(f"Demoted: {bucket}/{key}")
            return True
        except Exception as e:
            print(f"Failed to demote {bucket}/{key}: {e}")
            return False
    
    def migrate_to_hot(self, bucket, key, metadata):
        """Promote object to hot tier"""
        try:
            original_access = metadata.get('last_user_access')
            
            self.admin_s3.copy_object(
                CopySource={'Bucket': bucket, 'Key': key},
                Bucket=bucket,
                Key=key,
                StorageClass='STANDARD',
                MetadataDirective='COPY'
            )
            
            metadata['storage_class'] = 'STANDARD'
            metadata['last_migration'] = time.time()
            metadata['last_user_access'] = original_access
            
            db_key = f"{bucket}:{key}".encode()
            self.db.put(db_key, json.dumps(metadata).encode())
            
            print(f"Promoted: {bucket}/{key}")
            return True
        except Exception as e:
            print(f"Failed to promote {bucket}/{key}: {e}")
            return False

if __name__ == '__main__':
    engine = CephTieringEngine()
    engine.scan_and_tier()
```

---

## Next Steps - What We'll Create

1. **RGW Service** - Object storage gateway
2. **Storage Pools** - hot_pool, cold_pool
3. **S3 Users** - admin and regular users
4. **Access Tracker** - RocksDB-based monitoring
5. **Tiering Engine** - Automated promotion/demotion
6. **Test Buckets** - For validation
7. **Monitoring** - Dashboard and logs

**Should I proceed with Phase 1: Deploying RGW?**
