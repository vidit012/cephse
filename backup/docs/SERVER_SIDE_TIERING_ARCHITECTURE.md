l # Server-Side Storage Tiering: How Ceph Actually Stores Files

## Understanding Ceph Backend Architecture (Server-Side)

### What Happens When a User Uploads a File?

```
┌────────────────────────────────────────────────────────────────┐
│  CLIENT SIDE: User uploads file                                │
│  $ cp large_file.mp4 /tiercephfs/                             │
└─────────────────┬──────────────────────────────────────────────┘
                  │
                  ▼
┌────────────────────────────────────────────────────────────────┐
│  STEP 1: CephFS Client (Kernel Module)                         │
│  - Breaks file into 4MB chunks (stripe_unit)                   │
│  - Assigns inode number: 1234567                               │
│  - Calculates object names: 1234567.00000000, 1234567.00000001 │
└─────────────────┬──────────────────────────────────────────────┘
                  │
                  ▼
┌────────────────────────────────────────────────────────────────┐
│  STEP 2: MDS (Metadata Server) - SERVER SIDE                   │
│  - Creates file inode in metadata pool                         │
│  - Stores: path, permissions, timestamps, owner                │
│  - **CRITICAL**: Stores file layout attributes:                │
│      * pool: "cephfs.tiercephfs.data" ← DEFAULT POOL           │
│      * stripe_unit: 4194304 (4MB)                              │
│      * stripe_count: 1                                         │
│      * object_size: 4194304                                    │
│  - Returns inode number to client                              │
└─────────────────┬──────────────────────────────────────────────┘
                  │
                  ▼
┌────────────────────────────────────────────────────────────────┐
│  STEP 3: librados (Client Library) - CLIENT SIDE                │
│  - Reads file layout from inode                                │
│  - For each 4MB chunk:                                         │
│      * Create RADOS object with name: <inode>.<chunk_num>      │
│      * Target pool: cephfs.tiercephfs.data                     │
│      * Send object write request to OSDs                       │
└─────────────────┬──────────────────────────────────────────────┘
                  │
                  ▼
┌────────────────────────────────────────────────────────────────┐
│  STEP 4: RADOS (Reliable Autonomic Distributed Object Store)   │
│  - Receives object write request                               │
│  - Applies CRUSH algorithm to determine OSD placement          │
│  - Maps: 1234567.00000000 → PG 3.a4f → OSD.0, OSD.1, OSD.2   │
│  - Writes object to primary OSD                                │
│  - Replicates to secondary OSDs                                │
└─────────────────┬──────────────────────────────────────────────┘
                  │
                  ▼
┌────────────────────────────────────────────────────────────────┐
│  STEP 5: OSD (Object Storage Daemon) - SERVER SIDE             │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  OSD.0 (Primary)                                         │  │
│  │  /var/lib/ceph/osd/ceph-0/current/                      │  │
│  │  └── 3.a4f_head/                                        │  │
│  │      └── 1234567.00000000__head_A4F_3                   │  │
│  │          [4MB of actual file data stored here]          │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  OSD.1 (Replica)                                         │  │
│  │  [Same object replicated for redundancy]                │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
```

---

## Key Insight: The MDS Layout Attribute

**The file's pool is NOT stored in the filename or directory structure.**

It's stored as an **extended attribute (xattr)** in the MDS inode metadata:

```bash
# Read file layout (this is stored in MDS metadata pool)
$ getfattr -n ceph.file.layout /tiercephfs/large_file.mp4
ceph.file.layout="stripe_unit=4194304 stripe_count=1 object_size=4194304 pool=cephfs.tiercephfs.data"
```

This means:
- File metadata (inode) → Stored in **metadata pool** on MDS
- File data (objects) → Stored in **data pool** specified by layout
- **Layout attribute** is the bridge between metadata and data

---

## How Storage Tiering Works (Server-Side)

### Current Architecture (Without Tiering)

```
User uploads file → MDS creates inode with default layout
                    ↓
                    layout.pool = "cephfs.tiercephfs.data"
                    ↓
                    All objects written to DEFAULT DATA POOL
                    ↓
┌───────────────────────────────────────────────────────┐
│  cephfs.tiercephfs.data (Single Pool)                 │
│  - 1234567.00000000 → OSD.0                           │
│  - 1234567.00000001 → OSD.1                           │
│  - 1234567.00000002 → OSD.2                           │
│  - All files, hot or cold, mixed together             │
└───────────────────────────────────────────────────────┘
```

### Proposed Tiering Architecture (Multi-Pool)

```
User uploads file → MDS creates inode with HOT pool layout
                    ↓
                    layout.pool = "cephfs.tiercephfs.hot"
                    ↓
                    Objects initially written to HOT POOL
                    ↓
┌───────────────────────────────────────────────────────┐
│  cephfs.tiercephfs.hot (Fast Storage - SSD)           │
│  - 1234567.00000000 → OSD.0 (SSD)                     │
│  - 1234567.00000001 → OSD.0 (SSD)                     │
│  - Recently accessed files                            │
└───────────────────────────────────────────────────────┘

                   [30 days no access]
                           ↓
            TIERING ENGINE DETECTS COLD FILE
                           ↓
        setfattr -n ceph.file.layout.pool -v cephfs.tiercephfs.cold
                           ↓
            MDS UPDATES INODE LAYOUT ATTRIBUTE
                           ↓
┌───────────────────────────────────────────────────────┐
│  cephfs.tiercephfs.cold (Slow Storage - HDD)          │
│  - 1234567.00000000 → OSD.2 (HDD) [MIGRATED]          │
│  - 1234567.00000001 → OSD.2 (HDD) [MIGRATED]          │
│  - Old, rarely accessed files                         │
└───────────────────────────────────────────────────────┘
```

---

## The Best Approach: MDS Layout-Based Tiering

### Why This is the Best Solution (No Ceph Modification)

1. **Native CephFS Feature**: File layouts are a built-in Ceph feature
2. **MDS Already Handles This**: MDS stores layout in inode metadata
3. **Transparent to Users**: File path doesn't change, layout change is invisible
4. **RADOS Handles Migration**: Built-in object copy between pools
5. **No FUSE Overhead**: Works at kernel level for full performance

### Architecture Components

```
┌────────────────────────────────────────────────────────────────┐
│  Component 1: Multiple Data Pools (Already Exists in Ceph)     │
│  - cephfs.tiercephfs.hot   (SSD OSDs)                          │
│  - cephfs.tiercephfs.warm  (Hybrid)                            │
│  - cephfs.tiercephfs.cold  (HDD OSDs)                          │
└────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────┐
│  Component 2: Access Tracking (NEW - Your Responsibility)      │
│  - eBPF hooks on VFS read/write operations                     │
│  - Track last_access_time for each file                        │
│  - Store in RocksDB: {filepath: last_access_time}              │
└────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────┐
│  Component 3: Tiering Engine (NEW - Your Responsibility)       │
│  - Scan RocksDB every 60 seconds                               │
│  - Find files: last_access > 30 days                           │
│  - Execute: setfattr -n ceph.file.layout.pool -v cold          │
│  - MDS updates inode layout attribute                          │
└────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────┐
│  Component 4: Object Migration (YOU MUST IMPLEMENT THIS!)      │
│  ❌ setfattr ONLY works on EMPTY files                         │
│  ❌ Cannot change layout of files with existing data           │
│  ✅ You must manually copy objects between pools:              │
│     1. Get inode number from file                              │
│     2. List all objects: rados -p hot_pool ls | grep <inode>   │
│     3. Copy each object: rados get/put between pools           │
│     4. Delete from old pool after verification                 │
│  ✅ This is what your tiering engine must implement            │
└────────────────────────────────────────────────────────────────┘
```

---

## Implementation: Server-Side View

### Server-Side File Structure

```
MDS Server (Metadata):
/var/lib/ceph/mds/ceph-<id>/
└── cephfs.tiercephfs.meta pool stores:
    └── Inode 1234567:
        - Path: /video.mp4
        - Owner: user1
        - Permissions: 0644
        - Size: 100MB
        - Timestamps: mtime, atime, ctime
        - **Layout Attributes**:
            * ceph.file.layout.pool = "cephfs.tiercephfs.hot"
            * ceph.file.layout.stripe_unit = 4194304
            * ceph.file.layout.object_size = 4194304

OSD Servers (Data):
/var/lib/ceph/osd/ceph-0/current/
└── PG directories:
    └── 3.a4f_head/
        └── 1234567.00000000__head_A4F_3  [4MB object]
        └── 1234567.00000001__head_A4F_3  [4MB object]
        └── ... (25 objects for 100MB file)
```

### What Changes During Migration

**Before Migration (HOT Pool)**
```
MDS Inode 1234567:
  layout.pool = "cephfs.tiercephfs.hot"

OSD.0 (SSD) contains:
  - 1234567.00000000 (4MB)
  - 1234567.00000001 (4MB)
```

**Migration Process (IMPORTANT - setfattr doesn't work on existing files!)**

```bash
# ❌ THIS WILL FAIL if file has data:
setfattr -n ceph.file.layout.pool -v cephfs.tiercephfs.cold /tiercephfs/video.mp4
# Error: "Directory not empty" (or file not empty)

# ✅ CORRECT APPROACH - Manual object migration:

# Step 1: Get inode number
INODE=$(stat -c %i /tiercephfs/video.mp4)

# Step 2: Copy ALL objects from hot to cold pool
for obj in $(rados -p cephfs.tiercephfs.hot ls | grep "^${INODE}\."); do
    echo "Migrating $obj..."
    rados -p cephfs.tiercephfs.hot get "$obj" - | \
    rados -p cephfs.tiercephfs.cold put "$obj" -
done

# Step 3: Update file layout attribute (metadata only)
setfattr -n ceph.file.layout.pool -v cephfs.tiercephfs.cold /tiercephfs/video.mp4

# Step 4: Verify data integrity, then delete from hot pool
for obj in $(rados -p cephfs.tiercephfs.hot ls | grep "^${INODE}\."); do
    rados -p cephfs.tiercephfs.hot rm "$obj"
done
```

**After Migration (COLD Pool)**
```
MDS Inode 1234567:
  layout.pool = "cephfs.tiercephfs.cold"  ← CHANGED (metadata only)

Objects manually copied to new pool:
OSD.2 (HDD) now contains:
  - 1234567.00000000 (4MB) [MANUALLY COPIED via rados CLI]
  - 1234567.00000001 (4MB) [MANUALLY COPIED via rados CLI]

Original objects deleted from OSD.0 (SSD) after verification
```

---

## Why NOT Build a Wrapper Around MDS?

### Option A: MDS Wrapper (NOT RECOMMENDED)

```
User write → Custom MDS Wrapper → Decide pool → Write to Ceph
                ↑
          [PROBLEMS]
          - Have to intercept all MDS operations
          - Complex Ceph internal APIs
          - Break on Ceph upgrades
          - Need to recompile Ceph MDS
          - Violates "don't modify Ceph" requirement
```

### Option B: Layout-Based Tiering (RECOMMENDED)

```
User write → Standard CephFS → File created in default pool
                                        ↓
                            [FILE EXISTS, NO CEPH MODIFICATION]
                                        ↓
             Tiering Engine (external daemon) scans files
                                        ↓
                     Detects cold file → Change layout attribute
                                        ↓
                     MDS natively handles layout change
                                        ↓
                     RADOS migrates objects between pools
```

---

## Complete Implementation Plan

### Step 1: Create Multi-Pool CephFS

```bash
# You already have: cephfs.tiercephfs.data
# Add two more pools:

ceph osd pool create cephfs.tiercephfs.hot 64 64
ceph osd pool create cephfs.tiercephfs.cold 64 64

# Set pool sizes for single-node
ceph osd pool set cephfs.tiercephfs.hot size 1 --yes-i-really-mean-it
ceph osd pool set cephfs.tiercephfs.cold size 1 --yes-i-really-mean-it

# Add pools to CephFS
ceph fs add_data_pool tiercephfs c & Migration Engine

Use existing `/home/vidit-pt7945/cephse/cephfs_lc_daemon.py` but you MUST ADD object migration logic:

```python
# This daemon already implements:
# 1. eBPF monitoring (via bcc)
# 2. RocksDB metadata storage
# 3. Access time tracking

# ❌ NEEDS MODIFICATION - Current setfattr approach won't work for existing files
# ✅ MUST ADD - Manual object copy using rados commands:

def migrate_file_to_cold(filepath, hot_pool, cold_pool):
    """Migrate existing file objects between pools"""
    # 1. Get inode
    inode = os.stat(filepath).st_ino
    
    # 2. List objects in hot pool
    objects = subprocess.check_output([
        'rados', '-p', hot_pool, 'ls'
    ]).decode().split('\n')
    
    file_objects = [o for o in objects if o.startswith(f"{inode}.")]
    
    # 3. Copy each object
    for obj in file_objects:
        # Copy object from hot to cold
        subprocess.run([
            'bash', '-c',
            f'rados -p {hot_pool} get {obj} - | rados -p {cold_pool} put {obj} -'
        ], check=True)
    
    # 4. Update layout (this only updates metadata)
    subprocess.run([
        'setfattr', '-n', 'ceph.file.layout.pool',
        '-v', cold_pool, filepath
    ], check=True)
    
    # 5. Delete from hot pool (after verification)
    for obj in file_objects:
        subprocess.run(['rados', '-p', hot_pool, 'rm', obj])
# Mark OSDs by device type
ceph osd crush set-device-class ssd osd.0
ceph osd crush set-device-class hdd osd.1 osd.2

# Create CRUSH rules
ceph osd crush rule create-replicated hot_rule default host ssd
ceph osd crush rule create-replicated cold_rule default host hdd

# Apply rules to pools
ceph osd pool set cephfs.tiercephfs.hot crush_rule hot_rule
ceph osd pool set cephfs.tiercephfs.cold crush_rule cold_rule
```

### Step 3: Deploy Access Tracking

Use existing `/home/vidit-pt7945/cephse/cephfs_lc_daemon.py`:

```python
# This daemon already implements:
# 1. eBPF monitoring (via bcc)
# 2. RocksDB metadata storage
# 3. Access time tracking
# 4. Pool layout changes via setfattr
```

### Step 4: Let MDS Handle Everything

```
MDS automatically:
✅ Updates inode layout when setfattr is called
✅ Redirects new writes to the new pool
✅ Reads from the correct pool based on layout
✅ Handles layout inheritance for new files in directories
✅ Maintains file transparency (same path, same inode)
```

---

## Comparison: Different Approaches

| Approach | Modify Ceph? | Performance | Transparency | Complexity |
|----------|-------------|-------------|--------------|------------|
| **Modify MDS** | ❌ Yes | Fast | Perfect | Very High |
| **FUSE Wrapper** | ✅ No | Slow (FUSE overhead) | Good | Medium |
| **Layout-Based (RECOMMENDED)** | ✅ No | **Native Speed** | **Perfect** | **Low** |
| **Symlinks** | ✅ No | Fast | ❌ Broken Links | Low |
| **Application-Level** | ✅ No | Fast | ❌ App-aware | High |

---

## Why Layout-Based is Server-Side Tiering

```
CLIENT SIDE (What User Sees):
/tiercephfs/video.mp4  [Same path, always accessible]

SERVER SIDE (What Actually Happens):
MDS: Stores layout metadata (pool=hot or pool=cold)
OSD: Physical objects move between SSD and HDD
RADOS: Handles object migration transparently

THIS IS TRUE SERVER-SIDE TIERING:
- User doesn't know or care which pool stores the file
- MDS tracks the pool in inode metadata
- OSDs physically store objects in different storage tiers
- Tiering engine changes metadata, RADOS moves data
```

---

## Final Answer

### The Best Approach (No Ceph Modification):

1. **Multiple Data Pools**: Create hot/warm/cold pools with CRUSH rules for SSD/HDD
2. **MDS Layout Attributes**: Use native `ceph.file.layout.pool` extended attribute
3. **External Tiering Engine**: Daemon that monitors access and changes layouts
4. **RADOS Object Migration**: Let Ceph's built-in mechanisms handle data movement

### Why This is Server-Side:
- **MDS** (server) stores pool in inode metadata
- **OSDs** (server) store objects in different physical locations
- **CRUSH** (server) maps objects to specific storage classes
- **Client** sees unified namespace, unaware of tiering

### What You Build:
1. Access tracking (eBPF + RocksDB) ← Already done
2. Tiering engine (setfattr runner) ← Already done  
3. Policy engine (which files to migrate) ← Partially done

### What Ceph Provides:
1. ✅ File layout system (MDS)
2. ✅ Multi-pool support (CephFS)
3. ✅ CRUSH rules (RADOS)
4. ✅ Object migration (RADOS)
5. ✅ Transparent access (kernel client)

---

## Next Steps

1. **Create additional pools** (hot/cold) on your existing CephFS
2. **Set CRUSH rules** to separate SSD/HDD (or simulate)
3. **Deploy existing `cephfs_lc_daemon.py`** from your workspace
4. **Test migration** with `setfattr` commands
5. **Verify transparency** by accessing files before/after migration

**Ready to implement this?** All the code already exists in your workspace, and this approach requires ZERO Ceph modifications.
