"""
AgGit Command Line Interface (CLI).
Exposes core version control, lock-free agent collaboration, and Git/GitHub bridge commands.
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
    reconcile_parser.add_argument("--path", default=".", help="Repository directory")

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

    repo_dir = getattr(args, "path", ".")

    if args.command == "init":
        engine = AgGitEngine.init(repo_dir)
        print(f"[AgGit] Initialized empty AgGit repository in {os.path.abspath(repo_dir)}/.aggit/")

    elif args.command == "stage":
        engine = AgGitEngine(repo_dir)
        results = engine.staging.stage_files(args.agent, args.files, session_id=args.session)
        print(f"[AgGit] Agent '{args.agent}' staged {len(results)} files:")
        for f, sha in results.items():
            print(f"  + {f} (blob: {sha[:8]})")

    elif args.command == "stage-delete":
        engine = AgGitEngine(repo_dir)
        for f in args.files:
            engine.staging.stage_delete(args.agent, f, session_id=args.session)
        print(f"[AgGit] Agent '{args.agent}' staged deletion for {len(args.files)} files.")

    elif args.command == "commit":
        engine = AgGitEngine(repo_dir)
        tx = engine.commit(
            agent_id=args.agent,
            session_id=args.session,
            message=args.message,
            base_revision=args.base
        )
        print(f"[AgGit] Lock-free commit succeeded! [tx: {tx.tx_id}] by '{args.agent}'")
        print(f"        Message: {tx.message}")
        print(f"        Changes: {len(tx.changes)}")
        if tx.base_revision:
            print(f"        Base: {tx.base_revision[:8]}")

    elif args.command == "status":
        engine = AgGitEngine(repo_dir)
        staging_dir = os.path.join(engine.aggit_dir, "staging")
        staged_agents = []
        if os.path.exists(staging_dir):
            for f in os.listdir(staging_dir):
                if f.endswith(".json"):
                    try:
                        with open(os.path.join(staging_dir, f), "r", encoding="utf-8") as sfile:
                            data = json.load(sfile)
                            staged_agents.append(data)
                    except Exception:
                        pass

        txs = engine.journal.get_all_transactions()
        latest = txs[-1] if txs else None

        if args.json:
            out = {
                "active_staging_sessions": staged_agents,
                "total_transactions": len(txs),
                "latest_tx": latest.to_dict() if latest else None
            }
            print(json.dumps(out, indent=2))
        else:
            print("[AgGit Status]")
            if staged_agents:
                print(f"Active Staging Sessions ({len(staged_agents)}):")
                for s in staged_agents:
                    entries = s.get("entries", {})
                    print(f"  Agent: {s.get('agent_id')} (session: {s.get('session_id')}, gen: {s.get('generation')}, {len(entries)} files)")
                    for path, cr in entries.items():
                        sha = cr.get("blob_sha") or "deleted"
                        print(f"    - [{cr.get('action')}] {path} -> {sha[:8]}")
            else:
                print("  No active staging sessions.")

            print(f"\nTotal Recorded Transactions in Journal: {len(txs)}")
            if latest:
                print(f"HEAD: [{latest.tx_id[:8]}] by '{latest.agent_id}': {latest.message}")

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
        count, conflicts = engine.materialize()
        print(f"[AgGit] Reconciled and materialized {count} files to working tree.")
        if conflicts:
            print(f"WARNING: Found {len(conflicts)} files with conflicts:")
            for c in conflicts:
                print(f"  ! {c.path} ({c.conflict_type}: {c.details})")
        else:
            print("Status: 100% clean merge across all agent mutations.")

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

    elif args.command == "push":
        engine = AgGitEngine(repo_dir)
        bridge = GitBridge(engine)
        res = bridge.push_to_remote(branch_name=args.branch, remote=args.remote)
        if res.get("success"):
            print(f"[AgGit] Successfully pushed branch '{res['branch']}' to remote '{res['remote']}'.")
        else:
            print(f"[AgGit] Push failed: {res.get('error')}")
            print(f"        {res.get('guidance')}")
            if res.get("stderr"):
                print(f"        stderr: {res['stderr']}")

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
        if res.get("guidance"):
            print(f"           Guidance: {res.get('guidance')}")

    elif args.command == "import-git":
        engine = AgGitEngine(repo_dir)
        bridge = GitBridge(engine)
        tx_id = bridge.import_from_git(commit_ref=args.ref)
        print(f"[AgGit] Successfully imported Git snapshot from '{args.ref}' as AgGit revision: {tx_id}")

if __name__ == "__main__":
    main()
