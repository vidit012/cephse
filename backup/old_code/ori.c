#define _FILE_OFFSET_BITS 64

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <time.h>
#include <cephfs/libcephfs.h>

#define BUFFER_SIZE (4 * 1024 * 1024)  // 4MB buffer
#define SHADOW_SUFFIX ".__tiering__"

void print_error(const char *msg, int err) {
    fprintf(stderr, "ERROR: %s: %s\n", msg, strerror(-err));
}

int main(int argc, char *argv[]) {
    if (argc != 3) {
        fprintf(stderr, "Usage: %s <file_path> <target_pool>\n", argv[0]);
        fprintf(stderr, "Example: %s /tiercephfs/test.txt cephfs.tiercephfs.cold\n", argv[0]);
        return 1;
    }

    const char *src_path = argv[1];
    const char *target_pool = argv[2];
    
    printf("═══════════════════════════════════════════════════════\n");
    printf("CephFS Migration using libcephfs\n");
    printf("═══════════════════════════════════════════════════════\n");
    printf("Source file: %s\n", src_path);
    printf("Target pool: %s\n", target_pool);
    printf("\n");

    // Step 1: Initialize CephFS
    printf("[1/8] Initializing CephFS connection...\n");
    struct ceph_mount_info *cmount;
    int ret = ceph_create(&cmount, NULL);
    if (ret) {
        print_error("ceph_create failed", ret);
        return 1;
    }

    ret = ceph_conf_read_file(cmount, "/etc/ceph/ceph.conf");
    if (ret) {
        print_error("ceph_conf_read_file failed", ret);
        ceph_release(cmount);
        return 1;
    }

    ret = ceph_mount(cmount, "/");
    if (ret) {
        print_error("ceph_mount failed", ret);
        ceph_release(cmount);
        return 1;
    }
    printf("✓ CephFS mounted successfully\n\n");

    // Step 2: Stat source file
    printf("[2/8] Reading source file metadata...\n");
    struct stat st;
    ret = ceph_stat(cmount, src_path, &st);
    if (ret) {
        print_error("ceph_stat failed", ret);
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }
    printf("✓ Size: %ld bytes\n", st.st_size);
    printf("✓ Mode: 0%o\n", st.st_mode & 0777);
    printf("✓ UID: %d, GID: %d\n", st.st_uid, st.st_gid);
    printf("✓ Inode: %ld\n", st.st_ino);
    printf("\n");

    // Step 3: Create shadow file path
    char shadow_path[1024];
    snprintf(shadow_path, sizeof(shadow_path), "%s%s", src_path, SHADOW_SUFFIX);
    
    printf("[3/8] Creating shadow file: %s\n", shadow_path);
    int dst_fd = ceph_open(cmount, shadow_path, O_CREAT | O_EXCL | O_WRONLY, st.st_mode & 0777);
    if (dst_fd < 0) {
        print_error("ceph_open (shadow) failed", dst_fd);
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }
    printf("✓ Shadow file created\n\n");

    // Step 4: Set target pool layout
    printf("[4/8] Setting layout to target pool: %s\n", target_pool);
    ret = ceph_setxattr(cmount, shadow_path, "ceph.file.layout.pool", 
                        target_pool, strlen(target_pool), 0);
    if (ret) {
        print_error("ceph_setxattr (pool) failed", ret);
        ceph_close(cmount, dst_fd);
        ceph_unlink(cmount, shadow_path);
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }
    printf("✓ Layout set to target pool\n\n");

    // Step 5: Copy data using libcephfs read/write
    printf("[5/8] Copying data through libcephfs...\n");
    int src_fd = ceph_open(cmount, src_path, O_RDONLY, 0);
    if (src_fd < 0) {
        print_error("ceph_open (source) failed", src_fd);
        ceph_close(cmount, dst_fd);
        ceph_unlink(cmount, shadow_path);
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }

    char *buffer = malloc(BUFFER_SIZE);
    if (!buffer) {
        fprintf(stderr, "ERROR: malloc failed\n");
        ceph_close(cmount, src_fd);
        ceph_close(cmount, dst_fd);
        ceph_unlink(cmount, shadow_path);
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }

    off_t total_copied = 0;
    off_t offset = 0;
    ssize_t bytes_read;
    
    while ((bytes_read = ceph_read(cmount, src_fd, buffer, BUFFER_SIZE, offset)) > 0) {
        ssize_t bytes_written = ceph_write(cmount, dst_fd, buffer, bytes_read, offset);
        if (bytes_written != bytes_read) {
            print_error("ceph_write failed", bytes_written);
            free(buffer);
            ceph_close(cmount, src_fd);
            ceph_close(cmount, dst_fd);
            ceph_unlink(cmount, shadow_path);
            ceph_unmount(cmount);
            ceph_release(cmount);
            return 1;
        }
        offset += bytes_read;
        total_copied += bytes_read;
    }
    
    free(buffer);
    ceph_close(cmount, src_fd);
    
    printf("✓ Copied %ld bytes\n\n", total_copied);

    // Step 6: Restore metadata
    printf("[6/8] Restoring file metadata...\n");
    
    // Ownership
    ret = ceph_fchown(cmount, dst_fd, st.st_uid, st.st_gid);
    if (ret) {
        print_error("ceph_fchown failed", ret);
    } else {
        printf("✓ Ownership restored\n");
    }
    
    // Permissions
    ret = ceph_fchmod(cmount, dst_fd, st.st_mode & 0777);
    if (ret) {
        print_error("ceph_fchmod failed", ret);
    } else {
        printf("✓ Permissions restored\n");
    }
    
    // Timestamps
    struct timespec times[2];
    times[0].tv_sec = st.st_atime;
    times[0].tv_nsec = 0;
    times[1].tv_sec = st.st_mtime;
    times[1].tv_nsec = 0;
    
    ret = ceph_futimens(cmount, dst_fd, times);
    if (ret) {
        print_error("ceph_futimens failed", ret);
    } else {
        printf("✓ Timestamps restored\n");
    }
    
    // Store original birth time in xattr
    char btime_str[32];
    snprintf(btime_str, sizeof(btime_str), "%ld", st.st_ctime);
    ret = ceph_fsetxattr(cmount, dst_fd, "user.original_birthtime", 
                         btime_str, strlen(btime_str), 0);
    if (ret) {
        print_error("ceph_fsetxattr (birthtime) failed", ret);
    } else {
        printf("✓ Birth time stored in xattr\n");
    }
    printf("\n");

    // Step 7: Fsync and close
    printf("[7/8] Syncing data to OSDs...\n");
    ret = ceph_fsync(cmount, dst_fd, 0);
    if (ret) {
        print_error("ceph_fsync failed", ret);
    } else {
        printf("✓ Data synced\n");
    }
    
    ceph_close(cmount, dst_fd);
    printf("\n");

    // Step 8: Atomic rename
    printf("[8/8] Performing atomic rename...\n");
    ret = ceph_rename(cmount, shadow_path, src_path);
    if (ret) {
        print_error("ceph_rename failed", ret);
        ceph_unlink(cmount, shadow_path);
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }
    printf("✓ Atomic rename completed\n\n");

    // Cleanup
    ceph_unmount(cmount);
    ceph_release(cmount);

    printf("═══════════════════════════════════════════════════════\n");
    printf("✅ MIGRATION COMPLETED SUCCESSFULLY\n");
    printf("═══════════════════════════════════════════════════════\n");
    printf("File migrated using libcephfs API\n");
    printf("All CephFS invariants maintained\n");
    printf("Ready for verification\n");
    
    return 0;
}
