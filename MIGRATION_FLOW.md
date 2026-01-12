# CephFS Storage Tiering - Complete Migration Flow

## Overview
Files move between 3 pools: **data (hot)** ↔ **warm** ↔ **cold**

---

## Complete System Flow

```
┌─────────────────────────────────────────────────────────────────┐
│  Step 1: USER ACCESSES FILE                                      │
│  $ cat /tiercephfs/file1.txt                                     │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  Step 2: eBPF TRACKER CAPTURES                                   │
│  - Kernel hook on ceph_read_iter()                               │
│  - Gets: inode, uid, path, timestamp                             │
│  - Filters: Skip UID 0 (root), Skip hidden files (.swp)          │
│  - Logs to: file_access_log table                                │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  Step 3: AGGREGATOR (every 4 minutes)                            │
│  - Reads all from file_access_log                                │
│  - Updates file_metadata:                                        │
│    * Sets last_access = latest timestamp                         │
│    * Inserts new files                                           │
│  - Clears file_access_log                                        │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  Step 4: POLICY ENGINE (every 60 seconds)                        │
│                                                                   │
│  Check 1: DEMOTION (accessed files move to hot)                  │
│  --------------------------------------------------------         │
│  UPDATE file_metadata                                            │
│  SET needs_migration = TRUE,                                     │
│      target_pool = 'cephfs.tiercephfs.data'                      │
│  WHERE current_pool IN ('warm', 'cold')                          │
│    AND last_access >= NOW() - INTERVAL '3 minutes'               │
│    AND needs_migration = FALSE                                   │
│                                                                   │
│  Check 2: PROMOTION data → warm (idle 3+ min)                    │
│  --------------------------------------------------------         │
│  UPDATE file_metadata                                            │
│  SET needs_migration = TRUE,                                     │
│      target_pool = 'cephfs.tiercephfs.warm'                      │
│  WHERE current_pool = 'cephfs.tiercephfs.data'                   │
│    AND last_access < NOW() - INTERVAL '3 minutes'                │
│    AND needs_migration = FALSE                                   │
│                                                                   │
│  Check 3: PROMOTION warm → cold (idle 6+ min total)              │
│  --------------------------------------------------------         │
│  UPDATE file_metadata                                            │
│  SET needs_migration = TRUE,                                     │
│      target_pool = 'cephfs.tiercephfs.cold'                      │
│  WHERE current_pool = 'cephfs.tiercephfs.warm'                   │
│    AND last_access < NOW() - INTERVAL '6 minutes'                │
│    AND needs_migration = FALSE                                   │
│                                                                   │
│  Result: Files marked with target pool                           │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  Step 5: MIGRATION WORKER (every 30 seconds, 5 threads)          │
│                                                                   │
│  5.1: Query marked files                                         │
│  --------------------------------------------------------         │
│  SELECT inode, path, current_pool, target_pool                   │
│  FROM file_metadata                                              │
│  WHERE needs_migration = TRUE                                    │
│  ORDER BY last_access ASC                                        │
│  FOR UPDATE SKIP LOCKED  -- Prevents concurrent migrations       │
│                                                                   │
│  5.2: For each file (parallel with 5 workers)                    │
│  --------------------------------------------------------         │
│  Call: libcephfs_migrate /file1.txt cephfs.tiercephfs.warm      │
│                                                                   │
│  5.3: Update database on success                                 │
│  --------------------------------------------------------         │
│  UPDATE file_metadata                                            │
│  SET current_pool = target_pool,                                 │
│      target_pool = NULL,                                         │
│      needs_migration = FALSE                                     │
│  WHERE inode = <inode>                                           │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  Step 6: PHYSICAL MIGRATION (libcephfs_migrate binary)           │
│                                                                   │
│  This is THE KEY PART - How files actually move between pools    │
└─────────────────────────────────────────────────────────────────┘

---

## How Physical Migration Works (Shadow File Technique)

### Example: Migrating file1.txt from data pool → warm pool

```
Initial State:
/tiercephfs/file1.txt (inode: 1099511629333)
  └─ Pool: cephfs.tiercephfs.data
  └─ Content: "test1"
```

### Step-by-Step Migration Process:

#### 1. Mount CephFS
```c
ceph_create(&cmount, NULL);
ceph_conf_read_file(cmount, "/etc/ceph/ceph.conf");
ceph_mount(cmount, "/");
```
- Creates internal CephFS connection
- Mounts at root "/" (not /tiercephfs)

---

#### 2. Read Source File Metadata
```c
ceph_stat(cmount, "/file1.txt", &st);
```
- Gets: size, permissions, owner, timestamps
- Current pool: data

---

#### 3. Create Shadow File
```c
shadow_path = "/file1.txt.__tiering__"
ceph_open(cmount, shadow_path, O_CREAT | O_EXCL | O_WRONLY, 0644);
```

```
Filesystem now has:
/tiercephfs/file1.txt          (original, inode: 1099511629333)
/tiercephfs/file1.txt.__tiering__ (shadow, NEW inode: 1099511629334)
```

**KEY POINT**: Shadow file gets a **NEW INODE** automatically!

---

#### 4. Set Target Pool on Shadow File
```c
ceph_setxattr(cmount, shadow_path, 
              "ceph.file.layout.pool", 
              "cephfs.tiercephfs.warm", 
              strlen(target_pool), 0);
```

```
Shadow file now configured:
/tiercephfs/file1.txt.__tiering__ (inode: 1099511629334)
  └─ Pool: cephfs.tiercephfs.warm  ← TARGET POOL SET
  └─ Content: (empty, just created)
```

---

#### 5. Copy Data from Source to Shadow
```c
src_fd = ceph_open(cmount, "/file1.txt", O_RDONLY);

while ((bytes = ceph_read(src_fd, buffer, 4MB, offset)) > 0) {
    ceph_write(dst_fd, buffer, bytes, offset);
    offset += bytes;
}
```

```
Now:
Original: /tiercephfs/file1.txt (data pool, "test1")
Shadow:   /tiercephfs/file1.txt.__tiering__ (warm pool, "test1")
```

Data is now physically in the **warm pool** in the shadow file!

---

#### 6. Preserve Metadata
```c
ceph_fchown(dst_fd, st.st_uid, st.st_gid);      // Keep owner
ceph_fchmod(dst_fd, st.st_mode);                // Keep permissions
ceph_futimens(dst_fd, times);                   // Keep timestamps
```

Shadow file now identical to original, just in different pool.

---

#### 7. **ATOMIC RENAME** (The Magic Moment)
```c
ceph_rename(cmount, 
            "/file1.txt.__tiering__",    // shadow (new inode)
            "/file1.txt");               // original name
```

```
BEFORE RENAME:
/tiercephfs/file1.txt          (inode: 1099511629333, data pool)
/tiercephfs/file1.txt.__tiering__ (inode: 1099511629334, warm pool)

AFTER RENAME:
/tiercephfs/file1.txt          (inode: 1099511629334, warm pool)
Old inode 1099511629333 is DELETED
```

**CRITICAL**: The rename **replaces** the old file with the new one!
- Old inode disappears
- New inode takes the original filename
- File is now in warm pool

---

## Why Inode Changes During Migration

**This is NOT a bug - it's by design!**

1. **Shadow file gets new inode** when created
2. **Atomic rename** replaces old inode with new inode
3. **Old inode is deleted** after rename

### Example Timeline:
```
t=0:  file1.txt (inode: 123, data pool)
      ↓ Migration starts
t=1:  file1.txt (inode: 123, data pool)
      file1.txt.__tiering__ (inode: 456, warm pool) ← New inode!
      ↓ Copy data
t=2:  Both files exist with different inodes
      ↓ Atomic rename
t=3:  file1.txt (inode: 456, warm pool) ← New inode now!
      Old inode 123 DELETED
```

---

## Database Sync Problem

### The Issue:
The database stores the OLD inode (123), but after migration the file has a NEW inode (456).

### Current Flow:
```
1. Policy Engine marks:
   inode=123, needs_migration=TRUE, target_pool=warm

2. Migration Worker:
   - Finds file with inode=123
   - Calls libcephfs_migrate
   - Migration creates NEW inode=456
   - Updates database: inode=123, current_pool=warm ❌ WRONG!

3. Result:
   - Database thinks: inode=123 is in warm
   - Reality: inode=456 is in warm, inode=123 doesn't exist!
```

### Why This Happens:
The migration worker updates the database **using the old inode** from before migration, but the actual file now has a **new inode** after the atomic rename.

---

## Solution: Track Inode Changes

The migration worker needs to:

1. **Before migration**: Get old inode
2. **After migration**: Get new inode from the renamed file
3. **Update database**: Use new inode

### Fixed Migration Worker Logic:
```python
def migrate_file(file_info):
    old_inode = file_info['inode']
    path = file_info['path']
    target_pool = file_info['target_pool']
    
    # Migrate file (creates new inode)
    subprocess.run([libcephfs_bin, cephfs_path, target_pool])
    
    # Get NEW inode after migration
    new_inode = os.stat(full_path).st_ino
    
    # Update database with NEW inode
    if new_inode != old_inode:
        # Delete old inode entry
        DELETE FROM file_metadata WHERE inode = old_inode
        # Insert/update new inode entry  
        INSERT INTO file_metadata (inode, path, current_pool, ...)
        VALUES (new_inode, path, target_pool, ...)
```

---

## Complete Migration Example

### Scenario: file1.txt moves from data → warm → cold

#### Initial State:
```
File: /tiercephfs/file1.txt
Inode: 1000
Pool: data
Last Access: 2026-01-12 10:00:00
```

#### After 3 minutes (no access):
```sql
-- Policy engine marks for promotion
UPDATE file_metadata 
SET needs_migration=TRUE, target_pool='warm'
WHERE inode=1000 AND last_access < NOW() - '3 min'
```

#### Migration worker executes:
```
1. Calls: libcephfs_migrate /file1.txt cephfs.tiercephfs.warm
2. Shadow file created with inode 1001 in warm pool
3. Atomic rename: file1.txt now has inode 1001
4. OLD inode 1000 deleted

Result:
  File: /tiercephfs/file1.txt
  Inode: 1001 (NEW!)
  Pool: warm
```

#### Database should update:
```sql
-- Delete old inode
DELETE FROM file_metadata WHERE inode = 1000

-- Insert/update new inode
INSERT INTO file_metadata (inode, path, current_pool, last_access)
VALUES (1001, 'file1.txt', 'cephfs.tiercephfs.warm', '2026-01-12 10:03:00')
```

---

## Summary

### Key Points:
1. **Migration uses shadow file technique** for atomic, safe moves
2. **Every migration creates a NEW inode** (this is normal!)
3. **Database must track inode changes** after migration
4. **Migration worker needs to update** database with new inode
5. **Old inodes are automatically deleted** by CephFS after rename

### The Shadow File Technique:
- ✅ **Atomic**: Rename is atomic, no partial states
- ✅ **Safe**: Original file intact until rename succeeds
- ✅ **Fast**: No in-place modification of file data
- ⚠️ **Creates new inode**: Database must be updated

### Current Bug:
Migration worker updates database with **old inode**, but file has **new inode** after migration. This causes the database to be out of sync with reality.

### Fix Needed:
Modify migration_worker.py to:
1. Get new inode after migration
2. Delete old inode from database
3. Insert/update new inode in database
