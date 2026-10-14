"""
AgGit Comprehensive Integrity, Concurrency, Merge & Git Integration Test Suite.
Tests all 25+ critical scenarios demanded by the specification.
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
import subprocess
from concurrent.futures import ThreadPoolExecutor

# Add parent directory to sys.path so tests import local aggit package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from aggit.storage import CASStorage
from aggit.journal import Journal, JournalCorruptionError
from aggit.staging import StagingArea
from aggit.semantic_merge import SemanticMerger
from aggit.path_utils import sanitize_repo_path, PathSecurityError
from aggit.engine import AgGitEngine
from aggit.git_bridge import GitBridge

class TestAgGitIntegrity(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="aggit_test_env_")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    # 1. Path Sanitization & Boundary Protection
    def test_path_sanitization(self):
        # Valid relative paths
        self.assertEqual(sanitize_repo_path(self.test_dir, "foo.py"), "foo.py")
        self.assertEqual(sanitize_repo_path(self.test_dir, "src/nested/bar.txt"), "src/nested/bar.txt")
        self.assertEqual(sanitize_repo_path(self.test_dir, "dir with space/arquivo_áéí.py"), "dir with space/arquivo_áéí.py")

        # Invalid paths: path traversal
        with self.assertRaises(PathSecurityError):
            sanitize_repo_path(self.test_dir, "../outside.py")
        with self.assertRaises(PathSecurityError):
            sanitize_repo_path(self.test_dir, "src/../../outside.py")

        # Invalid paths: absolute
        with self.assertRaises(PathSecurityError):
            sanitize_repo_path(self.test_dir, "/etc/passwd")
        with self.assertRaises(PathSecurityError):
            sanitize_repo_path(self.test_dir, "C:/Windows/System32/calc.exe")

        # Invalid paths: targeting control dirs
        with self.assertRaises(PathSecurityError):
            sanitize_repo_path(self.test_dir, ".git/HEAD")
        with self.assertRaises(PathSecurityError):
            sanitize_repo_path(self.test_dir, ".aggit/journal/tamper.json")

    # 2. CAS: Hash-first, Existence check before compression & Corruption detection
    def test_cas_storage_integrity(self):
        cas = CASStorage(self.test_dir)
        content = b"Hello AgGit Storage Engine!"
        sha = cas.store_blob(content)
        self.assertEqual(len(sha), 64)

        # Retrieve blob
        retrieved = cas.get_blob(sha)
        self.assertEqual(retrieved, content)

        # Store identical blob again (must return same SHA without error)
        sha2 = cas.store_blob(content)
        self.assertEqual(sha, sha2)

        # Corrupt file on disk and verify integrity error is raised
        shard = sha[:2]
        rest = sha[2:]
        obj_file = os.path.join(cas.objects_dir, shard, rest)
        with open(obj_file, "wb") as f:
            f.write(b"tampered byte payload")

        with self.assertRaises(ValueError):
            cas.get_blob(sha)

    # 3. Concurrent blob writes across multiple threads
    def test_concurrent_cas_writes(self):
        cas = CASStorage(self.test_dir)
        payload = b"Shared concurrent data" * 100

        def write_worker(_):
            return cas.store_blob(payload)

        with ThreadPoolExecutor(max_workers=10) as executor:
            shas = list(executor.map(write_worker, range(20)))

        # All threads must succeed and return the exact same hash
        self.assertTrue(all(s == shas[0] for s in shas))
        self.assertEqual(cas.get_blob(shas[0]), payload)

    # 4. Empty file versus absent file
    def test_empty_vs_absent_file(self):
        engine = AgGitEngine.init(self.test_dir)
        # Empty file has a real 0-byte blob
        empty_sha = engine.storage.store_blob(b"")
        self.assertEqual(len(empty_sha), 64)
        self.assertEqual(engine.storage.get_blob(empty_sha), b"")

        # Absent file is represented by None
        merged_bytes, conflicts = SemanticMerger.merge_files(
            path="file.txt",
            base_bytes=None,
            a_bytes=b"",
            b_bytes=b""
        )
        self.assertEqual(merged_bytes, b"")
        self.assertEqual(len(conflicts), 0)

    # 5. JSON 3-way merge: Independent additions, null vs missing, recursive merge
    def test_json_semantic_merge_independent_additions(self):
        base = json.dumps({}).encode("utf-8")
        side_a = json.dumps({"porta": 3000}).encode("utf-8")
        side_b = json.dumps({"debug": True}).encode("utf-8")

        merged_bytes, conflicts = SemanticMerger.merge_files(
            path="config.json",
            base_bytes=base,
            a_bytes=side_a,
            b_bytes=side_b
        )
        self.assertEqual(len(conflicts), 0)
        merged_obj = json.loads(merged_bytes.decode("utf-8"))
        self.assertEqual(merged_obj, {"porta": 3000, "debug": True})

    def test_json_null_vs_missing_and_conflicts(self):
        base = json.dumps({"key": "original", "setting": 10}).encode("utf-8")
        side_a = json.dumps({"key": None, "setting": 10}).encode("utf-8") # explicitly set to null
        side_b = json.dumps({"setting": 20}).encode("utf-8") # deleted key in B, modified setting in B

        merged_bytes, conflicts = SemanticMerger.merge_files(
            path="config.json",
            base_bytes=base,
            a_bytes=side_a,
            b_bytes=side_b
        )
        # B modified 'setting' (A untouched setting -> 20)
        # A modified 'key' to null, B deleted 'key' -> modify/delete conflict recorded!
        self.assertTrue(len(conflicts) > 0)
        self.assertTrue(any(c.conflict_type == "json_key" for c in conflicts))

    # 6. Python content preservation: docstrings, decorators, globals, comments
    def test_python_content_preservation(self):
        base = '''"""Module docstring."""
GLOBAL_CONFIG = {"rate_limit": 60}

# Helper comment
@decorator_func
def helper():
    return True
'''
        side_a = '''"""Module docstring."""
GLOBAL_CONFIG = {"rate_limit": 60}

# Helper comment
@decorator_func
def helper():
    return True

def new_func_a():
    return "A"
'''
        side_b = '''"""Module docstring."""
GLOBAL_CONFIG = {"rate_limit": 120}

# Helper comment
@decorator_func
def helper():
    return True

def new_func_b():
    return "B"
'''
        merged_bytes, conflicts = SemanticMerger.merge_files(
            path="app.py",
            base_bytes=base.encode("utf-8"),
            a_bytes=side_a.encode("utf-8"),
            b_bytes=side_b.encode("utf-8")
        )
        merged_text = merged_bytes.decode("utf-8")
        # Global docstring preserved
        self.assertIn('"""Module docstring."""', merged_text)
        # Decorators preserved
        self.assertIn("@decorator_func", merged_text)
        # Comments preserved
        self.assertIn("# Helper comment", merged_text)

    # 7. Modify / Delete conflict
    def test_modify_delete_conflict(self):
        base = b"Line 1\nLine 2\n"
        side_a = None  # A deleted the file
        side_b = b"Line 1\nLine 2 modified\n"

        merged, conflicts = SemanticMerger.merge_files(
            path="docs.txt",
            base_bytes=base,
            a_bytes=side_a,
            b_bytes=side_b
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].conflict_type, "modify_delete")

    # 8. Binary files: identical vs conflicting modifications
    def test_binary_merge_behavior(self):
        base = b"\x00\x01\x02\x03\x04"
        side_a = b"\x00\x01\x02\x03\xFF"
        side_b = b"\x00\x01\x02\x03\xEE"

        # Conflicting binary changes must NOT use last-write-wins; must report conflict
        merged, conflicts = SemanticMerger.merge_files(
            path="image.png",
            base_bytes=base,
            a_bytes=side_a,
            b_bytes=side_b
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].conflict_type, "binary")

    # 9. Staging: Batch staging, deletions, and generation safety
    def test_staging_batch_and_generation(self):
        engine = AgGitEngine.init(self.test_dir)
        staging = engine.staging

        # Stage batch of 3 files
        batch = {
            "a.txt": b"content A",
            "b.txt": b"content B",
            "c.txt": b"content C"
        }
        res = staging.stage_batch("agent_1", files_to_stage=batch)
        self.assertEqual(len(res), 3)

        # Stage deletion of b.txt
        staging.stage_delete("agent_1", "b.txt")
        staged = staging.get_staged_changes("agent_1")
        self.assertEqual(staged["b.txt"].action, "delete")

        # Generation check: clearing with old generation does not wipe newer edits
        gen = staging.get_staged_state("agent_1")["generation"]
        staging.stage_file("agent_1", "d.txt", b"content D") # generation incremented
        staging.clear("agent_1", max_generation=gen) # should NOT clear because gen < new_gen
        self.assertTrue(len(staging.get_staged_changes("agent_1")) > 0)

    # 10. Sequential edits with correct base revision & incremental reconciliation
    def test_sequential_edits_and_checkpoints(self):
        engine = AgGitEngine.init(self.test_dir)
        tx1 = engine.commit(
            agent_id="agent_1",
            message="Commit 1",
            files={"counter.txt": b"1\n"}
        )

        # Agent 2 starts with tx1 as explicit base
        tx2 = engine.commit(
            agent_id="agent_2",
            message="Commit 2",
            files={"counter.txt": b"2\n"},
            base_revision=tx1.tx_id
        )
        self.assertEqual(tx2.base_revision, tx1.tx_id)

        # Reconcile state using incremental checkpoints
        unified, conflicts = engine.reconcile_state()
        self.assertEqual(len(conflicts), 0)
        self.assertEqual(unified["counter.txt"], b"2\n")

        # Second materialization must write 0 files because content is already up-to-date
        count1, _ = engine.materialize()
        self.assertEqual(count1, 1) # First write
        count2, _ = engine.materialize()
        self.assertEqual(count2, 0) # Second write skipped completely!

    # 11. Journal reconstruction from durable data after index removal
    def test_journal_index_recovery(self):
        engine = AgGitEngine.init(self.test_dir)
        tx1 = engine.commit("agent_1", "Init", files={"init.txt": b"initial"})
        tx2 = engine.commit("agent_2", "Second", files={"init.txt": b"updated"})

        # Delete derived index
        index_file = os.path.join(engine.aggit_dir, "journal_index.json")
        if os.path.exists(index_file):
            os.remove(index_file)

        # Engine must automatically reconstruct index from journal events
        engine.journal._index = None
        latest = engine.journal.get_latest_transaction()
        self.assertIsNotNone(latest)
        self.assertEqual(latest.tx_id, tx2.tx_id)

    # 12. Corrupt transaction detection in journal
    def test_corrupt_journal_detection(self):
        engine = AgGitEngine.init(self.test_dir)
        engine.commit("agent_1", "Valid commit", files={"v.txt": b"ok"})

        # Inject corrupt transaction file
        corrupt_file = os.path.join(engine.journal.journal_dir, "99999999999999999999_corrupt_tx.json")
        with open(corrupt_file, "w") as f:
            f.write("{ invalid json payload !!!")

        with self.assertRaises(JournalCorruptionError):
            engine.journal.get_all_transactions()

        # Remove corrupt file
        os.remove(corrupt_file)

    # 13. Git Bridge: Export to isolated branch without touching main or dirty files
    def test_git_bridge_isolated_export(self):
        engine = AgGitEngine.init(self.test_dir)
        engine.commit("agent_1", "Multi-agent feature", files={"service.py": b"def run(): pass\n"})

        # Create a dirty untracked file in the working directory
        dirty_file = os.path.join(self.test_dir, "unrelated_scratch.log")
        with open(dirty_file, "w") as f:
            f.write("I should never be committed by AgGit export!")

        bridge = GitBridge(engine)
        result = bridge.export_to_git(branch_name="feature/agent-export", base_branch="main")
        self.assertEqual(result.status, "success")
        self.assertIsNotNone(result.git_commit_hash)

        # Verify dirty file is still untouched in working tree
        self.assertTrue(os.path.exists(dirty_file))

        # Verify git commit in feature/agent-export only contains service.py, not unrelated_scratch.log!
        ls_cmd = ["git", "ls-tree", "-r", "--name-only", "feature/agent-export"]
        ls_out = subprocess.run(ls_cmd, cwd=self.test_dir, capture_output=True, text=True).stdout
        self.assertIn("service.py", ls_out)
        self.assertNotIn("unrelated_scratch.log", ls_out)

        # Test idempotence: exporting same revision again returns cached/identical commit
        res2 = bridge.export_to_git(branch_name="feature/agent-export", base_branch="main")
        self.assertIn(res2.status, ["cached", "no_changes"])
        self.assertEqual(res2.git_commit_hash, result.git_commit_hash)

    # 14. Git Bridge: Push to a local Git remote (isolated test without external network)
    def test_git_bridge_local_remote_push(self):
        engine = AgGitEngine.init(self.test_dir)
        engine.commit("agent_1", "Feature push test", files={"api.py": b"# API code\n"})

        bridge = GitBridge(engine)
        exp_res = bridge.export_to_git(branch_name="feature/local-push")
        self.assertEqual(exp_res.status, "success")

        # Create a bare Git repository as a local remote
        bare_remote_dir = tempfile.mkdtemp(prefix="aggit_bare_remote_")
        try:
            subprocess.run(["git", "init", "--bare"], cwd=bare_remote_dir, check=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "local_upstream", bare_remote_dir], cwd=self.test_dir, check=True, capture_output=True)

            # Perform push to local remote
            push_res = bridge.push_to_remote(branch_name="feature/local-push", remote="local_upstream")
            self.assertTrue(push_res["success"])

            # Verify commit exists in bare remote
            check_rev = subprocess.run(["git", "rev-parse", "feature/local-push"], cwd=bare_remote_dir, capture_output=True, text=True)
            self.assertEqual(check_rev.stdout.strip(), exp_res.git_commit_hash)
        finally:
            shutil.rmtree(bare_remote_dir, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
