# Git & GitHub Integration

🌐 **Translations / Idiomas:** English | [Português (Brasil)](translations/github-integration.pt-BR.md) | [Español](translations/github-integration.es.md)

**AgGit** serves as a concurrent multi-agent version control system while seamlessly bridging with traditional Git and GitHub workflows.

---

## 1. Safety & Isolation Principles

1. **Untouched Primary Workspace**:
   - Merge conflicts and staging operations never corrupt the user's primary working tree. Export planning is computed 100% in-memory.
2. **Isolated Git Worktrees**:
   - Git exports generate temporary worktree directories outside the repository root (`aggit_worktree_*`) and commit to a designated export branch (default: `aggit/sync`).
3. **Restricted File Scope**:
   - Only paths explicitly present in the export plan are staged (`git add -- <paths>`). Blind additions like `git add .` are strictly avoided.
4. **Idempotence**:
   - An internal mapping ledger (`.aggit/git_export_map.json`) pairs AgGit revisions to Git commit hashes. Exporting the same revision multiple times does not produce duplicate commits.
5. **Secure Authentication via GitHub CLI**:
   - AgGit never stores, prompts for, or transfers personal access tokens. All GitHub interactions delegate to the official `gh` CLI utility, reusing the user's active authenticated session.
6. **Branch Continuity Without Resets**:
   - When exporting sequential commits to the same branch, AgGit reuses the existing branch tip as direct ancestor (`commit 1 -> commit 2`), avoiding destructive force resets (`-B`) that destroy history.
7. **Preservation of External Git Files**:
   - Files existing on the Git branch that are unmanaged by AgGit (e.g., `.github/workflows/`, `keep.md`, `README.md`) are strictly preserved. Only files carrying an explicit deletion record (`action: "delete"`) in AgGit are removed from Git.
8. **Safe Plumbing in Empty Repositories**:
   - When targeting a freshly created Git repository with no root commit, AgGit creates the initial commit using low-level plumbing (`git commit-tree 4b825dc...` with the standard empty-tree SHA-1) and `git update-ref`, ensuring uncommitted local changes in the working tree are not accidentally captured.

---

## 2. Integration Command Workflow

### 2.1 Export Preview (Dry-Run)
Inspect modified files and detected conflicts without creating commits or mutating Git:
```bash
python -m aggit.cli export-preview --branch feature/agent-sprint
```

### 2.2 Local Git Export
Exports the reconciled multi-agent state to an isolated Git branch:
```bash
python -m aggit.cli export-git --branch feature/agent-sprint -m "feat: multi-agent autonomous sprint sync"
```

### 2.3 Explicit Publishing (Push)
Pushing to remote repositories only happens via explicit command:
```bash
python -m aggit.cli push --branch feature/agent-sprint --remote origin
```

### 2.4 Pull Request Creation (via GitHub CLI)
Opens a PR on GitHub or returns the URL of an existing open PR:
```bash
python -m aggit.cli pr --branch feature/agent-sprint --base-branch main --title "Feature: Multi-Agent Sprint"
```

### 2.5 Importing Git Commits into AgGit
Initializes a new multi-agent branch basing work off an existing Git commit:
```bash
python -m aggit.cli import-git --ref HEAD
```
