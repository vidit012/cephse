#!/usr/bin/env python3
"""
Phase 1: eBPF File Access Tracker → PostgreSQL
Tracks CephFS file access and writes directly to PostgreSQL database
"""

from bcc import BPF
import psycopg2
import time
import signal
import sys
from datetime import datetime

# PostgreSQL connection
DB_CONFIG = {
    'host': 'localhost',
    'port': 5432,
    'database': 'tiering',
    'user': 'tiering_user',
    'password': '1'
}

# eBPF program to track file access
BPF_PROGRAM = """
#include <uapi/linux/ptrace.h>
#include <linux/fs.h>
#include <linux/dcache.h>

struct access_event {
    u64 inode;
    u64 timestamp;
    char path[256];
};

BPF_PERF_OUTPUT(events);
BPF_HASH(last_access, u64, u64);  // Deduplication: inode -> last_timestamp

// Track reads
int trace_ceph_read(struct pt_regs *ctx, struct kiocb *iocb) {
    struct file *file = iocb->ki_filp;
    if (!file) return 0;
    
    struct inode *inode = file->f_inode;
    if (!inode) return 0;
    
    u64 ino = inode->i_ino;
    u64 now = bpf_ktime_get_ns();
    
    // Deduplicate: skip if accessed within last second
    u64 *last = last_access.lookup(&ino);
    if (last && (now - *last) < 1000000000) {
        return 0;
    }
    last_access.update(&ino, &now);
    
    // Prepare event
    struct access_event event = {};
    event.inode = ino;
    event.timestamp = now;
    
    // Get file path
    struct dentry *dentry = file->f_path.dentry;
    if (dentry) {
        bpf_probe_read_kernel_str(&event.path, sizeof(event.path), dentry->d_name.name);
    }
    
    events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}

// Track writes
int trace_ceph_write(struct pt_regs *ctx, struct kiocb *iocb) {
    return trace_ceph_read(ctx, iocb);
}
"""

class AccessTracker:
    def __init__(self):
        self.conn = None
        self.running = True
        self.event_count = 0
        self.update_count = 0
        
        # Connect to database
        self.connect_db()
        
        # Load eBPF program
        print("Loading eBPF program...")
        self.bpf = BPF(text=BPF_PROGRAM)
        
        # Attach to CephFS kernel functions
        try:
            self.bpf.attach_kprobe(event="ceph_read_iter", fn_name="trace_ceph_read")
            self.bpf.attach_kprobe(event="ceph_write_iter", fn_name="trace_ceph_write")
            print("✓ Attached to ceph_read_iter and ceph_write_iter")
        except Exception as e:
            print(f"Warning: Could not attach to ceph functions: {e}")
            print("Trying alternative attach points...")
            try:
                self.bpf.attach_kprobe(event="ceph_aio_read", fn_name="trace_ceph_read")
                self.bpf.attach_kprobe(event="ceph_aio_write", fn_name="trace_ceph_write")
                print("✓ Attached to ceph_aio_read and ceph_aio_write")
            except Exception as e2:
                print(f"Error: Could not attach to CephFS functions: {e2}")
                sys.exit(1)
        
        # Open perf buffer
        self.bpf["events"].open_perf_buffer(self.handle_event)
        
        # Setup signal handler
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
    
    def connect_db(self):
        """Connect to PostgreSQL"""
        try:
            self.conn = psycopg2.connect(**DB_CONFIG)
            self.conn.autocommit = True
            print(f"✓ Connected to PostgreSQL: {DB_CONFIG['database']}")
        except Exception as e:
            print(f"Error connecting to database: {e}")
            sys.exit(1)
    
    def handle_event(self, cpu, data, size):
        """Handle eBPF events"""
        event = self.bpf["events"].event(data)
        self.event_count += 1
        
        inode = event.inode
        path = event.path.decode('utf-8', errors='ignore')
        timestamp = datetime.fromtimestamp(event.timestamp / 1e9)
        
        # Update database
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO file_metadata (inode, path, last_access, current_pool)
                    VALUES (%s, %s, %s, 'cephfs.tiercephfs.data')
                    ON CONFLICT (inode) DO UPDATE 
                    SET last_access = EXCLUDED.last_access,
                        path = EXCLUDED.path
                """, (inode, path, timestamp))
                self.update_count += 1
                
                if self.update_count % 10 == 0:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Events: {self.event_count}, Updates: {self.update_count} | Latest: inode={inode} path={path}")
        
        except Exception as e:
            print(f"Database error: {e}")
            self.connect_db()
    
    def signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        print(f"\n\nShutting down... (received signal {signum})")
        self.running = False
    
    def run(self):
        """Main loop"""
        print("\n=== eBPF Access Tracker Started ===")
        print(f"Tracking CephFS file access...")
        print(f"Database: {DB_CONFIG['database']}@{DB_CONFIG['host']}")
        print(f"Press Ctrl+C to stop\n")
        
        try:
            while self.running:
                self.bpf.perf_buffer_poll(timeout=1000)
        except KeyboardInterrupt:
            pass
        finally:
            print(f"\n\nFinal Statistics:")
            print(f"  Total eBPF events: {self.event_count}")
            print(f"  Database updates: {self.update_count}")
            if self.conn:
                self.conn.close()
            print("\n✓ Shutdown complete")

if __name__ == '__main__':
    tracker = AccessTracker()
    tracker.run()
