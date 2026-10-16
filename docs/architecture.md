# AgGit: System Architecture

🌐 **Translations / Idiomas:** English | [Português (Brasil)](translations/architecture.pt-BR.md) | [Español](translations/architecture.es.md)

**AgGit** is a native version control system designed for autonomous AI agents that eliminates traditional Git index lock contention (`.git/index.lock`), providing safe concurrency, 3-way semantic reconciliation, and isolated Git/GitHub synchronization.

---

## 1. Architectural Overview

The system is decoupled into five orthogonal layers:

```
┌─────────────────────────────────────────────────────────────┐
│                          AI Agents                          │
└──────────────┬──────────────────────────────┬───────────────┘
               │                              │
               ▼                              ▼
┌──────────────────────────────┐┌──────────────────────────────┐
│ Session-Isolated Staging     ││ Content-Addressable Storage  │
│ (.aggit/staging/*_<hash>.json)││ (.aggit/objects/xx/yy...)   │
└──────────────┬───────────────┘└──────────────▲───────────────┘
               │                               │
               ▼                               │
┌──────────────────────────────┐               │
│ Append-Only Event Journal    ├───────────────┘
│ (.aggit/journal/*.json)      │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Semantic Merge Engine        │
│ (3-Way: Python, JSON, Text)  │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐┌──────────────────────────────┐
│ Incremental Materialization  ││ Git / GitHub Bridge          │
│ (.aggit/checkpoints/latest)  ││ (Isolated Worktrees & Branches)│
└──────────────────────────────┘└──────────────────────────────┘
```

---

## 2. Core Subsystems

### 2.1 Content-Addressable Storage (CAS)
- **Hashing**: SHA-256 computed over the Git-compatible header `[type] [size]\0` and raw payload.
- **Short-circuiting**: Object existence is verified on disk **prior** to compression, avoiding redundant CPU cycles.
- **Atomicity**: Writes use temporary files isolated by PID, thread ID, and UUID (`.tmp_obj_<pid>_<uuid>.tmp`), with `fsync` and atomic rename (`os.replace` / `fs::rename`).
- **Integrity Validation**: Decompression validates SHA-256 integrity and enforces a 200 MB uncompressed limit to guard against zip bombs.

### 2.2 Namespaced Virtual Staging & Session Concurrency
- Rather than sharing a single monolithic `.git/index`, each `(agent_id, session_id)` pair maintains an isolated staging file protected against hash collisions (`.aggit/staging/<agent>_<hash>.json`).
- **Cross-Platform Session File Locks**: Staging updates acquire exclusive per-session file locks (`<path>.lock`) with exponential backoff and configurable timeouts (`msvcrt` on Windows, `fcntl` on POSIX systems via `aggit/lock_utils.py`).
- **Atomic Writes with Exclusive Tempfiles**: Staging writes go to unique temporary files (`.tmp_stg_<pid>_<thread>_<uuid>.tmp`) before atomic renaming, eliminating `[WinError 32]` sharing violations.
- **Generation Tracking**: Each write increments a monotonic `generation` counter. When commits conclude, staging is only pruned if the current generation is $\le$ the commit snapshot generation, preserving newer concurrent edits.
- **Explicit Deletions**: Full lifecycle support for additions, modifications, and deletions (`stage_delete`).

### 2.3 Append-Only Journal & Derived Index
- Every commit generates an immutable event with a full UUID v4 and nanosecond timestamp.
- Writes use `.tmp` suffixes to guarantee that concurrent `.json` readers never parse partial files.
- The derived index (`.aggit/journal_index.json`) synchronizes access under file locking. If the index is missing or out of sync with `.aggit/journal/`, it is automatically rebuilt using the append-only journal as the single source of truth.

### 2.4 Reconciliation Engine, DAG & Conflict Lifecycle
- **DAG Causal Ordering**: Events are topologically sorted by explicit ancestor references (`parents`), without relying on wall-clock synchronization across distributed agent nodes.
- **True Ancestor Base**: Merges compare against the real common ancestor (`base_revision` or LCA in the DAG), never assuming empty bases.
- **Semantic JSON Merging**: Recursively merges independent key additions (e.g., `{"port": 3000}` and `{"debug": true}`) cleanly without conflict, respects deletions, and differentiates missing keys from JSON `null`.
- **Python & Text Merging**: 3-way merge based on diff3 / `git merge-file`, preserving module docstrings, functions, conditionals, and decorators.
- **Persistent Conflict Lifecycle**:
  - Content divergences, modify/delete incompatibilities, and binary collisions are structured in the checkpoint (`pending_conflicts`).
  - Subsequent runs of `reconcile`, `status`, or `export-preview` preserve pending conflicts until explicitly resolved.
  - **Pure Dry-Run**: `--dry-run` calculates state entirely in memory without writing CAS blobs, updating checkpoints, or touching the working tree.
  - **Pipeline Gating**: Working directory materialization and Git exports are strictly blocked while conflicts remain unresolved.
  - **Explicit Resolution (`resolve_conflict`)**: Creates a reconciliation transaction that supersedes divergent DAG branches, persists the resolved content to CAS, and clears targeted conflict IDs.

### 2.5 3-State Safe Materialization
- **Pre-flight 3-Way Validation**:
  Prior to modifying disk files, the engine inspects: (1) Last recorded materialized manifest (`materialized_state.json`), (2) Current physical disk state, and (3) Desired reconciled state.
  - Detects **local user edits**: if disk content differs from the recorded manifest, materialization halts with an error to prevent data loss.
  - Detects **local deletions**: if a file was deleted manually on disk, restoration is omitted unless forced.
  - Detects **untracked files**: prevents overwriting unversioned files created outside AgGit.
  - Flag `--force`: allows overriding safety checks when intentional reset is required.
- **Atomic Writing**:
  Disk writes use temporary files (`.tmp_mat_<pid>_<thread>_<uuid>.tmp`) with `fsync` and atomic rename (`os.replace`). The manifest is only updated after physical disk confirmation.

### 2.6 Git & GitHub Bridge
- **Complete Workspace Isolation**: The primary working directory remains untouched. Commits are prepared inside isolated temporary Git worktrees (`aggit_worktree_*`) outside the workspace and cleanly discarded in `finally` blocks.
- **Branch Continuity**: Sequential exports to an existing Git branch check out the branch tip (`git checkout <branch>`) without destructive flags (`-B`), ensuring proper commit lineage (`commit 1 -> commit 2`).
- **Preservation of External Git Files**: Only AgGit-managed files are modified. Existing repository files (e.g., `.github/`, `README.md`, `keep.md`) are preserved unless explicitly deleted via `action: "delete"`.
- **Empty Git Repositories**: Bootstraps the root commit on empty repositories using empty-tree plumbing (`git commit-tree 4b825dc...`) and `git update-ref`, avoiding indexing untracked workspace files.
- **Idempotent Export Ledger**: Records composite keys (`revision::branch::parent_commit`) in `.aggit/git_export_map.json`, ensuring repeatable exports without duplicate commits.
- **Secure Pull Request Automation**: Reuses authenticated local GitHub CLI (`gh`) sessions, detecting existing open PRs without duplicate requests.

### 2.7 Path Containment & Security
- **Path Traversal Prevention**: Rejects absolute paths, drive prefixes, UNC paths, and parent traversals (`..`).
- **Symlink Resolution**: Verifies canonical paths (`os.path.realpath`) to guarantee target paths remain strictly inside the repository root.
- **Reserved Operating System Names**: Blocks Windows reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`) across all path components.
