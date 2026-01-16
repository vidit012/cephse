# CephFS Tiering System - Quick Reference

## 🚀 Most Useful Commands

### Check System Status (One-Liner)
```bash
sudo systemctl status cephfs-{tracker,policy-engine,migration-worker}.service --no-pager
```

### View Current File Distribution
```sql
sudo -u postgres psql tiering -c "
SELECT SUBSTRING(current_pool, 20) as pool, 
       COUNT(*) as files,
       ROUND(AVG(score), 2) as avg_score
FROM file_metadata 
GROUP BY current_pool 
ORDER BY avg_score DESC;"
```

### Watch Live Migrations
```bash
watch -n 5 "sudo -u postgres psql tiering -c \"SELECT path, SUBSTRING(current_pool, 20) as pool, score FROM file_metadata WHERE needs_migration = TRUE;\""
```

### Test End-to-End Flow
```bash
# 1. Create and access file
echo "test" > /tiercephfs/test.txt && cat /tiercephfs/test.txt

# 2. Force aggregation
sudo -u postgres psql tiering -c "SELECT aggregate_access_log();"

# 3. Check tracking
sudo -u postgres psql tiering -c "SELECT * FROM file_metadata WHERE path = 'test.txt';"
```

---

## 📊 Key Formulas

```
Score = 0.90 × access_freq
access_freq = GREATEST(1, COUNT(*) / 2)  # Fix 2x read inflation

Thresholds:
  score ≥ 9:   HOT (DATA pool)
  4.5 ≤ score < 9: WARM
  score < 4.5: COLD
```

---

## ⏱️ Timing Rules

| Pool | Accessed | Not Accessed |
|------|----------|--------------|
| DATA | 3-min wait | 3-min wait |
| WARM | Immediate | 3-min wait |
| COLD | Immediate | No demotion |

---

## 🔧 Troubleshooting

### Service Won't Start
```bash
# Check logs
sudo journalctl -xe -u cephfs-tracker.service

# Common issues:
# - PostgreSQL not running: sudo systemctl start postgresql
# - eBPF not supported: uname -r (need 4.4+)
# - Port conflict: netstat -tulpn | grep 5432
```

### Files Not Being Tracked
```bash
# 1. Check eBPF is attached
sudo bpftool prog list | grep ceph

# 2. Check hot table has events
sudo -u postgres psql tiering -c "SELECT COUNT(*) FROM file_access_log;"

# 3. Force aggregation
sudo -u postgres psql tiering -c "SELECT aggregate_access_log();"
```

### Migration Stuck
```bash
# Check migration worker
sudo systemctl status cephfs-migration-worker.service

# Check for errors
sudo journalctl -u cephfs-migration-worker.service --since "10 minutes ago"

# Reset stuck migrations
sudo -u postgres psql tiering -c "UPDATE file_metadata SET needs_migration = FALSE WHERE needs_migration = TRUE;"
```

---

## 📈 Performance Tuning

```sql
-- Add indexes if slow
CREATE INDEX IF NOT EXISTS idx_score ON file_metadata(score DESC);
CREATE INDEX IF NOT EXISTS idx_pool_score ON file_metadata(current_pool, score);

-- Vacuum database regularly
VACUUM ANALYZE file_metadata;
VACUUM ANALYZE file_access_log;

-- Clean old hot table entries (older than 1 hour)
DELETE FROM file_access_log WHERE access_time < NOW() - INTERVAL '1 hour';
```

---

## 🎯 Demo Commands for Engineers

```bash
# 1. Show architecture
systemctl list-units --type=service | grep cephfs

# 2. Show storage pools
ceph osd pool ls

# 3. Create test file in DATA (hot)
echo "frequently accessed" > /tiercephfs/hot.txt
for i in {1..15}; do cat /tiercephfs/hot.txt > /dev/null; done

# 4. Create test file that will cool down
echo "rarely accessed" > /tiercephfs/cold.txt
cat /tiercephfs/cold.txt > /dev/null

# 5. Wait and show migration
sleep 200  # Wait for 3-min evaluation + migration

# 6. Show final pool distribution
sudo -u postgres psql tiering -c "
SELECT path, 
       SUBSTRING(current_pool, 20) as pool, 
       score 
FROM file_metadata 
WHERE path IN ('hot.txt', 'cold.txt');"

# 7. Verify physical pools
getfattr -n ceph.file.layout.pool /tiercephfs/hot.txt
getfattr -n ceph.file.layout.pool /tiercephfs/cold.txt
```

---

## 💡 Key Talking Points for Engineers

1. **Why eBPF?**
   - Zero overhead (kernel space)
   - No CephFS code changes
   - Cannot be bypassed

2. **Why 2-Table Design?**
   - Hot table: Append-only, fast writes
   - Cold table: Indexed, fast queries
   - Batch aggregation reduces overhead

3. **Why Pool-Specific Evaluation?**
   - DATA: Stable (avoid thrashing)
   - WARM: Responsive (quick promotion)
   - COLD: Urgent (immediate recovery)

4. **Why Server-Side Migration?**
   - No network transfer to client
   - Uses Ceph's internal copy
   - 10x faster than client-side copy

5. **Why Score-Based (not time-based)?**
   - Adapts to workload intensity
   - Self-tuning thresholds
   - Better resource utilization

---

## 🐛 Common Engineer Questions

**Q: What if eBPF tracker crashes?**
- A: Systemd restarts it automatically. Events in PostgreSQL are safe.

**Q: What about concurrent migrations?**
- A: We use parallel workers (5 concurrent). PostgreSQL row locks prevent conflicts.

**Q: How do you handle inode changes?**
- A: `reset_file_after_migration()` updates metadata with new inode.

**Q: Can users bypass the tracker?**
- A: No. Kernel-level tracking captures all access patterns.

**Q: What's the overhead on CephFS?**
- A: <1% CPU, negligible I/O (batch writes to PostgreSQL).

---

## 📞 Emergency Commands

```bash
# Stop all tiering (emergency)
sudo systemctl stop cephfs-tracker.service
sudo systemctl stop cephfs-policy-engine.service
sudo systemctl stop cephfs-migration-worker.service

# Reset all migrations
sudo -u postgres psql tiering -c "
UPDATE file_metadata 
SET needs_migration = FALSE, 
    target_pool = NULL;"

# Clear all tracking data
sudo -u postgres psql tiering -c "
TRUNCATE file_access_log;
TRUNCATE file_metadata;"

# Restart everything
sudo systemctl restart cephfs-tracker.service
sudo systemctl restart cephfs-policy-engine.service
sudo systemctl restart cephfs-migration-worker.service
```
