"""
AgGit Storage Engine: Content-Addressable Storage (CAS)
Stores blobs, trees, and commit manifests indexed by SHA-256 with zlib compression.
Guarantees:
- Hash computed before compression; existence checked before compressing.
- Unique tempfiles per thread/process.
- Atomic filesystem replacement with concurrent race handling.
- Verification of SHA-256, header, size, and decompression limits on read.
"""

import os
import hashlib
import zlib
import json
import tempfile
import uuid
import re
from typing import Tuple, Dict, Any

HEX_SHA_REGEX = re.compile(r"^[0-9a-f]{64}$")
MAX_DECOMPRESSED_BYTES = 200 * 1024 * 1024  # 200 MB safe limit

class CASStorage:
    def __init__(self, repo_dir: str):
        self.repo_dir = os.path.abspath(repo_dir)
        self.aggit_dir = os.path.join(self.repo_dir, ".aggit")
        self.objects_dir = os.path.join(self.aggit_dir, "objects")
        os.makedirs(self.objects_dir, exist_ok=True)

    def _compute_hash_and_header(self, obj_type: str, data: bytes) -> Tuple[str, bytes]:
        header = f"{obj_type} {len(data)}\0".encode("utf-8")
        full_payload = header + data
        sha = hashlib.sha256(full_payload).hexdigest()
        return sha, full_payload

    def write_object(self, obj_type: str, data: bytes) -> str:
        """
        Writes an object to CAS atomically.
        Computes SHA first and checks existence before compressing.
        Handles concurrent writes safely.
        """
        sha, full_payload = self._compute_hash_and_header(obj_type, data)
        shard = sha[:2]
        rest = sha[2:]
        shard_dir = os.path.join(self.objects_dir, shard)
        os.makedirs(shard_dir, exist_ok=True)
        final_path = os.path.join(shard_dir, rest)

        # Early return if object already exists in CAS (skips compression entirely)
        if os.path.exists(final_path):
            return sha

        # Compress only when object does not exist yet
        compressed = zlib.compress(full_payload, level=6)

        # Unique tempfile per thread and process
        unique_id = f"{os.getpid()}_{uuid.uuid4().hex}"
        fd, tmp_path = tempfile.mkstemp(dir=shard_dir, prefix=f".tmp_{unique_id}_", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(compressed)
                f.flush()
                os.fsync(f.fileno())

            # Atomic rename (on Windows, os.replace replaces existing file atomically)
            os.replace(tmp_path, final_path)
        except Exception as e:
            # Check if another process completed writing the exact same object
            if os.path.exists(final_path):
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                return sha
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise e

        # Ensure object is actually present and valid
        if not os.path.exists(final_path):
            raise IOError(f"Failed to publish object {sha} into CAS")

        return sha

    def read_object(self, sha: str) -> Tuple[str, bytes]:
        """
        Reads, verifies, and decompresses an object from CAS.
        Verifies SHA-256 integrity, header format, payload length, and size limits.
        """
        clean_sha = sha.strip().lower()
        if not HEX_SHA_REGEX.match(clean_sha):
            raise ValueError(f"Invalid SHA-256 hash format: '{sha}'")

        shard = clean_sha[:2]
        rest = clean_sha[2:]
        obj_path = os.path.join(self.objects_dir, shard, rest)
        if not os.path.exists(obj_path):
            raise FileNotFoundError(f"Object {clean_sha} not found in CAS")

        with open(obj_path, "rb") as f:
            raw = f.read()

        # Decompress with size limit protection
        try:
            decompressor = zlib.decompressobj()
            decompressed = decompressor.decompress(raw, MAX_DECOMPRESSED_BYTES)
        except zlib.error as e:
            raise ValueError(f"Corrupt compressed object {clean_sha}: {e}")
        if decompressor.unconsumed_tail:
            raise ValueError(f"Object {clean_sha} exceeds maximum allowed decompressed size ({MAX_DECOMPRESSED_BYTES} bytes)")

        # Verify full payload hash matches the requested SHA
        calculated_sha = hashlib.sha256(decompressed).hexdigest()
        if calculated_sha != clean_sha:
            raise ValueError(f"Integrity check failed for object {clean_sha}: calculated {calculated_sha}")

        null_idx = decompressed.find(b"\0")
        if null_idx == -1:
            raise ValueError(f"Corrupt object header in {clean_sha}: missing null byte")

        header_bytes = decompressed[:null_idx]
        try:
            header_str = header_bytes.decode("utf-8")
            parts = header_str.split(" ")
            if len(parts) != 2:
                raise ValueError("Header must have type and size")
            obj_type, size_str = parts[0], parts[1]
            expected_size = int(size_str)
        except Exception as e:
            raise ValueError(f"Corrupt object header in {clean_sha}: {e}")

        payload = decompressed[null_idx + 1:]
        if len(payload) != expected_size:
            raise ValueError(f"Payload size mismatch for object {clean_sha}: expected {expected_size}, got {len(payload)}")

        return obj_type, payload

    def store_blob(self, content: bytes) -> str:
        return self.write_object("blob", content)

    def get_blob(self, sha: str) -> bytes:
        obj_type, payload = self.read_object(sha)
        if obj_type != "blob":
            raise ValueError(f"Expected blob object for {sha}, got {obj_type}")
        return payload

    def store_manifest(self, manifest: Dict[str, Any]) -> str:
        data = json.dumps(manifest, sort_keys=True).encode("utf-8")
        return self.write_object("manifest", data)

    def get_manifest(self, sha: str) -> Dict[str, Any]:
        obj_type, payload = self.read_object(sha)
        if obj_type != "manifest":
            raise ValueError(f"Expected manifest object for {sha}, got {obj_type}")
        return json.loads(payload.decode("utf-8"))
