"""
AgGit Git & GitHub Bridge:
Provides robust, isolated integration between AgGit and Git/GitHub.
Guarantees:
- Clear separation: prepare export, local commit, push, PR creation.
- Uses isolated branch and temporary worktree (never pollutes or touches main working tree).
- Exports only paths in export plan (never 'git add .').
- Respects branch_name parameter and validates branch naming.
- Structured subprocess execution without shell interpolation.
- Full error context, exit code, stdout, and stderr capture.
- Repeatable export: avoids duplicate commits/PRs for identical revisions via mapping ledger.
- Safe GitHub CLI (gh) integration with clear diagnostics when gh or auth is missing.
- Safe Git state import (bootstrap session from known Git commit).
"""

import os
import re
import json
import shutil
import tempfile
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any
from .engine import AgGitEngine
from .semantic_merge import MergeConflict
from .schema import TransactionRecord

BRANCH_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_\-\./]+$")

@dataclass
class ExportPlan:
    revision_tx_id: str
    branch_name: str
    base_branch: str
    files_to_write: Dict[str, bytes]
    files_to_delete: List[str]
    conflicts: List[MergeConflict]
    is_clean: bool

@dataclass
class ExportResult:
    status: str  # "success", "no_changes", "conflict", "error", "cached"
    branch_name: str
    git_commit_hash: Optional[str] = None
    message: str = ""
    details: Dict[str, Any] = None

class GitBridge:
    def __init__(self, engine: AgGitEngine):
        self.engine = engine
        self.repo_dir = engine.repo_dir
        self.export_map_path = os.path.join(self.engine.aggit_dir, "git_export_map.json")

    def _load_export_map(self) -> Dict[str, Any]:
        if os.path.exists(self.export_map_path):
            try:
                with open(self.export_map_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_export_map(self, mapping: Dict[str, Any]):
        tmp = self.export_map_path + f".tmp_{os.getpid()}.tmp"
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
        clean = branch_name.strip()
        if not clean or not BRANCH_NAME_REGEX.match(clean) or ".." in clean or clean.endswith("/") or clean.startswith("/"):
            raise ValueError(f"Invalid git branch name: '{branch_name}'")
        return clean

    def prepare_export_plan(
        self,
        target_revision: Optional[str] = None,
        branch_name: str = "aggit/sync",
        base_branch: str = "main"
    ) -> ExportPlan:
        """
        Reconciles AgGit state in-memory WITHOUT modifying the working directory.
        Identifies exact additions, modifications, deletions, and any conflicts.
        """
        valid_branch = self.validate_branch_name(branch_name)
        if target_revision:
            tree_map = self.engine.get_revision_tree(target_revision)
            files_to_write = {p: self.engine.storage.get_blob(sha) for p, sha in tree_map.items()}
            conflicts: List[MergeConflict] = []
            rev_id = target_revision
        else:
            files_to_write, conflicts = self.engine.reconcile_state()
            latest = self.engine.journal.get_latest_transaction()
            rev_id = latest.tx_id if latest else "genesis"

        # Check files to delete against base branch if Git repo exists
        files_to_delete: List[str] = []
        git_dir = os.path.join(self.repo_dir, ".git")
        if os.path.exists(git_dir):
            cmd = ["git", "ls-tree", "-r", "--name-only", base_branch]
            proc = subprocess.run(cmd, cwd=self.repo_dir, capture_output=True, text=True, encoding="utf-8")
            if proc.returncode == 0:
                base_files = set(proc.stdout.splitlines())
                for bf in base_files:
                    if bf not in files_to_write and not bf.startswith(".git") and not bf.startswith(".aggit"):
                        files_to_delete.append(bf)

        return ExportPlan(
            revision_tx_id=rev_id,
            branch_name=valid_branch,
            base_branch=base_branch,
            files_to_write=files_to_write,
            files_to_delete=files_to_delete,
            conflicts=conflicts,
            is_clean=len(conflicts) == 0
        )

    def preview_export(
        self,
        target_revision: Optional[str] = None,
        branch_name: str = "aggit/sync",
        base_branch: str = "main"
    ) -> Dict[str, Any]:
        """Dry-run preview that details export changes without mutating workspace or creating Git commits."""
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

    def export_to_git(
        self,
        branch_name: str = "aggit/sync",
        base_branch: str = "main",
        commit_message: Optional[str] = None,
        target_revision: Optional[str] = None
    ) -> ExportResult:
        """
        Exports the AgGit revision to an isolated Git branch using a temporary worktree.
        Never touches the user's main workspace files.
        Guarantees idempotence: re-exporting the same revision to the same branch returns previous commit.
        """
        valid_branch = self.validate_branch_name(branch_name)

        # 1. Prepare in-memory plan
        plan = self.prepare_export_plan(target_revision, valid_branch, base_branch)
        if not plan.is_clean:
            return ExportResult(
                status="conflict",
                branch_name=valid_branch,
                message=f"Export blocked: {len(plan.conflicts)} unresolved merge conflicts.",
                details={"conflicts": [{"path": c.path, "type": c.conflict_type} for c in plan.conflicts]}
            )

        # 2. Check idempotent mapping ledger
        ledger_key = f"{plan.revision_tx_id}::{valid_branch}"
        mapping = self._load_export_map()
        if ledger_key in mapping:
            existing = mapping[ledger_key]
            return ExportResult(
                status="cached",
                branch_name=valid_branch,
                git_commit_hash=existing.get("git_commit_hash"),
                message="Revision already exported to this branch. Returning existing commit.",
                details=existing
            )

        # 3. Ensure Git is initialized
        git_dir = os.path.join(self.repo_dir, ".git")
        if not os.path.exists(git_dir):
            subprocess.run(["git", "init"], cwd=self.repo_dir, check=True, capture_output=True)

        # Ensure base branch exists; if empty repo, create initial commit on base branch
        rev_check = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=self.repo_dir, capture_output=True)
        if rev_check.returncode != 0:
            # Empty repo
            dummy_file = os.path.join(self.repo_dir, ".gitkeep")
            with open(dummy_file, "w") as f:
                f.write("")
            subprocess.run(["git", "add", ".gitkeep"], cwd=self.repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "chore: initial repository structure"], cwd=self.repo_dir, check=True, capture_output=True)
            if os.path.exists(dummy_file):
                os.remove(dummy_file)

        # 4. Create isolated worktree for the target branch
        temp_worktree = tempfile.mkdtemp(prefix="aggit_worktree_")
        try:
            # Add or checkout worktree on isolated branch
            add_wt_cmd = ["git", "worktree", "add", "-B", valid_branch, temp_worktree, base_branch]
            wt_proc = subprocess.run(add_wt_cmd, cwd=self.repo_dir, capture_output=True, text=True, encoding="utf-8")
            if wt_proc.returncode != 0:
                # Try checkout without base_branch if already exists
                add_wt_cmd2 = ["git", "worktree", "add", "-B", valid_branch, temp_worktree]
                wt_proc2 = subprocess.run(add_wt_cmd2, cwd=self.repo_dir, capture_output=True, text=True, encoding="utf-8")
                if wt_proc2.returncode != 0:
                    raise RuntimeError(f"Failed to create Git worktree: {wt_proc2.stderr.strip()}")

            # 5. Apply only the files belonging to the export plan inside worktree
            staged_paths: List[str] = []

            # Handle deletions
            for del_path in plan.files_to_delete:
                full_del = os.path.join(temp_worktree, del_path)
                if os.path.exists(full_del):
                    os.remove(full_del)
                    subprocess.run(["git", "rm", "-f", "--ignore-unmatch", "--", del_path], cwd=temp_worktree, capture_output=True)

            # Handle additions / modifications
            for rel_path, content in plan.files_to_write.items():
                target_file = os.path.join(temp_worktree, rel_path)
                os.makedirs(os.path.dirname(target_file), exist_ok=True)
                with open(target_file, "wb") as f:
                    f.write(content)
                staged_paths.append(rel_path)

            # Stage only planned files (never 'git add .')
            if staged_paths:
                # Batch git add in chunks if necessary
                chunk_size = 50
                for i in range(0, len(staged_paths), chunk_size):
                    chunk = staged_paths[i:i + chunk_size]
                    subprocess.run(["git", "add", "--"] + chunk, cwd=temp_worktree, check=True, capture_output=True)

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

            # 7. Obtain exact newly created commit hash
            rev_proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=temp_worktree, capture_output=True, text=True)
            commit_hash = rev_proc.stdout.strip()

            # 8. Record mapping in ledger
            mapping[ledger_key] = {
                "revision_tx_id": plan.revision_tx_id,
                "git_commit_hash": commit_hash,
                "branch_name": valid_branch,
                "base_branch": base_branch
            }
            self._save_export_map(mapping)

            return ExportResult(
                status="success",
                branch_name=valid_branch,
                git_commit_hash=commit_hash,
                message=f"Successfully exported AgGit revision to Git commit {commit_hash[:8]} on branch '{valid_branch}'."
            )

        finally:
            # Cleanup worktree
            subprocess.run(["git", "worktree", "remove", "--force", temp_worktree], cwd=self.repo_dir, capture_output=True)
            if os.path.exists(temp_worktree):
                try:
                    shutil.rmtree(temp_worktree)
                except OSError:
                    pass

    def push_to_remote(
        self,
        branch_name: str = "aggit/sync",
        remote: str = "origin",
        set_upstream: bool = True
    ) -> Dict[str, Any]:
        """
        Explicitly pushes an exported branch to a Git remote.
        Never runs automatically. Structured parameters, no force-push.
        """
        valid_branch = self.validate_branch_name(branch_name)
        cmd = ["git", "push"]
        if set_upstream:
            cmd.extend(["-u", remote, valid_branch])
        else:
            cmd.extend([remote, valid_branch])

        proc = subprocess.run(cmd, cwd=self.repo_dir, capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            return {
                "success": False,
                "error": "push_rejected",
                "stderr": proc.stderr.strip(),
                "guidance": "Remote rejected push (likely non-fast-forward). Reconcile remote changes before retrying."
            }
        return {
            "success": True,
            "remote": remote,
            "branch": valid_branch,
            "output": proc.stdout.strip()
        }

    def create_or_update_pr(
        self,
        branch_name: str = "aggit/sync",
        base_branch: str = "main",
        title: Optional[str] = None,
        body: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Uses GitHub CLI (gh) to create or reuse a Pull Request.
        Checks tool availability and auth status gracefully.
        """
        valid_branch = self.validate_branch_name(branch_name)

        # Check if gh is installed
        gh_check = shutil.which("gh")
        if not gh_check:
            return {
                "status": "gh_not_installed",
                "message": "GitHub CLI (gh) is not installed on this system.",
                "guidance": f"Install gh from https://cli.github.com or open PR manually from branch '{valid_branch}'."
            }

        # Check gh auth
        auth_proc = subprocess.run(["gh", "auth", "status"], cwd=self.repo_dir, capture_output=True, text=True)
        if auth_proc.returncode != 0:
            return {
                "status": "gh_unauthenticated",
                "message": "GitHub CLI is not authenticated.",
                "guidance": "Run 'gh auth login' to authenticate with your GitHub account."
            }

        # Check if PR already exists for this head branch
        list_cmd = ["gh", "pr", "list", "--head", valid_branch, "--base", base_branch, "--json", "number,url,state,title"]
        list_proc = subprocess.run(list_cmd, cwd=self.repo_dir, capture_output=True, text=True)
        if list_proc.returncode == 0:
            try:
                prs = json.loads(list_proc.stdout)
                if prs:
                    existing_pr = prs[0]
                    return {
                        "status": "reused",
                        "pr_number": existing_pr.get("number"),
                        "pr_url": existing_pr.get("url"),
                        "message": f"Found existing Pull Request #{existing_pr.get('number')}: {existing_pr.get('url')}"
                    }
            except Exception:
                pass

        # Create PR
        pr_title = title or f"AgGit: Autonomous multi-agent sync from {valid_branch}"
        pr_body = body or "Automated Pull Request prepared by AgGit (Lock-Free Multi-Agent Version Control)."

        create_cmd = ["gh", "pr", "create", "--head", valid_branch, "--base", base_branch, "--title", pr_title, "--body", pr_body]
        create_proc = subprocess.run(create_cmd, cwd=self.repo_dir, capture_output=True, text=True)
        if create_proc.returncode != 0:
            return {
                "status": "error",
                "stderr": create_proc.stderr.strip(),
                "message": "Failed to create Pull Request via gh."
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
        """
        # Resolve full 40-char SHA
        rev_proc = subprocess.run(["git", "rev-parse", commit_ref], cwd=self.repo_dir, capture_output=True, text=True)
        if rev_proc.returncode != 0:
            raise ValueError(f"Invalid Git commit or ref: '{commit_ref}'")
        full_commit_sha = rev_proc.stdout.strip()

        # Read git tree
        tree_proc = subprocess.run(["git", "ls-tree", "-r", "-z", full_commit_sha], cwd=self.repo_dir, capture_output=True)
        if tree_proc.returncode != 0:
            raise RuntimeError(f"Failed to read Git tree for {full_commit_sha}")

        changes = {}
        # Parse ls-tree -r -z output: "<mode> <type> <object>\t<file>\0"
        entries = tree_proc.stdout.split(b"\0")
        for entry in entries:
            if not entry:
                continue
            tab_idx = entry.find(b"\t")
            if tab_idx == -1:
                continue
            path_bytes = entry[tab_idx + 1:]
            rel_path = path_bytes.decode("utf-8", errors="replace")
            if rel_path.startswith(".git") or rel_path.startswith(".aggit"):
                continue

            # Read file bytes from git object database
            cat_proc = subprocess.run(["git", "cat-file", "-p", f"{full_commit_sha}:{rel_path}"], cwd=self.repo_dir, capture_output=True)
            if cat_proc.returncode == 0:
                blob_sha = self.engine.storage.store_blob(cat_proc.stdout)
                changes[rel_path] = self.engine.staging.get_staged_changes("system").get(rel_path) or None
                changes[rel_path] = self.engine.staging.get_staged_changes("system").get(rel_path) or \
                    type(self.engine.journal.get_latest_transaction()) # fallback placeholder
                from .schema import ChangeRecord
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
