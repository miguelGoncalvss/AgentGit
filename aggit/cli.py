"""
AgGit Command Line Interface (CLI).
Exposes core version control, lock-free agent collaboration, conflict resolution, and Git/GitHub bridge commands.
"""

import sys
import os
import argparse
import json
from .engine import AgGitEngine
from .git_bridge import GitBridge

def main():
    parser = argparse.ArgumentParser(
        prog="aggit",
        description="AgGit: Lock-Free, Multi-Agent Native Version Control System"
    )
    subparsers = parser.add_subparsers(dest="command", help="AgGit commands")

    # init
    init_parser = subparsers.add_parser("init", help="Initialize an AgGit repository")
    init_parser.add_argument("--path", default=".", help="Target directory")

    # stage
    stage_parser = subparsers.add_parser("stage", help="Stage files for an agent")
    stage_parser.add_argument("files", nargs="+", help="Files to stage")
    stage_parser.add_argument("--agent", required=True, help="Agent identifier")
    stage_parser.add_argument("--session", default=None, help="Optional session identifier")
    stage_parser.add_argument("--path", default=".", help="Repository directory")

    # stage-delete
    stage_del_parser = subparsers.add_parser("stage-delete", help="Stage file deletions for an agent")
    stage_del_parser.add_argument("files", nargs="+", help="Files to mark as deleted")
    stage_del_parser.add_argument("--agent", required=True, help="Agent identifier")
    stage_del_parser.add_argument("--session", default=None, help="Optional session identifier")
    stage_del_parser.add_argument("--path", default=".", help="Repository directory")

    # commit
    commit_parser = subparsers.add_parser("commit", help="Commit staged changes lock-free")
    commit_parser.add_argument("-m", "--message", required=True, help="Commit message")
    commit_parser.add_argument("--agent", required=True, help="Agent identifier")
    commit_parser.add_argument("--session", default=None, help="Optional session identifier")
    commit_parser.add_argument("--base", default=None, help="Baseline revision (tx_id)")
    commit_parser.add_argument("--path", default=".", help="Repository directory")

    # status
    status_parser = subparsers.add_parser("status", help="Show current staging and transaction status")
    status_parser.add_argument("--json", action="store_true", help="Output status in JSON format")
    status_parser.add_argument("--path", default=".", help="Repository directory")

    # log
    log_parser = subparsers.add_parser("log", help="Show transaction log")
    log_parser.add_argument("-n", "--limit", type=int, default=20, help="Maximum transactions to show")
    log_parser.add_argument("--json", action="store_true", help="Output log in JSON format")
    log_parser.add_argument("--path", default=".", help="Repository directory")

    # reconcile
    reconcile_parser = subparsers.add_parser("reconcile", help="Reconcile and smart-materialize state to disk")
    reconcile_parser.add_argument("--dry-run", action="store_true", help="Preview reconciled state without modifying files")
    reconcile_parser.add_argument("--force", action="store_true", help="Force overwrite despite local uncommitted edits")
    reconcile_parser.add_argument("--path", default=".", help="Repository directory")

    # resolve
    resolve_parser = subparsers.add_parser("resolve", help="Explicitly resolve a pending conflict on a path")
    resolve_parser.add_argument("target_path", help="Path of the conflicted file")
    resolve_parser.add_argument("--file", default=None, help="File containing resolved content (omit to resolve by deletion)")
    resolve_parser.add_argument("--agent", default="resolver", help="Agent identifier")
    resolve_parser.add_argument("-m", "--message", default=None, help="Resolution message")
    resolve_parser.add_argument("--path", default=".", help="Repository directory")

    # export-preview
    preview_parser = subparsers.add_parser("export-preview", help="Dry-run preview of export to Git")
    preview_parser.add_argument("--branch", default="aggit/sync", help="Target Git branch name")
    preview_parser.add_argument("--base-branch", default="main", help="Base Git branch")
    preview_parser.add_argument("--revision", default=None, help="Specific AgGit revision tx_id")
    preview_parser.add_argument("--path", default=".", help="Repository directory")

    # export-git
    export_parser = subparsers.add_parser("export-git", help="Export unified state to an isolated Git branch")
    export_parser.add_argument("--branch", default="aggit/sync", help="Target Git branch name")
    export_parser.add_argument("--base-branch", default="main", help="Base Git branch")
    export_parser.add_argument("-m", "--message", default=None, help="Git commit message")
    export_parser.add_argument("--revision", default=None, help="Specific AgGit revision tx_id")
    export_parser.add_argument("--path", default=".", help="Repository directory")

    # push
    push_parser = subparsers.add_parser("push", help="Push exported branch to Git remote")
    push_parser.add_argument("--branch", default="aggit/sync", help="Branch name to push")
    push_parser.add_argument("--remote", default="origin", help="Git remote name")
    push_parser.add_argument("--path", default=".", help="Repository directory")

    # pr
    pr_parser = subparsers.add_parser("pr", help="Create or view Pull Request via GitHub CLI")
    pr_parser.add_argument("--branch", default="aggit/sync", help="Head branch for the PR")
    pr_parser.add_argument("--base-branch", default="main", help="Base target branch")
    pr_parser.add_argument("--title", default=None, help="Pull Request title")
    pr_parser.add_argument("--body", default=None, help="Pull Request body")
    pr_parser.add_argument("--path", default=".", help="Repository directory")

    # import-git
    import_parser = subparsers.add_parser("import-git", help="Import a Git commit snapshot as AgGit baseline")
    import_parser.add_argument("--ref", default="HEAD", help="Git commit SHA, branch, or ref")
    import_parser.add_argument("--path", default=".", help="Repository directory")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    repo_dir = os.path.abspath(args.path)

    if args.command == "init":
        engine = AgGitEngine.init(repo_dir)
        print(f"[AgGit] Initialized empty AgGit repository in {engine.aggit_dir}")

    elif args.command == "stage":
        engine = AgGitEngine(repo_dir)
        for filepath in args.files:
            full_path = os.path.join(repo_dir, filepath) if not os.path.isabs(filepath) else filepath
            if not os.path.exists(full_path):
                print(f"Error: File '{filepath}' not found.", file=sys.stderr)
                sys.exit(1)
            rel_path = os.path.relpath(full_path, repo_dir)
            sha = engine.stage_file(agent_id=args.agent, rel_path=rel_path, session_id=args.session)
            print(f"[AgGit] Staged '{rel_path}' for agent '{args.agent}' (blob: {sha[:8]})")

    elif args.command == "stage-delete":
        engine = AgGitEngine(repo_dir)
        for filepath in args.files:
            engine.stage_delete(agent_id=args.agent, rel_path=filepath, session_id=args.session)
            print(f"[AgGit] Staged deletion of '{filepath}' for agent '{args.agent}'")

    elif args.command == "commit":
        engine = AgGitEngine(repo_dir)
        try:
            tx = engine.commit(
                agent_id=args.agent,
                message=args.message,
                session_id=args.session,
                base_revision=args.base
            )
            print(f"[AgGit] Commit succeeded: [tx {tx.tx_id}] by '{tx.agent_id}'")
            print(f"        Message: {tx.message}")
            print(f"        Changes: {len(tx.changes)} file(s)")
            if tx.base_revision:
                print(f"        Base Revision: {tx.base_revision}")
        except Exception as e:
            print(f"Error committing changes: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "status":
        engine = AgGitEngine(repo_dir)
        staging_dir = engine.staging.staging_dir
        staged_agents = []
        if os.path.exists(staging_dir):
            for fn in os.listdir(staging_dir):
                if fn.endswith(".json") and not fn.startswith(".tmp_"):
                    staged_agents.append(fn)

        latest_tx = engine.journal.get_latest_transaction()
        all_txs = engine.journal.get_all_transactions()

        # Check pending conflicts
        cp = engine._load_latest_checkpoint()
        pending_conflicts = cp.get("pending_conflicts", [])

        if args.json:
            status_data = {
                "active_staging_files": len(staged_agents),
                "total_transactions": len(all_txs),
                "latest_transaction": latest_tx.to_dict() if latest_tx else None,
                "pending_conflicts_count": len(pending_conflicts),
                "pending_conflicts": pending_conflicts
            }
            print(json.dumps(status_data, indent=2))
        else:
            print(f"[AgGit Status - {repo_dir}]")
            print(f"Total Transactions: {len(all_txs)}")
            if latest_tx:
                print(f"Latest Revision:    {latest_tx.tx_id} by '{latest_tx.agent_id}' ({latest_tx.message})")
            else:
                print("Latest Revision:    genesis (no commits yet)")
            print(f"Active Staged Sessions: {len(staged_agents)}")
            if pending_conflicts:
                print(f"Pending Conflicts:  {len(pending_conflicts)} file(s) require explicit resolution:")
                for c in pending_conflicts:
                    print(f"  ! {c.get('path')} ({c.get('conflict_type')})")
            else:
                print("Pending Conflicts:  0 (clean)")

    elif args.command == "log":
        engine = AgGitEngine(repo_dir)
        txs = engine.journal.get_all_transactions()
        selected = txs[-args.limit:] if args.limit else txs
        if args.json:
            print(json.dumps([t.to_dict() for t in reversed(selected)], indent=2))
        else:
            print(f"[AgGit Log - showing {len(selected)} of {len(txs)} transactions]")
            for tx in reversed(selected):
                print(f"* tx {tx.tx_id} | Agent: {tx.agent_id} | Time: {tx.timestamp}")
                print(f"  Message: {tx.message}")
                if tx.base_revision:
                    print(f"  Base Revision: {tx.base_revision}")
                changes_summary = [f"{p} ({cr.action})" for p, cr in tx.changes.items()]
                print(f"  Changes: {', '.join(changes_summary[:8])}{' ...' if len(changes_summary) > 8 else ''}")
                print()

    elif args.command == "reconcile":
        engine = AgGitEngine(repo_dir)
        if args.dry_run:
            files, conflicts = engine.reconcile_state(dry_run=True)
            print(f"[AgGit Dry-run] Reconciled in-memory: {len(files)} files, {len(conflicts)} conflict(s).")
            for c in conflicts:
                print(f"  ! {c.path} ({c.conflict_type}: {c.details})")
        else:
            try:
                count, conflicts = engine.materialize(force=args.force)
                print(f"[AgGit] Reconciled and materialized {count} files to working tree.")
                if conflicts:
                    print(f"WARNING: Found {len(conflicts)} files with conflicts:")
                    for c in conflicts:
                        print(f"  ! {c.path} ({c.conflict_type}: {c.details})")
                else:
                    print("Status: 100% clean merge across all agent mutations.")
            except Exception as e:
                print(f"Error during materialization: {e}", file=sys.stderr)
                sys.exit(1)

    elif args.command == "resolve":
        engine = AgGitEngine(repo_dir)
        resolved_bytes = None
        if args.file:
            if not os.path.exists(args.file):
                print(f"Error: Resolution source file '{args.file}' not found.", file=sys.stderr)
                sys.exit(1)
            with open(args.file, "rb") as f:
                resolved_bytes = f.read()

        try:
            tx = engine.resolve_conflict(
                path=args.target_path,
                resolved_bytes=resolved_bytes,
                agent_id=args.agent,
                message=args.message
            )
            print(f"[AgGit] Conflict on '{args.target_path}' successfully resolved via tx {tx.tx_id[:8]}.")
        except Exception as e:
            print(f"Error resolving conflict: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "export-preview":
        engine = AgGitEngine(repo_dir)
        bridge = GitBridge(engine)
        preview = bridge.preview_export(
            target_revision=args.revision,
            branch_name=args.branch,
            base_branch=args.base_branch
        )
        print(json.dumps(preview, indent=2))

    elif args.command == "export-git":
        engine = AgGitEngine(repo_dir)
        bridge = GitBridge(engine)
        result = bridge.export_to_git(
            branch_name=args.branch,
            base_branch=args.base_branch,
            commit_message=args.message,
            target_revision=args.revision
        )
        print(f"[AgGit] Export status: {result.status.upper()}")
        print(f"        Branch: {result.branch_name}")
        if result.git_commit_hash:
            print(f"        Git Commit: {result.git_commit_hash}")
        print(f"        Message: {result.message}")
        if result.details:
            print(f"        Details: {json.dumps(result.details, indent=2)}")
        if result.status in ("conflict", "failed"):
            sys.exit(1)

    elif args.command == "push":
        engine = AgGitEngine(repo_dir)
        bridge = GitBridge(engine)
        res = bridge.push_to_remote(branch_name=args.branch, remote=args.remote)
        if res.get("success"):
            print(f"[AgGit] Successfully pushed branch '{res['branch']}' to remote '{res['remote']}'.")
        else:
            print(f"[AgGit] Push failed: {res.get('error')}", file=sys.stderr)
            print(f"        {res.get('message')}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "pr":
        engine = AgGitEngine(repo_dir)
        bridge = GitBridge(engine)
        res = bridge.create_or_update_pr(
            branch_name=args.branch,
            base_branch=args.base_branch,
            title=args.title,
            body=args.body
        )
        print(f"[AgGit PR] Status: {res.get('status')}")
        print(f"           Message: {res.get('message')}")
        if res.get("pr_url"):
            print(f"           URL: {res.get('pr_url')}")
        if res.get("action_required"):
            print(f"           Action Required: {res.get('action_required')}")
        if res.get("status") == "failed":
            sys.exit(1)

    elif args.command == "import-git":
        engine = AgGitEngine(repo_dir)
        bridge = GitBridge(engine)
        try:
            tx_id = bridge.import_from_git(commit_ref=args.ref)
            print(f"[AgGit] Successfully imported Git snapshot from '{args.ref}' as AgGit revision: {tx_id}")
        except Exception as e:
            print(f"Error importing from Git: {e}", file=sys.stderr)
            sys.exit(1)

if __name__ == "__main__":
    main()
