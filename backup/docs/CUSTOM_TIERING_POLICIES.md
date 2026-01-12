# CephFS Storage Tiering - Custom Policy Examples

## Current Policy (Default)
```json
{
  "cold_age_days": 30,  // Files not accessed in 30 days → COLD (HDD)
  "hot_age_days": 15    // Files accessed within 15 days → HOT (SSD)
}
```

## Policy Examples - Modify to YOUR needs!

### Example 1: Aggressive Tiering (Save SSD Space)
```json
{
  "cold_age_days": 7,   // Move to HDD after 7 days
  "hot_age_days": 3     // Bring back if accessed within 3 days
}
```

### Example 2: Conservative (Keep on SSD Longer)
```json
{
  "cold_age_days": 90,  // Move to HDD after 90 days
  "hot_age_days": 30    // Bring back if accessed within 30 days
}
```

### Example 3: Size-Based Tiering (Add to daemon)
```python
def should_demote(self, filepath, age_seconds):
    """Custom logic: Consider file size"""
    file_size = os.path.getsize(filepath)
    
    # Large files (>100MB) → demote faster
    if file_size > 100 * 1024 * 1024:
        threshold = 7 * 86400  # 7 days
    else:
        threshold = self.cold_threshold
    
    return age_seconds > threshold
```

### Example 4: File Type Based
```python
def should_demote(self, filepath, age_seconds):
    """Custom logic: Consider file type"""
    # Videos/backups → cold faster
    if filepath.endswith(('.mp4', '.avi', '.backup', '.tar.gz')):
        return age_seconds > (7 * 86400)  # 7 days
    
    # Code/docs → keep hot longer
    elif filepath.endswith(('.py', '.c', '.txt', '.md')):
        return age_seconds > (60 * 86400)  # 60 days
    
    # Default
    return age_seconds > self.cold_threshold
```

### Example 5: Directory-Based Policy
```python
def should_demote(self, filepath, age_seconds):
    """Custom logic: Different rules per directory"""
    
    # /cephfs/active/ - never demote
    if filepath.startswith('/cephfs/active/'):
        return False
    
    # /cephfs/archive/ - demote immediately
    if filepath.startswith('/cephfs/archive/'):
        return True
    
    # /cephfs/projects/ - 30 day rule
    if filepath.startswith('/cephfs/projects/'):
        return age_seconds > (30 * 86400)
    
    # Default
    return age_seconds > self.cold_threshold
```

### Example 6: User-Based (Check File Owner)
```python
def should_demote(self, filepath, age_seconds):
    """Custom logic: VIP users keep files on SSD"""
    import pwd
    
    stat_info = os.stat(filepath)
    owner_uid = stat_info.st_uid
    owner_name = pwd.getpwuid(owner_uid).pw_name
    
    # VIP users - keep on SSD for 90 days
    if owner_name in ['admin', 'ceo', 'important_user']:
        return age_seconds > (90 * 86400)
    
    # Regular users - 30 days
    return age_seconds > self.cold_threshold
```

### Example 7: Time-of-Day Aware
```python
def run_cycle(self):
    """Only run during off-peak hours"""
    current_hour = datetime.now().hour
    
    # Only tier during night (22:00 - 06:00)
    if current_hour < 6 or current_hour >= 22:
        super().run_cycle()
    else:
        self.logger.info("Skipping cycle (business hours)")
```

## How to Modify

1. **Edit configuration file:**
```bash
sudo nano /etc/ceph/cephfs_lc.conf
```

2. **Add custom logic to daemon:**
```bash
sudo nano /usr/local/bin/cephfs_lc
# Edit the process_file() or should_demote() methods
```

3. **Restart service:**
```bash
sudo systemctl restart cephfs-tiering
```

4. **Test first:**
```bash
# Run once without daemon mode
sudo /usr/local/bin/cephfs_lc --once
```

## Your Logic Possibilities (vs Cache Tiering)

| Feature | Cache Tiering | Your Daemon |
|---------|--------------|-------------|
| Age-based | ❌ No | ✅ Yes (atime) |
| Size-based | ❌ No | ✅ Easy to add |
| File type | ❌ No | ✅ Easy to add |
| Directory rules | ❌ No | ✅ Easy to add |
| User-based | ❌ No | ✅ Easy to add |
| Time windows | ❌ No | ✅ Easy to add |
| Custom logic | ❌ C++ only | ✅ Python! |

## Next Steps

1. Choose your policy (start with default: 30 days)
2. Deploy: `bash deploy_tiering.sh`
3. Test: `sudo /usr/local/bin/cephfs_lc --once`
4. Enable: `sudo systemctl enable --now cephfs-tiering`
5. Monitor: `sudo tail -f /var/log/ceph/cephfs_lc.log`
