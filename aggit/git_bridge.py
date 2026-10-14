"""
AgGit Git Bridge:
Synthesizes AgGit multi-agent transactions into standard upstream Git commits.
Provides seamless bi-directional synchronization with GitHub / GitLab.
"""

import os
import subprocess
from typing import Optional, List
from .engine import AgGitEngine

class GitBridge:
    def __init__(self, engine: AgGitEngine):
        self.engine = engine
        self.repo_dir = engine.repo_dir

    def export_to_git(self, branch_name: str = "aggit-main", commit_message: Optional[str] = None) -> str:
        """
        Materializes reconciled AgGit state and creates a clean Git commit.
        """
        # Ensure working directory has unified state
        count, conflicts = self.engine.materialize()
        if conflicts:
            raise RuntimeError(f"Cannot export to Git with unresolved conflicts in: {conflicts}")

        # Check if git is initialized in this repo
        git_dir = os.path.join(self.repo_dir, ".git")
        if not os.path.exists(git_dir):
            subprocess.run(["git", "init"], cwd=self.repo_dir, check=True, capture_output=True)

        # Stage all files except .aggit
        # Write .gitignore if not present
        gitignore_path = os.path.join(self.repo_dir, ".gitignore")
        if not os.path.exists(gitignore_path):
            with open(gitignore_path, "w", encoding="utf-8") as f:
                f.write(".aggit/\n__pycache__/\n*.tmp\n")
        else:
            with open(gitignore_path, "r", encoding="utf-8") as f:
                content = f.read()
            if ".aggit" not in content:
                with open(gitignore_path, "a", encoding="utf-8") as f:
                    f.write("\n.aggit/\n")

        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True, capture_output=True)

        txs = self.engine.journal.get_all_transactions()
        if not commit_message:
            agents = sorted(list(set(tx["agent_id"] for tx in txs)))
            commit_message = f"chore(aggit): unified multi-agent sync ({len(txs)} txs by {len(agents)} agents: {', '.join(agents)})"

        res = subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=self.repo_dir,
            capture_output=True,
            text=True
        )

        # Get commit hash
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True
        )
        return rev.stdout.strip()
