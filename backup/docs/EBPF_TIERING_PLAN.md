# eBPF-Based CephFS Storage Tiering Implementation Plan

## Architecture Overview

**Goal:** Transparent file system tiering where files automatically move between HOT/WARM/COLD pools based on access patterns, with eBPF monitoring VFS operations.

## Components

### 1. CephFS Setup (NEW)
- Deploy CephFS on existing Ceph cluster
- Create 3 data pools: `cephfs_hot`, `cephfs_warm`, `cephfs_cold`
- Mount CephFS on VM: `/mnt/cephfs`

### 2. eBPF Access Monitor (NEW)
**File:** `ebpf_access_monitor.py`

**eBPF Probes:**
- `kprobe/vfs_read` - Track file reads
- `kprobe/vfs_write` - Track file writes  
- `kprobe/vfs_open` - Track file opens

**Data Captured:**
```c
struct file_access_event {
    u64 timestamp;
    u64 inode;
    u32 pid;
    char filename[256];
    u8 operation;  // 0=read, 1=write, 2=open
};
```

**User-space Handler:**
- Read events from ring buffer
- Normalize path (resolve inode → full path)
- Update RocksDB with latest access time

### 3. RocksDB Metadata Store
**Schema:**
```
Key: /mnt/cephfs/user/document.pdf
Value: {
    "last_access": 1704470400,  # Unix timestamp
    "last_modified": 1704470400,
    "size_bytes": 1048576,
    "current_tier": "hot",
    "inode": 1234567,
    "access_count_30d": 15
}
```

### 4. Tiering Engine (ADAPTED FROM LUSTRE)
**File:** `cephfs_tiering_engine.py`

**Demotion Logic:**
- Scan RocksDB every 60 seconds
- Find files with `last_access < now - 30 days`
- Calculate score: `0.8 × accesses + 0.2 × recency`
- Demote if score < threshold:
  - HOT → WARM: score < 50
  - WARM → COLD: score < 25

**Promotion Logic:**
- On access detection from eBPF
- Immediate promotion: COLD → WARM
- Scheduled promotion: WARM → HOT if score > 50

**File Migration Command:**
```bash
# Get current pool
getfattr -n ceph.file.layout.pool /mnt/cephfs/file.txt

# Move to COLD pool
setfattr -n ceph.file.layout.pool -v cephfs_cold /mnt/cephfs/file.txt
```

### 5. CephFS File Layout Manipulation
**How data moves between pools:**

1. **Get current layout:**
   ```bash
   getfattr -n ceph.file.layout /path/to/file
   ```

2. **Change pool (migrates data):**
   ```bash
   # This triggers actual data movement in Ceph backend
   setfattr -n ceph.file.layout.pool -v new_pool_name /path/to/file
   ```

3. **Verify migration:**
   ```bash
   ceph osd map cephfs_data <file_object_name>
   ```

**Key Point:** CephFS moves data lazily - subsequent writes go to new pool, old data migrates gradually.

## Implementation Steps

### Phase 1: CephFS Setup (Day 1)
```bash
# 1. Create CephFS pools
ceph osd pool create cephfs_metadata 32 32
ceph osd pool create cephfs_hot 64 64
ceph osd pool create cephfs_warm 64 64
ceph osd pool create cephfs_cold 64 64

# 2. Set pool sizes to 1 for single-node
ceph osd pool set cephfs_metadata size 1 --yes-i-really-mean-it
ceph osd pool set cephfs_hot size 1 --yes-i-really-mean-it
ceph osd pool set cephfs_warm size 1 --yes-i-really-mean-it
ceph osd pool set cephfs_cold size 1 --yes-i-really-mean-it

# 3. Create CephFS
ceph fs new cephfs cephfs_metadata cephfs_hot

# 4. Add data pools to CephFS
ceph fs add_data_pool cephfs cephfs_warm
ceph fs add_data_pool cephfs cephfs_cold

# 5. Mount CephFS
mkdir -p /mnt/cephfs
mount -t ceph cephvm@.cephfs=/ /mnt/cephfs -o name=admin,secret=$(cat /etc/ceph/ceph.client.admin.keyring | grep key | awk '{print $3}')
```

### Phase 2: eBPF Monitor (Day 2-3)
**Dependencies:**
```bash
apt-get install -y python3-bpfcc bpfcc-tools linux-headers-$(uname -r)
pip3 install bcc rocksdb
```

**eBPF Program Structure:**
```python
from bcc import BPF
import rocksdb

# BPF program to attach to VFS
bpf_text = """
#include <uapi/linux/ptrace.h>
#include <linux/fs.h>

struct file_event {
    u64 ts;
    u64 inode;
    u32 pid;
    char comm[16];
};

BPF_PERF_OUTPUT(events);

int trace_vfs_read(struct pt_regs *ctx, struct file *file) {
    struct file_event event = {};
    event.ts = bpf_ktime_get_ns();
    event.inode = file->f_inode->i_ino;
    event.pid = bpf_get_current_pid_tgid() >> 32;
    bpf_get_current_comm(&event.comm, sizeof(event.comm));
    
    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}
"""

# Load and attach
b = BPF(text=bpf_text)
b.attach_kprobe(event="vfs_read", fn_name="trace_vfs_read")

# Process events and update RocksDB
def process_event(cpu, data, size):
    event = b["events"].event(data)
    # Resolve inode to path
    path = inode_to_path(event.inode)
    # Update RocksDB
    update_access_time(path, event.ts)
```

### Phase 3: RocksDB Setup (Day 3)
```python
import rocksdb
import json
import time

# Open database
db = rocksdb.DB("/var/lib/cephfs_tiering/metadata.db", 
                rocksdb.Options(create_if_missing=True))

# Store access event
def update_access_time(filepath, timestamp):
    # Get existing metadata
    try:
        data = json.loads(db.get(filepath.encode()))
    except:
        data = {"access_count_30d": 0}
    
    # Update
    data.update({
        "last_access": timestamp,
        "last_modified": time.time(),
        "current_tier": get_current_pool(filepath)
    })
    data["access_count_30d"] += 1
    
    # Store
    db.put(filepath.encode(), json.dumps(data).encode())

def get_current_pool(filepath):
    # Run: getfattr -n ceph.file.layout.pool --only-values filepath
    import subprocess
    result = subprocess.run(
        ['getfattr', '-n', 'ceph.file.layout.pool', '--only-values', filepath],
        capture_output=True, text=True
    )
    return result.stdout.strip()
```

### Phase 4: Tiering Engine (Day 4-5)
**Adapt existing `demotion_engine_final.py` and `promotion_engine_final.py`:**

**Changes needed:**
1. Replace PostgreSQL with RocksDB queries
2. Replace Lustre `lfs` commands with CephFS `setfattr`
3. Update pool names: hot/warm/cold → cephfs_hot/cephfs_warm/cephfs_cold
4. Add file layout verification

**Demotion example:**
```python
import subprocess
import json
import rocksdb

db = rocksdb.DB("/var/lib/cephfs_tiering/metadata.db")

def demote_file(filepath, target_pool):
    """Move file to lower tier pool"""
    try:
        # Change pool using setfattr
        subprocess.run([
            'setfattr', '-n', 'ceph.file.layout.pool',
            '-v', target_pool, filepath
        ], check=True)
        
        # Update metadata
        data = json.loads(db.get(filepath.encode()))
        data['current_tier'] = target_pool
        db.put(filepath.encode(), json.dumps(data).encode())
        
        return True
    except Exception as e:
        print(f"Failed to demote {filepath}: {e}")
        return False

# Scan for cold files
it = db.iteritems()
it.seek_to_first()

for key, value in it:
    filepath = key.decode()
    metadata = json.loads(value)
    
    age_days = (time.time() - metadata['last_access']) / 86400
    
    if age_days > 30 and metadata['current_tier'] == 'cephfs_hot':
        print(f"Demoting cold file: {filepath}")
        demote_file(filepath, 'cephfs_cold')
```

### Phase 5: Integration & Testing (Day 6-7)
1. **Test file creation:**
   ```bash
   echo "test" > /mnt/cephfs/testfile.txt
   # Should be in cephfs_hot by default
   ```

2. **Verify eBPF monitoring:**
   ```bash
   cat /mnt/cephfs/testfile.txt
   # Check RocksDB updated
   ```

3. **Test demotion:**
   ```bash
   # Simulate 30 days (or wait 3 minutes in debug mode)
   python3 cephfs_tiering_engine.py
   # Verify file moved to cephfs_cold
   ```

4. **Test promotion:**
   ```bash
   cat /mnt/cephfs/testfile.txt
   # Should trigger promotion back to hot
   ```

## Deployment Architecture

```
┌─────────────────────────────────────────────┐
│              CephVM                         │
│                                             │
│  ┌──────────────────────────────────────┐  │
│  │  Kernel Space                        │  │
│  │  ┌────────────────────────────────┐  │  │
│  │  │  eBPF Probes on VFS            │  │  │
│  │  │  - vfs_read, vfs_write         │  │  │
│  │  └────────────────────────────────┘  │  │
│  └──────────────────────────────────────┘  │
│         ↓ ring buffer                      │
│  ┌──────────────────────────────────────┐  │
│  │  User Space                          │  │
│  │                                      │  │
│  │  [eBPF Monitor]                      │  │
│  │       ↓                              │  │
│  │  [RocksDB: /var/lib/cephfs_tiering] │  │
│  │       ↓                              │  │
│  │  [Tiering Engine]                    │  │
│  │       ↓                              │  │
│  │  [setfattr → CephFS]                 │  │
│  └──────────────────────────────────────┘  │
│         ↓                                  │
│  ┌──────────────────────────────────────┐  │
│  │  CephFS Mount: /mnt/cephfs           │  │
│  └──────────────────────────────────────┘  │
│         ↓                                  │
│  ┌──────────────────────────────────────┐  │
│  │  Ceph Cluster                        │  │
│  │  - cephfs_hot (default)              │  │
│  │  - cephfs_warm                       │  │
│  │  - cephfs_cold (lz4 compression)     │  │
│  └──────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

## Key Technical Details

### Why eBPF?
- **Zero overhead** when no monitoring active
- **Kernel-level accuracy** - captures all file access
- **No library injection** - works with any application
- **Real-time** - sub-microsecond latency

### Why RocksDB?
- **Fast key-value lookups** - O(log n)
- **Built-in compression** - saves space
- **Write-optimized** - LSM tree architecture
- **No external dependencies** - embedded database

### CephFS vs RGW?
| Feature | CephFS | RGW S3 |
|---------|--------|--------|
| User interface | POSIX (transparent) | S3 API |
| eBPF monitoring | ✅ VFS probes work | ❌ No kernel visibility |
| Tiering method | File layouts | Storage classes |
| Access tracking | eBPF real-time | Lua hooks (complex) |

## Performance Considerations

### eBPF Overhead
- **Per-access cost:** ~100-200 nanoseconds
- **Ring buffer:** Batched processing reduces syscalls
- **Filtering:** Only monitor `/mnt/cephfs/*` paths

### RocksDB Tuning
```python
options = rocksdb.Options()
options.create_if_missing = True
options.compression = rocksdb.CompressionType.lz4_compression
options.write_buffer_size = 64 * 1024 * 1024  # 64MB
options.max_write_buffer_number = 3
options.target_file_size_base = 64 * 1024 * 1024
```

### Tiering Engine Intervals
- **Production:** Check every 1 hour, demote files older than 30 days
- **Testing:** Check every 60 seconds, demote after 3 minutes

## Next Steps

1. **Deploy CephFS** on your existing Ceph cluster
2. **Install eBPF tools** (bcc, bpftrace)
3. **Create eBPF monitor** for VFS operations
4. **Set up RocksDB** metadata store
5. **Adapt Lustre engines** for CephFS
6. **Test end-to-end** with sample files

Ready to start implementation?
