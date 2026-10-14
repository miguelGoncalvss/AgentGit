"""
AgGit Core Engine:
High-level API for initializing, staging, committing (lock-free), and reconciling multi-agent state.
"""

import os
import json
from typing import Dict, List, Any, Optional, Tuple
from .storage import CASStorage
from .journal import Journal
from .staging import StagingArea
from .semantic_merge import SemanticMerger

class AgGitEngine:
    def __init__(self, repo_dir: str):
        self.repo_dir = os.path.abspath(repo_dir)
        self.aggit_dir = os.path.join(self.repo_dir, ".aggit")
        self.storage = CASStorage(self.repo_dir)
        self.journal = Journal(self.repo_dir, self.storage)
        self.staging = StagingArea(self.repo_dir, self.storage)

    @classmethod
    def init(cls, repo_dir: str) -> "AgGitEngine":
        os.makedirs(os.path.join(repo_dir, ".aggit"), exist_ok=True)
        config_path = os.path.join(repo_dir, ".aggit", "config.json")
        if not os.path.exists(config_path):
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({"version": "1.0.0", "engine": "aggit-concurrent-vcs"}, f, indent=2)
        return cls(repo_dir)

    def is_initialized(self) -> bool:
        return os.path.exists(os.path.join(self.aggit_dir, "config.json"))

    def stage_file(self, agent_id: str, rel_path: str, content: bytes = None) -> str:
        return self.staging.stage_file(agent_id, rel_path, content)

    def stage_files(self, agent_id: str, rel_paths: List[str]) -> Dict[str, str]:
        return self.staging.stage_files(agent_id, rel_paths)

    def commit(
        self,
        agent_id: str,
        message: str,
        files: Optional[Dict[str, bytes]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Performs a lock-free commit for this agent.
        If files are provided explicitly, stages them first.
        Otherwise uses current staged files for this agent.
        """
        if files:
            for p, content in files.items():
                self.stage_file(agent_id, p, content)

        staged = self.staging.get_staged(agent_id)
        if not staged and not files:
            raise ValueError(f"Nothing to commit for agent {agent_id} (staging is empty)")

        # Record transaction in journal
        latest_tx = self.journal.get_latest_transaction()
        parents = [latest_tx["tx_id"]] if latest_tx else []

        tx = self.journal.append_transaction(
            agent_id=agent_id,
            message=message,
            files_manifest=staged,
            parent_tx_ids=parents,
            metadata=metadata
        )

        # Clear agent staging
        self.staging.clear(agent_id)
        return tx

    def reconcile_unified_state(self) -> Tuple[Dict[str, bytes], List[str]]:
        """
        Traverses all transactions in causal order and merges files using SemanticMerger.
        Returns:
            unified_files: rel_path -> merged bytes content
            conflicts: list of file paths that contain unresolved conflicts
        """
        transactions = self.journal.get_all_transactions()
        unified_files: Dict[str, bytes] = {}
        conflicts = []

        # Replay each transaction
        for tx in transactions:
            manifest = tx.get("files", {})
            for rel_path, blob_sha in manifest.items():
                new_bytes = self.storage.get_blob(blob_sha)
                
                if rel_path not in unified_files:
                    unified_files[rel_path] = new_bytes
                else:
                    curr_bytes = unified_files[rel_path]
                    if curr_bytes == new_bytes:
                        continue
                    
                    # 3-way merge between empty/base, curr and new
                    try:
                        curr_text = curr_bytes.decode("utf-8")
                        new_text = new_bytes.decode("utf-8")
                        merged_text, has_conflict = SemanticMerger.merge_content(
                            base_text="",
                            a_text=curr_text,
                            b_text=new_text,
                            filename=rel_path
                        )
                        unified_files[rel_path] = merged_text.encode("utf-8")
                        if has_conflict:
                            conflicts.append(rel_path)
                    except UnicodeDecodeError:
                        # Binary file: last-write-wins by timestamp
                        unified_files[rel_path] = new_bytes

        return unified_files, conflicts

    def materialize(self) -> Tuple[int, List[str]]:
        """
        Materializes the reconciled multi-agent state into the working directory on disk.
        Returns (number_of_files_written, list_of_conflicts).
        """
        unified, conflicts = self.reconcile_unified_state()
        for rel_path, content in unified.items():
            full_path = os.path.join(self.repo_dir, rel_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(content)
        return len(unified), conflicts
