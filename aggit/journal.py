"""
AgGit Journal: Lock-Free Append-Only Transaction Log.
Enables concurrent commits from multiple agents without a global lock.
Each commit is recorded as an immutable transaction event.
"""

import os
import time
import json
import uuid
import glob
from typing import List, Dict, Any, Optional
from .storage import CASStorage

class Journal:
    def __init__(self, repo_dir: str, storage: CASStorage):
        self.repo_dir = os.path.abspath(repo_dir)
        self.storage = storage
        self.journal_dir = os.path.join(self.repo_dir, ".aggit", "journal")
        os.makedirs(self.journal_dir, exist_ok=True)

    def append_transaction(
        self,
        agent_id: str,
        message: str,
        files_manifest: Dict[str, str],  # rel_path -> blob_sha
        parent_tx_ids: List[str] = None,
        metadata: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        Atomically appends a new transaction for the given agent.
        Zero locking: writes to an agent-unique transaction file.
        """
        now = time.time()
        tx_id = str(uuid.uuid4())[:8]
        ts_nano = time.time_ns()

        manifest_sha = self.storage.store_manifest({
            "files": files_manifest,
            "agent_id": agent_id,
            "timestamp": now,
            "message": message,
            "metadata": metadata or {}
        })

        tx_record = {
            "tx_id": tx_id,
            "agent_id": agent_id,
            "timestamp": now,
            "timestamp_ns": ts_nano,
            "message": message,
            "manifest_sha": manifest_sha,
            "files": files_manifest,
            "parents": parent_tx_ids or [],
            "metadata": metadata or {}
        }

        # Filename encodes timestamp and agent to preserve causal/temporal sorting
        filename = f"{ts_nano:020d}_{agent_id}_{tx_id}.json"
        tx_path = os.path.join(self.journal_dir, filename)

        # Atomic write
        tmp_path = tx_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(tx_record, f, indent=2, sort_keys=True)
        os.replace(tmp_path, tx_path)

        return tx_record

    def get_all_transactions(self) -> List[Dict[str, Any]]:
        """Returns all transactions sorted chronologically by timestamp_ns."""
        records = []
        pattern = os.path.join(self.journal_dir, "*.json")
        for filepath in sorted(glob.glob(pattern)):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    records.append(json.load(f))
            except Exception:
                continue
        return records

    def get_agent_transactions(self, agent_id: str) -> List[Dict[str, Any]]:
        return [tx for tx in self.get_all_transactions() if tx.get("agent_id") == agent_id]

    def get_latest_transaction(self) -> Optional[Dict[str, Any]]:
        txs = self.get_all_transactions()
        return txs[-1] if txs else None
