/*
 * Simplified libcephfs Migration
 * Note: This creates its own CephFS connection (separate from /tiercephfs kernel mount)
 */

#define _FILE_OFFSET_BITS 64

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <sys/stat.h>
#include <cephfs/libcephfs.h>

#define BUFFER_SIZE (4 * 1024 * 1024)  // 4MB

int main(int argc, char *argv[]) {
    if (argc != 3) {
        fprintf(stderr, "Usage: %s <file_path> <target_pool>\n", argv[0]);
        fprintf(stderr, "Example: %s /test.txt cephfs.tiercephfs.cold\n", argv[0]);
        return 1;
    }

    const char *src_path = argv[1];
    const char *target_pool = argv[2];
    struct ceph_mount_info *cmount;
    int ret;

    // Initialize and mount CephFS (separate from kernel mount!)
    printf("Connecting to CephFS via libcephfs...\n");
    if ((ret = ceph_create(&cmount, NULL)) ||
        (ret = ceph_conf_read_file(cmount, "/etc/ceph/ceph.conf")) ||
        (ret = ceph_mount(cmount, "/"))) {
        fprintf(stderr, "Failed to connect: %s\n", strerror(-ret));
        return 1;
    }

    // Get source file info
    struct stat st;
    if (ceph_stat(cmount, src_path, &st) < 0) {
        fprintf(stderr, "File not found: %s\n", src_path);
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }
    printf("Source: %s (%ld bytes, mode 0%o)\n", src_path, st.st_size, st.st_mode & 0777);

    // Create shadow file
    char shadow_path[1024];
    snprintf(shadow_path, sizeof(shadow_path), "%s.__tiering__", src_path);
    
    int dst_fd = ceph_open(cmount, shadow_path, O_CREAT | O_EXCL | O_WRONLY, st.st_mode & 0777);
    if (dst_fd < 0) {
        fprintf(stderr, "Failed to create shadow file\n");
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }

    // Set target pool layout
    printf("Setting layout to pool: %s\n", target_pool);
    if (ceph_setxattr(cmount, shadow_path, "ceph.file.layout.pool", 
                      target_pool, strlen(target_pool), 0) < 0) {
        fprintf(stderr, "Failed to set pool layout\n");
        ceph_close(cmount, dst_fd);
        ceph_unlink(cmount, shadow_path);
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }

    // Copy data
    printf("Copying data...\n");
    int src_fd = ceph_open(cmount, src_path, O_RDONLY, 0);
    char *buffer = malloc(BUFFER_SIZE);
    off_t offset = 0;
    ssize_t bytes_read;
    
    while ((bytes_read = ceph_read(cmount, src_fd, buffer, BUFFER_SIZE, offset)) > 0) {
        ceph_write(cmount, dst_fd, buffer, bytes_read, offset);
        offset += bytes_read;
    }
    
    free(buffer);
    ceph_close(cmount, src_fd);
    printf("Copied %ld bytes\n", offset);

    // Restore metadata
    ceph_fchown(cmount, dst_fd, st.st_uid, st.st_gid);
    ceph_fchmod(cmount, dst_fd, st.st_mode & 0777);
    
    struct timespec times[2];
    times[0].tv_sec = st.st_atime;
    times[0].tv_nsec = 0;
    times[1].tv_sec = st.st_mtime;
    times[1].tv_nsec = 0;
    ceph_futimens(cmount, dst_fd, times);
    
    // Sync and close
    ceph_fsync(cmount, dst_fd, 0);
    ceph_close(cmount, dst_fd);

    // Atomic rename
    printf("Performing atomic rename...\n");
    if (ceph_rename(cmount, shadow_path, src_path) < 0) {
        fprintf(stderr, "Rename failed\n");
        ceph_unlink(cmount, shadow_path);
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }

    // Cleanup (disconnect from CephFS)
    ceph_unmount(cmount);
    ceph_release(cmount);

    printf("✅ Migration complete: %s → %s\n", src_path, target_pool);
    return 0;
}
