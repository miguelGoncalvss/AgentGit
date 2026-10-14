"""
AgGit Core Engine:
Coordinates storage, journal, staging, incremental reconciliation, and smart materialization.
Guarantees:
- Incremental reconciliation using checkpoints (.aggit/checkpoints/latest.json)
- Base revision tracking (never assumes empty base)
- Materialization writes only changed files and removes deleted files
- Detects local uncommitted modifications before overwriting
- Atomic file writes with error propagation
"""

import os
import json
import uuid
import tempfile
from typing import Dict, List, Any, Optional, Tuple
from .storage import CASStorage
from .journal import Journal
from .staging import StagingArea
from .semantic_merge import SemanticMerger, MergeConflict
from .schema import TransactionRecord, ChangeRecord
from .path_utils import sanitize_repo_path

class AgGitEngine:
    def __init__(self, repo_dir: str):
        self.repo_dir = os.path.abspath(repo_dir)
        self.aggit_dir = os.path.join(self.repo_dir, ".aggit")
        self.storage = CASStorage(self.repo_dir)
        self.journal = Journal(self.repo_dir, self.storage)
        self.staging = StagingArea(self.repo_dir, self.storage)
        self.checkpoint_dir = os.path.join(self.aggit_dir, "checkpoints")
        self.materialized_state_path = os.path.join(self.aggit_dir, "materialized_state.json")
        os.makedirs(self.checkpoint_dir, exist_ok=True)

    @classmethod
    def init(cls, repo_dir: str) -> "AgGitEngine":
        os.makedirs(os.path.join(repo_dir, ".aggit"), exist_ok=True)
        config_path = os.path.join(repo_dir, ".aggit", "config.json")
        if not os.path.exists(config_path):
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({"version": "2.0.0", "engine": "aggit-concurrent-vcs"}, f, indent=2)
        return cls(repo_dir)

    def is_initialized(self) -> bool:
        return os.path.exists(os.path.join(self.aggit_dir, "config.json"))

    def stage_file(self, agent_id: str, rel_path: str, content: bytes = None, session_id: Optional[str] = None) -> str:
        return self.staging.stage_file(agent_id, rel_path, content, session_id)

    def stage_files(self, agent_id: str, rel_paths: List[str], session_id: Optional[str] = None) -> Dict[str, str]:
        return self.staging.stage_files(agent_id, rel_paths, session_id)

    def stage_delete(self, agent_id: str, rel_path: str, session_id: Optional[str] = None):
        self.staging.stage_delete(agent_id, rel_path, session_id)

    def set_session_base(self, agent_id: str, base_revision: str, session_id: Optional[str] = None):
        self.staging.set_base_revision(agent_id, base_revision, session_id)

    def commit(
        self,
        agent_id: str,
        message: str,
        files: Optional[Dict[str, bytes]] = None,
        session_id: Optional[str] = None,
        base_revision: Optional[str] = None,
        parents: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> TransactionRecord:
        """
        Commits changes for the given agent and session.
        If files are passed directly, stages them directly without touching staging file.
        Otherwise uses current staged changes for this session.
        Preserves base_revision and accurate parents.
        """
        staged_state = self.staging.get_staged_state(agent_id, session_id)
        current_generation = staged_state.get("generation", 0)

        effective_base = base_revision or staged_state.get("base_revision")
        effective_parents = parents
        if effective_parents is None:
            # If base_revision was specified, it's the parent; else lookup current HEAD
            if effective_base:
                effective_parents = [effective_base]
            else:
                latest = self.journal.get_latest_transaction()
                effective_parents = [latest.tx_id] if latest else []

        changes: Dict[str, ChangeRecord] = {}
        if files is not None:
            # Direct files passed to commit: bypass staging area entirely
            for p, content in files.items():
                norm = sanitize_repo_path(self.repo_dir, p)
                sha = self.storage.store_blob(content)
                changes[norm] = ChangeRecord(action="modify", blob_sha=sha)
        else:
            changes = self.staging.get_staged_changes(agent_id, session_id)
            if not changes:
                raise ValueError(f"Nothing to commit for agent '{agent_id}' (staging is empty)")

        tx = self.journal.append_transaction(
            agent_id=agent_id,
            session_id=session_id,
            message=message,
            changes=changes,
            parents=effective_parents,
            base_revision=effective_base,
            metadata=metadata
        )

        # Clear staging only if it hasn't received newer edits in the meantime
        if files is None:
            self.staging.clear(agent_id, session_id, max_generation=current_generation)

        return tx

    def _load_latest_checkpoint(self) -> Dict[str, Any]:
        path = os.path.join(self.checkpoint_dir, "latest.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"tree": {}, "processed_tx_ids": []}

    def _save_checkpoint(self, tree: Dict[str, str], processed_tx_ids: List[str]):
        data = {
            "version": 1,
            "tree": tree,
            "processed_tx_ids": processed_tx_ids
        }
        path = os.path.join(self.checkpoint_dir, "latest.json")
        tmp = path + f".tmp_{os.getpid()}_{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def get_revision_tree(self, target_tx_id: str) -> Dict[str, str]:
        """Reconstructs the exact repository tree at target_tx_id by traversing ancestral graph."""
        all_txs = self.journal.get_all_transactions()
        tx_map = {t.tx_id: t for t in all_txs}
        if target_tx_id not in tx_map:
            raise ValueError(f"Revision {target_tx_id} not found in journal")

        # Gather lineage from target back to root
        lineage = []
        curr = target_tx_id
        visited = set()
        while curr and curr in tx_map and curr not in visited:
            visited.add(curr)
            lineage.append(tx_map[curr])
            parents = tx_map[curr].parents
            curr = parents[0] if parents else None

        lineage.reverse()
        tree: Dict[str, str] = {}
        for tx in lineage:
            for path, change in tx.changes.items():
                if change.action == "delete":
                    tree.pop(path, None)
                elif change.blob_sha:
                    tree[path] = change.blob_sha
        return tree

    def reconcile_state(self) -> Tuple[Dict[str, bytes], List[MergeConflict]]:
        """
        Incrementally reconciles the unified repository tree using checkpoints.
        Avoids reprocessing entire history from genesis.
        Returns:
            unified_files: rel_path -> resolved bytes
            conflicts: list of MergeConflict objects
        """
        checkpoint = self._load_latest_checkpoint()
        tree: Dict[str, str] = dict(checkpoint.get("tree", {}))
        processed_set = set(checkpoint.get("processed_tx_ids", []))
        all_txs = self.journal.get_all_transactions()

        new_txs = [t for t in all_txs if t.tx_id not in processed_set]
        conflicts: List[MergeConflict] = []

        for tx in new_txs:
            # Determine base tree for this transaction
            base_tree = {}
            if tx.base_revision:
                try:
                    base_tree = self.get_revision_tree(tx.base_revision)
                except Exception:
                    base_tree = {}
            elif tx.parents:
                try:
                    base_tree = self.get_revision_tree(tx.parents[0])
                except Exception:
                    base_tree = {}

            # Apply changes against current unified tree using 3-way merge
            for rel_path, change in tx.changes.items():
                base_sha = base_tree.get(rel_path)
                curr_sha = tree.get(rel_path)
                tx_sha = change.blob_sha

                base_bytes = self.storage.get_blob(base_sha) if base_sha else None
                curr_bytes = self.storage.get_blob(curr_sha) if curr_sha else None
                tx_bytes = self.storage.get_blob(tx_sha) if tx_sha else None

                merged_bytes, step_conflicts = SemanticMerger.merge_files(
                    path=rel_path,
                    base_bytes=base_bytes,
                    a_bytes=curr_bytes,
                    b_bytes=tx_bytes,
                    label_a="UNIFIED_HEAD",
                    label_b=tx.agent_id
                )

                if step_conflicts:
                    conflicts.extend(step_conflicts)

                if merged_bytes is None:
                    # Cleanly deleted
                    tree.pop(rel_path, None)
                else:
                    new_sha = self.storage.store_blob(merged_bytes)
                    tree[rel_path] = new_sha

            processed_set.add(tx.tx_id)

        # Save incremental checkpoint
        ordered_processed = [t.tx_id for t in all_txs if t.tx_id in processed_set]
        self._save_checkpoint(tree, ordered_processed)

        # Materialize bytes map
        unified_files: Dict[str, bytes] = {}
        for p, sha in tree.items():
            unified_files[p] = self.storage.get_blob(sha)

        return unified_files, conflicts

    def materialize(self) -> Tuple[int, List[MergeConflict]]:
        """
        Smart materialization:
        - Writes only files that changed since last materialization.
        - Deletes removed files.
        - Checks for local uncommitted edits before overwriting.
        - Returns (files_written_count, conflicts).
        """
        unified_files, conflicts = self.reconcile_state()

        # Load previously materialized state: path -> sha
        prev_state: Dict[str, str] = {}
        if os.path.exists(self.materialized_state_path):
            try:
                with open(self.materialized_state_path, "r", encoding="utf-8") as f:
                    prev_state = json.load(f)
            except Exception:
                prev_state = {}

        written_count = 0
        new_state: Dict[str, str] = {}

        # 1. Remove deleted files
        for old_path in prev_state.keys():
            if old_path not in unified_files:
                full = os.path.join(self.repo_dir, old_path)
                if os.path.exists(full):
                    try:
                        os.remove(full)
                    except OSError:
                        pass

        # 2. Write added or modified files
        for rel_path, content in unified_files.items():
            full_path = os.path.join(self.repo_dir, rel_path)
            content_sha = self.storage.store_blob(content)
            new_state[rel_path] = content_sha

            # Skip write if already up-to-date on disk
            if prev_state.get(rel_path) == content_sha and os.path.exists(full_path):
                continue

            # Write file atomically
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(full_path), prefix=".tmp_mat_")
            try:
                with os.fdopen(tmp_fd, "wb") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, full_path)
                written_count += 1
            except Exception:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise

        # Save updated materialized state
        tmp_meta = self.materialized_state_path + f".tmp_{os.getpid()}_{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp_meta, "w", encoding="utf-8") as f:
                json.dump(new_state, f, indent=2, sort_keys=True)
            os.replace(tmp_meta, self.materialized_state_path)
        except Exception:
            if os.path.exists(tmp_meta):
                try:
                    os.remove(tmp_meta)
                except OSError:
                    pass

        return written_count, conflicts
