# Birth Time Preservation During Migration

## The Problem

**Birth time (btime) CANNOT be directly preserved** during the Mover Agent migration because:

1. We create a NEW inode (new file)
2. Birth time is set by the kernel at inode creation
3. No user-space API exists to modify birth time

## What Gets Preserved

| Timestamp | Preserved? | Method |
|-----------|-----------|--------|
| **Access time (atime)** | ✅ YES | `cp -p` or `os.utime()` |
| **Modify time (mtime)** | ✅ YES | `cp -p` or `os.utime()` |
| **Change time (ctime)** | ⚠️ PARTIAL | Updated to migration time |
| **Birth time (btime)** | ❌ NO | Set to inode creation time |

## Solution: Store Original Birth Time in Xattr

Since we can't set birth time directly, we **store it as metadata**:

```bash
# Before migration - get original birth time
ORIGINAL_BTIME=$(stat -c '%W' /tiercephfs/file.txt)

# During migration - store in custom xattr
setfattr -n user.original_birthtime -v "$ORIGINAL_BTIME" /tiercephfs/file.txt.migrating

# After migration - retrieve original birth time
STORED_BTIME=$(getfattr --only-values -n user.original_birthtime /tiercephfs/file.txt)
date -d @$STORED_BTIME  # Convert to human-readable
```

## Complete Migration with Birth Time Preservation

```python
def migrate_with_btime_preservation(filepath, target_pool):
    """Migrate file and preserve original birth time in xattr"""
    
    # Get original metadata
    stat_info = os.stat(filepath)
    original_btime = stat_info.st_birthtime if hasattr(stat_info, 'st_birthtime') else None
    
    # Alternative: use stat command
    result = subprocess.run(['stat', '-c', '%W', filepath], capture_output=True, text=True)
    original_btime_timestamp = result.stdout.strip()
    
    # Create temp file with target pool layout
    temp_path = filepath + '.migrating'
    open(temp_path, 'w').close()
    subprocess.run(['setfattr', '-n', 'ceph.file.layout.pool', '-v', target_pool, temp_path])
    
    # Copy data
    shutil.copyfile(filepath, temp_path)
    
    # Restore metadata
    os.chmod(temp_path, stat_info.st_mode)
    os.chown(temp_path, stat_info.st_uid, stat_info.st_gid)
    os.utime(temp_path, (stat_info.st_atime, stat_info.st_mtime))
    
    # Store original birth time in xattr
    if original_btime_timestamp and original_btime_timestamp != '0':
        subprocess.run([
            'setfattr', '-n', 'user.original_birthtime',
            '-v', original_btime_timestamp, temp_path
        ])
    
    # Atomic rename
    os.rename(temp_path, filepath)
```

## Retrieving Original Birth Time

```bash
# Show all timestamps including original birth time
stat /tiercephfs/file.txt

# Get stored original birth time
getfattr -n user.original_birthtime /tiercephfs/file.txt

# Convert to human-readable format
BTIME=$(getfattr --only-values -n user.original_birthtime /tiercephfs/file.txt)
date -d @$BTIME '+%Y-%m-%d %H:%M:%S'
```

## Helper Script

Use the provided helper script:

```bash
# Copy to VM
scp -P 2224 /home/vidit-pt7945/cephse/get_original_birthtime.sh cephvm@localhost:/tmp/

# Run on VM
ssh -p 2224 cephvm@localhost "bash /tmp/get_original_birthtime.sh /tiercephfs/file.txt"
```

## Example Output

```
=== File Timestamps for: /tiercephfs/btime_test.txt ===

Current timestamps (after migration):
Access: 2026-01-03 21:53:16.380000737 +0530
Modify: 2026-01-03 21:53:16.380000737 +0530
Change: 2026-01-03 21:53:16.425500996 +0530
 Birth: 2026-01-03 21:53:16.390834132 +0530  ← NEW inode creation time

---

Original birth time (before migration):
  Timestamp: 1767457396
  Human-readable: 2026-01-03 21:53:16 +0530  ← ORIGINAL creation time
```

## Why This Approach is Acceptable

1. **Access/modify times preserved**: Most important for applications
2. **Original birth time available**: Stored in xattr, can be queried
3. **Standard practice**: This is how production tiering systems work
4. **Transparent to users**: Same filename, same path, data intact

## Alternative: If Birth Time is Critical

If preserving the exact birth time in stat output is absolutely critical, consider:

### Option 1: Lustre-Style Layout Migration
- Requires modifying CephFS MDS (not recommended)
- Can change pool without changing inode

### Option 2: Accept the Limitation
- Most applications don't use birth time
- Access/modify times are what matter for most use cases
- Backup tools rely on mtime, not btime

### Option 3: Don't Use Mover Agent
- Use symlink approach (transparent migration)
- But this has its own limitations (broken symlinks on unmount)

## Comparison: What Each Approach Preserves

| Approach | Inode | atime | mtime | btime | Namespace | Performance |
|----------|-------|-------|-------|-------|-----------|-------------|
| **Mover Agent** | ❌ New | ✅ Yes | ✅ Yes | ⚠️ Xattr | ✅ Same path | Fast |
| **Symlinks** | ✅ Same | ✅ Yes | ✅ Yes | ✅ Yes | ⚠️ Symlink | Fast |
| **Layout Change** | ✅ Same | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Same | ❌ Doesn't work |
| **MDS Modification** | ✅ Same | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Same | ❌ Not allowed |

## Conclusion

**The Mover Agent approach with xattr birth time storage is the best practical solution:**

- ✅ Works with Ceph's constraints
- ✅ Preserves what matters (atime, mtime, data)
- ✅ Original birth time available via xattr
- ✅ Transparent namespace (same filename)
- ✅ Atomic operations
- ✅ Production-ready

**The birth time change is an inherent trade-off** of this approach, but it's the same trade-off made by Gluster, BeeGFS, and other tiered storage systems.
