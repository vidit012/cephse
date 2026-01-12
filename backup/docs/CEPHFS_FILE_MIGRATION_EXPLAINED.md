# How CephFS Files Are Moved Between Storage Tiers

## TL;DR
Files move **between pools** (not storage classes). Data is **physically copied** from one pool's OSDs to another pool's OSDs, then the file's metadata pointer is updated.

---

## The Three Concepts Explained

### 1. **Pools** (Logical Storage Groups)
- Collection of Placement Groups (PGs)
- Maps to physical OSDs
- Example: `cephfs_hot`, `cephfs_warm`, `cephfs_cold`

### 2. **OSDs** (Object Storage Daemons)
- Physical storage devices (HDDs, SSDs)
- Store actual data objects
- Each pool distributes data across multiple OSDs

### 3. **Storage Classes** (RGW Only)
- RGW-specific concept for S3 storage tiers
- NOT used in CephFS
- You saw this with STANDARD/COLD in your S3 bucket

---

## How CephFS Stores Files

### File Layout Structure
Every file in CephFS has a **layout** that defines:

```
File Layout:
├── Pool: Which pool stores the data (cephfs_hot, cephfs_warm, cephfs_cold)
├── Stripe Unit: How much data in each object (4MB default)
├── Stripe Count: How many objects per stripe (1 default)
└── Object Size: Size of each RADOS object (4MB default)
```

### Example: A 100MB File in HOT Pool

```
/mnt/cephfs/user/video.mp4 (100MB)
    ↓
File Metadata (stored in cephfs_metadata pool):
    - Inode: 123456
    - Size: 100MB
    - Layout Pool: cephfs_hot  ← THIS IS KEY
    - Stripe Unit: 4MB
    
Data Objects (stored in cephfs_hot pool):
    - 123456.00000000 (4MB) → OSD.0
    - 123456.00000001 (4MB) → OSD.1
    - 123456.00000002 (4MB) → OSD.2
    - ... 25 objects total (100MB / 4MB)
```

---

## Migration Process: HOT → COLD

When you run:
```bash
setfattr -n ceph.file.layout.pool -v cephfs_cold /mnt/cephfs/user/video.mp4
```

### What Happens Step-by-Step:

#### **Step 1: Layout Change (Immediate)**
```
File Metadata Update:
    - Layout Pool: cephfs_hot → cephfs_cold  [UPDATED IN METADATA POOL]
```
This happens **instantly** - just a metadata write.

#### **Step 2: Data Migration (Background)**
CephFS uses **lazy migration**. Two approaches:

**Approach A: Copy-on-Write (NEW WRITES ONLY)**
```
Old data remains in cephfs_hot pool:
    - 123456.00000000 (4MB) → OSD.0 [cephfs_hot]
    - 123456.00000001 (4MB) → OSD.1 [cephfs_hot]
    
New writes go to cephfs_cold pool:
    - 123456.00000002 (4MB) → OSD.2 [cephfs_cold]  [NEW WRITE]
```

**Approach B: Forced Migration (IMMEDIATE COPY)**
Use `ceph-objectstore-tool` or custom migration script:
```bash
# For each object in the file
for obj in $(ceph osd map cephfs_hot 123456.00000000 | grep object); do
    # Copy object from hot to cold pool
    rados -p cephfs_hot get $obj /tmp/$obj
    rados -p cephfs_cold put $obj /tmp/$obj
    rm /tmp/$obj
done

# Delete old objects from hot pool
rados -p cephfs_hot rm 123456.00000000
rados -p cephfs_hot rm 123456.00000001
...
```

---

## Visual: How Data Moves Between OSDs

### Before Migration (File in HOT Pool)

```
┌─────────────────────────────────────────────────────────┐
│  File: /mnt/cephfs/video.mp4                            │
│  Layout Pool: cephfs_hot                                │
└─────────────────────────────────────────────────────────┘
           ↓
┌─────────────────────────────────────────────────────────┐
│  cephfs_hot Pool (PGs: 0.1, 0.2, 0.3...)               │
└─────────────────────────────────────────────────────────┘
           ↓ CRUSH algorithm maps to OSDs
┌──────────────┬──────────────┬──────────────┐
│   OSD.0      │   OSD.1      │   OSD.2      │
│  (SSD/HDD)   │  (SSD/HDD)   │  (SSD/HDD)   │
│              │              │              │
│ Object:      │ Object:      │ Object:      │
│ 123456.0000  │ 123456.0001  │ 123456.0002  │
│ (4MB data)   │ (4MB data)   │ (4MB data)   │
└──────────────┴──────────────┴──────────────┘
```

### After Migration (File in COLD Pool)

```
┌─────────────────────────────────────────────────────────┐
│  File: /mnt/cephfs/video.mp4                            │
│  Layout Pool: cephfs_cold  ← METADATA UPDATED           │
└─────────────────────────────────────────────────────────┘
           ↓
┌─────────────────────────────────────────────────────────┐
│  cephfs_cold Pool (PGs: 5.1, 5.2, 5.3...)              │
│  (with lz4 compression enabled)                         │
└─────────────────────────────────────────────────────────┘
           ↓ CRUSH algorithm maps to OSDs (same OSDs!)
┌──────────────┬──────────────┬──────────────┐
│   OSD.0      │   OSD.1      │   OSD.2      │
│  (SSD/HDD)   │  (SSD/HDD)   │  (SSD/HDD)   │
│              │              │              │
│ Object:      │ Object:      │ Object:      │
│ 123456.0000  │ 123456.0001  │ 123456.0002  │
│ (2MB comp)   │ (2MB comp)   │ (2MB comp)   │
│ [COLD POOL]  │ [COLD POOL]  │ [COLD POOL]  │
└──────────────┴──────────────┴──────────────┘
```

**Key Point:** In a single-node cluster, the same OSDs store both pools! The difference is:
- Different PGs (different namespace)
- Different compression settings (lz4 for cold)
- Different CRUSH rules (can target different device classes)

---

## Pool vs OSD vs Device Class

### In a Multi-Node Production Cluster

You'd configure pools to target different **device classes**:

```bash
# HOT pool uses SSDs
ceph osd pool create cephfs_hot 64 64
ceph osd pool set cephfs_hot crush_rule ssd_rule

# COLD pool uses HDDs
ceph osd pool create cephfs_cold 64 64
ceph osd pool set cephfs_cold crush_rule hdd_rule
```

**CRUSH Map determines which OSDs store which pool's data:**

```
Device Classes:
├── ssd (OSD.0, OSD.1, OSD.2) ← cephfs_hot pool data goes here
└── hdd (OSD.3, OSD.4, OSD.5) ← cephfs_cold pool data goes here
```

**Migration then means:**
1. Copy data from SSD OSDs (OSD.0-2) to HDD OSDs (OSD.3-5)
2. Update file layout metadata
3. Delete old data from SSD OSDs

---

## Your Single-Node Setup

Since you have **3 OSDs on 1 node**, all pools share the same OSDs. The benefit is:

### What You Get:
- ✅ **Different compression** (lz4 on cold, none on hot)
- ✅ **Logical separation** (easier management)
- ✅ **Different I/O priorities** (can set pool-level QoS)

### What You DON'T Get:
- ❌ Different physical devices (all on same HDDs)
- ❌ Performance tiers (no SSD vs HDD separation)

---

## Practical Migration Commands

### Check Current File Layout
```bash
# Show which pool stores the file
getfattr -n ceph.file.layout /mnt/cephfs/myfile.txt

# Output:
# ceph.file.layout="stripe_unit=4194304 stripe_count=1 object_size=4194304 pool=cephfs_hot"
```

### Move File to Different Pool
```bash
# Change pool (metadata update only)
setfattr -n ceph.file.layout.pool -v cephfs_cold /mnt/cephfs/myfile.txt

# Verify
getfattr -n ceph.file.layout.pool /mnt/cephfs/myfile.txt
```

### Force Immediate Data Migration
```bash
# Rewrite file to trigger data movement
dd if=/mnt/cephfs/myfile.txt of=/tmp/temp bs=1M
dd if=/tmp/temp of=/mnt/cephfs/myfile.txt bs=1M conv=notrunc
rm /tmp/temp

# OR use rados directly (more efficient)
# Get file's inode
INODE=$(stat -c %i /mnt/cephfs/myfile.txt)

# Find objects
rados -p cephfs_hot ls | grep $INODE

# Copy each object to new pool
for obj in $(rados -p cephfs_hot ls | grep $INODE); do
    rados -p cephfs_hot get $obj - | rados -p cephfs_cold put $obj -
    rados -p cephfs_hot rm $obj
done
```

---

## Comparing RGW vs CephFS Migration

| Aspect | RGW (S3 - What You Tested) | CephFS (File System - eBPF Plan) |
|--------|---------------------------|-----------------------------------|
| **Tier Concept** | Storage Classes (STANDARD, COLD) | Pools (cephfs_hot, cephfs_cold) |
| **Data Location** | Different pools per storage class | Different pools per tier |
| **Metadata Update** | Bucket index + object manifest | File inode layout attribute |
| **Data Copy** | Shadow objects created, original kept | Objects copied between pools |
| **User Visibility** | S3 API shows StorageClass tag | Transparent (same file path) |
| **Migration Trigger** | Lifecycle policy (LC daemon) | Tiering engine + setfattr |
| **Compression** | Per storage class (lz4 on COLD) | Per pool (lz4 on cold pool) |
| **Access After Move** | Reads from shadow objects | Reads from new pool objects |

---

## Summary: How Files Move in eBPF Tiering

```
User creates file:
    echo "data" > /mnt/cephfs/test.txt
    ↓
File stored in DEFAULT pool (cephfs_hot):
    Object: <inode>.00000000 → OSD.0, OSD.1, OSD.2
    Metadata: layout.pool = cephfs_hot
    ↓
eBPF monitors access for 30 days:
    RocksDB: {last_access: Jan 1, 2026}
    ↓
Tiering engine detects cold file (Feb 1, 2026):
    Age > 30 days, score < 25
    ↓
Execute migration:
    setfattr -n ceph.file.layout.pool -v cephfs_cold /mnt/cephfs/test.txt
    ↓
Metadata updated immediately:
    layout.pool = cephfs_hot → cephfs_cold
    ↓
Data migration (2 options):
    A) Lazy: Next write goes to cephfs_cold, old data stays in cephfs_hot
    B) Eager: Script copies objects, deletes from cephfs_hot
    ↓
Final state:
    Object: <inode>.00000000 → cephfs_cold pool → OSD.0, OSD.1, OSD.2
    File compressed with lz4
    User sees same path: /mnt/cephfs/test.txt
```

---

## Recommendation for Your Implementation

Given your single-node setup, I recommend:

### **Option 1: Pool-Based Tiers (Simpler)**
- Create 3 pools: `cephfs_hot`, `cephfs_warm`, `cephfs_cold`
- Use `setfattr` to change file layouts
- Enable compression on cold pool only
- **Migration:** Metadata-only (fast), lazy data migration

### **Option 2: OSD-Based Tiers (More Complex)**
- Use device classes: mark OSDs as `hot`, `warm`, `cold`
- Create CRUSH rules to target specific OSD classes
- Pools automatically use different OSDs
- **Migration:** Physical data movement between OSDs

For **testing and learning**, use **Option 1**. It's simpler and shows the concept clearly.

For **production with multiple nodes and mixed storage (SSD/HDD)**, use **Option 2**.

---

## Next: Do You Want to Implement This?

I can help you:
1. Set up CephFS with 3 pools (hot/warm/cold)
2. Test file layout changes with `setfattr`
3. Write a simple migration script
4. Show how eBPF would track these files

Ready to start?
