"""
AgGit Virtual Namespaced Staging:
Each agent operates on its own staging context without colliding with others.
Replaces the monolithic .git/index file.
"""

import os
import json
from typing import Dict, List, Optional
from .storage import CASStorage

class StagingArea:
    def __init__(self, repo_dir: str, storage: CASStorage):
        self.repo_dir = os.path.abspath(repo_dir)
        self.storage = storage
        self.staging_dir = os.path.join(self.repo_dir, ".aggit", "staging")
        os.makedirs(self.staging_dir, exist_ok=True)

    def _agent_staging_path(self, agent_id: str) -> str:
        safe_agent_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in agent_id)
        return os.path.join(self.staging_dir, f"{safe_agent_id}.json")

    def get_staged(self, agent_id: str) -> Dict[str, str]:
        """Returns rel_path -> blob_sha for this agent."""
        path = self._agent_staging_path(agent_id)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def stage_file(self, agent_id: str, rel_path: str, content: bytes = None) -> str:
        """
        Stages a file for the specified agent.
        If content is None, reads directly from repo_dir/rel_path.
        Stores blob in CAS and records in agent's staging area.
        """
        norm_path = os.path.normpath(rel_path).replace("\\", "/")
        if content is None:
            full_path = os.path.join(self.repo_dir, rel_path)
            if not os.path.exists(full_path):
                raise FileNotFoundError(f"File {rel_path} does not exist in working directory")
            with open(full_path, "rb") as f:
                content = f.read()

        blob_sha = self.storage.store_blob(content)
        current = self.get_staged(agent_id)
        current[norm_path] = blob_sha

        path = self._agent_staging_path(agent_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, sort_keys=True)
        os.replace(tmp, path)

        return blob_sha

    def stage_files(self, agent_id: str, rel_paths: List[str]) -> Dict[str, str]:
        results = {}
        for p in rel_paths:
            results[p] = self.stage_file(agent_id, p)
        return results

    def clear(self, agent_id: str):
        path = self._agent_staging_path(agent_id)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
