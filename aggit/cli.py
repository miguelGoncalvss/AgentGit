"""
AgGit Command Line Interface (CLI).
Provides developer and agent commands for concurrent version control.
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
    stage_parser.add_argument("--path", default=".", help="Repository directory")

    # commit
    commit_parser = subparsers.add_parser("commit", help="Commit staged changes lock-free")
    commit_parser.add_argument("-m", "--message", required=True, help="Commit message")
    commit_parser.add_argument("--agent", required=True, help="Agent identifier")
    commit_parser.add_argument("--path", default=".", help="Repository directory")

    # status
    status_parser = subparsers.add_parser("status", help="Show current staging and transaction status")
    status_parser.add_argument("--path", default=".", help="Repository directory")

    # log
    log_parser = subparsers.add_parser("log", help="Show transaction log")
    log_parser.add_argument("--path", default=".", help="Repository directory")

    # reconcile
    reconcile_parser = subparsers.add_parser("reconcile", help="Reconcile and materialize multi-agent state to disk")
    reconcile_parser.add_argument("--path", default=".", help="Repository directory")

    # export-git
    export_parser = subparsers.add_parser("export-git", help="Export unified state to standard Git")
    export_parser.add_argument("-m", "--message", default=None, help="Git commit message")
    export_parser.add_argument("--path", default=".", help="Repository directory")

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
        results = engine.stage_files(args.agent, args.files)
        print(f"[AgGit] Agent '{args.agent}' staged {len(results)} files:")
        for f, sha in results.items():
            print(f"  + {f} (blob: {sha[:8]})")

    elif args.command == "commit":
        engine = AgGitEngine(repo_dir)
        tx = engine.commit(args.agent, args.message)
        print(f"[AgGit] Lock-free commit succeeded! [tx: {tx['tx_id']}] by '{args.agent}'")
        print(f"        Message: {tx['message']}")
        print(f"        Files affected: {len(tx['files'])}")

    elif args.command == "status":
        engine = AgGitEngine(repo_dir)
        staging_dir = os.path.join(engine.aggit_dir, "staging")
        staged_agents = []
        if os.path.exists(staging_dir):
            for f in os.listdir(staging_dir):
                if f.endswith(".json"):
                    agent_name = f[:-5]
                    staged_agents.append((agent_name, engine.staging.get_staged(agent_name)))
        
        print("[AgGit Status]")
        if staged_agents:
            print("Active Agent Staging Areas:")
            for agent, files in staged_agents:
                print(f"  Agent: {agent} ({len(files)} files staged)")
                for path, sha in files.items():
                    print(f"    - {path} -> {sha[:8]}")
        else:
            print("  No active agent staging areas.")

        txs = engine.journal.get_all_transactions()
        print(f"\nTotal Recorded Transactions in Journal: {len(txs)}")
        if txs:
            latest = txs[-1]
            print(f"Latest: [{latest['tx_id']}] by '{latest['agent_id']}': {latest['message']}")

    elif args.command == "log":
        engine = AgGitEngine(repo_dir)
        txs = engine.journal.get_all_transactions()
        print(f"[AgGit Log - {len(txs)} transactions]")
        for tx in reversed(txs):
            print(f"* tx {tx['tx_id']} | Agent: {tx['agent_id']} | Time: {tx['timestamp']}")
            print(f"  Message: {tx['message']}")
            print(f"  Files: {', '.join(tx['files'].keys())}")
            print()

    elif args.command == "reconcile":
        engine = AgGitEngine(repo_dir)
        count, conflicts = engine.materialize()
        print(f"[AgGit] Reconciled and materialized {count} files to working tree.")
        if conflicts:
            print(f"WARNING: Found {len(conflicts)} files with conflicts:")
            for c in conflicts:
                print(f"  ! {c}")
        else:
            print("Status: 100% clean merge across all agent mutations.")

    elif args.command == "export-git":
        engine = AgGitEngine(repo_dir)
        bridge = GitBridge(engine)
        rev = bridge.export_to_git(commit_message=args.message)
        print(f"[AgGit] Exported unified state to Git HEAD: {rev}")

if __name__ == "__main__":
    main()
