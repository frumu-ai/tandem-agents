"""Bounded streaming decryption for the v3 backup AEAD record format."""

import hashlib
import io
import os
import stat

from .backup_archive import CHUNK_SIZE, MAGIC, canonical_json, open_no_symlink, plain_path


class DecryptingReader(io.RawIOBase):
    def __init__(self, path, metadata, dek, context, domain):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if len(dek) != 32 or type(metadata.get("chunks")) is not int or metadata["chunks"] <= 0:
            raise ValueError("invalid encrypted backup metadata")
        try:
            prefix = bytes.fromhex(metadata["nonce_prefix"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid backup nonce prefix") from None
        if len(prefix) != 8 or prefix[:1] != domain:
            raise ValueError("backup nonce domain is invalid")
        self._path = plain_path(path)
        self._fd = open_no_symlink(self._path)
        self._source = os.fdopen(self._fd, "rb", closefd=False)
        self._before = os.fstat(self._fd)
        if (not stat.S_ISREG(self._before.st_mode) or self._before.st_nlink != 1
                or self._before.st_size != metadata.get("size")):
            self.close()
            raise ValueError("encrypted backup object changed while opening")
        self._metadata = metadata
        self._aead = AESGCM(dek)
        self._prefix = prefix
        self._context = canonical_json(context)
        self._digest = hashlib.sha256()
        self._size = 0
        self._index = 0
        self._buffer = bytearray()
        self._finished = False
        try:
            if self._read_exact(len(MAGIC)) != MAGIC:
                raise ValueError("invalid encrypted backup object magic")
        except BaseException:
            self.close()
            raise

    def readable(self):
        return True

    def _read_exact(self, count):
        value = self._source.read(count)
        self._digest.update(value)
        self._size += len(value)
        if len(value) != count:
            raise ValueError("truncated encrypted backup object")
        return value

    def _next_chunk(self):
        if self._index == self._metadata["chunks"]:
            if self._source.read(1):
                raise ValueError("encrypted backup object has trailing data")
            after = os.fstat(self._fd)
            if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    != (self._before.st_dev, self._before.st_ino,
                        self._before.st_size, self._before.st_mtime_ns)
                    or self._size != self._metadata["size"]
                    or self._digest.hexdigest() != self._metadata["sha256"]):
                raise ValueError("encrypted backup object digest or identity changed")
            self._finished = True
            return
        length = int.from_bytes(self._read_exact(4), "big")
        if not 17 <= length <= CHUNK_SIZE + 16:
            raise ValueError("invalid encrypted backup record length")
        ciphertext = self._read_exact(length)
        index = self._index.to_bytes(4, "big")
        plaintext = self._aead.decrypt(self._prefix + index, ciphertext,
                                       self._context + index)
        if (not plaintext or len(plaintext) > CHUNK_SIZE
                or (self._index + 1 < self._metadata["chunks"]
                    and len(plaintext) != CHUNK_SIZE)):
            raise ValueError("invalid encrypted backup plaintext length")
        self._buffer.extend(plaintext)
        self._index += 1

    def read(self, size=-1):
        if size is None or size < 0:
            raise ValueError("backup verifier requires bounded streaming reads")
        while len(self._buffer) < size and not self._finished:
            self._next_chunk()
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def finish(self):
        # tarfile stops at the first archive end marker. Any remaining
        # authenticated plaintext may only be tar block padding.
        while block := self.read(CHUNK_SIZE):
            if any(block):
                raise ValueError("backup tar has nonzero trailing plaintext")

    def close(self):
        if hasattr(self, "_source") and not self._source.closed:
            self._source.close()
            os.close(self._fd)
        super().close()
