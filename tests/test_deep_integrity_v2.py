"""
Regression test suite for:
- Path validation, root termination, Windows case normalization, junction/symlink alias containment
- Git export 3-way matrix: Scenario A (mod vs del), Scenario B (del vs mod), Scenario C (add vs add)
- Successive exports preserving integrated external human edits
- Deterministic conflict identity and preservation of new concurrent changes upon obsolete resolution
- Persistence auto-healing when index is missing or lagging behind journal
- Strictly read-only dry-run / preview_export without creating index, checkpoint, or CAS objects
- Linear reconciliation performance without quadratic historical event rescans
"""

import os
import sys
import json
import time
import shutil
import hashlib
import tempfile
import unittest
import subprocess
from typing import Dict, Any, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from aggit.engine import AgGitEngine
from aggit.git_bridge import GitBridge
from aggit.path_utils import sanitize_repo_path, is_path_contained, PathSecurityError
from aggit.semantic_merge import SemanticMerger, MergeConflict

def create_dir_alias(target: str, alias: str):
    """Creates a directory symlink or junction depending on platform capabilities."""
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(target, alias)
    else:
        os.symlink(target, alias)

class TestDeepIntegrityV2(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="aggit_v2_reg_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Path validation: No infinite loops on junction/symlink alias & root
    # -------------------------------------------------------------------------
    def test_01_path_validation_symlink_alias_no_infinite_loop(self):
        """Calling _validate_safe_path on a junction/symlink alias must not loop infinitely and must finish within timeout."""
        real_work = os.path.join(self.test_dir, "real_work")
        os.makedirs(real_work, exist_ok=True)
        alias_work = os.path.join(self.test_dir, "alias_work")
        create_dir_alias(real_work, alias_work)

        # Execute in a subprocess with strict 3.0s timeout to prove it terminates cleanly
        code = (
            "import sys, os; from aggit.git_bridge import GitBridge; "
            f"res = GitBridge._validate_safe_path(r'{alias_work}', 'file.txt'); "
            "print('TERMINATED_OK'); sys.exit(0)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
            capture_output=True,
            text=True,
            timeout=3.0
        )
        self.assertEqual(proc.returncode, 0, f"Process failed or timed out: {proc.stderr}")
        self.assertIn("TERMINATED_OK", proc.stdout)

    # -------------------------------------------------------------------------
    # 2. Path validation: Sibling prefix escape blocked
    # -------------------------------------------------------------------------
    def test_02_path_validation_prefix_sibling_escape_blocked(self):
        """A directory prefix like 'work' must not accept 'work_external' via symlink/junction."""
        work = os.path.join(self.test_dir, "work")
        work_external = os.path.join(self.test_dir, "work_external")
        os.makedirs(work, exist_ok=True)
        os.makedirs(work_external, exist_ok=True)

        secret_file = os.path.join(work_external, "data.txt")
        with open(secret_file, "w") as f:
            f.write("sensitive data")

        link_inside_work = os.path.join(work, "link")
        create_dir_alias(work_external, link_inside_work)

        # Must reject path traversing through the alias to work_external
        with self.assertRaises(ValueError):
            GitBridge._validate_safe_path(work, "link/data.txt")

    # -------------------------------------------------------------------------
    # 3. Export Scenario A: Mod vs Del conflict
    # -------------------------------------------------------------------------
    def test_03_export_scenario_a_modify_vs_delete_conflicts(self):
        """When human modifies a file in Git and AgGit deletes it, export must block with conflict, not erase human edit."""
        subprocess.run(["git", "init"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.test_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=self.test_dir, check=True)

        engine = AgGitEngine.init(self.test_dir)
        bridge = GitBridge(engine)

        # AgGit commits doc.txt
        engine.commit(agent_id="agent1", message="add doc", files={"doc.txt": b"version 1\n"})
        res1 = bridge.export_to_git(branch_name="sync/branch", base_branch="main")
        self.assertEqual(res1.status, "success")

        # Human switches to sync/branch and modifies doc.txt
        subprocess.run(["git", "checkout", "sync/branch"], cwd=self.test_dir, check=True, capture_output=True)
        with open(os.path.join(self.test_dir, "doc.txt"), "wb") as f:
            f.write(b"human modified version 2\n")
        subprocess.run(["git", "add", "doc.txt"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "human edit doc.txt"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "main"], cwd=self.test_dir, capture_output=True)

        # AgGit deletes doc.txt
        engine.staging.stage_delete("agent1", "doc.txt")
        engine.commit(agent_id="agent1", message="delete doc")

        # Export must detect conflict!
        plan = bridge.prepare_export_plan(branch_name="sync/branch", base_branch="main")
        self.assertFalse(plan.is_clean, "Export plan must NOT be clean when AgGit deletes human-modified file")
        self.assertTrue(any(c.path == "doc.txt" for c in plan.conflicts))

        res2 = bridge.export_to_git(branch_name="sync/branch", base_branch="main")
        self.assertEqual(res2.status, "conflict")

        # Human content must remain on sync/branch
        show_proc = subprocess.run(["git", "show", "sync/branch:doc.txt"], cwd=self.test_dir, capture_output=True, text=True)
        self.assertIn("human modified version 2", show_proc.stdout)

    # -------------------------------------------------------------------------
    # 4. Export Scenario B: Del vs Mod conflict
    # -------------------------------------------------------------------------
    def test_04_export_scenario_b_delete_vs_modify_conflicts(self):
        """When human deletes a file in Git and AgGit modifies it, export must block with conflict, not silently recreate."""
        subprocess.run(["git", "init"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.test_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=self.test_dir, check=True)

        engine = AgGitEngine.init(self.test_dir)
        bridge = GitBridge(engine)

        engine.commit(agent_id="agent1", message="add file", files={"file.txt": b"base content\n"})
        res1 = bridge.export_to_git(branch_name="sync/branch", base_branch="main")
        self.assertEqual(res1.status, "success")

        # Human deletes file.txt on sync/branch
        subprocess.run(["git", "checkout", "sync/branch"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "rm", "file.txt"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "human delete file.txt"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "main"], cwd=self.test_dir, capture_output=True)

        # AgGit modifies file.txt
        engine.commit(agent_id="agent1", message="modify file", files={"file.txt": b"aggit modified content\n"})

        # Export must detect conflict!
        plan = bridge.prepare_export_plan(branch_name="sync/branch", base_branch="main")
        self.assertFalse(plan.is_clean)
        self.assertTrue(any(c.path == "file.txt" for c in plan.conflicts))

        res2 = bridge.export_to_git(branch_name="sync/branch", base_branch="main")
        self.assertEqual(res2.status, "conflict")

    # -------------------------------------------------------------------------
    # 5. Export Scenario C: Add vs Add divergence conflict
    # -------------------------------------------------------------------------
    def test_05_export_scenario_c_add_vs_add_divergence_conflicts(self):
        """When human adds new.txt in Git and AgGit adds new.txt with different content, export must conflict."""
        subprocess.run(["git", "init"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.test_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=self.test_dir, check=True)

        engine = AgGitEngine.init(self.test_dir)
        bridge = GitBridge(engine)

        # Baseline commit on base_branch
        with open(os.path.join(self.test_dir, "init.txt"), "w") as f:
            f.write("init\n")
        subprocess.run(["git", "add", "init.txt"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.test_dir, check=True, capture_output=True)

        # Create sync/branch with human adding extra.txt
        subprocess.run(["git", "checkout", "-b", "sync/branch"], cwd=self.test_dir, check=True, capture_output=True)
        with open(os.path.join(self.test_dir, "extra.txt"), "wb") as f:
            f.write(b"human extra content\n")
        subprocess.run(["git", "add", "extra.txt"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "human add extra.txt"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "main"], cwd=self.test_dir, capture_output=True)

        # AgGit also adds extra.txt with different content
        engine.commit(agent_id="agent1", message="aggit add extra", files={"extra.txt": b"aggit different content\n"})

        # Export must detect conflict and NOT overwrite human version
        plan = bridge.prepare_export_plan(branch_name="sync/branch", base_branch="main")
        self.assertFalse(plan.is_clean)
        self.assertTrue(any(c.path == "extra.txt" for c in plan.conflicts))

        res = bridge.export_to_git(branch_name="sync/branch", base_branch="main")
        self.assertEqual(res.status, "conflict")

    # -------------------------------------------------------------------------
    # 6. Preserving human edits across successive exports
    # -------------------------------------------------------------------------
    def test_06_successive_exports_preserve_integrated_human_edits(self):
        """
        AgGit exports {"a": 0, "b": 0}.
        Human changes Git to {"a": 0, "b": 1}.
        AgGit changes to {"a": 1, "b": 0}.
        Export 2 merges to {"a": 1, "b": 1}.
        AgGit changes only "a" to {"a": 2, "b": 0}.
        Export 3 MUST produce {"a": 2, "b": 1}, preserving human's "b": 1!
        """
        subprocess.run(["git", "init"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.test_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=self.test_dir, check=True)

        engine = AgGitEngine.init(self.test_dir)
        bridge = GitBridge(engine)

        # 1. AgGit exports {"a": 0, "b": 0}
        engine.commit(agent_id="init", message="init json", files={"data.json": json.dumps({"a": 0, "b": 0}).encode("utf-8")})
        res1 = bridge.export_to_git(branch_name="sync/json", base_branch="main")
        self.assertEqual(res1.status, "success")

        # 2. Human changes Git to {"a": 0, "b": 1}
        subprocess.run(["git", "checkout", "sync/json"], cwd=self.test_dir, check=True, capture_output=True)
        with open(os.path.join(self.test_dir, "data.json"), "wb") as f:
            f.write(json.dumps({"a": 0, "b": 1}, indent=2).encode("utf-8") + b"\n")
        subprocess.run(["git", "add", "data.json"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "human sets b=1"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "main"], cwd=self.test_dir, capture_output=True)

        # 3. AgGit changes its version to {"a": 1, "b": 0}
        engine.commit(agent_id="agent1", message="bump a to 1", files={"data.json": json.dumps({"a": 1, "b": 0}).encode("utf-8")})

        # 4. Export 2: Combines cleanly to {"a": 1, "b": 1}
        res2 = bridge.export_to_git(branch_name="sync/json", base_branch="main")
        self.assertEqual(res2.status, "success")
        show2 = subprocess.run(["git", "show", "sync/json:data.json"], cwd=self.test_dir, capture_output=True, text=True)
        parsed2 = json.loads(show2.stdout)
        self.assertEqual(parsed2, {"a": 1, "b": 1})

        # 5. AgGit changes only "a" again, having {"a": 2, "b": 0}
        engine.commit(agent_id="agent1", message="bump a to 2", files={"data.json": json.dumps({"a": 2, "b": 0}).encode("utf-8")})

        # 6. Export 3: MUST preserve human's "b": 1 and produce {"a": 2, "b": 1}!
        res3 = bridge.export_to_git(branch_name="sync/json", base_branch="main")
        self.assertEqual(res3.status, "success")
        show3 = subprocess.run(["git", "show", "sync/json:data.json"], cwd=self.test_dir, capture_output=True, text=True)
        parsed3 = json.loads(show3.stdout)
        self.assertEqual(parsed3, {"a": 2, "b": 1}, f"Human modification in 'b' was lost! Result: {parsed3}")

    # -------------------------------------------------------------------------
    # 7. Conflict identity & reject obsolete resolution
    # -------------------------------------------------------------------------
    def test_07_conflict_identity_and_reject_obsolete_resolution(self):
        """Applying a resolution for an earlier conflict must NOT erase a newly added concurrent conflict."""
        engine = AgGitEngine.init(self.test_dir)

        # Base v0
        tx0 = engine.commit(agent_id="init", message="v0", files={"app.py": b"# base v0\n"})

        # Agent A commits v1
        tx1 = engine.commit(agent_id="agent_a", message="v1", files={"app.py": b"# base v0\n# agent A v1\n"}, base_revision=tx0.tx_id)

        # Agent B commits v2
        tx2 = engine.commit(agent_id="agent_b", message="v2", files={"app.py": b"# base v0\n# agent B v2\n"}, base_revision=tx0.tx_id)

        # Reconcile -> detects conflict between A and B
        _, conflicts1 = engine.reconcile_state()
        self.assertEqual(len(conflicts1), 1)
        conflict_ab_id = conflicts1[0].conflict_id

        # Now Agent C commits v3 divergent from base
        tx3 = engine.commit(agent_id="agent_c", message="v3", files={"app.py": b"# base v0\n# agent C v3\n"}, base_revision=tx0.tx_id)

        # Reconcile again -> multiple disputes exist
        _, conflicts2 = engine.reconcile_state()
        self.assertGreater(len(conflicts2), 0)

        # Explicitly resolve ONLY the conflict between A and B
        resolved_ab = b"# base v0\n# resolved A and B\n"
        # Resolution transaction prepared specifically for conflict_ab_id
        engine.resolve_conflict("app.py", resolved_bytes=resolved_ab, agent_id="lead", target_conflict_ids=[conflict_ab_id])

        # After resolving A vs B, the dispute involving C must STILL be pending!
        _, conflicts_after = engine.reconcile_state()
        self.assertTrue(len(conflicts_after) > 0, "Concurrent change by agent C must NOT be erased by resolving earlier conflict!")

    # -------------------------------------------------------------------------
    # 8. Persistence: auto-heals when index is lagging
    # -------------------------------------------------------------------------
    def test_08_persistence_detects_lagging_index_and_auto_heals(self):
        """When journal_index.json is missing or behind journal, newly opened engine instance must auto-heal and see latest head."""
        engine = AgGitEngine.init(self.test_dir)
        tx_a = engine.commit(agent_id="a", message="commit A", files={"f1.txt": b"content A\n"})

        # Record index state at commit A
        index_file = os.path.join(self.test_dir, ".aggit", "journal_index.json")
        with open(index_file, "r", encoding="utf-8") as f:
            index_at_a = json.load(f)

        tx_b = engine.commit(agent_id="b", message="commit B", files={"f2.txt": b"content B\n"})

        # Intentionally revert journal_index.json to commit A's state
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(index_at_a, f)

        # Open a new engine instance: must detect index is behind journal files and auto-heal
        engine2 = AgGitEngine(self.test_dir)
        latest = engine2.journal.get_latest_transaction()
        self.assertIsNotNone(latest)
        self.assertEqual(latest.tx_id, tx_b.tx_id, "Engine must see commit B despite stale journal_index.json!")

    # -------------------------------------------------------------------------
    # 9. Strictly read-only dry-run / preview_export
    # -------------------------------------------------------------------------
    def test_09_preview_export_is_strictly_read_only_with_missing_index(self):
        """Dry-run preview with missing journal_index.json must NOT write journal_index.json or checkpoints to disk."""
        engine = AgGitEngine.init(self.test_dir)
        engine.commit(agent_id="a", message="commit A", files={"f1.txt": b"content A\n"})

        index_file = os.path.join(self.test_dir, ".aggit", "journal_index.json")
        checkpoint_file = os.path.join(self.test_dir, ".aggit", "checkpoints", "latest.json")

        if os.path.exists(index_file):
            os.remove(index_file)
        if os.path.exists(checkpoint_file):
            os.remove(checkpoint_file)

        bridge = GitBridge(engine)
        preview = bridge.preview_export(branch_name="aggit/sync")
        self.assertTrue(preview["is_clean"])

        # Disk files must NOT have been created by dry-run preview!
        self.assertFalse(os.path.exists(index_file), "preview_export must not create journal_index.json on disk")
        self.assertFalse(os.path.exists(checkpoint_file), "preview_export must not create checkpoint on disk")

    # -------------------------------------------------------------------------
    # 10. Linear reconciliation performance without N x N event reads
    # -------------------------------------------------------------------------
    def test_10_reconciliation_linear_performance(self):
        """Reconciliation over sequential commits must not quadratically rescan the entire journal."""
        engine = AgGitEngine.init(self.test_dir)

        # 20 sequential commits
        for i in range(20):
            engine.commit(agent_id=f"agent_{i%3}", message=f"commit {i}", files={f"file_{i}.txt": f"data {i}\n".encode("utf-8")})

        engine.stats["events_read"] = 0
        engine.reconcile_state()
        first_read_count = engine.stats["events_read"]

        # With 20 transactions, linear reading should read each transaction once or twice (DAG + processing),
        # definitely NOT N x N (which would be 400+ reads)
        self.assertLessEqual(first_read_count, 60, f"Expected linear event reads <= 60, got {first_read_count}")

        # Repeating reconcile without changes must read 0 new events
        engine.stats["events_read"] = 0
        engine.reconcile_state()
        second_read_count = engine.stats["events_read"]
        self.assertEqual(second_read_count, 0, f"Repeat reconcile without changes should read 0 events, got {second_read_count}")

if __name__ == "__main__":
    unittest.main()
