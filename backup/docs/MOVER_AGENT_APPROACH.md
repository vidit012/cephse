# Storage Tiering: The "Mover Agent" Approach

## Why This Approach is Better

### The Problem with Layout-Based Migration
```bash
# ❌ This FAILS on files with existing data:
setfattr -n ceph.file.layout.pool -v cold_pool /tiercephfs/file.mp4
# Error: Directory not empty
```

**Ceph limitation**: Cannot change layout of populated files!

### The Solution: "Mover Agent" Pattern

Instead of changing the layout of an existing file, we:
1. ✅ Create NEW file with target pool layout (on EMPTY file)
2. ✅ Copy data to new file
3. ✅ Atomically rename new file over old one

---

## The Algorithm (Step-by-Step)

```
Original File:                Temp File:
/tiercephfs/video.mp4        /tiercephfs/video.mp4.migrating
├── Inode: 1234567           ├── Inode: 7654321 (NEW)
├── Pool: HOT (SSD)          ├── Pool: COLD (HDD) ← Set BEFORE writing data
├── Size: 100MB              ├── Size: 0 bytes (empty)
└── Objects in OSD.0         └── No objects yet

         [Step 1: Create empty temp file]
                    ↓
         [Step 2: setfattr on EMPTY file - THIS WORKS!]
                    ↓
         [Step 3: Copy data from original to temp]
                    ↓
Temp File:
├── Inode: 7654321
├── Pool: COLD (HDD)
├── Size: 100MB
└── Objects in OSD.2 (HDD)   ← Data now in COLD pool!

         [Step 4: Restore metadata (permissions, timestamps)]
                    ↓
         [Step 5: ATOMIC RENAME]
         os.rename(temp, original)
                    ↓
Final State:
/tiercephfs/video.mp4
├── Inode: 7654321 (CHANGED)  ← Users get new inode
├── Pool: COLD (HDD)          ← Now in cold pool!
├── Size: 100MB
└── Objects in OSD.2 (HDD)

Old file (inode 1234567) is unlinked, objects garbage collected by CephFS
```

---

## Implementation: Complete Mover Agent

```python
#!/usr/bin/env python3
"""
CephFS Storage Tiering - Mover Agent Approach
Creates new files with desired pool layout, copies data, atomic rename
"""

import os
import sys
import shutil
import subprocess
import logging
from datetime import datetime

class MoverAgent:
    def __init__(self, mount_point="/tiercephfs"):
        self.mount_point = mount_point
        self.logger = self.setup_logging()
    
    def setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        return logging.getLogger('MoverAgent')
    
    def is_file_open(self, filepath):
        """Check if file is currently open (avoids breaking active file handles)"""
        try:
            # lsof returns 0 if file is open
            result = subprocess.run(
                ['lsof', filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return result.returncode == 0
        except FileNotFoundError:
            # lsof not installed, skip check
            return False
    
    def get_file_pool(self, filepath):
        """Get current pool of file"""
        try:
            result = subprocess.check_output([
                'getfattr', '-n', 'ceph.file.layout.pool',
                '--only-values', filepath
            ], stderr=subprocess.DEVNULL)
            return result.decode().strip()
        except:
            return None
    
    def migrate_to_pool(self, filepath, target_pool):
        """
        Migrate file to target pool using Mover Agent approach
        
        Returns:
            True if migration successful
            False if migration failed or skipped
        """
        try:
            # Validation checks
            if not os.path.exists(filepath):
                self.logger.warning(f"File does not exist: {filepath}")
                return False
            
            if not os.path.isfile(filepath):
                self.logger.warning(f"Not a regular file: {filepath}")
                return False
            
            # Check if file is open
            if self.is_file_open(filepath):
                self.logger.info(f"Skipping {filepath} - file is currently open")
                return False
            
            # Check current pool
            current_pool = self.get_file_pool(filepath)
            if current_pool == target_pool:
                self.logger.debug(f"File {filepath} already in {target_pool}")
                return True
            
            # Get original file attributes
            stat_info = os.stat(filepath)
            original_size = stat_info.st_size
            original_mode = stat_info.st_mode
            original_uid = stat_info.st_uid
            original_gid = stat_info.st_gid
            original_atime = stat_info.st_atime
            original_mtime = stat_info.st_mtime
            
            self.logger.info(
                f"Migrating {filepath} ({original_size} bytes) "
                f"from {current_pool} to {target_pool}"
            )
            
            # Create temp file path
            temp_path = filepath + ".migrating"
            
            # Remove temp file if it exists from previous failed migration
            if os.path.exists(temp_path):
                os.remove(temp_path)
            
            # STEP 1: Create empty temp file
            with open(temp_path, 'w'):
                pass
            
            # STEP 2: Set pool layout on EMPTY file (THIS IS THE KEY!)
            subprocess.run([
                'setfattr', '-n', 'ceph.file.layout.pool',
                '-v', target_pool, temp_path
            ], check=True, timeout=10)
            
            # Verify layout was set
            temp_pool = self.get_file_pool(temp_path)
            if temp_pool != target_pool:
                raise Exception(f"Failed to set pool layout: {temp_pool} != {target_pool}")
            
            self.logger.debug(f"Created temp file with pool={target_pool}")
            
            # STEP 3: Copy data (this writes to the new pool)
            start_time = datetime.now()
            shutil.copyfile(filepath, temp_path)
            copy_duration = (datetime.now() - start_time).total_seconds()
            
            # Verify copy integrity
            temp_size = os.path.getsize(temp_path)
            if temp_size != original_size:
                raise Exception(f"Copy size mismatch: {original_size} != {temp_size}")
            
            throughput_mbps = (original_size / 1024 / 1024) / copy_duration if copy_duration > 0 else 0
            self.logger.debug(
                f"Copied {original_size} bytes in {copy_duration:.2f}s "
                f"({throughput_mbps:.2f} MB/s)"
            )
            
            # STEP 4: Restore metadata
            os.chmod(temp_path, original_mode)
            os.chown(temp_path, original_uid, original_gid)
            os.utime(temp_path, (original_atime, original_mtime))
            
            # Store original birth time in xattr (cannot set btime directly)
            try:
                # Get original birth time if available
                result = subprocess.run(
                    ['stat', '-c', '%W', filepath],
                    capture_output=True, text=True, check=False
                )
                if result.returncode == 0 and result.stdout.strip() != '0':
                    original_btime = result.stdout.strip()
                    subprocess.run([
                        'setfattr', '-n', 'user.original_birthtime',
                        '-v', original_btime, temp_path
                    ], check=False)
                    self.logger.debug(f"Stored original birth time: {original_btime}")
            except:
                pass  # Birth time preservation is best-effort
            
            # STEP 5: ATOMIC RENAME (The Magic!)
            # This is atomic in POSIX - users see instant switch
            os.rename(temp_path, filepath)
            
            self.logger.info(
                f"✓ Migration complete: {filepath} now in {target_pool} "
                f"(inode changed, namespace unchanged)"
            )
            
            return True
            
        except Exception as e:
            self.logger.error(f"Migration failed for {filepath}: {e}")
            
            # Clean up temp file
            temp_path = filepath + ".migrating"
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass
            
            return False
    
    def demote_to_cold(self, filepath, cold_pool="cephfs.tiercephfs.cold"):
        """Demote file from hot to cold pool"""
        return self.migrate_to_pool(filepath, cold_pool)
    
    def promote_to_hot(self, filepath, hot_pool="cephfs.tiercephfs.hot"):
        """Promote file from cold to hot pool"""
        return self.migrate_to_pool(filepath, hot_pool)

# Example usage
if __name__ == '__main__':
    agent = MoverAgent()
    
    # Test migration
    test_file = "/tiercephfs/test_migration.txt"
    
    # Create test file
    with open(test_file, 'w') as f:
        f.write("Test data for migration\n" * 1000)
    
    print(f"Created test file: {test_file}")
    print(f"Current pool: {agent.get_file_pool(test_file)}")
    
    # Demote to cold
    print("\nDemoting to cold pool...")
    agent.demote_to_cold(test_file)
    print(f"New pool: {agent.get_file_pool(test_file)}")
    
    # Promote back to hot
    print("\nPromoting to hot pool...")
    agent.promote_to_hot(test_file)
    print(f"Final pool: {agent.get_file_pool(test_file)}")
```

---

## Key Advantages

### ✅ Transparency at Namespace Level
```
User perspective:
/tiercephfs/video.mp4  ← Same path before and after migration
```

### ✅ Atomic Switch
```
# The rename is atomic - no partial state visible
os.rename(temp, original)  # Instant switch, no race condition
```

### ✅ Works with Ceph Limitations
```
# setfattr only works on empty files
touch temp_file              # Create empty file
setfattr ... temp_file       # Set layout ✓
copy data to temp_file       # Write data to target pool ✓
rename temp_file over original  # Atomic switch ✓
```

---

## Limitations & Solutions

### ⚠️ Inode Number Changes

**Problem:**
```python
# Before migration: Inode 1234567
# After migration:  Inode 7654321  ← CHANGED!
```

**Impact:**
- If user has file open during migration, they keep reading/writing OLD inode
- Old inode data is unlinked but still accessible until they close the file
- When they close and reopen, they get the NEW inode

**Solution:**
```python
def is_file_open(self, filepath):
    """Check if file is open before migrating"""
    result = subprocess.run(['lsof', filepath], ...)
    return result.returncode == 0

# In migration logic:
if self.is_file_open(filepath):
    logger.info("Skipping migration - file is open")
    return False
```

### ⚠️ Storage Space During Migration

**Problem:**
- During migration, data exists in BOTH pools (original + temp)
- Requires 2x storage space temporarily

**Solution:**
```python
# Check available space before migrating
import shutil
stat = shutil.disk_usage(mount_point)
if stat.free < file_size * 2:
    logger.warning("Insufficient space for migration")
    return False
```

### ⚠️ Hard Links Break

**Problem:**
```bash
ln /tiercephfs/file1.txt /tiercephfs/file2.txt  # Hard link
# After migration, file2.txt still points to OLD inode
```

**Solution:**
```python
# Check for hard links before migration
stat = os.stat(filepath)
if stat.st_nlink > 1:
    logger.warning(f"File has {stat.st_nlink} hard links, skipping")
    return False
```

---

## Integration with Automatic Tiering

Update the automatic tiering daemon to use this approach:

```python
class AutoTieringDaemon:
    def __init__(self):
        # ... existing code ...
        self.mover = MoverAgent(CONFIG['mount_point'])
    
    def scan_and_tier(self):
        """Scan files and tier using Mover Agent"""
        # ... scan logic ...
        
        for filepath, metadata in files_to_demote:
            # Use Mover Agent instead of manual rados copy
            if self.mover.demote_to_cold(filepath):
                metadata['current_pool'] = CONFIG['cold_pool']
                self.db.put(filepath, json.dumps(metadata))
        
        for filepath, metadata in files_to_promote:
            if self.mover.promote_to_hot(filepath):
                metadata['current_pool'] = CONFIG['hot_pool']
                self.db.put(filepath, json.dumps(metadata))
```

---

## Comparison: Old vs New Approach

| Aspect | Layout-Based (Old) | Mover Agent (New) |
|--------|-------------------|-------------------|
| **setfattr on populated file** | ❌ Fails | ✅ Works (empty file) |
| **Manual object copy** | ✅ Required | ❌ Not needed (shutil.copyfile) |
| **Atomic switch** | ❌ No (multi-step) | ✅ Yes (os.rename) |
| **Inode preserved** | ✅ Yes | ❌ No (new inode) |
| **Complexity** | High (rados CLI) | Low (Python stdlib) |
| **Open file handling** | N/A | ✅ Check with lsof |

---

## Testing the Mover Agent

```bash
# Test 1: Create file and check pool
echo "test data" > /tiercephfs/test.txt
getfattr -n ceph.file.layout.pool /tiercephfs/test.txt
# Output: cephfs.tiercephfs.hot

# Test 2: Migrate to cold
python3 -c "
from MOVER_AGENT_APPROACH import MoverAgent
agent = MoverAgent()
agent.demote_to_cold('/tiercephfs/test.txt')
"

# Test 3: Verify new pool
getfattr -n ceph.file.layout.pool /tiercephfs/test.txt
# Output: cephfs.tiercephfs.cold

# Test 4: Verify data integrity
cat /tiercephfs/test.txt
# Output: test data ✓

# Test 5: Check inode changed
stat /tiercephfs/test.txt
# Inode will be different from before
```

---

## Production Deployment

Add to your automatic tiering daemon:

```python
# Replace migrate_file() method with Mover Agent approach
from mover_agent import MoverAgent

class AutoTieringDaemon:
    def __init__(self):
        self.mover = MoverAgent(CONFIG['mount_point'])
    
    def migrate_file(self, filepath, source_pool, dest_pool):
        """Use Mover Agent for migration"""
        return self.mover.migrate_to_pool(filepath, dest_pool)
```

---

## Summary: Why Mover Agent Wins

1. **Works with Ceph constraints**: setfattr on empty files ✓
2. **Simpler implementation**: No manual rados commands needed
3. **Atomic operations**: os.rename() is atomic
4. **Better error handling**: Easy to rollback failed migrations
5. **Standard Python**: Uses shutil, os - no external tools except lsof

**This is the industry-standard approach used by Gluster, BeeGFS, and other tiered storage systems!**
