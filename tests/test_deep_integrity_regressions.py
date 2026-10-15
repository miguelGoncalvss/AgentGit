"""
Comprehensive regression test suite for deep integrity, concurrency,
export delta, conflict lifecycle, and 3-state materialization.
Reproduces all reported failure modes before fixes are applied.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
import subprocess
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from aggit.engine import AgGitEngine
from aggit.staging import StagingArea
from aggit.storage import CASStorage
from aggit.semantic_merge import SemanticMerger
from aggit.git_bridge import GitBridge

class TestDeepIntegrityRegressions(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="aggit_deep_reg_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_concurrent_staging_16_threads_same_session(self):
        """16 concurrent operations in the same session must preserve all 16 entries without lost updates."""
        engine = AgGitEngine.init(self.test_dir)
        staging = engine.staging

        def stage_worker(idx):
            staging.stage_batch(
                agent_id="agent_concurrent",
                session_id="session_16",
                files_to_stage={f"file_{idx:02d}.txt": f"Content from worker {idx}".encode("utf-8")}
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(stage_worker, i) for i in range(16)]
            for f in futures:
                f.result()

        staged_changes = staging.get_staged_changes("agent_concurrent", "session_16")
        self.assertEqual(len(staged_changes), 16, f"Expected 16 staged files, but got {len(staged_changes)}: {list(staged_changes.keys())}")

    def test_02_stage_concurrent_with_commit_preserves_new_files(self):
        """Staging a new file while commit is executing must preserve the new file for subsequent commit."""
        engine = AgGitEngine.init(self.test_dir)
        staging = engine.staging

        # Agent stages file1
        staging.stage_file("agent_a", "file1.txt", b"v1", session_id="s1")

        # Intercept append_transaction to simulate concurrent staging of file2 while commit runs
        orig_append = engine.journal.append_transaction
        def concurrent_append(*args, **kwargs):
            # Concurrent stage happens after commit snapshot is taken!
            staging.stage_file("agent_a", "file2.txt", b"v2", session_id="s1")
            return orig_append(*args, **kwargs)

        engine.journal.append_transaction = concurrent_append
        engine.commit(agent_id="agent_a", session_id="s1", message="commit 1")

        # Verify file2 is still in staging and file1 is cleared!
        remaining = staging.get_staged_changes("agent_a", "s1")
        self.assertIn("file2.txt", remaining, "file2.txt should remain in staging after commit of file1")
        self.assertNotIn("file1.txt", remaining, "file1.txt should have been cleared after commit")


    def test_03_third_agent_commit_does_not_clear_pending_conflict(self):
        """Conflict in value.txt must NOT disappear when a third agent commits a normal change to value.txt."""
        engine = AgGitEngine.init(self.test_dir)

        # Genesis
        tx_init = engine.commit(agent_id="init", message="init", files={"value.txt": b"base\n"})

        # Agent A changes value.txt
        engine.commit(agent_id="agent_a", message="change A", files={"value.txt": b"base\nfrom A\n"}, base_revision=tx_init.tx_id)

        # Agent B changes value.txt concurrently
        engine.commit(agent_id="agent_b", message="change B", files={"value.txt": b"base\nfrom B\n"}, base_revision=tx_init.tx_id)

        # Reconcile -> must conflict
        _, conflicts = engine.reconcile_state()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].path, "value.txt")

        # Agent C makes a normal commit on value.txt (NOT an explicit resolution)
        engine.commit(agent_id="agent_c", message="change C by third agent", files={"value.txt": b"base\nfrom C\n"})

        # Reconcile again -> conflict MUST still be pending!
        _, conflicts_after = engine.reconcile_state()
        conflicted_paths = [c.path for c in conflicts_after]
        self.assertIn("value.txt", conflicted_paths, "Normal commit by agent_c must NOT clear the pending conflict on value.txt")

    def test_04_resolve_json_conflict_with_keys_clears_conflict(self):
        """Resolving a conflict on config.json must clear conflicts on keys within config.json."""
        engine = AgGitEngine.init(self.test_dir)

        base_json = json.dumps({"v": 1, "port": 8080}).encode("utf-8")
        tx_init = engine.commit(agent_id="init", message="init", files={"config.json": base_json})

        a_json = json.dumps({"v": 2, "port": 8080}).encode("utf-8")
        engine.commit(agent_id="agent_a", message="bump v to 2", files={"config.json": a_json}, base_revision=tx_init.tx_id)

        b_json = json.dumps({"v": 3, "port": 8080}).encode("utf-8")
        engine.commit(agent_id="agent_b", message="bump v to 3", files={"config.json": b_json}, base_revision=tx_init.tx_id)

        # Reconcile -> conflict on key v in config.json
        _, conflicts = engine.reconcile_state()
        self.assertTrue(len(conflicts) > 0, "Should have conflict on config.json key v")

        # Explicit resolution on config.json
        resolved_json = json.dumps({"v": 4, "port": 8080}).encode("utf-8")
        engine.resolve_conflict(path="config.json", resolved_bytes=resolved_json, agent_id="lead")

        # Reconcile again -> must be 100% clean!
        _, conflicts_after = engine.reconcile_state()
        self.assertEqual(len(conflicts_after), 0, f"Expected 0 conflicts after explicit resolution, got: {conflicts_after}")

    def test_05_json_merge_distinguishes_absent_key_from_null(self):
        """Base {}, A adds {'key': null}, B adds {'key': 42} must CONFLICT, not resolve to 42."""
        base_obj = {}
        a_obj = {"key": None}
        b_obj = {"key": 42}

        merged, conflicts = SemanticMerger.merge_json_recursive(base_obj, a_obj, b_obj, "data.json")
        self.assertTrue(len(conflicts) > 0, f"Adding null vs 42 to absent key must conflict! Got merged={merged}, conflicts={conflicts}")

    def test_06_legacy_checkpoint_without_pending_conflicts_rebuilds_and_detects_conflict(self):
        """A legacy checkpoint missing 'pending_conflicts' must safely rebuild from journal to detect conflicts."""
        engine = AgGitEngine.init(self.test_dir)

        # Create two conflicting transactions
        tx_init = engine.commit(agent_id="init", message="init", files={"file.txt": b"base\n"})
        engine.commit(agent_id="agent_a", message="A", files={"file.txt": b"A\n"}, base_revision=tx_init.tx_id)
        engine.commit(agent_id="agent_b", message="B", files={"file.txt": b"B\n"}, base_revision=tx_init.tx_id)

        # Simulate legacy checkpoint (Schema v1) that does NOT have pending_conflicts key
        ckpt_path = os.path.join(self.test_dir, ".aggit", "checkpoints", "latest.json")
        with open(ckpt_path, "w", encoding="utf-8") as f:
            json.dump({
                "version": 1,
                "tree": {"file.txt": "dummy"},
                "processed_tx_ids": [tx_init.tx_id]
            }, f)

        # Reopen engine and reconcile
        engine2 = AgGitEngine(self.test_dir)
        _, conflicts = engine2.reconcile_state()
        self.assertTrue(len(conflicts) > 0, "Reconcile on legacy checkpoint missing pending_conflicts must rebuild and detect conflict!")

    def test_07_export_delta_handles_addition_then_deletion(self):
        """Exporting new.txt, deleting it in AgGit, and exporting again must remove new.txt from Git branch."""
        subprocess.run(["git", "init"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.test_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=self.test_dir, check=True)

        engine = AgGitEngine.init(self.test_dir)
        bridge = GitBridge(engine)

        # Commit new.txt in AgGit
        engine.commit(agent_id="agent_a", message="add new.txt", files={"new.txt": b"content of new.txt\n"})

        # Export 1
        res1 = bridge.export_to_git(branch_name="feature/sync", base_branch="main", commit_message="export 1")
        self.assertEqual(res1.status, "success")

        # Verify new.txt exists on feature/sync in Git
        ls1 = subprocess.run(["git", "ls-tree", "-r", "--name-only", "feature/sync"], cwd=self.test_dir, capture_output=True, text=True)
        self.assertIn("new.txt", ls1.stdout)

        # Now delete new.txt in AgGit
        engine.staging.stage_delete("agent_a", "new.txt")
        engine.commit(agent_id="agent_a", message="delete new.txt")

        # Export 2
        res2 = bridge.export_to_git(branch_name="feature/sync", base_branch="main", commit_message="export 2")
        self.assertEqual(res2.status, "success")

        # Verify new.txt is DELETED from feature/sync in Git!
        ls2 = subprocess.run(["git", "ls-tree", "-r", "--name-only", "feature/sync"], cwd=self.test_dir, capture_output=True, text=True)
        self.assertNotIn("new.txt", ls2.stdout, "new.txt must be deleted from feature/sync in export 2")

    def test_08_export_preserves_external_human_commits_on_export_branch(self):
        """Exporting changes to fileB must preserve external human commits made to fileA on export branch."""
        subprocess.run(["git", "init"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.test_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=self.test_dir, check=True)

        engine = AgGitEngine.init(self.test_dir)
        bridge = GitBridge(engine)

        # AgGit has fileA and fileB
        engine.commit(agent_id="init", message="init A and B", files={"fileA.txt": b"fileA v1\n", "fileB.txt": b"fileB v1\n"})
        res1 = bridge.export_to_git(branch_name="feature/sync", base_branch="main", commit_message="export 1")
        self.assertEqual(res1.status, "success")

        # Human switches to feature/sync and edits fileA
        subprocess.run(["git", "checkout", "feature/sync"], cwd=self.test_dir, check=True, capture_output=True)
        with open(os.path.join(self.test_dir, "fileA.txt"), "wb") as f:
            f.write(b"fileA HUMAN EDIT\n")
        subprocess.run(["git", "add", "fileA.txt"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "human edit on fileA"], cwd=self.test_dir, check=True, capture_output=True)

        # Switch back to main (or detach)
        subprocess.run(["git", "checkout", "main"], cwd=self.test_dir, capture_output=True)

        # AgGit modifies ONLY fileB
        engine.commit(agent_id="agent_b", message="modify fileB", files={"fileB.txt": b"fileB v2\n"})

        # Export 2 to feature/sync
        res2 = bridge.export_to_git(branch_name="feature/sync", base_branch="main", commit_message="export 2")
        self.assertEqual(res2.status, "success")

        # Inspect fileA on feature/sync: MUST retain "fileA HUMAN EDIT"
        show_proc = subprocess.run(["git", "show", "feature/sync:fileA.txt"], cwd=self.test_dir, capture_output=True, text=True)
        self.assertIn("HUMAN EDIT", show_proc.stdout, "Human edit on fileA must NOT be overwritten by export of fileB!")

    def test_09_dry_run_strictly_zero_cas_writes(self):
        """A dry-run reconcile or export preview must NOT write any new objects into .aggit/objects."""
        engine = AgGitEngine.init(self.test_dir)

        # Commit two changes that will produce a 3-way merge
        tx_init = engine.commit(agent_id="init", message="init", files={"app.py": b"# header\ndef foo(): pass\n# footer\n"})
        engine.commit(agent_id="agent_a", message="add bar", files={"app.py": b"# header\ndef foo(): pass\ndef bar(): pass\n# footer\n"}, base_revision=tx_init.tx_id)
        engine.commit(agent_id="agent_b", message="add baz", files={"app.py": b"# header\ndef baz(): pass\ndef foo(): pass\n# footer\n"}, base_revision=tx_init.tx_id)

        cas_dir = os.path.join(self.test_dir, ".aggit", "objects")
        def count_cas_objects():
            count = 0
            for root, _, files in os.walk(cas_dir):
                count += len(files)
            return count

        initial_count = count_cas_objects()

        # Run dry_run reconcile
        files, conflicts = engine.reconcile_state(dry_run=True)
        self.assertEqual(len(conflicts), 0)
        self.assertIn("def bar(): pass", files["app.py"].decode("utf-8"))
        self.assertIn("def baz(): pass", files["app.py"].decode("utf-8"))

        count_after = count_cas_objects()
        self.assertEqual(initial_count, count_after, f"Dry-run must write 0 objects to CAS! Initial: {initial_count}, After: {count_after}")

    def test_10_append_transaction_does_not_quadratic_read_all_history(self):
        """Appending transactions must NOT read all previous transaction files on every single commit."""
        engine = AgGitEngine.init(self.test_dir)

        # Commit 10 transactions
        for i in range(10):
            engine.commit(agent_id="agent_perf", message=f"commit {i}", files={f"file_{i}.txt": f"content {i}".encode("utf-8")})

        # Measure events read on the 11th commit
        # The append operation itself should be O(1) in file reads
        # Check that append doesn't do a full journal scan
        t0 = time.time()
        engine.commit(agent_id="agent_perf", message="commit 11", files={"file_11.txt": b"content 11"})
        elapsed = time.time() - t0
        self.assertLess(elapsed, 0.5, "Appending a transaction took too long; likely performing full journal scan")

if __name__ == "__main__":
    unittest.main()
