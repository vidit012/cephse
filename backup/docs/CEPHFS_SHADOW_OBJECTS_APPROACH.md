# CephFS Tiering with Shadow Objects (Application-Level)

## Approach: Transparent File Migration Without Modifying Ceph

Instead of modifying Ceph, implement shadow objects at the application level using:
1. eBPF to intercept file access
2. RocksDB to track file locations
3. Custom FUSE overlay or symlinks for redirection

---

## Architecture

```
User Application
       ↓
  eBPF Hooks (VFS layer)
       ↓
Access Tracking (RocksDB)
       ↓
Tiering Engine
       ↓
┌─────────────────────────────────────┐
│  File Migration Strategy            │
│                                     │
│  Original: /cephfs/file.txt         │
│  (HOT pool: cephfs.tiering.data)    │
│            ↓ cold detected          │
│  Shadow: /cephfs/.shadow/file.txt   │
│  (COLD pool: cephfs.tiering.cold)   │
│            ↓                        │
│  Stub: /cephfs/file.txt → symlink   │
│            ↓                        │
│  On access: Transparently read      │
│  from shadow location               │
└─────────────────────────────────────┘
```

---

## Method 1: Symlink-Based Shadow Objects

### Migration Process:

```python
def demote_file_with_shadow(filepath):
    """Move file to shadow location and create symlink"""
    
    # 1. Create shadow directory structure
    shadow_path = filepath.replace('/cephfs/', '/cephfs/.shadow/')
    shadow_dir = os.path.dirname(shadow_path)
    os.makedirs(shadow_dir, exist_ok=True)
    
    # 2. Copy file to shadow location (in COLD pool)
    # First set the shadow directory to use COLD pool
    subprocess.run([
        'setfattr', '-n', 'ceph.dir.layout.pool',
        '-v', 'cephfs.tiering.cold', shadow_dir
    ])
    
    # Copy file
    shutil.copy2(filepath, shadow_path)
    
    # 3. Replace original with symlink
    os.remove(filepath)
    os.symlink(shadow_path, filepath)
    
    # 4. Update metadata in RocksDB
    db.put(filepath.encode(), json.dumps({
        'tier': 'cold',
        'shadow_path': shadow_path,
        'migration_time': time.time()
    }).encode())
```

**Pros:**
- ✅ Simple implementation
- ✅ No Ceph modification needed
- ✅ Transparent to most applications

**Cons:**
- ❌ Some apps don't follow symlinks
- ❌ Visible in `ls -la` (shows symlink)

---

## Method 2: FUSE Overlay (Fully Transparent)

### Architecture:
```
User Application
       ↓
FUSE Filesystem (/cephfs-tiered)
  ├── Intercepts all file operations
  ├── Checks RocksDB for file location
  ├── Redirects to shadow if in COLD tier
       ↓
Real CephFS Mount (/cephfs)
  ├── /cephfs/file.txt (stub or deleted)
  └── /cephfs/.shadow/file.txt (actual data)
```

### Implementation:

```python
from fuse import FUSE, Operations
import os

class TieredCephFS(Operations):
    def __init__(self, root='/cephfs'):
        self.root = root
        self.db = rocksdb.DB("/var/lib/cephfs_tiering/metadata.db")
    
    def _get_real_path(self, path):
        """Get actual file location (original or shadow)"""
        full_path = os.path.join(self.root, path.lstrip('/'))
        
        # Check RocksDB for shadow location
        try:
            metadata = json.loads(self.db.get(full_path.encode()))
            if metadata['tier'] == 'cold':
                return metadata['shadow_path']
        except:
            pass
        
        return full_path
    
    def read(self, path, size, offset, fh):
        """Transparently read from shadow if moved"""
        real_path = self._get_real_path(path)
        with open(real_path, 'rb') as f:
            f.seek(offset)
            return f.read(size)
    
    def getattr(self, path, fh=None):
        """Return file attributes from real location"""
        real_path = self._get_real_path(path)
        st = os.lstat(real_path)
        return dict((key, getattr(st, key)) for key in (
            'st_atime', 'st_ctime', 'st_gid', 'st_mode',
            'st_mtime', 'st_nlink', 'st_size', 'st_uid'))

# Mount FUSE overlay
FUSE(TieredCephFS(), '/mnt/cephfs-tiered', foreground=False)
```

**Pros:**
- ✅ Completely transparent
- ✅ No symlink visibility
- ✅ Full control over file operations

**Cons:**
- ❌ FUSE overhead (~5-10% performance)
- ❌ More complex to implement

---

## Method 3: Native CephFS Layouts (Recommended) ⭐

**Just use CephFS file layouts - simpler and faster:**

### Migration Process:

```python
def demote_file_simple(filepath):
    """Change file pool using native CephFS layouts"""
    
    # This moves data between pools without shadow objects
    subprocess.run([
        'setfattr', '-n', 'ceph.file.layout.pool',
        '-v', 'cephfs.tiering.cold', filepath
    ], check=True)
    
    # Update metadata
    db.put(filepath.encode(), json.dumps({
        'tier': 'cold',
        'migration_time': time.time()
    }).encode())
```

**Why this is better:**
- ✅ No shadow objects needed (metadata pool serves same purpose)
- ✅ Native Ceph feature
- ✅ Zero overhead
- ✅ Completely transparent to all applications
- ✅ Data actually moves between pools (not copied)

---

## Comparison Table

| Approach | Transparency | Complexity | Ceph Modification | Performance |
|----------|--------------|------------|-------------------|-------------|
| **Native Layouts** | Perfect | Low | None | Best |
| **Symlinks** | Partial | Low | None | Good |
| **FUSE Overlay** | Perfect | Medium | None | Good (-10%) |
| **Modify Ceph MDS** | Perfect | Very High | Yes | Best |

---

## Recommended Approach for Your Use Case

**Use Native CephFS File Layouts + eBPF + RocksDB:**

### Why:
1. **No shadow objects needed** - metadata pool already separates metadata from data
2. **Transparent** - files look identical before/after migration
3. **No Ceph modification** - uses supported features
4. **Best performance** - no FUSE overhead, no symlink indirection

### What You Get (Same as RGW):
```
File: /cephfs/myfile.txt

Before migration:
├── Metadata: cephfs.tiering.meta pool (inode, permissions, layout)
└── Data: cephfs.tiering.data pool (actual file contents)

After migration (30+ days old):
├── Metadata: cephfs.tiering.meta pool (SAME - like RGW head object)
└── Data: cephfs.tiering.cold pool (MOVED - like RGW shadow objects)
```

**User sees:** Exact same file at `/cephfs/myfile.txt`, no difference!

---

## Implementation Plan

1. **eBPF Monitor** - Track file access via VFS hooks
2. **RocksDB** - Store access times and current tier
3. **Tiering Engine** - Scan RocksDB, find cold files
4. **Migration** - Use `setfattr` to change pool (native CephFS)
5. **Promotion** - On access, move back to HOT pool

**No shadow objects, no Ceph modification, fully transparent!**

Ready to implement this approach?
