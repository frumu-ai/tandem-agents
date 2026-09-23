"""Read-only inventory and chunk-authenticated archive construction for v3 export."""

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile


CHUNK_SIZE = 1024 * 1024
MAGIC = b"TANDEM-BACKUP-AEAD-1\n"


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def plain_path(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("backup paths must be absolute and contain no symlinks")
    return path


def _stable(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns if os.name == "posix" else 0)


def file_info(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"backup input is not a single-link regular file: {path}")
    return info


def open_no_symlink(path, *, directory=False):
    """Open every POSIX path component without following a replacement link."""
    path = plain_path(path)
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    if os.name != "posix":
        return os.open(path, flags)
    if path == Path(path.anchor):
        raise ValueError("filesystem root cannot be a backup input")
    if any(part in (".", "..") for part in path.parts):
        raise ValueError("backup path contains traversal")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        parts = path.parts[1:]
        for component in parts[:-1]:
            next_descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY
                                      | os.O_NOFOLLOW | os.O_CLOEXEC,
                                      dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        result = os.open(parts[-1], flags, dir_fd=descriptor)
        return result
    finally:
        os.close(descriptor)


def read_file(path):
    """Read a small control file without following its final component."""
    file_info(path)
    descriptor = open_no_symlink(path)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("backup input changed while opening")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read()
        if _stable(before) != _stable(os.fstat(descriptor)):
            raise ValueError("backup input changed while reading")
        return data
    finally:
        os.close(descriptor)


def _hash_file(path):
    expected = file_info(path)
    descriptor = open_no_symlink(path)
    try:
        before = os.fstat(descriptor)
        if _stable(expected) != _stable(before):
            raise ValueError(f"backup input changed while opening: {path}")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for block in iter(lambda: handle.read(CHUNK_SIZE), b""):
                digest.update(block)
        if _stable(before) != _stable(os.fstat(descriptor)):
            raise ValueError("backup input changed while reading")
        return digest.hexdigest(), before
    finally:
        os.close(descriptor)


def _entry(group, relative, path, info=None):
    info = info or path.lstat()
    name = str(PurePosixPath(group, *relative.parts))
    if any(part in ("", ".", "..") for part in PurePosixPath(name).parts):
        raise ValueError("backup input contains an unsafe path")
    common = {"archive_path": name, "source_path": str(path),
              "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid,
              "device": info.st_dev, "inode": info.st_ino}
    if stat.S_ISDIR(info.st_mode):
        return {**common, "type": "directory"}
    digest, checked = _hash_file(path)
    return {**common, "type": "file", "size": checked.st_size, "sha256": digest}


def inventory_root(group, root):
    root = plain_path(root)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"required backup root is not a directory: {root}")
    entries = [_entry(group, Path(), root)]

    def visit(directory, relative, expected):
        descriptor = open_no_symlink(directory, directory=True) if os.name == "posix" else None
        try:
            opened = os.fstat(descriptor) if descriptor is not None else directory.lstat()
            if (opened.st_dev, opened.st_ino) != (expected["device"], expected["inode"]):
                raise ValueError("backup directory changed during inventory")
            with os.scandir(descriptor if descriptor is not None else directory) as children:
                names = sorted(item.name for item in children)
            for name in names:
                child = directory / name
                child_relative = relative / name
                info = (os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        if descriptor is not None else child.lstat())
                entry = _entry(group, child_relative, child, info)
                entries.append(entry)
                if entry["type"] == "directory":
                    visit(child, child_relative, entry)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    visit(root, Path(), entries[0])
    return entries


def inventory_file(group, path):
    path = plain_path(path)
    return _entry(group, Path(path.name), path)


class _HashingReader:
    def __init__(self, path, expected):
        self._descriptor = open_no_symlink(path)
        self._file = os.fdopen(self._descriptor, "rb", closefd=False)
        self._before = os.fstat(self._descriptor)
        if not stat.S_ISREG(self._before.st_mode) or self._before.st_nlink != 1:
            self.close()
            raise ValueError("backup file changed before archive read")
        if (self._before.st_dev, self._before.st_ino, self._before.st_size) != (
                expected["device"], expected["inode"], expected["size"]):
            self.close()
            raise ValueError("backup file changed before archive read")
        self._expected = expected
        self._digest = hashlib.sha256()
        self._bytes = 0

    def read(self, count=-1):
        block = self._file.read(count)
        self._digest.update(block)
        self._bytes += len(block)
        return block

    def finish(self):
        if (self._bytes != self._expected["size"]
                or self._digest.hexdigest() != self._expected["sha256"]
                or _stable(self._before) != _stable(os.fstat(self._descriptor))):
            raise ValueError("backup file changed while archiving")

    def close(self):
        self._file.close()
        os.close(self._descriptor)


class EncryptingWriter(io.RawIOBase):
    """AES-GCM records with a unique nonce per chunk and bound record order."""

    def __init__(self, output, key, nonce_prefix, context):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if len(key) != 32 or len(nonce_prefix) != 8:
            raise ValueError("invalid backup encryption material")
        self._output = output
        self._aead = AESGCM(key)
        self._prefix = nonce_prefix
        self._context = canonical_json(context)
        self._buffer = bytearray()
        self._index = 0
        self._digest = hashlib.sha256()
        self._size = 0
        self._record(MAGIC)

    def writable(self):
        return True

    def _record(self, data):
        self._output.write(data)
        self._digest.update(data)
        self._size += len(data)

    def write(self, data):
        self._buffer.extend(data)
        while len(self._buffer) >= CHUNK_SIZE:
            self._flush_chunk(CHUNK_SIZE)
        return len(data)

    def _flush_chunk(self, size):
        if self._index >= 2**32:
            raise ValueError("backup archive exceeds nonce space")
        plaintext = bytes(self._buffer[:size])
        del self._buffer[:size]
        index = self._index.to_bytes(4, "big")
        ciphertext = self._aead.encrypt(self._prefix + index, plaintext, self._context + index)
        self._record(len(ciphertext).to_bytes(4, "big"))
        self._record(ciphertext)
        self._index += 1

    def finish(self):
        if self._buffer:
            self._flush_chunk(len(self._buffer))
        self._output.flush()
        os.fsync(self._output.fileno())
        return {"sha256": self._digest.hexdigest(), "size": self._size,
                "chunks": self._index, "nonce_prefix": self._prefix.hex()}


def write_encrypted_tar(output_path, entries, key, nonce_prefix, context, *, output_fd=None):
    # The strict-host exporter creates this descriptor relative to its private
    # directory fd with O_EXCL | O_NOFOLLOW. Keep that inode open while writing.
    output = (os.fdopen(output_fd, "wb", buffering=0) if output_fd is not None
              else open(output_path, "xb", buffering=0))
    with output:
        if output_fd is not None and os.name == "posix":
            os.fchmod(output.fileno(), 0o600)
        elif output_fd is None:
            os.chmod(output_path, 0o600)
        writer = EncryptingWriter(output, key, nonce_prefix, context)
        with tarfile.open(fileobj=writer, mode="w|") as archive:
            for entry in entries:
                info = tarfile.TarInfo(entry["archive_path"])
                info.mode = entry["mode"]
                info.uid = entry["uid"]
                info.gid = entry["gid"]
                info.mtime = 0
                if entry["type"] == "directory":
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                else:
                    info.size = entry["size"]
                    reader = _HashingReader(entry["source_path"], entry)
                    try:
                        archive.addfile(info, reader)
                        reader.finish()
                    finally:
                        reader.close()
        return writer.finish()
