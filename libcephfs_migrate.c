#define _FILE_OFFSET_BITS 64

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <cephfs/libcephfs.h>

#define BUFFER_SIZE (4 * 1024 * 1024)
#define SHADOW_SUFFIX ".__tiering__"

int main(int argc, char *argv[]) {
    if (argc != 3) {
        fprintf(stderr, "Usage: %s <file_path> <target_pool>\n", argv[0]);
        return 1;
    }

    const char *src_path = argv[1];
    const char *target_pool = argv[2];
    struct ceph_mount_info *cmount;
    struct stat st;
    char shadow_path[1024];
    char *buffer;
    int src_fd, dst_fd, ret;
    off_t offset = 0;
    ssize_t bytes_read;

    ret = ceph_create(&cmount, NULL);
    if (ret) {
        fprintf(stderr, "ERROR: ceph_create failed: %d\n", ret);
        return 1;
    }

    ret = ceph_conf_read_file(cmount, "/etc/ceph/ceph.conf");
    if (ret) {
        fprintf(stderr, "ERROR: ceph_conf_read_file failed: %d\n", ret);
        ceph_release(cmount);
        return 1;
    }

    ret = ceph_mount(cmount, "/");
    if (ret) {
        fprintf(stderr, "ERROR: ceph_mount failed: %d\n", ret);
        ceph_release(cmount);
        return 1;
    }

    ret = ceph_stat(cmount, src_path, &st);
    if (ret) {
        fprintf(stderr, "ERROR: ceph_stat failed for %s: %d (%s)\n", src_path, ret, strerror(-ret));
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }

    snprintf(shadow_path, sizeof(shadow_path), "%s%s", src_path, SHADOW_SUFFIX);

    dst_fd = ceph_open(cmount, shadow_path, O_CREAT | O_EXCL | O_WRONLY, st.st_mode & 0777);
    if (dst_fd < 0) {
        fprintf(stderr, "ERROR: ceph_open failed for %s: %d (%s)\n", shadow_path, dst_fd, strerror(-dst_fd));
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }

    ret = ceph_setxattr(cmount, shadow_path, "ceph.file.layout.pool", target_pool, strlen(target_pool), 0);
    if (ret) {
        fprintf(stderr, "ERROR: ceph_setxattr failed for pool %s: %d (%s)\n", target_pool, ret, strerror(-ret));
        ceph_close(cmount, dst_fd);
        ceph_unlink(cmount, shadow_path);
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }

    src_fd = ceph_open(cmount, src_path, O_RDONLY, 0);
    if (src_fd < 0) {
        fprintf(stderr, "ERROR: ceph_open failed for source %s: %d (%s)\n", src_path, src_fd, strerror(-src_fd));
        ceph_close(cmount, dst_fd);
        ceph_unlink(cmount, shadow_path);
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }

    buffer = malloc(BUFFER_SIZE);
    if (!buffer) {
        ceph_close(cmount, src_fd);
        ceph_close(cmount, dst_fd);
        ceph_unlink(cmount, shadow_path);
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }

    while ((bytes_read = ceph_read(cmount, src_fd, buffer, BUFFER_SIZE, offset)) > 0) {
        if (ceph_write(cmount, dst_fd, buffer, bytes_read, offset) != bytes_read) {
            fprintf(stderr, "ERROR: ceph_write failed at offset %ld\n", offset);
            free(buffer);
            ceph_close(cmount, src_fd);
            ceph_close(cmount, dst_fd);
            ceph_unlink(cmount, shadow_path);
            ceph_unmount(cmount);
            ceph_release(cmount);
            return 1;
        }
        offset += bytes_read;
    }

    free(buffer);
    ceph_close(cmount, src_fd);

    ceph_fchown(cmount, dst_fd, st.st_uid, st.st_gid);
    ceph_fchmod(cmount, dst_fd, st.st_mode & 0777);

    struct timespec times[2];
    times[0].tv_sec = st.st_atime;
    times[0].tv_nsec = 0;
    times[1].tv_sec = st.st_mtime;
    times[1].tv_nsec = 0;
    ceph_futimens(cmount, dst_fd, times);

    char btime_str[32];
    snprintf(btime_str, sizeof(btime_str), "%ld", st.st_ctime);
    ceph_fsetxattr(cmount, dst_fd, "user.original_birthtime", btime_str, strlen(btime_str), 0);

    ceph_fsync(cmount, dst_fd, 0);
    ceph_close(cmount, dst_fd);

    ret = ceph_rename(cmount, shadow_path, src_path);
    if (ret) {
        fprintf(stderr, "ERROR: ceph_rename failed: %d (%s)\n", ret, strerror(-ret));
        ceph_unlink(cmount, shadow_path);
        ceph_unmount(cmount);
        ceph_release(cmount);
        return 1;
    }

    ceph_unmount(cmount);
    ceph_release(cmount);
    return 0;
}
