"""
Regression test suite targeting the specific data integrity, export, and concurrency
issues identified in review of commit 281fd09539f213fc6f9d08a7da83ed90fbf9684c.
These tests reproduce the failures prior to fixes and validate the fixes once implemented.
"""

import os
import sys
import shutil
import tempfile
import unittest
import subprocess
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from aggit.engine import AgGitEngine
from aggit.git_bridge import GitBridge
from aggit.path_utils import sanitize_repo_path, PathSecurityError
from aggit.journal import Journal
from aggit.staging import StagingArea
from aggit.storage import CASStorage

class TestDataIntegrityAndExportRegressions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="aggit_regress_")
        self.engine = AgGitEngine.init(self.temp_dir)
        self.bridge = GitBridge(self.engine)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -------------------------------------------------------------
    # 1. Conflicts must persist until explicit resolution
    # -------------------------------------------------------------
    def test_reconcile_conflict_persists_across_calls(self):
        """Two transactions with same base modifying same file must report conflict on consecutive calls."""
        # 1. Base commit
        tx0 = self.engine.commit(
            agent_id="agent0",
            message="Base commit",
            files={"conflicted.txt": b"base content\n"}
        )
        self.engine.reconcile_state()

        # 2. Agent 1 commits based on tx0
        tx1 = self.engine.commit(
            agent_id="agent1",
            message="Agent 1 edit",
            files={"conflicted.txt": b"agent 1 content\n"},
            base_revision=tx0.tx_id
        )

        # 3. Agent 2 commits based on tx0
        tx2 = self.engine.commit(
            agent_id="agent2",
            message="Agent 2 edit",
            files={"conflicted.txt": b"agent 2 content\n"},
            base_revision=tx0.tx_id
        )

        # Call 1: Must detect conflict
        _, conflicts1 = self.engine.reconcile_state()
        self.assertGreater(len(conflicts1), 0, "First reconcile must detect conflict")

        # Call 2 without resolution: MUST STILL DETECT CONFLICT!
        _, conflicts2 = self.engine.reconcile_state()
        self.assertGreater(len(conflicts2), 0, "Second reconcile must still detect conflict without resolution!")

    def test_preview_export_does_not_mutate_state(self):
        """preview_export (dry-run) must NOT modify checkpoints, ledger, or working tree."""
        tx0 = self.engine.commit(
            agent_id="agent0",
            message="Base commit",
            files={"test.txt": b"content\n"}
        )

        checkpoint_file = os.path.join(self.temp_dir, ".aggit", "checkpoints", "latest.json")
        cp_before = os.path.exists(checkpoint_file)

        # Run preview
        preview = self.bridge.preview_export(branch_name="aggit/preview-test")
        self.assertTrue(preview["is_clean"])

        # Checkpoint must NOT be created or updated by dry-run preview!
        cp_after = os.path.exists(checkpoint_file)
        self.assertEqual(cp_before, cp_after, "preview_export must not create checkpoint file")

    # -------------------------------------------------------------
    # 2. Materialization must preserve local changes
    # -------------------------------------------------------------
    def test_materialize_blocks_on_local_edit(self):
        """Materialize must detect that user modified a file locally and abort without overwriting."""
        # 1. Agent commits and materializes v1
        self.engine.commit(
            agent_id="agent0",
            message="Initial file",
            files={"code.py": b"print('version 1')\n"}
        )
        self.engine.materialize()

        # 2. User edits code.py locally in working directory
        local_file = os.path.join(self.temp_dir, "code.py")
        with open(local_file, "wb") as f:
            f.write(b"print('user modified version 1 locally')\n")

        # 3. Agent commits v2
        self.engine.commit(
            agent_id="agent0",
            message="Agent updates code",
            files={"code.py": b"print('version 2 by agent')\n"}
        )

        # 4. Materialize must preserve user's local edit and raise or report blockage!
        with self.assertRaises(Exception):
            self.engine.materialize()

        # Verify user content is still intact
        with open(local_file, "rb") as f:
            self.assertEqual(f.read(), b"print('user modified version 1 locally')\n")

    def test_materialize_blocks_on_local_edit_before_delete(self):
        """Materialize must not delete a file if the user edited it locally."""
        self.engine.commit(
            agent_id="agent0",
            message="Initial file",
            files={"delete_me.txt": b"original\n"}
        )
        self.engine.materialize()

        # User edits file locally
        local_file = os.path.join(self.temp_dir, "delete_me.txt")
        with open(local_file, "wb") as f:
            f.write(b"user precious local changes\n")

        # Agent commits deletion
        self.engine.staging.stage_delete("agent0", "delete_me.txt")
        self.engine.commit(agent_id="agent0", message="Delete file")

        # Materialize must fail/block instead of deleting user's precious changes
        with self.assertRaises(Exception):
            self.engine.materialize()

        self.assertTrue(os.path.exists(local_file), "User edited file must not be deleted!")

    def test_materialize_blocks_on_untracked_file(self):
        """Materialize must not overwrite an untracked local file created by user."""
        # User creates untracked file
        untracked = os.path.join(self.temp_dir, "untracked.txt")
        with open(untracked, "wb") as f:
            f.write(b"untracked user content\n")

        # Agent commits file with same name
        self.engine.commit(
            agent_id="agent0",
            message="Add file with same name as untracked",
            files={"untracked.txt": b"agent content\n"}
        )

        with self.assertRaises(Exception):
            self.engine.materialize()

        with open(untracked, "rb") as f:
            self.assertEqual(f.read(), b"untracked user content\n")

    # -------------------------------------------------------------
    # 3. Git Export must preserve files and history
    # -------------------------------------------------------------
    def test_git_export_preserves_unrelated_git_files(self):
        """Git has keep.md and AgGit has only new.txt: export must keep both."""
        # Initialize Git repo with keep.md
        subprocess.run(["git", "init"], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=self.temp_dir, check=True, capture_output=True)

        keep_file = os.path.join(self.temp_dir, "keep.md")
        with open(keep_file, "w") as f:
            f.write("# Git File That Must Be Preserved\n")
        subprocess.run(["git", "add", "keep.md"], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init git with keep.md"], cwd=self.temp_dir, check=True, capture_output=True)

        # AgGit commits only new.txt
        self.engine.commit(
            agent_id="agent0",
            message="AgGit only adds new.txt",
            files={"new.txt": b"new content\n"}
        )

        # Export to Git branch aggit/sync
        res = self.bridge.export_to_git(branch_name="aggit/sync")
        self.assertIn(res.status, ("success", "clean"))

        # Verify keep.md STILL EXISTS on aggit/sync branch in Git!
        ls_proc = subprocess.run(["git", "ls-tree", "-r", "--name-only", "aggit/sync"], cwd=self.temp_dir, capture_output=True, text=True)
        git_files = ls_proc.stdout.splitlines()
        self.assertIn("keep.md", git_files, "keep.md must NOT be deleted by partial AgGit export!")
        self.assertIn("new.txt", git_files, "new.txt must be exported to Git branch")

    def test_git_export_successive_commits_preserves_ancestry(self):
        """Two successive exports: first commit must be ancestor of second commit."""
        subprocess.run(["git", "init"], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=self.temp_dir, check=True, capture_output=True)

        init_file = os.path.join(self.temp_dir, "init.txt")
        with open(init_file, "w") as f:
            f.write("init\n")
        subprocess.run(["git", "add", "init.txt"], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=self.temp_dir, check=True, capture_output=True)

        # Export 1
        self.engine.commit(agent_id="agent0", message="Tx 1", files={"f1.txt": b"c1\n"})
        res1 = self.bridge.export_to_git(branch_name="aggit/sync", commit_message="Export 1")
        commit1 = res1.git_commit_hash
        self.assertIsNotNone(commit1)

        # Export 2
        self.engine.commit(agent_id="agent0", message="Tx 2", files={"f2.txt": b"c2\n"})
        res2 = self.bridge.export_to_git(branch_name="aggit/sync", commit_message="Export 2")
        commit2 = res2.git_commit_hash
        self.assertIsNotNone(commit2)

        # Check ancestry: is commit1 an ancestor of commit2?
        merge_base = subprocess.run(
            ["git", "merge-base", commit1, commit2],
            cwd=self.temp_dir,
            capture_output=True,
            text=True
        ).stdout.strip()
        self.assertEqual(merge_base, commit1, "Commit 1 must be ancestor of commit 2 in export branch!")

    def test_git_import_preserves_github_and_gitignore(self):
        """Git repo containing .github/ and .gitignore must import them into AgGit."""
        subprocess.run(["git", "init"], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=self.temp_dir, check=True, capture_output=True)

        os.makedirs(os.path.join(self.temp_dir, ".github", "workflows"), exist_ok=True)
        with open(os.path.join(self.temp_dir, ".github", "workflows", "ci.yml"), "w") as f:
            f.write("name: CI\n")
        with open(os.path.join(self.temp_dir, ".gitignore"), "w") as f:
            f.write("*.pyc\n")
        with open(os.path.join(self.temp_dir, "app.py"), "w") as f:
            f.write("print('hello')\n")

        subprocess.run(["git", "add", "."], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init with meta"], cwd=self.temp_dir, check=True, capture_output=True)

        tx_id = self.bridge.import_from_git("HEAD")
        tx = self.engine.journal.get_transaction(tx_id)
        self.assertIsNotNone(tx)

        changes = tx.changes
        self.assertIn(".github/workflows/ci.yml", changes, ".github files must NOT be skipped on import!")
        self.assertIn(".gitignore", changes, ".gitignore must NOT be skipped on import!")

    # -------------------------------------------------------------
    # 4. Path sanitization: Symlink escape rejection
    # -------------------------------------------------------------
    def test_symlink_escape_rejected(self):
        """Symlink inside repository pointing outside root must be rejected."""
        outside_dir = tempfile.mkdtemp(prefix="aggit_outside_")
        try:
            link_path = os.path.join(self.temp_dir, "symlink_dir")
            try:
                os.symlink(outside_dir, link_path)
            except (OSError, NotImplementedError):
                # Symlinks might require admin or developer mode on some Windows setups
                self.skipTest("Symlinks not supported in current Windows user environment")

            # Attempting to target symlink_dir/secret.txt must raise PathSecurityError
            with self.assertRaises(PathSecurityError):
                sanitize_repo_path(self.temp_dir, "symlink_dir/secret.txt")
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    # -------------------------------------------------------------
    # 5. Staging concurrency on same session
    # -------------------------------------------------------------
    def test_concurrent_staging_same_session(self):
        """16 concurrent staging calls on the same agent and session must all succeed without dropping entries."""
        agent_id = "agent_shared"
        session_id = "sess_concurrent"

        def stage_worker(i):
            self.engine.staging.stage_file(
                agent_id=agent_id,
                rel_path=f"file_{i}.txt",
                content=f"content from worker {i}\n".encode("utf-8"),
                session_id=session_id
            )
            return i

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(stage_worker, range(16)))

        self.assertEqual(len(results), 16)
        changes = self.engine.staging.get_staged_changes(agent_id, session_id)
        # All 16 files must be staged, none dropped
        self.assertEqual(len(changes), 16, f"Expected 16 staged files, found {len(changes)}")

    # -------------------------------------------------------------
    # 6. Journal index concurrency
    # -------------------------------------------------------------
    def test_two_journal_instances_stale_index_preservation(self):
        """Instance A and Instance B append transactions; B's transaction must not disappear from index."""
        storage = CASStorage(self.temp_dir)
        j1 = Journal(self.temp_dir, storage)
        j2 = Journal(self.temp_dir, storage)

        # Both load index
        j1.get_latest_transaction()
        j2.get_latest_transaction()

        # j1 appends tx1
        tx1 = j1.append_transaction(agent_id="a1", message="tx1", changes={})
        # j2 appends tx2
        tx2 = j2.append_transaction(agent_id="a2", message="tx2", changes={})

        # Fresh journal instance checks index
        j3 = Journal(self.temp_dir, storage)
        all_txs = [t.tx_id for t in j3.get_all_transactions()]
        self.assertIn(tx1.tx_id, all_txs, "tx1 must exist in journal")
        self.assertIn(tx2.tx_id, all_txs, "tx2 must exist in journal")

        # Lookup by ID in j3 must find both
        self.assertIsNotNone(j3.get_transaction(tx1.tx_id), "tx1 must be in index")
        self.assertIsNotNone(j3.get_transaction(tx2.tx_id), "tx2 must be in index")

    # -------------------------------------------------------------
    # 7. Successive pushes, idempotence, and workspace preservation
    # -------------------------------------------------------------
    def test_two_successive_pushes_to_bare_remote_without_force(self):
        """Two successive exports and pushes to a local bare remote work without force."""
        subprocess.run(["git", "init"], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=self.temp_dir, check=True, capture_output=True)

        bare_dir = tempfile.mkdtemp(prefix="aggit_test_bare_")
        try:
            subprocess.run(["git", "init", "--bare"], cwd=bare_dir, check=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "bare_origin", bare_dir], cwd=self.temp_dir, check=True, capture_output=True)

            # Export & push 1
            self.engine.commit(agent_id="a1", message="Commit 1", files={"doc1.txt": b"1\n"})
            res1 = self.bridge.export_to_git(branch_name="aggit/sync")
            self.assertEqual(res1.status, "success")
            push1 = self.bridge.push_to_remote(branch_name="aggit/sync", remote="bare_origin")
            self.assertTrue(push1["success"])

            # Export & push 2
            self.engine.commit(agent_id="a1", message="Commit 2", files={"doc2.txt": b"2\n"})
            res2 = self.bridge.export_to_git(branch_name="aggit/sync")
            self.assertEqual(res2.status, "success")
            push2 = self.bridge.push_to_remote(branch_name="aggit/sync", remote="bare_origin")
            self.assertTrue(push2["success"])

            # Verify bare remote has commit 2
            rev_bare = subprocess.run(["git", "rev-parse", "aggit/sync"], cwd=bare_dir, capture_output=True, text=True).stdout.strip()
            self.assertEqual(rev_bare, res2.git_commit_hash)
        finally:
            shutil.rmtree(bare_dir, ignore_errors=True)

    def test_reexport_same_state_is_idempotent(self):
        """Re-exporting the exact same AgGit revision does not create a duplicate commit."""
        subprocess.run(["git", "init"], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=self.temp_dir, check=True, capture_output=True)

        self.engine.commit(agent_id="a1", message="Commit", files={"x.txt": b"x\n"})
        res1 = self.bridge.export_to_git(branch_name="aggit/sync")
        self.assertEqual(res1.status, "success")

        # Second export of identical state
        res2 = self.bridge.export_to_git(branch_name="aggit/sync")
        self.assertIn(res2.status, ("cached", "no_changes"))
        self.assertEqual(res1.git_commit_hash, res2.git_commit_hash)

    def test_user_dirty_working_tree_and_staging_remain_intact(self):
        """User uncommitted working tree and git staging must remain completely intact during export."""
        subprocess.run(["git", "init"], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=self.temp_dir, check=True, capture_output=True)

        # User working directory dirty state
        user_dirty = os.path.join(self.temp_dir, "user_scratch.txt")
        with open(user_dirty, "w") as f:
            f.write("user unsaved notes\n")

        # User git staging dirty state
        user_staged = os.path.join(self.temp_dir, "user_staged.txt")
        with open(user_staged, "w") as f:
            f.write("user staged work\n")
        subprocess.run(["git", "add", "user_staged.txt"], cwd=self.temp_dir, check=True, capture_output=True)

        # AgGit commits and exports
        self.engine.commit(agent_id="a1", message="AgGit commit", files={"server.py": b"# server\n"})
        res = self.bridge.export_to_git(branch_name="aggit/sync")
        self.assertEqual(res.status, "success")

        # Verify user files are still intact
        self.assertTrue(os.path.exists(user_dirty))
        with open(user_dirty, "r") as f:
            self.assertEqual(f.read(), "user unsaved notes\n")

        # Verify git status in user workspace still has user_staged.txt in index
        st = subprocess.run(["git", "status", "--porcelain"], cwd=self.temp_dir, capture_output=True, text=True).stdout
        self.assertIn("A  user_staged.txt", st)
        self.assertIn("?? user_scratch.txt", st)

    def test_explicit_conflict_resolution(self):
        """Conflicted file can be explicitly resolved with resolve_conflict, clearing pending conflict."""
        tx0 = self.engine.commit("a0", "base", files={"f.txt": b"base\n"})
        self.engine.reconcile_state()
        self.engine.commit("a1", "edit 1", files={"f.txt": b"edit 1\n"}, base_revision=tx0.tx_id)
        self.engine.commit("a2", "edit 2", files={"f.txt": b"edit 2\n"}, base_revision=tx0.tx_id)

        _, conflicts = self.engine.reconcile_state()
        self.assertEqual(len(conflicts), 1)

        # Explicit resolution
        res_tx = self.engine.resolve_conflict("f.txt", resolved_bytes=b"clean manual resolution\n")
        self.assertIsNotNone(res_tx)

        # Reconcile again: must have ZERO conflicts now!
        unified, conflicts_after = self.engine.reconcile_state()
        self.assertEqual(len(conflicts_after), 0)
        self.assertEqual(unified.get("f.txt"), b"clean manual resolution\n")

if __name__ == "__main__":
    unittest.main()
