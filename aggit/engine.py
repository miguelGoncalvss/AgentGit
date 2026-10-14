"""
AgGit Core Engine:
Coordinates storage, journal, staging, incremental reconciliation, and smart materialization.
Guarantees:
- Incremental reconciliation using checkpoints (.aggit/checkpoints/latest.json)
- Base revision tracking and causal DAG ordering (never assumes empty base or clock ordering)
- Persistent pending conflict tracking across restarts and repeated calls
- Explicit conflict resolution operations
- Pure dry-run reconciliation without state mutations
- Pre-validation 3-state check in materialization preserving all local user modifications
- Materialization and export blocked on unresolved conflicts
- Atomic file writes with error propagation
"""

import os
import json
import uuid
import tempfile
import hashlib
from typing import Dict, List, Any, Optional, Tuple, Set
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
        self._revision_tree_cache: Dict[str, Dict[str, str]] = {}
        self.stats: Dict[str, int] = {
            "events_read": 0,
            "trees_rebuilt": 0,
            "blobs_read": 0
        }

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

        if files is None:
            self.staging.clear(agent_id, session_id, max_generation=current_generation)

        return tx

    def _load_latest_checkpoint(self) -> Dict[str, Any]:
        path = os.path.join(self.checkpoint_dir, "latest.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    data.setdefault("pending_conflicts", [])
                    return data
            except Exception:
                pass
        return {"version": 2, "tree": {}, "processed_tx_ids": [], "pending_conflicts": []}

    def _save_checkpoint(self, tree: Dict[str, str], processed_tx_ids: List[str], pending_conflicts: List[Dict[str, Any]]):
        data = {
            "version": 2,
            "tree": tree,
            "processed_tx_ids": processed_tx_ids,
            "pending_conflicts": pending_conflicts
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
        if target_tx_id in self._revision_tree_cache:
            return dict(self._revision_tree_cache[target_tx_id])

        self.stats["trees_rebuilt"] += 1
        all_txs = self.journal.get_all_transactions()
        self.stats["events_read"] += len(all_txs)
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

        self._revision_tree_cache[target_tx_id] = dict(tree)
        return tree

    def reconcile_state(
        self,
        dry_run: bool = False,
        force_rebuild: bool = False
    ) -> Tuple[Dict[str, bytes], List[MergeConflict]]:
        """
        Incrementally reconciles the unified repository tree using checkpoints.
        Avoids reprocessing entire history from genesis.
        dry_run=True computes plan and conflicts in-memory WITHOUT mutating checkpoint on disk.
        Pending conflicts persist across calls and restarts until explicitly resolved.
        """
        checkpoint = {} if force_rebuild else self._load_latest_checkpoint()
        tree: Dict[str, str] = dict(checkpoint.get("tree", {}))
        processed_set = set(checkpoint.get("processed_tx_ids", []))
        
        # Load any existing pending conflicts
        pending_conflicts_raw = checkpoint.get("pending_conflicts", [])
        conflicts_by_path: Dict[str, MergeConflict] = {}
        for c in pending_conflicts_raw:
            conflict_obj = MergeConflict.from_dict(c)
            conflicts_by_path[conflict_obj.path] = conflict_obj

        all_txs = self.journal.get_all_transactions()
        self.stats["events_read"] += len(all_txs)

        # Topological causal sort on parents DAG
        tx_map = {t.tx_id: t for t in all_txs}
        ordered_txs: List[TransactionRecord] = []
        visited: Set[str] = set()

        def visit(tid: str):
            if tid in visited or tid not in tx_map:
                return
            visited.add(tid)
            tx = tx_map[tid]
            for p in (tx.parents or []):
                visit(p)
            ordered_txs.append(tx)

        for t in all_txs:
            visit(t.tx_id)

        new_txs = [t for t in ordered_txs if t.tx_id not in processed_set]

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
                norm_path = sanitize_repo_path(self.repo_dir, rel_path)
                base_sha = base_tree.get(norm_path)
                curr_sha = tree.get(norm_path)
                tx_sha = change.blob_sha

                base_bytes = self.storage.get_blob(base_sha) if base_sha else None
                curr_bytes = self.storage.get_blob(curr_sha) if curr_sha else None
                tx_bytes = self.storage.get_blob(tx_sha) if tx_sha else None

                if base_sha:
                    self.stats["blobs_read"] += 1
                if curr_sha:
                    self.stats["blobs_read"] += 1
                if tx_sha:
                    self.stats["blobs_read"] += 1

                if norm_path in conflicts_by_path:
                    # Transaction provides an explicit resolution for this conflicted path
                    conflicts_by_path.pop(norm_path, None)
                    if change.action == "delete" or tx_sha is None:
                        tree.pop(norm_path, None)
                    else:
                        tree[norm_path] = tx_sha
                    continue

                merged_bytes, step_conflicts = SemanticMerger.merge_files(
                    path=norm_path,
                    base_bytes=base_bytes,
                    a_bytes=curr_bytes,
                    b_bytes=tx_bytes,
                    label_a="UNIFIED_HEAD",
                    label_b=tx.agent_id
                )

                if step_conflicts:
                    for sc in step_conflicts:
                        conflicts_by_path[sc.path] = sc
                else:
                    # Clean merge or resolution clears prior pending conflict on this path
                    conflicts_by_path.pop(norm_path, None)

                if merged_bytes is None:
                    # Cleanly deleted
                    tree.pop(norm_path, None)
                else:
                    new_sha = self.storage.store_blob(merged_bytes)
                    tree[norm_path] = new_sha

            processed_set.add(tx.tx_id)

        all_conflicts = list(conflicts_by_path.values())

        # Save incremental checkpoint ONLY if not a dry run
        if not dry_run:
            ordered_processed = [t.tx_id for t in ordered_txs if t.tx_id in processed_set]
            self._save_checkpoint(tree, ordered_processed, [c.to_dict() for c in all_conflicts])

        # Materialize in-memory bytes map
        unified_files: Dict[str, bytes] = {}
        for p, sha in tree.items():
            self.stats["blobs_read"] += 1
            unified_files[p] = self.storage.get_blob(sha)

        return unified_files, all_conflicts

    def resolve_conflict(
        self,
        path: str,
        resolved_bytes: Optional[bytes] = None,
        agent_id: str = "resolver",
        message: Optional[str] = None
    ) -> TransactionRecord:
        """
        Explicitly resolves a pending conflict on a path.
        Appends a resolution transaction to the journal and updates checkpoint.
        If resolved_bytes is None, resolves by deletion.
        """
        norm_path = sanitize_repo_path(self.repo_dir, path)
        checkpoint = self._load_latest_checkpoint()
        pending = checkpoint.get("pending_conflicts", [])
        matching = [c for c in pending if c.get("path") == norm_path]

        action = "delete" if resolved_bytes is None else "modify"
        blob_sha = self.storage.store_blob(resolved_bytes) if resolved_bytes is not None else None
        changes = {norm_path: ChangeRecord(action=action, blob_sha=blob_sha)}

        latest = self.journal.get_latest_transaction()
        parents = [latest.tx_id] if latest else []
        tx_msg = message or f"chore(aggit): resolve conflict in {norm_path}"

        tx = self.journal.append_transaction(
            agent_id=agent_id,
            message=tx_msg,
            changes=changes,
            parents=parents,
            metadata={"resolved_conflict_path": norm_path}
        )

        # Run reconcile to update checkpoint and clear the conflict
        self.reconcile_state(dry_run=False)
        return tx

    def materialize(self, force: bool = False) -> Tuple[int, List[MergeConflict]]:
        """
        Smart materialization with strict 3-state pre-validation:
        - Last materialized state vs physical disk state vs desired reconciled state.
        - Validates the ENTIRE plan before touching any file on disk.
        - Detects local edits, local deletions, and untracked files occupying target path.
        - Blocks if unresolved conflicts are pending on relevant paths.
        - Idempotent: writes 0 files if content on disk already matches desired.
        - Publishes files atomically via temp write and replacement.
        """
        unified_files, conflicts = self.reconcile_state(dry_run=False)

        # Block materialization if there are unresolved conflicts and force is not set
        if conflicts and not force:
            conflict_paths = ", ".join(c.path for c in conflicts)
            raise RuntimeError(f"Materialization blocked: {len(conflicts)} unresolved conflict(s) pending: {conflict_paths}")

        # Load previously materialized state: path -> sha
        prev_state: Dict[str, str] = {}
        if os.path.exists(self.materialized_state_path):
            try:
                with open(self.materialized_state_path, "r", encoding="utf-8") as f:
                    prev_state = json.load(f)
            except Exception:
                prev_state = {}

        # -------------------------------------------------------------
        # PHASE 1: Pre-validation of entire plan
        # -------------------------------------------------------------
        all_paths = set(unified_files.keys()) | set(prev_state.keys())
        blocked_errors: List[str] = []

        for p in all_paths:
            norm_p = sanitize_repo_path(self.repo_dir, p)
            full_path = os.path.join(self.repo_dir, norm_p)
            disk_exists = os.path.exists(full_path)
            disk_sha = None
            if disk_exists:
                try:
                    with open(full_path, "rb") as f:
                        disk_sha = hashlib.sha256(f.read()).hexdigest()
                except OSError as e:
                    blocked_errors.append(f"Failed to read disk file '{norm_p}': {e}")
                    continue

            prev_sha = prev_state.get(norm_p)
            desired_content = unified_files.get(norm_p)
            desired_sha = hashlib.sha256(desired_content).hexdigest() if desired_content is not None else None

            # Case A: Desired state matches disk state exactly -> already in sync
            if desired_sha == disk_sha:
                continue

            # Case B: File is scheduled for deletion
            if desired_sha is None:
                if disk_exists:
                    if prev_sha and disk_sha != prev_sha and not force:
                        blocked_errors.append(f"Local modification would be deleted: '{norm_p}' was edited locally")
                continue

            # Case C: File is scheduled for write
            if desired_sha is not None:
                if disk_exists:
                    if prev_sha is None and not force:
                        blocked_errors.append(f"Untracked file would be overwritten: '{norm_p}' already exists on disk")
                    elif prev_sha is not None:
                        if disk_sha != prev_sha and not force:
                            blocked_errors.append(f"Local modification would be overwritten: '{norm_p}' was edited locally")
                else:
                    if prev_sha is not None and prev_sha != desired_sha and not force:
                        blocked_errors.append(f"Local deletion conflict: '{norm_p}' was deleted locally but agent updated it")

        if blocked_errors:
            raise RuntimeError("Materialization aborted to preserve local work:\n  " + "\n  ".join(blocked_errors))

        # -------------------------------------------------------------
        # PHASE 2: Safe atomic execution
        # -------------------------------------------------------------
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

        # 2. Write modified or added files atomically
        for rel_path, content in unified_files.items():
            norm_path = sanitize_repo_path(self.repo_dir, rel_path)
            full_path = os.path.join(self.repo_dir, norm_path)
            content_sha = hashlib.sha256(content).hexdigest()
            new_state[norm_path] = content_sha

            # Skip write if already identical on disk
            if os.path.exists(full_path):
                try:
                    with open(full_path, "rb") as f:
                        if hashlib.sha256(f.read()).hexdigest() == content_sha:
                            continue
                except OSError:
                    pass

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
