#!/usr/bin/env python3
"""
CephFS FUSE Overlay - Enterprise Grade
Makes storage tiering completely transparent to users by hiding symlinks
"""

import os
import sys
import errno
import stat
import logging
from fuse import FUSE, FuseOSError, Operations, LoggingMixIn
from threading import Lock

class CephFSOverlay(LoggingMixIn, Operations):
    """
    FUSE overlay that makes tiered storage transparent
    - Hides symlinks (shows as regular files)
    - Hides .tiers directory
    - All file operations work transparently
    """
    
    def __init__(self, source_mount, hidden_dirs=None):
        self.source = source_mount.rstrip('/')
        self.hidden_dirs = hidden_dirs or ['.tiers', '.snapshot']
        self.fd_map = {}
        self.fd_counter = 0
        self.fd_lock = Lock()
        
        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            handlers=[
                logging.FileHandler('/var/log/ceph/cephfs_fuse.log'),
                logging.StreamHandler()
            ]
        )
        self.log = logging.getLogger('cephfs_fuse')
        self.log.info(f"Initialized overlay: {source_mount}")
    
    def _full_path(self, partial):
        """Convert overlay path to real CephFS path"""
        if partial.startswith("/"):
            partial = partial[1:]
        path = os.path.join(self.source, partial)
        return path
    
    def _resolve_symlink(self, path):
        """
        Resolve symlink to actual file if it's a tiered file
        Returns: (is_symlink, resolved_path)
        """
        if os.path.islink(path):
            target = os.readlink(path)
            # If absolute path, use it; otherwise resolve relative
            if target.startswith('/'):
                return True, target
            else:
                return True, os.path.join(os.path.dirname(path), target)
        return False, path
    
    def _is_hidden(self, name):
        """Check if directory should be hidden"""
        return name in self.hidden_dirs
    
    # Filesystem methods
    
    def access(self, path, mode):
        full_path = self._full_path(path)
        is_link, resolved = self._resolve_symlink(full_path)
        
        if not os.access(resolved, mode):
            raise FuseOSError(errno.EACCES)
    
    def chmod(self, path, mode):
        full_path = self._full_path(path)
        is_link, resolved = self._resolve_symlink(full_path)
        return os.chmod(resolved, mode)
    
    def chown(self, path, uid, gid):
        full_path = self._full_path(path)
        is_link, resolved = self._resolve_symlink(full_path)
        return os.chown(resolved, uid, gid)
    
    def getattr(self, path, fh=None):
        """
        Get file attributes
        KEY: Make symlinks appear as regular files!
        """
        full_path = self._full_path(path)
        
        try:
            # Check if it's a symlink to cold storage
            is_link, resolved = self._resolve_symlink(full_path)
            
            if is_link:
                # Get stats from the actual file
                st = os.lstat(resolved)
                
                # CRITICAL: Change mode to make it look like regular file
                # Remove symlink bit, make it look like regular file
                mode = st.st_mode
                if stat.S_ISLNK(mode):
                    # Convert symlink mode to regular file mode
                    mode = stat.S_IFREG | (mode & 0o777)
                
                return {
                    'st_atime': st.st_atime,
                    'st_ctime': st.st_ctime,
                    'st_gid': st.st_gid,
                    'st_mode': mode,  # Modified to hide symlink
                    'st_mtime': st.st_mtime,
                    'st_nlink': st.st_nlink,
                    'st_size': st.st_size,
                    'st_uid': st.st_uid,
                }
            else:
                # Regular file, just pass through
                st = os.lstat(full_path)
                return {
                    'st_atime': st.st_atime,
                    'st_ctime': st.st_ctime,
                    'st_gid': st.st_gid,
                    'st_mode': st.st_mode,
                    'st_mtime': st.st_mtime,
                    'st_nlink': st.st_nlink,
                    'st_size': st.st_size,
                    'st_uid': st.st_uid,
                }
        except OSError as e:
            raise FuseOSError(e.errno)
    
    def readdir(self, path, fh):
        """
        Read directory contents
        KEY: Hide .tiers and other hidden directories
        """
        full_path = self._full_path(path)
        
        dirents = ['.', '..']
        if os.path.isdir(full_path):
            dirents.extend(os.listdir(full_path))
        
        # Filter out hidden directories
        dirents = [d for d in dirents if not self._is_hidden(d)]
        
        for r in dirents:
            yield r
    
    def readlink(self, path):
        """Read symlink target (though we hide symlinks, still support it)"""
        full_path = self._full_path(path)
        pathname = os.readlink(full_path)
        
        if pathname.startswith("/"):
            return pathname
        else:
            return os.path.join(os.path.dirname(full_path), pathname)
    
    def mknod(self, path, mode, dev):
        return os.mknod(self._full_path(path), mode, dev)
    
    def rmdir(self, path):
        full_path = self._full_path(path)
        return os.rmdir(full_path)
    
    def mkdir(self, path, mode):
        return os.mkdir(self._full_path(path), mode)
    
    def statfs(self, path):
        full_path = self._full_path(path)
        stv = os.statvfs(full_path)
        return dict((key, getattr(stv, key)) for key in (
            'f_bavail', 'f_bfree', 'f_blocks', 'f_bsize', 'f_favail',
            'f_ffree', 'f_files', 'f_flag', 'f_frsize', 'f_namemax'))
    
    def unlink(self, path):
        """
        Delete file
        If it's a symlink (tiered file), delete the symlink
        """
        return os.unlink(self._full_path(path))
    
    def symlink(self, name, target):
        return os.symlink(target, self._full_path(name))
    
    def rename(self, old, new):
        """
        Rename file
        Handle symlinks transparently
        """
        old_path = self._full_path(old)
        new_path = self._full_path(new)
        
        return os.rename(old_path, new_path)
    
    def link(self, target, name):
        return os.link(self._full_path(name), self._full_path(target))
    
    def utimens(self, path, times=None):
        full_path = self._full_path(path)
        is_link, resolved = self._resolve_symlink(full_path)
        return os.utime(resolved, times)
    
    # File methods
    
    def open(self, path, flags):
        """
        Open file
        KEY: Resolve symlink and open actual file
        """
        full_path = self._full_path(path)
        is_link, resolved = self._resolve_symlink(full_path)
        
        # Open the actual file (follows symlink)
        fd = os.open(resolved, flags)
        
        # Map FUSE fd to real fd
        with self.fd_lock:
            self.fd_counter += 1
            fuse_fd = self.fd_counter
            self.fd_map[fuse_fd] = fd
        
        return fuse_fd
    
    def create(self, path, mode, fi=None):
        """Create new file"""
        full_path = self._full_path(path)
        fd = os.open(full_path, os.O_WRONLY | os.O_CREAT, mode)
        
        with self.fd_lock:
            self.fd_counter += 1
            fuse_fd = self.fd_counter
            self.fd_map[fuse_fd] = fd
        
        return fuse_fd
    
    def read(self, path, length, offset, fh):
        """
        Read from file
        KEY: Read from actual location (symlink already resolved in open)
        """
        with self.fd_lock:
            real_fd = self.fd_map.get(fh)
        
        if real_fd is None:
            raise FuseOSError(errno.EBADF)
        
        os.lseek(real_fd, offset, os.SEEK_SET)
        return os.read(real_fd, length)
    
    def write(self, path, buf, offset, fh):
        """Write to file"""
        with self.fd_lock:
            real_fd = self.fd_map.get(fh)
        
        if real_fd is None:
            raise FuseOSError(errno.EBADF)
        
        os.lseek(real_fd, offset, os.SEEK_SET)
        return os.write(real_fd, buf)
    
    def truncate(self, path, length, fh=None):
        """Truncate file"""
        full_path = self._full_path(path)
        is_link, resolved = self._resolve_symlink(full_path)
        
        with open(resolved, 'r+') as f:
            f.truncate(length)
    
    def flush(self, path, fh):
        """Flush file buffers"""
        with self.fd_lock:
            real_fd = self.fd_map.get(fh)
        
        if real_fd is not None:
            return os.fsync(real_fd)
        return 0
    
    def release(self, path, fh):
        """Close file"""
        with self.fd_lock:
            real_fd = self.fd_map.pop(fh, None)
        
        if real_fd is not None:
            return os.close(real_fd)
        return 0
    
    def fsync(self, path, fdatasync, fh):
        """Sync file to disk"""
        with self.fd_lock:
            real_fd = self.fd_map.get(fh)
        
        if real_fd is not None:
            if fdatasync:
                return os.fdatasync(real_fd)
            else:
                return os.fsync(real_fd)
        return 0


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='CephFS FUSE Overlay - Makes tiering transparent to users'
    )
    parser.add_argument('source', help='Source CephFS mount point (e.g., /cephfs)')
    parser.add_argument('mountpoint', help='Overlay mount point (e.g., /cephfs-overlay)')
    parser.add_argument('-f', '--foreground', action='store_true',
                       help='Run in foreground (default: background)')
    parser.add_argument('-d', '--debug', action='store_true',
                       help='Enable debug output')
    parser.add_argument('--allow-other', action='store_true',
                       help='Allow other users to access the filesystem')
    
    args = parser.parse_args()
    
    # Check if source exists
    if not os.path.ismount(args.source):
        print(f"Error: {args.source} is not a mounted filesystem")
        sys.exit(1)
    
    # Create mountpoint if needed
    os.makedirs(args.mountpoint, exist_ok=True)
    
    # FUSE options
    fuse_opts = {
        'foreground': args.foreground or args.debug,
        'allow_other': args.allow_other,
        'default_permissions': True,
    }
    
    if args.debug:
        fuse_opts['debug'] = True
    
    print(f"Mounting CephFS overlay:")
    print(f"  Source: {args.source}")
    print(f"  Overlay: {args.mountpoint}")
    print(f"  Options: {fuse_opts}")
    print()
    print("Users should access files through:", args.mountpoint)
    print("Press Ctrl+C to unmount")
    print()
    
    # Start FUSE
    FUSE(
        CephFSOverlay(args.source),
        args.mountpoint,
        nothreads=False,
        **fuse_opts
    )


if __name__ == '__main__':
    main()
