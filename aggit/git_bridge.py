"""
AgGit Git & GitHub Interoperability Bridge:
Enables exporting AgGit virtual revisions into native Git branches,
pushing to Git remotes, and creating GitHub Pull Requests via GitHub CLI (`gh`).
Guarantees:
- Pure dry-run preview without mutating state or checkpoints
- Full isolation: operations run in temporary git worktrees without mutating the user workspace
- Branch continuity: continues on existing export branch without resetting ancestry
- Strict preservation of unrelated Git files not touched by AgGit
- Deletions in Git are only executed for paths explicitly deleted in AgGit
- Blocks export when unresolved merge conflicts exist
- Import accurately preserves .github/, .gitignore, and .gitattributes
"""

import os
import re
import json
import uuid
import shutil
import hashlib
import tempfile
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from .engine import AgGitEngine
from .semantic_merge import SemanticMerger, MergeConflict
from .schema import ChangeRecord
from .path_utils import is_path_contained

@dataclass
class ExportPlan:
    revision_tx_id: str
    branch_name: str
    base_branch: str
    files_to_write: Dict[str, bytes]
    files_to_delete: List[str]
    conflicts: List[MergeConflict]
    is_clean: bool
    current_aggit_tree: Dict[str, str] = field(default_factory=dict)

@dataclass
class ExportResult:
    status: str  # "success", "no_changes", "cached", "conflict", "failed"
    branch_name: str
    git_commit_hash: Optional[str] = None
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

class GitBridge:
    def __init__(self, engine: AgGitEngine):
        self.engine = engine
        self.repo_dir = engine.repo_dir
        self.export_map_path = os.path.join(self.repo_dir, ".aggit", "git_export_map.json")

    def _load_export_map(self) -> Dict[str, Any]:
        if os.path.exists(self.export_map_path):
            try:
                with open(self.export_map_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_export_map(self, mapping: Dict[str, Any]):
        tmp = self.export_map_path + f".tmp_{os.getpid()}_{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(mapping, f, indent=2, sort_keys=True)
            os.replace(tmp, self.export_map_path)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def validate_branch_name(self, branch_name: str) -> str:
        """Sanitizes and validates branch name according to git check-ref-format rules."""
        clean = branch_name.strip()
        if not clean:
            raise ValueError("Branch name cannot be empty")
        if re.search(r"[\s~^:?*\[\\]", clean) or clean.startswith("-") or clean.endswith("/") or clean.endswith(".lock"):
            raise ValueError(f"Invalid branch name syntax: '{clean}'")
        return clean

    @staticmethod
    def _validate_safe_path(base_dir: str, rel_path: str) -> str:
        if not rel_path or not rel_path.strip():
            raise ValueError("Path cannot be empty or whitespace only")
        if "\0" in rel_path:
            raise ValueError(f"Null byte detected in path: '{rel_path}'")

        # Disallow Windows drive prefixes or UNC paths
        if ":" in rel_path or rel_path.startswith("\\\\") or rel_path.startswith("//"):
            raise ValueError(f"Absolute or drive/UNC paths not allowed: '{rel_path}'")

        norm = os.path.normpath(rel_path).replace("\\", "/")
        parts = [p for p in norm.split("/") if p]
        if any(part == ".." for part in parts) or norm.startswith("/") or os.path.isabs(norm):
            raise ValueError(f"Directory traversal detected: '{rel_path}'")

        abs_base = os.path.abspath(base_dir)
        full = os.path.abspath(os.path.join(abs_base, norm))

        # Check containment via component-wise canonical comparison
        if not is_path_contained(full, abs_base):
            raise ValueError(f"Path escapes worktree: '{rel_path}'")

        # Walk up from full to abs_base to ensure no intermediate symlink escapes
        curr = full
        base_norm = os.path.normcase(abs_base)
        while True:
            if os.path.islink(curr):
                real_target = os.path.realpath(curr)
                if not is_path_contained(real_target, abs_base):
                    raise ValueError(f"Symlink traversal detected: '{rel_path}' points outside worktree")
            parent = os.path.dirname(curr)
            if parent == curr or os.path.normcase(os.path.abspath(curr)) == base_norm:
                break
            curr = parent

        return full

    @staticmethod
    def _get_git_file_bytes(repo_dir: str, commit_or_branch: str, rel_path: str) -> Optional[bytes]:
        norm = rel_path.replace("\\", "/")
        proc = subprocess.run(
            ["git", "cat-file", "-p", f"{commit_or_branch}:{norm}"],
            cwd=repo_dir,
            capture_output=True
        )
        if proc.returncode == 0:
            return proc.stdout
        return None

    def prepare_export_plan(
        self,
        target_revision: Optional[str] = None,
        branch_name: str = "aggit/sync",
        base_branch: str = "main"
    ) -> ExportPlan:
        """
        Reconciles AgGit state in-memory WITHOUT modifying the working directory or checkpoints.
        Identifies exact delimited additions, modifications, deletions, and any conflicts.
        Preserves external changes made to untouched files on the destination branch.
        """
        valid_branch = self.validate_branch_name(branch_name)

        orig_read_only = getattr(self.engine.journal, "read_only", False)
        self.engine.journal.read_only = True
        try:
            if target_revision:
                tree_map = self.engine.get_revision_tree(target_revision)
                unified_files = {p: self.engine.storage.get_blob(sha) for p, sha in tree_map.items()}
                conflicts: List[MergeConflict] = []
                rev_id = target_revision
            else:
                unified_files, conflicts = self.engine.reconcile_state(dry_run=True)
                latest = self.engine.journal.get_latest_transaction()
                rev_id = latest.tx_id if latest else "genesis"
        finally:
            self.engine.journal.read_only = orig_read_only

        current_aggit_tree = {}
        for p, content in unified_files.items():
            header = f"blob {len(content)}\0".encode("utf-8")
            cas_sha = hashlib.sha256(header + content).hexdigest()
            current_aggit_tree[p] = cas_sha

        # Inspect current state in Git repository
        branch_check = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{valid_branch}"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True
        )
        branch_exists = (branch_check.returncode == 0)
        current_git_ref = valid_branch if branch_exists else base_branch

        git_files: Dict[str, str] = {}
        if os.path.exists(os.path.join(self.repo_dir, ".git")):
            ls_proc = subprocess.run(
                ["git", "ls-tree", "-r", "-z", current_git_ref],
                cwd=self.repo_dir,
                capture_output=True
            )
            if ls_proc.returncode == 0:
                raw_entries = ls_proc.stdout.split(b"\0")
                for entry in raw_entries:
                    if not entry:
                        continue
                    tab_idx = entry.find(b"\t")
                    if tab_idx != -1:
                        path_str = entry[tab_idx + 1:].decode("utf-8", errors="replace").replace("\\", "/")
                        parts = entry[:tab_idx].split()
                        blob_sha = parts[2].decode("ascii", errors="replace") if len(parts) >= 3 else ""
                        git_files[path_str] = blob_sha

        mapping = self._load_export_map()
        branch_state = mapping.get("branches", {}).get(valid_branch)

        files_to_write: Dict[str, bytes] = {}
        files_to_delete: List[str] = []

        if branch_state and "last_aggit_tree" in branch_state:
            last_aggit_tree = branch_state["last_aggit_tree"]
            last_git_commit = branch_state.get("last_git_commit")

            # Determine all paths touched by AgGit previously or currently
            # Untouched external files in git_files are naturally preserved
            all_candidate_paths = set(current_aggit_tree.keys()) | set(last_aggit_tree.keys())

            for p in all_candidate_paths:
                cur_aggit_bytes = unified_files.get(p)
                cur_aggit_sha = current_aggit_tree.get(p)
                last_aggit_sha = last_aggit_tree.get(p)
                last_aggit_bytes = self.engine.storage.get_blob(last_aggit_sha) if last_aggit_sha else None

                cur_git_bytes = self._get_git_file_bytes(self.repo_dir, current_git_ref, p) if p in git_files else None
                last_git_bytes = self._get_git_file_bytes(self.repo_dir, last_git_commit, p) if last_git_commit else None

                aggit_changed = (cur_aggit_sha != last_aggit_sha)
                git_changed = (cur_git_bytes != last_git_bytes) if last_git_commit else (cur_git_bytes is not None)

                # 1. Neither side changed
                if not aggit_changed and not git_changed:
                    continue

                # 2. Only Git changed externally: PRESERVE Git!
                if not aggit_changed and git_changed:
                    continue

                # 3. AgGit changed, Git tip did not change since last export
                if aggit_changed and not git_changed:
                    # Check if Git has previously integrated external changes relative to AgGit's base
                    if cur_git_bytes is not None and last_aggit_bytes is not None and cur_git_bytes != last_aggit_bytes:
                        if cur_aggit_bytes is None:
                            # AgGit deleted, but Git has integrated changes -> conflict!
                            conflicts.append(MergeConflict(
                                path=p,
                                conflict_type="modify_delete",
                                details=f"File '{p}' deleted in AgGit but has integrated Git modifications",
                                base_sha=last_aggit_sha,
                                a_sha=hashlib.sha256(cur_git_bytes).hexdigest(),
                                b_sha=None,
                                label_a=f"git_{valid_branch}",
                                label_b="aggit"
                            ))
                        else:
                            # 3-way merge applying AgGit's delta onto Git's integrated content
                            merged_bytes, step_conflicts = SemanticMerger.merge_files(
                                path=p,
                                base_bytes=last_aggit_bytes,
                                a_bytes=cur_git_bytes,
                                b_bytes=cur_aggit_bytes,
                                label_a=f"git_{valid_branch}",
                                label_b="aggit"
                            )
                            if step_conflicts:
                                conflicts.extend(step_conflicts)
                            elif merged_bytes is not None:
                                files_to_write[p] = merged_bytes
                    else:
                        # Clean unilateral AgGit change
                        if cur_aggit_bytes is None:
                            files_to_delete.append(p)
                        else:
                            files_to_write[p] = cur_aggit_bytes
                    continue

                # 4. Both sides changed concurrently (aggit_changed and git_changed)
                if cur_aggit_bytes is None and cur_git_bytes is None:
                    # Both deleted
                    continue

                if cur_aggit_bytes is None and cur_git_bytes is not None:
                    # Scenario A: AgGit deleted, human modified in Git
                    conflicts.append(MergeConflict(
                        path=p,
                        conflict_type="modify_delete",
                        details=f"File '{p}' deleted in AgGit but modified externally in Git",
                        base_sha=last_aggit_sha,
                        a_sha=hashlib.sha256(cur_git_bytes).hexdigest(),
                        b_sha=None,
                        label_a=f"git_{valid_branch}",
                        label_b="aggit"
                    ))
                    continue

                if cur_aggit_bytes is not None and cur_git_bytes is None:
                    # Scenario B: Human deleted in Git, AgGit modified
                    conflicts.append(MergeConflict(
                        path=p,
                        conflict_type="modify_delete",
                        details=f"File '{p}' modified in AgGit but deleted externally in Git",
                        base_sha=last_aggit_sha,
                        a_sha=None,
                        b_sha=cur_aggit_sha,
                        label_a=f"git_{valid_branch}",
                        label_b="aggit"
                    ))
                    continue

                # Both sides modified or added
                assert cur_aggit_bytes is not None and cur_git_bytes is not None
                if last_aggit_bytes is None and last_git_bytes is None:
                    # Scenario C: Both added concurrently without common base
                    if cur_aggit_bytes == cur_git_bytes:
                        files_to_write[p] = cur_aggit_bytes
                    else:
                        conflicts.append(MergeConflict(
                            path=p,
                            conflict_type="add_add_divergence",
                            details=f"File '{p}' added concurrently with differing content in Git and AgGit",
                            base_sha=None,
                            a_sha=hashlib.sha256(cur_git_bytes).hexdigest(),
                            b_sha=cur_aggit_sha,
                            label_a=f"git_{valid_branch}",
                            label_b="aggit"
                        ))
                else:
                    # 3-way merge with common base
                    base_bytes = last_aggit_bytes or last_git_bytes
                    merged_bytes, step_conflicts = SemanticMerger.merge_files(
                        path=p,
                        base_bytes=base_bytes,
                        a_bytes=cur_git_bytes,
                        b_bytes=cur_aggit_bytes,
                        label_a=f"git_{valid_branch}",
                        label_b="aggit"
                    )
                    if step_conflicts:
                        conflicts.extend(step_conflicts)
                    elif merged_bytes is not None:
                        files_to_write[p] = merged_bytes

        else:
            # First export to valid_branch:
            for p, cur_aggit_bytes in unified_files.items():
                cur_git_bytes = self._get_git_file_bytes(self.repo_dir, current_git_ref, p) if p in git_files else None
                if cur_git_bytes is None:
                    files_to_write[p] = cur_aggit_bytes
                else:
                    if cur_aggit_bytes == cur_git_bytes:
                        continue
                    else:
                        conflicts.append(MergeConflict(
                            path=p,
                            conflict_type="add_add_divergence",
                            details=f"File '{p}' exists in both Git and AgGit with divergent content without baseline",
                            base_sha=None,
                            a_sha=hashlib.sha256(cur_git_bytes).hexdigest(),
                            b_sha=hashlib.sha256(cur_aggit_bytes).hexdigest(),
                            label_a=f"git_{valid_branch}",
                            label_b="aggit"
                        ))

            # Check if any files from current_git_ref were explicitly deleted in AgGit
            deleted_in_aggit = set()
            for tx in self.engine.journal.get_all_transactions():
                for p, ch in tx.changes.items():
                    if ch.action == "delete":
                        deleted_in_aggit.add(p)
                    elif p in deleted_in_aggit:
                        deleted_in_aggit.remove(p)

            for del_path in deleted_in_aggit:
                if del_path in git_files and del_path not in unified_files:
                    files_to_delete.append(del_path)

        return ExportPlan(
            revision_tx_id=rev_id,
            branch_name=valid_branch,
            base_branch=base_branch,
            files_to_write=files_to_write,
            files_to_delete=files_to_delete,
            conflicts=conflicts,
            is_clean=len(conflicts) == 0,
            current_aggit_tree=current_aggit_tree
        )

    def preview_export(
        self,
        target_revision: Optional[str] = None,
        branch_name: str = "aggit/sync",
        base_branch: str = "main"
    ) -> Dict[str, Any]:
        """Dry-run preview that details export changes without mutating workspace or creating Git commits."""
        orig_read_only = getattr(self.engine.journal, "read_only", False)
        self.engine.journal.read_only = True
        try:
            plan = self.prepare_export_plan(target_revision, branch_name, base_branch)
            return {
                "revision_tx_id": plan.revision_tx_id,
                "target_branch": plan.branch_name,
                "base_branch": plan.base_branch,
                "is_clean": plan.is_clean,
                "files_to_write_count": len(plan.files_to_write),
                "files_to_write": sorted(list(plan.files_to_write.keys())),
                "files_to_delete_count": len(plan.files_to_delete),
                "files_to_delete": sorted(plan.files_to_delete),
                "conflicts_count": len(plan.conflicts),
                "conflicts": [{"path": c.path, "type": c.conflict_type, "details": c.details} for c in plan.conflicts]
            }
        finally:
            self.engine.journal.read_only = orig_read_only
        return {
            "revision_tx_id": plan.revision_tx_id,
            "target_branch": plan.branch_name,
            "base_branch": plan.base_branch,
            "is_clean": plan.is_clean,
            "files_to_write_count": len(plan.files_to_write),
            "files_to_write": sorted(list(plan.files_to_write.keys())),
            "files_to_delete_count": len(plan.files_to_delete),
            "files_to_delete": sorted(plan.files_to_delete),
            "conflicts_count": len(plan.conflicts),
            "conflicts": [{"path": c.path, "type": c.conflict_type, "details": c.details} for c in plan.conflicts]
        }

    def export_to_git(
        self,
        branch_name: str = "aggit/sync",
        base_branch: str = "main",
        commit_message: Optional[str] = None,
        target_revision: Optional[str] = None
    ) -> ExportResult:
        """
        Exports the AgGit revision to an isolated Git branch using a temporary worktree.
        Preserves branch continuity and ancestor history without reset.
        Never touches the user's main workspace files.
        """
        valid_branch = self.validate_branch_name(branch_name)

        # 1. Prepare in-memory plan
        plan = self.prepare_export_plan(target_revision, valid_branch, base_branch)
        if not plan.is_clean:
            return ExportResult(
                status="conflict",
                branch_name=valid_branch,
                message=f"Export blocked: {len(plan.conflicts)} unresolved merge conflicts.",
                details={"conflicts": [c.to_dict() for c in plan.conflicts]}
            )

        # 2. Ensure Git is initialized
        git_dir = os.path.join(self.repo_dir, ".git")
        if not os.path.exists(git_dir):
            subprocess.run(["git", "init"], cwd=self.repo_dir, check=True, capture_output=True)

        # Check if repo has any commits yet
        rev_check = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=self.repo_dir, capture_output=True)
        if rev_check.returncode != 0:
            # Empty repo: create initial commit on empty tree via git plumbing without touching user index/workspace
            empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
            subprocess.run(["git", "mktree"], input=b"", cwd=self.repo_dir, capture_output=True)
            c_proc = subprocess.run(["git", "commit-tree", empty_tree, "-m", "chore: initial repository structure"], cwd=self.repo_dir, capture_output=True, text=True)
            if c_proc.returncode == 0:
                root_commit = c_proc.stdout.strip()
                subprocess.run(["git", "update-ref", f"refs/heads/{base_branch}", root_commit], cwd=self.repo_dir, capture_output=True)
                subprocess.run(["git", "symbolic-ref", "HEAD", f"refs/heads/{base_branch}"], cwd=self.repo_dir, capture_output=True)

        # Check if the target branch already exists in Git
        branch_check = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{valid_branch}"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True
        )
        branch_exists = (branch_check.returncode == 0)

        # Determine parent commit on the target branch or base branch
        if branch_exists:
            current_branch_tip = branch_check.stdout.strip()
        else:
            base_check = subprocess.run(["git", "rev-parse", "--verify", base_branch], cwd=self.repo_dir, capture_output=True, text=True)
            current_branch_tip = base_check.stdout.strip() if base_check.returncode == 0 else ""

        # Check idempotent mapping ledger
        ledger_key = f"{plan.revision_tx_id}::{valid_branch}::{current_branch_tip}"
        mapping = self._load_export_map()
        if ledger_key in mapping:
            existing = mapping[ledger_key]
            cached_commit = existing.get("git_commit_hash")
            # Verify commit still exists in git object DB
            if cached_commit and subprocess.run(["git", "cat-file", "-e", cached_commit], cwd=self.repo_dir, capture_output=True).returncode == 0:
                return ExportResult(
                    status="cached",
                    branch_name=valid_branch,
                    git_commit_hash=cached_commit,
                    message="Revision already exported to this branch. Returning existing commit.",
                    details=existing
                )

        # Early return if no delta to write or delete
        if len(plan.files_to_write) == 0 and len(plan.files_to_delete) == 0:
            return ExportResult(
                status="no_changes",
                branch_name=valid_branch,
                git_commit_hash=current_branch_tip,
                message="No delta between target revision and last export. Working tree already in sync."
            )

        # 3. Create isolated worktree for the target branch
        temp_worktree = tempfile.mkdtemp(prefix="aggit_worktree_")
        try:
            if not branch_exists:
                # Create branch from base_branch
                add_wt_cmd = ["git", "worktree", "add", "-b", valid_branch, temp_worktree, base_branch]
                wt_proc = subprocess.run(add_wt_cmd, cwd=self.repo_dir, capture_output=True, text=True, encoding="utf-8")
                if wt_proc.returncode != 0:
                    # Fallback without base_branch
                    add_wt_cmd2 = ["git", "worktree", "add", "-b", valid_branch, temp_worktree]
                    wt_proc2 = subprocess.run(add_wt_cmd2, cwd=self.repo_dir, capture_output=True, text=True, encoding="utf-8")
                    if wt_proc2.returncode != 0:
                        raise RuntimeError(f"Failed to create Git worktree: {wt_proc2.stderr.strip()}")
            else:
                # Branch already exists: checkout directly to continue history!
                add_wt_cmd = ["git", "worktree", "add", temp_worktree, valid_branch]
                wt_proc = subprocess.run(add_wt_cmd, cwd=self.repo_dir, capture_output=True, text=True, encoding="utf-8")
                if wt_proc.returncode != 0:
                    raise RuntimeError(f"Failed to checkout Git worktree on existing branch: {wt_proc.stderr.strip()}")

            # 4. Apply deletions safely
            for del_path in plan.files_to_delete:
                full_del = self._validate_safe_path(temp_worktree, del_path)
                if os.path.exists(full_del):
                    if os.path.isdir(full_del):
                        shutil.rmtree(full_del, ignore_errors=True)
                    else:
                        try:
                            os.remove(full_del)
                        except OSError:
                            pass
                subprocess.run(["git", "rm", "-f", "--ignore-unmatch", "--", del_path], cwd=temp_worktree, capture_output=True)

            # 5. Apply additions and modifications safely
            staged_paths: List[str] = []
            for rel_path, content in plan.files_to_write.items():
                target_file = self._validate_safe_path(temp_worktree, rel_path)
                os.makedirs(os.path.dirname(target_file), exist_ok=True)
                with open(target_file, "wb") as f:
                    f.write(content)
                staged_paths.append(rel_path)

            # Stage only planned files
            if staged_paths:
                chunk_size = 50
                for i in range(0, len(staged_paths), chunk_size):
                    chunk = staged_paths[i:i + chunk_size]
                    subprocess.run(["git", "add", "--"] + chunk, cwd=temp_worktree, check=True, capture_output=True)

            # Check for concurrent modification on destination branch before committing (CAS)
            if branch_exists:
                tip_check = subprocess.run(
                    ["git", "rev-parse", "--verify", f"refs/heads/{valid_branch}"],
                    cwd=self.repo_dir,
                    capture_output=True,
                    text=True
                )
                current_tip_now = tip_check.stdout.strip() if tip_check.returncode == 0 else ""
                if current_tip_now != current_branch_tip:
                    raise RuntimeError(f"Concurrent modification detected on '{valid_branch}': branch tip moved during export")

            # 6. Create Git commit
            msg = commit_message or f"chore(aggit): exported revision {plan.revision_tx_id[:8]} to {valid_branch}"
            commit_proc = subprocess.run(
                ["git", "commit", "-m", msg],
                cwd=temp_worktree,
                capture_output=True,
                text=True,
                encoding="utf-8"
            )

            if commit_proc.returncode != 0:
                if "nothing to commit" in commit_proc.stdout.lower() or "working tree clean" in commit_proc.stdout.lower():
                    rev_hash = subprocess.run(["git", "rev-parse", "HEAD"], cwd=temp_worktree, capture_output=True, text=True).stdout.strip()
                    return ExportResult(
                        status="no_changes",
                        branch_name=valid_branch,
                        git_commit_hash=rev_hash,
                        message="Working tree already matches target revision. No new commit needed."
                    )
                raise RuntimeError(f"Git commit failed: {commit_proc.stderr.strip()}")

            rev_hash = subprocess.run(["git", "rev-parse", "HEAD"], cwd=temp_worktree, capture_output=True, text=True).stdout.strip()

            # Record in idempotent export mapping ledger and update branch tracking
            mapping = self._load_export_map()
            branches_map = mapping.setdefault("branches", {})
            branches_map[valid_branch] = {
                "last_aggit_tree": plan.current_aggit_tree,
                "last_git_commit": rev_hash,
                "last_aggit_revision": plan.revision_tx_id
            }
            mapping[ledger_key] = {
                "revision_tx_id": plan.revision_tx_id,
                "branch_name": valid_branch,
                "git_commit_hash": rev_hash,
                "parent_git_commit": current_branch_tip
            }
            self._save_export_map(mapping)

            return ExportResult(
                status="success",
                branch_name=valid_branch,
                git_commit_hash=rev_hash,
                message=f"Exported revision {plan.revision_tx_id[:8]} to Git branch '{valid_branch}' (commit: {rev_hash[:8]})",
                details={"commit_hash": rev_hash}
            )

        finally:
            # Safely remove worktree
            subprocess.run(["git", "worktree", "remove", "--force", temp_worktree], cwd=self.repo_dir, capture_output=True)
            if os.path.exists(temp_worktree):
                shutil.rmtree(temp_worktree, ignore_errors=True)

    def push_to_remote(self, branch_name: str, remote_name: str = "origin", remote: Optional[str] = None) -> Dict[str, Any]:
        """Pushes an exported branch to a Git remote."""
        target_remote = remote or remote_name
        valid_branch = self.validate_branch_name(branch_name)
        remote_check = subprocess.run(["git", "remote", "get-url", target_remote], cwd=self.repo_dir, capture_output=True, text=True)
        if remote_check.returncode != 0:
            return {
                "success": False,
                "status": "skipped",
                "message": f"Remote '{target_remote}' is not configured in this Git repository.",
                "action_required": f"Run 'git remote add {target_remote} <url>' to configure."
            }

        push_cmd = ["git", "push", "-u", target_remote, f"{valid_branch}:{valid_branch}"]
        proc = subprocess.run(push_cmd, cwd=self.repo_dir, capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            return {
                "success": False,
                "status": "failed",
                "error": proc.stderr.strip(),
                "message": f"Git push to '{target_remote}/{valid_branch}' failed."
            }

        return {
            "success": True,
            "status": "pushed",
            "remote": target_remote,
            "branch": valid_branch,
            "message": f"Successfully pushed branch '{valid_branch}' to remote '{target_remote}'."
        }

    def create_or_update_pr(
        self,
        branch_name: str,
        base_branch: str = "main",
        title: Optional[str] = None,
        body: Optional[str] = None
    ) -> Dict[str, Any]:
        """Creates or updates a GitHub Pull Request via GitHub CLI (`gh`)."""
        valid_branch = self.validate_branch_name(branch_name)
        gh_check = subprocess.run(["gh", "--version"], capture_output=True, text=True)
        if gh_check.returncode != 0:
            return {
                "status": "gh_not_installed",
                "message": "GitHub CLI (`gh`) is not installed or not in PATH.",
                "action_required": "Install `gh` (e.g. `winget install GitHub.cli` or `brew install gh`) or open PR via github.com."
            }

        check_pr_cmd = ["gh", "pr", "view", valid_branch, "--json", "number,url,state"]
        check_proc = subprocess.run(check_pr_cmd, cwd=self.repo_dir, capture_output=True, text=True)

        pr_title = title or f"feat(aggit): sync changes from {valid_branch}"
        pr_body = body or f"Automated Pull Request synchronizing AgGit revisions from branch `{valid_branch}` into `{base_branch}`."

        if check_proc.returncode == 0:
            try:
                pr_data = json.loads(check_proc.stdout)
                pr_url = pr_data.get("url")
                return {
                    "status": "already_exists",
                    "pr_url": pr_url,
                    "message": f"Pull Request for '{valid_branch}' already exists: {pr_url}"
                }
            except Exception:
                pass

        create_cmd = [
            "gh", "pr", "create",
            "--head", valid_branch,
            "--base", base_branch,
            "--title", pr_title,
            "--body", pr_body
        ]
        create_proc = subprocess.run(create_cmd, cwd=self.repo_dir, capture_output=True, text=True)
        if create_proc.returncode != 0:
            return {
                "status": "failed",
                "error": create_proc.stderr.strip(),
                "message": f"Failed to create Pull Request via `gh`: {create_proc.stderr.strip()}"
            }

        pr_url = create_proc.stdout.strip()
        return {
            "status": "created",
            "pr_url": pr_url,
            "message": f"Pull Request successfully created: {pr_url}"
        }

    def import_from_git(self, commit_ref: str = "HEAD") -> str:
        """
        Imports a Git commit snapshot into AgGit as a baseline revision.
        Enables bootstrapping an AgGit multi-agent session from a known Git state.
        Preserves .github/, .gitignore, and .gitattributes by exact directory matching.
        """
        rev_proc = subprocess.run(["git", "rev-parse", commit_ref], cwd=self.repo_dir, capture_output=True, text=True)
        if rev_proc.returncode != 0:
            raise ValueError(f"Invalid Git commit or ref: '{commit_ref}'")
        full_commit_sha = rev_proc.stdout.strip()

        tree_proc = subprocess.run(["git", "ls-tree", "-r", "-z", full_commit_sha], cwd=self.repo_dir, capture_output=True)
        if tree_proc.returncode != 0:
            raise RuntimeError(f"Failed to read Git tree for {full_commit_sha}")

        changes = {}
        entries = tree_proc.stdout.split(b"\0")
        for entry in entries:
            if not entry:
                continue
            tab_idx = entry.find(b"\t")
            if tab_idx == -1:
                continue
            path_bytes = entry[tab_idx + 1:]
            rel_path = path_bytes.decode("utf-8", errors="replace")

            # Check exact directory components, not startswith('.git')
            parts = rel_path.replace("\\", "/").split("/")
            if parts[0] in {".git", ".aggit"}:
                continue

            cat_proc = subprocess.run(["git", "cat-file", "-p", f"{full_commit_sha}:{rel_path}"], cwd=self.repo_dir, capture_output=True)
            if cat_proc.returncode == 0:
                blob_sha = self.engine.storage.store_blob(cat_proc.stdout)
                changes[rel_path] = ChangeRecord(action="add", blob_sha=blob_sha)

        tx = self.engine.journal.append_transaction(
            agent_id="git_importer",
            message=f"chore(aggit): imported baseline snapshot from Git commit {full_commit_sha[:8]}",
            changes=changes,
            parents=[],
            base_revision=None,
            metadata={"git_commit_sha": full_commit_sha}
        )

        return tx.tx_id
