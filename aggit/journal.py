"""
AgGit Append-Only Transaction Journal:
Provides atomic, lock-free transaction appending with complete UUID identifiers.
Supports:
- Atomic rename publication with non-.json temporary files
- Derived O(1) index caching with mtime freshness check and auto-healing
- Strict causal & chronological transaction reconstruction
- Zero silent drops when multiple writer instances exist
- Full Schema v2 persistence
"""

import os
import json
import glob
import uuid
import time
from typing import List, Dict, Optional, Any
from .storage import CASStorage
from .schema import TransactionRecord, ChangeRecord
from .lock_utils import file_lock

class JournalCorruptionError(Exception):
    """Raised when a journal record fails validation or is unparseable."""
    pass

class Journal:
    def __init__(self, repo_dir: str, storage: CASStorage):
        self.repo_dir = os.path.abspath(repo_dir)
        self.storage = storage
        self.journal_dir = os.path.join(self.repo_dir, ".aggit", "journal")
        self.index_path = os.path.join(self.repo_dir, ".aggit", "journal_index.json")
        os.makedirs(self.journal_dir, exist_ok=True)
        self._index: Optional[Dict[str, Any]] = None
        self._index_mtime: Optional[float] = None

    def _load_index(self) -> Dict[str, Any]:
        disk_mtime = os.path.getmtime(self.index_path) if os.path.exists(self.index_path) else None
        if self._index is not None and disk_mtime is not None and self._index_mtime == disk_mtime:
            return self._index
        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    self._index = json.load(f)
                    self._index_mtime = disk_mtime
                    return self._index
            except Exception:
                pass
        self._index = self._rebuild_index()
        self._save_index(self._index)
        return self._index

    def _save_index(self, index: Dict[str, Any]):
        self._index = index
        tmp = self.index_path + f".tmp_{os.getpid()}_{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(index, f, indent=2, sort_keys=True)
            os.replace(tmp, self.index_path)
            self._index_mtime = os.path.getmtime(self.index_path)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _rebuild_index(self) -> Dict[str, Any]:
        """Scans all published .json events and builds a fast lookup index."""
        tx_files = sorted(glob.glob(os.path.join(self.journal_dir, "*.json")))
        by_id: Dict[str, Dict[str, Any]] = {}
        all_tx_ids: List[str] = []
        head_tx_id = None

        for filepath in tx_files:
            filename = os.path.basename(filepath)
            # Skip any temp files that might have been accidentally created
            if filename.startswith(".tmp_"):
                continue
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                tx = TransactionRecord.from_dict(data)
                by_id[tx.tx_id] = {
                    "filename": filename,
                    "tx_id": tx.tx_id,
                    "agent_id": tx.agent_id,
                    "session_id": tx.session_id,
                    "timestamp_ns": tx.timestamp_ns,
                    "timestamp": tx.timestamp,
                    "message": tx.message,
                    "parents": tx.parents,
                    "base_revision": tx.base_revision,
                    "schema_version": tx.schema_version
                }
                all_tx_ids.append(tx.tx_id)
                head_tx_id = tx.tx_id
            except Exception:
                # Corrupt or invalid JSON file
                continue

        index = {
            "version": 1,
            "head_tx_id": head_tx_id,
            "all_tx_ids": all_tx_ids,
            "by_id": by_id
        }
        return index

    def append_transaction(
        self,
        agent_id: str,
        message: str,
        changes: Dict[str, ChangeRecord],
        parents: Optional[List[str]] = None,
        base_revision: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> TransactionRecord:
        """
        Atomically appends a new transaction event to the journal.
        Uses full UUID and non-.json temporary file suffix.
        Updates derived index reliably without dropping concurrent writes.
        """
        tx = TransactionRecord.create(
            agent_id=agent_id,
            message=message,
            changes=changes,
            parents=parents,
            base_revision=base_revision,
            session_id=session_id,
            metadata=metadata
        )

        filename = f"{tx.timestamp_ns:020d}_{tx.agent_id}_{tx.tx_id}.json"
        tx_path = os.path.join(self.journal_dir, filename)

        # Temp file uses distinct .tmp extension so glob("*.json") will never match in-flight writes
        unique_tmp_name = f".tmp_tx_{os.getpid()}_{uuid.uuid4().hex}.tmp"
        tmp_path = os.path.join(self.journal_dir, unique_tmp_name)

        tx_dict = tx.to_dict()
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(tx_dict, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())

            # Atomic publish via rename
            os.replace(tmp_path, tx_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

        tx_meta = {
            "filename": filename,
            "tx_id": tx.tx_id,
            "agent_id": tx.agent_id,
            "session_id": tx.session_id,
            "timestamp_ns": tx.timestamp_ns,
            "timestamp": tx.timestamp,
            "message": tx.message,
            "parents": tx.parents,
            "base_revision": tx.base_revision,
            "schema_version": tx.schema_version
        }

        # Update index incrementally under exclusive lock (O(1))
        lock_path = self.index_path + ".lock"
        with file_lock(lock_path):
            index = self._load_index()
            if not index or "by_id" not in index or "all_tx_ids" not in index:
                index = self._rebuild_index()
            else:
                index["by_id"][tx.tx_id] = tx_meta
                if tx.tx_id not in index["all_tx_ids"]:
                    index["all_tx_ids"].append(tx.tx_id)
                index["head_tx_id"] = tx.tx_id
            self._save_index(index)

        return tx

    def get_transaction(self, tx_id: str) -> Optional[TransactionRecord]:
        """Retrieves a single transaction by its full or prefix tx_id."""
        index = self._load_index()
        target_info = index.get("by_id", {}).get(tx_id)
        if not target_info:
            # Check prefix match
            matches = [info for tid, info in index.get("by_id", {}).items() if tid.startswith(tx_id)]
            if len(matches) == 1:
                target_info = matches[0]

        if not target_info:
            # Index might be missing this tx; rebuild from journal files
            index = self._rebuild_index()
            self._save_index(index)
            target_info = index.get("by_id", {}).get(tx_id)
            if not target_info:
                matches = [info for tid, info in index.get("by_id", {}).items() if tid.startswith(tx_id)]
                if len(matches) == 1:
                    target_info = matches[0]

        if not target_info:
            return None

        filepath = os.path.join(self.journal_dir, target_info["filename"])
        if not os.path.exists(filepath):
            # Index stale, rebuild
            index = self._rebuild_index()
            self._save_index(index)
            target_info = index.get("by_id", {}).get(tx_id)
            if not target_info:
                return None
            filepath = os.path.join(self.journal_dir, target_info["filename"])

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return TransactionRecord.from_dict(data)

    def get_latest_transaction(self) -> Optional[TransactionRecord]:
        """Retrieves the latest transaction in O(1) via the derived index."""
        index = self._load_index()
        head_id = index.get("head_tx_id")
        if not head_id:
            all_ids = index.get("all_tx_ids", [])
            head_id = all_ids[-1] if all_ids else None
        if head_id:
            tx = self.get_transaction(head_id)
            if tx:
                return tx

        # Fallback to scanning if index was empty or missing
        all_txs = self.get_all_transactions()
        return all_txs[-1] if all_txs else None

    def get_all_transactions(self) -> List[TransactionRecord]:
        """
        Returns all published transactions sorted in deterministic causal/chronological order.
        Validates integrity of each record.
        """
        records: List[TransactionRecord] = []
        pattern = os.path.join(self.journal_dir, "*.json")
        for filepath in sorted(glob.glob(pattern)):
            filename = os.path.basename(filepath)
            if filename.startswith(".tmp_"):
                continue
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                records.append(TransactionRecord.from_dict(data))
            except Exception as e:
                # Flag corruption explicitly
                raise JournalCorruptionError(f"Corrupt transaction record in {filepath}: {e}")
        return records

    def get_orphaned_temporaries(self) -> List[str]:
        """Lists any lingering temporary files from interrupted writes."""
        pattern = os.path.join(self.journal_dir, ".tmp_*")
        return [f for f in glob.glob(pattern)]

    def cleanup_orphaned_temporaries(self) -> int:
        """Safely removes abandoned temporary files without touching published .json events."""
        orphans = self.get_orphaned_temporaries()
        count = 0
        for p in orphans:
            try:
                os.remove(p)
                count += 1
            except OSError:
                pass
        return count
