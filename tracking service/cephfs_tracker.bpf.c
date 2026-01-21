// SPDX-License-Identifier: GPL-2.0
// CephFS File Access Tracker
// Hooks into CephFS read operations to track file access

#include <vmlinux.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

#define PATH_MAX 256

struct access_event {
    u64 inode;
    u64 timestamp_ns;
    u32 pid;
    u32 uid;
    char path[PATH_MAX];
};

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 8 * 1024 * 1024);  // 8MB ring buffer
} events SEC(".maps");

// Deduplication map: prevent duplicate events within 1 second
struct {
    __uint(type, BPF_MAP_TYPE_LRU_HASH);
    __uint(max_entries, 100000);
    __type(key, u64);    // inode
    __type(value, u64);  // last_timestamp_ns
} dedup SEC(".maps");

static __always_inline void track_access(struct file *file)
{
    struct access_event *e;
    struct inode *inode;
    struct dentry *dentry;
    struct qstr d_name;
    u64 now, *last_ts, ino;
    u32 uid;
    
    if (!file)
        return;
    
    // Get inode
    inode = BPF_CORE_READ(file, f_inode);
    if (!inode)
        return;
    
    ino = BPF_CORE_READ(inode, i_ino);
    now = bpf_ktime_get_ns();
    
    // Deduplication: skip if accessed within last 1 second
    last_ts = bpf_map_lookup_elem(&dedup, &ino);
    if (last_ts && (now - *last_ts) < 1000000000ULL)
        return;
    
    // Update dedup map
    bpf_map_update_elem(&dedup, &ino, &now, BPF_ANY);
    
    // Reserve ringbuf space
    e = bpf_ringbuf_reserve(&events, sizeof(*e), 0);
    if (!e)
        return;
    
    e->inode = ino;
    e->timestamp_ns = now;
    e->pid = bpf_get_current_pid_tgid() >> 32;
    e->uid = bpf_get_current_uid_gid() & 0xFFFFFFFF;
    
    // Get filename from dentry
    dentry = BPF_CORE_READ(file, f_path.dentry);
    if (dentry) {
        d_name = BPF_CORE_READ(dentry, d_name);
        bpf_probe_read_kernel_str(e->path, sizeof(e->path), d_name.name);
    } else {
        e->path[0] = '\0';
    }
    
    bpf_ringbuf_submit(e, 0);
}

// Hook: CephFS read operations
SEC("fentry/ceph_read_iter")
int BPF_PROG(trace_read, struct kiocb *iocb)
{
    struct file *file = BPF_CORE_READ(iocb, ki_filp);
    track_access(file);
    return 0;
}

// Hook: CephFS write operations (also indicates file is "hot")
SEC("fentry/ceph_write_iter")
int BPF_PROG(trace_write, struct kiocb *iocb)
{
    struct file *file = BPF_CORE_READ(iocb, ki_filp);
    track_access(file);
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
