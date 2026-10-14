"""
AgGit Storage Engine: Content-Addressable Storage (CAS)
Stores blobs, trees, and commit manifests indexed by SHA-256 with zlib compression.
Lock-free writes: uses atomic file creation + rename.
"""

import os
import hashlib
import zlib
import json
import tempfile
from typing import Optional, Tuple, Dict, Any

class CASStorage:
    def __init__(self, repo_dir: str):
        self.repo_dir = os.path.abspath(repo_dir)
        self.aggit_dir = os.path.join(self.repo_dir, ".aggit")
        self.objects_dir = os.path.join(self.aggit_dir, "objects")
        os.makedirs(self.objects_dir, exist_ok=True)

    def _hash_and_compress(self, obj_type: str, data: bytes) -> Tuple[str, bytes]:
        header = f"{obj_type} {len(data)}\0".encode("utf-8")
        full_payload = header + data
        sha = hashlib.sha256(full_payload).hexdigest()
        compressed = zlib.compress(full_payload, level=6)
        return sha, compressed

    def write_object(self, obj_type: str, data: bytes) -> str:
        """Writes an object to CAS atomically. If it already exists, returns hash immediately."""
        sha, compressed = self._hash_and_compress(obj_type, data)
        shard = sha[:2]
        rest = sha[2:]
        shard_dir = os.path.join(self.objects_dir, shard)
        os.makedirs(shard_dir, exist_ok=True)
        final_path = os.path.join(shard_dir, rest)

        if os.path.exists(final_path):
            return sha

        # Atomic write via temp file in same filesystem directory
        fd, tmp_path = tempfile.mkstemp(dir=shard_dir, prefix=".tmp_obj_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(compressed)
            # Atomic rename (replace if race condition)
            os.replace(tmp_path, final_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise
        return sha

    def read_object(self, sha: str) -> Tuple[str, bytes]:
        """Reads and verifies an object from CAS."""
        shard = sha[:2]
        rest = sha[2:]
        obj_path = os.path.join(self.objects_dir, shard, rest)
        if not os.path.exists(obj_path):
            raise FileNotFoundError(f"Object {sha} not found in CAS")

        with open(obj_path, "rb") as f:
            raw = f.read()

        decompressed = zlib.decompress(raw)
        null_idx = decompressed.find(b"\0")
        if null_idx == -1:
            raise ValueError(f"Corrupt object header in {sha}")

        header = decompressed[:null_idx].decode("utf-8")
        obj_type, size_str = header.split(" ")
        payload = decompressed[null_idx + 1:]

        if len(payload) != int(size_str):
            raise ValueError(f"Payload size mismatch for object {sha}")

        return obj_type, payload

    def store_blob(self, content: bytes) -> str:
        return self.write_object("blob", content)

    def get_blob(self, sha: str) -> bytes:
        obj_type, payload = self.read_object(sha)
        if obj_type != "blob":
            raise ValueError(f"Expected blob, got {obj_type}")
        return payload

    def store_manifest(self, manifest: Dict[str, Any]) -> str:
        data = json.dumps(manifest, sort_keys=True).encode("utf-8")
        return self.write_object("manifest", data)

    def get_manifest(self, sha: str) -> Dict[str, Any]:
        obj_type, payload = self.read_object(sha)
        if obj_type != "manifest":
            raise ValueError(f"Expected manifest, got {obj_type}")
        return json.loads(payload.decode("utf-8"))
