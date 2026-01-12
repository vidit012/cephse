# Complete Storage Tiering: SSD to HDD Based on Last Access Time

## System Architecture

```
User accesses files in /cephfs
         ↓
   eBPF monitors VFS
         ↓
   Updates RocksDB (last access time)
         ↓
   Tiering Engine (runs every 60s)
   ├── Checks RocksDB
   ├── Files not accessed > 30 days → Demote to HDD
   └── Files accessed recently → Promote to SSD
         ↓
   ┌─────────────────┬─────────────────┐
   │   SSD Pool      │   HDD Pool      │
   │  (HOT - Fast)   │  (COLD - Slow)  │
   │  OSD.0, OSD.1   │  OSD.2, OSD.3   │
   └─────────────────┴─────────────────┘
```

---

## Prerequisites

### Hardware Setup:
- **SSD OSDs**: Fast storage for hot data (OSD.0, OSD.1)
- **HDD OSDs**: Slow storage for cold data (OSD.2, OSD.3)

### Software:
- CephFS mounted at `/cephfs`
- eBPF tools (bcc)
- RocksDB (Python bindings)
- Python 3.8+

---

## Step 1: Configure Device Classes

### 1.1 Check Current OSDs
```bash
sudo cephadm shell -- ceph osd tree
```

### 1.2 Set Device Classes
```bash
# Mark SSDs (assuming OSD.0, OSD.1 are SSDs)
sudo cephadm shell -- ceph osd crush set-device-class ssd osd.0
sudo cephadm shell -- ceph osd crush set-device-class ssd osd.1

# Mark HDDs (assuming OSD.2, OSD.3 are HDDs)
sudo cephadm shell -- ceph osd crush set-device-class hdd osd.2
sudo cephadm shell -- ceph osd crush set-device-class hdd osd.3
```

**For your current 3-OSD setup (all HDDs):**
```bash
# Simulate by marking 2 as "hot" and 1 as "cold"
sudo cephadm shell -- ceph osd crush set-device-class hot osd.0 osd.1
sudo cephadm shell -- ceph osd crush set-device-class cold osd.2
```

### 1.3 Create CRUSH Rules
```bash
# Rule for SSD/HOT OSDs
sudo cephadm shell -- ceph osd crush rule create-replicated ssd_rule default host ssd

# Rule for HDD/COLD OSDs  
sudo cephadm shell -- ceph osd crush rule create-replicated hdd_rule default host hdd

# For simulated setup:
sudo cephadm shell -- ceph osd crush rule create-replicated hot_rule default host hot
sudo cephadm shell -- ceph osd crush rule create-replicated cold_rule default host cold
```

### 1.4 Apply CRUSH Rules to Pools
```bash
# HOT pool uses SSD OSDs
sudo cephadm shell -- ceph osd pool set cephfs.tiering.data crush_rule ssd_rule

# COLD pool uses HDD OSDs
sudo cephadm shell -- ceph osd pool set cephfs.tiering.cold crush_rule hdd_rule
```

**Verify:**
```bash
sudo cephadm shell -- ceph osd pool get cephfs.tiering.data crush_rule
sudo cephadm shell -- ceph osd pool get cephfs.tiering.cold crush_rule
```

---

## Step 2: Install eBPF Monitoring Tools

```bash
# On VM
sudo apt-get update
sudo apt-get install -y python3-bpfcc bpfcc-tools linux-headers-$(uname -r)
pip3 install python-rocksdb psutil
```

---

## Step 3: Create eBPF Access Monitor

### File: `/opt/cephfs_tiering/ebpf_monitor.py`

```python
#!/usr/bin/env python3
"""
eBPF-based CephFS Access Monitor
Tracks file access times by monitoring VFS operations
"""

from bcc import BPF
import rocksdb
import json
import time
import os
from datetime import datetime

# eBPF program to trace file access
BPF_PROGRAM = """
#include <uapi/linux/ptrace.h>
#include <linux/fs.h>
#include <linux/dcache.h>

struct access_event {
    u64 timestamp;
    u64 inode;
    u32 pid;
    u8 op_type;  // 0=read, 1=write
    char filename[256];
};

BPF_PERF_OUTPUT(events);
BPF_HASH(active_inodes, u64, u64);

int trace_vfs_read(struct pt_regs *ctx, struct file *file, char __user *buf, size_t count, loff_t *pos) {
    struct access_event event = {};
    struct dentry *dentry = file->f_path.dentry;
    struct inode *inode = file->f_inode;
    
    // Only monitor /cephfs mount
    char comm[16];
    bpf_get_current_comm(&comm, sizeof(comm));
    
    event.timestamp = bpf_ktime_get_ns();
    event.inode = inode->i_ino;
    event.pid = bpf_get_current_pid_tgid() >> 32;
    event.op_type = 0;  // read
    
    bpf_probe_read_kernel_str(&event.filename, sizeof(event.filename), dentry->d_name.name);
    
    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}

int trace_vfs_write(struct pt_regs *ctx, struct file *file, const char __user *buf, size_t count, loff_t *pos) {
    struct access_event event = {};
    struct dentry *dentry = file->f_path.dentry;
    struct inode *inode = file->f_inode;
    
    event.timestamp = bpf_ktime_get_ns();
    event.inode = inode->i_ino;
    event.pid = bpf_get_current_pid_tgid() >> 32;
    event.op_type = 1;  // write
    
    bpf_probe_read_kernel_str(&event.filename, sizeof(event.filename), dentry->d_name.name);
    
    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}
"""

class CephFSAccessMonitor:
    def __init__(self, rocksdb_path="/var/lib/cephfs_tiering/access.db", mount_point="/cephfs"):
        self.mount_point = mount_point
        self.db = rocksdb.DB(rocksdb_path, rocksdb.Options(create_if_missing=True))
        self.bpf = BPF(text=BPF_PROGRAM)
        
        # Attach to VFS functions
        self.bpf.attach_kprobe(event="vfs_read", fn_name="trace_vfs_read")
        self.bpf.attach_kprobe(event="vfs_write", fn_name="trace_vfs_write")
        
        print(f"[eBPF Monitor] Started monitoring {mount_point}")
        print(f"[eBPF Monitor] RocksDB: {rocksdb_path}")
    
    def process_event(self, cpu, data, size):
        """Process file access events from eBPF"""
        event = self.bpf["events"].event(data)
        
        # Convert inode to full path
        filepath = self.inode_to_path(event.inode)
        if not filepath or not filepath.startswith(self.mount_point):
            return
        
        # Update access time in RocksDB
        timestamp = time.time()
        self.update_access_time(filepath, timestamp, event.op_type)
    
    def inode_to_path(self, inode):
        """Resolve inode to file path"""
        try:
            # Use find to locate inode
            result = os.popen(f"find {self.mount_point} -inum {inode} 2>/dev/null").read().strip()
            return result if result else None
        except:
            return None
    
    def update_access_time(self, filepath, timestamp, op_type):
        """Update file access metadata in RocksDB"""
        try:
            # Get existing metadata
            try:
                metadata = json.loads(self.db.get(filepath.encode()))
            except:
                metadata = {
                    "first_seen": timestamp,
                    "access_count": 0,
                    "current_tier": "hot"
                }
            
            # Update
            metadata["last_access"] = timestamp
            metadata["access_count"] = metadata.get("access_count", 0) + 1
            metadata["last_op_type"] = "read" if op_type == 0 else "write"
            
            # Store
            self.db.put(filepath.encode(), json.dumps(metadata).encode())
            
            print(f"[Access] {filepath} | Count: {metadata['access_count']}")
            
        except Exception as e:
            print(f"[Error] Failed to update {filepath}: {e}")
    
    def run(self):
        """Start monitoring loop"""
        self.bpf["events"].open_perf_buffer(self.process_event)
        
        print("[eBPF Monitor] Monitoring active. Press Ctrl+C to stop.")
        
        while True:
            try:
                self.bpf.perf_buffer_poll()
            except KeyboardInterrupt:
                print("\n[eBPF Monitor] Stopping...")
                break

if __name__ == "__main__":
    monitor = CephFSAccessMonitor()
    monitor.run()
```

---

## Step 4: Create Tiering Engine

### File: `/opt/cephfs_tiering/tiering_engine.py`

```python
#!/usr/bin/env python3
"""
CephFS Tiering Engine
Moves files between SSD and HDD pools based on access patterns
"""

import rocksdb
import json
import time
import os
import subprocess
import shutil
from datetime import datetime, timedelta

class CephFSTieringEngine:
    def __init__(self, 
                 rocksdb_path="/var/lib/cephfs_tiering/access.db",
                 mount_point="/cephfs",
                 cold_age_days=30,
                 check_interval=60):
        
        self.db = rocksdb.DB(rocksdb_path, rocksdb.Options(create_if_missing=True))
        self.mount_point = mount_point
        self.cold_threshold = cold_age_days * 86400  # Convert to seconds
        self.check_interval = check_interval
        
        # Create tier directories
        self.hot_dir = f"{mount_point}/.tiers/hot"
        self.cold_dir = f"{mount_point}/.tiers/cold"
        self._setup_tier_directories()
        
        print(f"[Tiering Engine] Started")
        print(f"[Config] Cold threshold: {cold_age_days} days")
        print(f"[Config] Check interval: {check_interval} seconds")
    
    def _setup_tier_directories(self):
        """Create and configure tier directories"""
        os.makedirs(self.hot_dir, exist_ok=True)
        os.makedirs(self.cold_dir, exist_ok=True)
        
        # Set pool layouts
        subprocess.run([
            'setfattr', '-n', 'ceph.dir.layout.pool',
            '-v', 'cephfs.tiering.data', self.hot_dir
        ], check=False)
        
        subprocess.run([
            'setfattr', '-n', 'ceph.dir.layout.pool',
            '-v', 'cephfs.tiering.cold', self.cold_dir
        ], check=False)
        
        print(f"[Setup] Hot tier: {self.hot_dir}")
        print(f"[Setup] Cold tier: {self.cold_dir}")
    
    def get_file_pool(self, filepath):
        """Get current pool for a file"""
        try:
            result = subprocess.run(
                ['getfattr', '-n', 'ceph.file.layout.pool', '--only-values', filepath],
                capture_output=True, text=True, check=True
            )
            return result.stdout.strip()
        except:
            return None
    
    def demote_file(self, filepath):
        """Move file from HOT (SSD) to COLD (HDD)"""
        try:
            # Get current pool
            current_pool = self.get_file_pool(filepath)
            if current_pool == 'cephfs.tiering.cold':
                print(f"[Skip] {filepath} already in COLD")
                return True
            
            # Create cold path
            relative_path = filepath.replace(self.mount_point + '/', '')
            cold_path = os.path.join(self.cold_dir, relative_path)
            os.makedirs(os.path.dirname(cold_path), exist_ok=True)
            
            # Copy to cold (triggers write to COLD pool)
            shutil.copy2(filepath, cold_path)
            
            # Verify
            new_pool = self.get_file_pool(cold_path)
            if new_pool == 'cephfs.tiering.cold':
                # Delete original
                os.remove(filepath)
                # Create symlink for transparency
                os.symlink(cold_path, filepath)
                
                print(f"[Demote] {filepath} → COLD (HDD)")
                return True
            else:
                print(f"[Error] Demotion failed for {filepath}")
                os.remove(cold_path)
                return False
                
        except Exception as e:
            print(f"[Error] Demote {filepath}: {e}")
            return False
    
    def promote_file(self, filepath):
        """Move file from COLD (HDD) to HOT (SSD)"""
        try:
            # Check if it's a symlink to cold storage
            if not os.path.islink(filepath):
                return True
            
            # Resolve symlink
            cold_path = os.readlink(filepath)
            if not cold_path.startswith(self.cold_dir):
                return True
            
            # Copy back to hot
            os.remove(filepath)  # Remove symlink
            shutil.copy2(cold_path, filepath)
            
            # Verify
            new_pool = self.get_file_pool(filepath)
            if new_pool == 'cephfs.tiering.data':
                os.remove(cold_path)  # Delete cold copy
                print(f"[Promote] {filepath} → HOT (SSD)")
                return True
            
        except Exception as e:
            print(f"[Error] Promote {filepath}: {e}")
            return False
    
    def scan_and_tier(self):
        """Scan RocksDB and move files between tiers"""
        now = time.time()
        demoted = 0
        promoted = 0
        
        print(f"\n[Scan] Starting at {datetime.now()}")
        
        it = self.db.iteritems()
        it.seek_to_first()
        
        for key, value in it:
            try:
                filepath = key.decode()
                metadata = json.loads(value)
                
                # Skip if file doesn't exist
                if not os.path.exists(filepath) and not os.path.islink(filepath):
                    continue
                
                last_access = metadata.get('last_access', now)
                age_seconds = now - last_access
                current_tier = metadata.get('current_tier', 'hot')
                
                # Demotion logic: Not accessed in 30+ days
                if age_seconds > self.cold_threshold and current_tier == 'hot':
                    if self.demote_file(filepath):
                        metadata['current_tier'] = 'cold'
                        metadata['demoted_at'] = now
                        self.db.put(key, json.dumps(metadata).encode())
                        demoted += 1
                
                # Promotion logic: Recently accessed
                elif age_seconds < (self.cold_threshold / 2) and current_tier == 'cold':
                    if self.promote_file(filepath):
                        metadata['current_tier'] = 'hot'
                        metadata['promoted_at'] = now
                        self.db.put(key, json.dumps(metadata).encode())
                        promoted += 1
                        
            except Exception as e:
                print(f"[Error] Processing {key}: {e}")
        
        print(f"[Scan] Complete - Demoted: {demoted}, Promoted: {promoted}")
    
    def run(self):
        """Main tiering loop"""
        print("[Tiering Engine] Running...")
        
        while True:
            try:
                self.scan_and_tier()
                time.sleep(self.check_interval)
            except KeyboardInterrupt:
                print("\n[Tiering Engine] Stopping...")
                break

if __name__ == "__main__":
    engine = CephFSTieringEngine(cold_age_days=30, check_interval=60)
    engine.run()
```

---

## Step 5: Create Systemd Services

### eBPF Monitor Service: `/etc/systemd/system/cephfs-monitor.service`
```ini
[Unit]
Description=CephFS eBPF Access Monitor
After=network.target ceph.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/cephfs_tiering/ebpf_monitor.py
Restart=always
User=root

[Install]
WantedBy=multi-user.target
```

### Tiering Engine Service: `/etc/systemd/system/cephfs-tiering.service`
```ini
[Unit]
Description=CephFS Tiering Engine
After=network.target cephfs-monitor.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/cephfs_tiering/tiering_engine.py
Restart=always
User=root

[Install]
WantedBy=multi-user.target
```

### Enable and Start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable cephfs-monitor cephfs-tiering
sudo systemctl start cephfs-monitor cephfs-tiering
```

---

## Step 6: Testing

### Create test files
```bash
# Create files in CephFS
echo "Hot file" > /cephfs/test_hot.txt
dd if=/dev/urandom of=/cephfs/large_file.dat bs=1M count=100
```

### Monitor access
```bash
# Check RocksDB
python3 -c "
import rocksdb
db = rocksdb.DB('/var/lib/cephfs_tiering/access.db')
it = db.iteritems()
it.seek_to_first()
for k, v in it:
    print(f'{k.decode()}: {v.decode()}')
"
```

### Check service logs
```bash
sudo journalctl -u cephfs-monitor -f
sudo journalctl -u cephfs-tiering -f
```

### Verify tiering
```bash
# Check file pool
getfattr -n ceph.file.layout.pool /cephfs/test_hot.txt

# Check OSD usage
sudo cephadm shell -- ceph osd df
```

---

## Expected Behavior

1. **New files** → Created in HOT pool (SSD)
2. **Active files** → Stay in HOT pool
3. **Files not accessed 30+ days** → Demoted to COLD pool (HDD)
4. **Accessed cold files** → Promoted back to HOT pool

**Completely transparent to users!** File paths never change.

---

## Next Steps

1. Deploy device classes and CRUSH rules
2. Install eBPF monitor and tiering engine
3. Test with sample files
4. Monitor with Grafana dashboards
5. Tune cold_age_days threshold based on usage patterns

Ready to implement?
