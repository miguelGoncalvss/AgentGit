"""
AgGit Virtual Namespaced Staging:
Provides isolated, concurrent staging per agent and session.
Supports:
- Batch staging with single write per batch
- Tracking file additions, modifications, and deletions
- Generation counter to prevent clearing newer staged files on commit
- Hash-disambiguated filenames preventing agent_id collisions
- Process/thread-safe per-agent-session locking preventing read-modify-write races
- Path security validation (no traversal, no absolute paths, no .git/.aggit)
"""

import os
import json
import uuid
import hashlib
import threading
from typing import Dict, List, Optional, Tuple, Any
from .storage import CASStorage
from .path_utils import sanitize_repo_path
from .schema import ChangeRecord
from .lock_utils import file_lock

class StagingArea:
    def __init__(self, repo_dir: str, storage: CASStorage):
        self.repo_dir = os.path.abspath(repo_dir)
        self.storage = storage
        self.staging_dir = os.path.join(self.repo_dir, ".aggit", "staging")
        os.makedirs(self.staging_dir, exist_ok=True)

    def _agent_staging_filename(self, agent_id: str, session_id: Optional[str] = None) -> str:
        ident = f"{agent_id}::{session_id or agent_id}"
        ident_hash = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:12]
        safe_prefix = "".join(c if c.isalnum() or c in "-_" else "_" for c in agent_id)[:24]
        return f"{safe_prefix}_{ident_hash}.json"

    def _agent_staging_path(self, agent_id: str, session_id: Optional[str] = None) -> str:
        return os.path.join(self.staging_dir, self._agent_staging_filename(agent_id, session_id))

    def _agent_lock_path(self, agent_id: str, session_id: Optional[str] = None) -> str:
        return self._agent_staging_path(agent_id, session_id) + ".lock"

    def get_staged_state(self, agent_id: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        """Returns full staging document including generation, base_revision, and entries."""
        path = self._agent_staging_path(agent_id, session_id)
        if not os.path.exists(path):
            return {"generation": 0, "agent_id": agent_id, "session_id": session_id or agent_id, "entries": {}, "base_revision": None}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "entries" not in data:
                    # Legacy migration: data was {"rel_path": "sha"}
                    entries = {}
                    for p, sha in data.items():
                        entries[p] = {"action": "modify", "blob_sha": sha}
                    return {"generation": 1, "agent_id": agent_id, "session_id": session_id or agent_id, "entries": entries, "base_revision": None}
                return data
        except Exception:
            return {"generation": 0, "agent_id": agent_id, "session_id": session_id or agent_id, "entries": {}, "base_revision": None}

    def get_staged_changes(self, agent_id: str, session_id: Optional[str] = None) -> Dict[str, ChangeRecord]:
        """Returns map of sanitized rel_path -> ChangeRecord for this agent/session."""
        state = self.get_staged_state(agent_id, session_id)
        changes = {}
        for path, entry_data in state.get("entries", {}).items():
            changes[path] = ChangeRecord.from_dict(entry_data)
        return changes

    def set_base_revision(self, agent_id: str, base_revision: str, session_id: Optional[str] = None):
        """Sets the starting revision baseline for this agent's session under lock."""
        lock_file = self._agent_lock_path(agent_id, session_id)
        with file_lock(lock_file):
            state = self.get_staged_state(agent_id, session_id)
            state["base_revision"] = base_revision
            state["generation"] = state.get("generation", 0) + 1
            path = self._agent_staging_path(agent_id, session_id)
            tmp = path + f".tmp_{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2, sort_keys=True)
                os.replace(tmp, path)
            except Exception:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                raise

    def stage_batch(
        self,
        agent_id: str,
        files_to_stage: Optional[Dict[str, Optional[bytes]]] = None,
        files_to_delete: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        base_revision: Optional[str] = None
    ) -> Dict[str, ChangeRecord]:
        """
        Stages multiple additions/modifications and deletions in a single atomic batch write.
        Protected by session-scoped lock to ensure concurrency safety between threads and processes.
        """
        # First compute all blob hashes outside the lock to minimize critical section
        staged_blobs: Dict[str, Tuple[str, Optional[str]]] = {}
        if files_to_stage:
            for rel_path, content in files_to_stage.items():
                norm_path = sanitize_repo_path(self.repo_dir, rel_path)
                if content is None:
                    full_path = os.path.join(self.repo_dir, norm_path)
                    if not os.path.exists(full_path):
                        raise FileNotFoundError(f"File {rel_path} does not exist in working directory")
                    with open(full_path, "rb") as f:
                        content = f.read()
                blob_sha = self.storage.store_blob(content)
                staged_blobs[norm_path] = (blob_sha, None)

        lock_file = self._agent_lock_path(agent_id, session_id)
        with file_lock(lock_file):
            state = self.get_staged_state(agent_id, session_id)
            entries = state.setdefault("entries", {})
            if base_revision:
                state["base_revision"] = base_revision

            # Apply additions / modifications
            for norm_path, (blob_sha, _) in staged_blobs.items():
                old_sha = entries.get(norm_path, {}).get("blob_sha")
                action = "add" if old_sha is None else "modify"
                entries[norm_path] = ChangeRecord(action=action, blob_sha=blob_sha, old_sha=old_sha).to_dict()

            # Process deletions
            if files_to_delete:
                for rel_path in files_to_delete:
                    norm_path = sanitize_repo_path(self.repo_dir, rel_path)
                    old_sha = entries.get(norm_path, {}).get("blob_sha")
                    entries[norm_path] = ChangeRecord(action="delete", blob_sha=None, old_sha=old_sha).to_dict()

            state["generation"] = state.get("generation", 0) + 1

            path = self._agent_staging_path(agent_id, session_id)
            tmp = path + f".tmp_{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2, sort_keys=True)
                os.replace(tmp, path)
            except Exception:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                raise

            return {p: ChangeRecord.from_dict(v) for p, v in entries.items()}

    def stage_file(self, agent_id: str, rel_path: str, content: bytes = None, session_id: Optional[str] = None) -> str:
        res = self.stage_batch(agent_id, files_to_stage={rel_path: content}, session_id=session_id)
        norm = sanitize_repo_path(self.repo_dir, rel_path)
        return res[norm].blob_sha or ""

    def stage_files(self, agent_id: str, rel_paths: List[str], session_id: Optional[str] = None) -> Dict[str, str]:
        files_dict = {p: None for p in rel_paths}
        res = self.stage_batch(agent_id, files_to_stage=files_dict, session_id=session_id)
        return {p: cr.blob_sha or "" for p, cr in res.items() if cr.blob_sha}

    def stage_delete(self, agent_id: str, rel_path: str, session_id: Optional[str] = None):
        self.stage_batch(agent_id, files_to_delete=[rel_path], session_id=session_id)

    def clear(self, agent_id: str, session_id: Optional[str] = None, max_generation: Optional[int] = None):
        """
        Clears the staging area only if current generation <= max_generation.
        Atomically verified under session lock to avoid race conditions.
        """
        path = self._agent_staging_path(agent_id, session_id)
        if not os.path.exists(path):
            return

        lock_file = self._agent_lock_path(agent_id, session_id)
        with file_lock(lock_file):
            if not os.path.exists(path):
                return
            if max_generation is not None:
                state = self.get_staged_state(agent_id, session_id)
                if state.get("generation", 0) > max_generation:
                    # Retain newer staged changes
                    return

            try:
                os.remove(path)
            except OSError:
                pass
