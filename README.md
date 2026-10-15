# AgGit: Version Control System for Multi-Agent AI Systems

[![Build & Test](https://github.com/miguelGoncalvss/AgentGit/actions/workflows/ci.yml/badge.svg)](https://github.com/miguelGoncalvss/AgentGit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Rust: 1.80+](https://img.shields.io/badge/Rust-1.80%2B-orange.svg)](https://www.rust-lang.org)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://www.python.org)

🌐 **Translations / Idiomas:** [English](README.md) | [Português (Brasil)](docs/translations/README.pt-BR.md) | [Español](docs/translations/README.es.md)

**AgGit** is a modern version control system designed specifically for **Multi-Agent Systems (MAS)**. It replaces Git's monolithic exclusive lock (`.git/index.lock`) with a concurrent append-only event journal, session-isolated staging areas, 3-way semantic reconciliation, and safe, isolated Git/GitHub integration.

---

## 📑 Table of Contents

- [Overview & Core Concepts](#-overview--core-concepts)
- [Concurrency & Durability Guarantees](#-concurrency--durability-guarantees)
- [Requirements & Installation](#-requirements--installation)
- [Quick Start Guide](#-quick-start-guide)
- [Conflict Resolution & Lifecycle](#-conflict-resolution--lifecycle)
- [3-State Safe Materialization](#-3-state-safe-materialization)
- [Git & GitHub Integration](#-git--github-integration)
- [Benchmarks](#-benchmarks)
- [Test Suite](#-test-suite)
- [Known Limitations](#-known-limitations)
- [Additional Technical Documentation](#-additional-technical-documentation)

---

## 💡 Overview & Core Concepts

To eliminate ambiguities in distributed agent environments, AgGit adopts formal definitions:

- **Agent (`agent_id`)**: The logical identity of the AI agent (e.g., `agent_backend`, `agent_qa`).
- **Session (`session_id`)**: The active execution context of the agent. Allows the same agent to execute concurrent parallel tasks without overwriting its own staging area.
- **Revision (`tx_id`)**: An immutable identifier (UUID v4) for an event published to the journal, containing metadata, authors, causal base, and file operations.
- **Base Revision (`base_revision`)**: The exact revision hash from which the agent started its work. Prevents merges from assuming empty bases or incorrect linear ancestors.
- **Checkpoint**: A recoverable derived snapshot (`.aggit/checkpoints/latest.json`) recording the incorporated causal boundary, enabling $O(1)$ incremental reconciliation.

---

## 🔒 Concurrency & Durability Guarantees

- **Atomic Publication**: CAS storage objects and journal events are written to exclusive temporary files (`.tmp`) with explicit `fsync` and renamed atomically via OS primitives (`os.replace` / `fs::rename`).
- **Safe Concurrent Reading**: In-flight temporary files are never treated as completed transactions by readers.
- **No Lost Updates in Staging**: Staging areas feature a monotonic `generation` counter and cross-platform per-session file locks (`<path>.lock`). Previous commits do not wipe files staged by concurrent operations.
- **Cryptographic Integrity**: All CAS objects have their SHA-256 hashes validated during read and decompression.

---

## 🛠️ Requirements & Installation

### Prerequisites
- **Git** 2.30+ installed.
- **Python** 3.10+ (for Python CLI execution).
- **Rust** 1.80+ and Cargo (for compiling the native Rust binary).
- **GitHub CLI (`gh`)** (optional, required only for automated Pull Request creation).

### Native Rust Build
```bash
cd aggit-rs
cargo build --release
# The binary is generated at: target/release/aggit (or aggit.exe on Windows)
```

---

## 🚀 Quick Start Guide

### 1. Initialize Repository
```bash
# Via native binary:
./aggit init

# Or via Python module:
python -m aggit.cli init
```

### 2. Staging & Committing
```bash
# Agent A stages files within its isolated session namespace
./aggit stage src/auth.py --agent agent_backend --session sprint_1

# Agent B stages files simultaneously without index contention
./aggit stage src/ui.py --agent agent_frontend --session sprint_1

# Agent A commits specifying message and session context
./aggit commit -m "feat: user authentication endpoint" --agent agent_backend --session sprint_1

# Agent B commits concurrently
./aggit commit -m "feat: login interface" --agent agent_frontend --session sprint_1
```

### 3. File Deletion in Staging
```bash
./aggit stage-delete src/legacy.py --agent agent_backend
./aggit commit -m "chore: remove deprecated file" --agent agent_backend
```

### 4. Status & History
```bash
./aggit status
./aggit log -n 5
```

### 5. Reconcile & Materialize to Working Tree
Incremental reconciliation processes only transactions appended since the last checkpoint and writes only modified files:
```bash
# Dry-run calculation without disk or state mutation:
python -m aggit.cli reconcile --dry-run

# Reconcile with 3-state pre-flight validation:
python -m aggit.cli reconcile

# Force overwrite local changes if intentionally needed:
python -m aggit.cli reconcile --force
```

### 6. Explicit Conflict Resolution
Unresolved conflicts persist across restarts in checkpoints and block materialization and Git export until resolved:
```bash
# Resolve conflict by providing resolved content inline:
python -m aggit.cli resolve src/config.py --content "PORT = 8080\nDEBUG = False" -m "fix: resolve port conflict" --agent agent_lead

# Or resolve conflict from a file on disk:
python -m aggit.cli resolve src/config.py --file src/config_resolved.py -m "fix: resolved config"
```

---

## ⚖️ Conflict Resolution & Lifecycle

AgGit's 3-way reconciliation engine classifies and handles divergence deterministically:

1. **Orthogonal Modifications**:
   - Distinct files or non-overlapping line ranges in text files are automatically merged without human intervention.
2. **JSON Files**:
   - Independent keys added by different agents (e.g., `{"port": 3000}` and `{"debug": true}`) are cleanly merged recursively into the base dictionary without conflict.
3. **Persistent Conflict Lifecycle**:
   - Overlapping edits, modify/delete incompatibilities, and binary divergence are stored in Checkpoint Schema v2 (`pending_conflicts`).
   - Re-running `reconcile`, `status`, or `export-preview` **preserves pending conflicts** until explicit resolution.
   - Materialization to disk and export to Git are **strictly blocked** while pending conflicts exist.
4. **Traceable Resolution**:
   - The `aggit resolve` command writes a formal resolution transaction to the journal DAG, superseding the divergent revisions and unblocking the pipeline.

---

## 🛡️ 3-State Safe Materialization

To protect human developer edits and prevent data loss, materialization executes a 3-state pre-flight validation:
- Compares: (1) Last recorded materialized manifest, (2) Current physical disk state, and (3) Desired reconciled state.
- Detects:
  - **Local manual edits**: if physical disk content diverges from the recorded manifest, the operation is aborted.
  - **Local deletions**: if a file was manually deleted by the user, accidental restoration is prevented.
  - **Untracked files**: prevents AgGit from silently overwriting files created outside version control.
- Atomic write: updates are written to exclusive temporary files (`.tmp_mat_<pid>_<thread>_<uuid>.tmp`) with `fsync` and atomic replacement (`os.replace`). The manifest is only updated after physical disk confirmation.

---

## 🐙 Git & GitHub Integration

AgGit synchronizes multi-agent work with traditional Git without interfering with the user's primary working tree:

```bash
# 1. In-memory Dry-Run: inspect plan and conflicts without touching Git or checkpoints
python -m aggit.cli export-preview --branch feature/agent-sprint

# 2. Isolated export: creates a clean commit in a temporary worktree preserving branch continuity
python -m aggit.cli export-git --branch feature/agent-sprint -m "feat: autonomous sprint sync"

# 3. Explicit push:
python -m aggit.cli push --branch feature/agent-sprint --remote origin

# 4. Create or reuse Pull Request via GitHub CLI:
python -m aggit.cli pr --branch feature/agent-sprint --base-branch main

# 5. Import existing Git commit as base:
python -m aggit.cli import-git --ref HEAD
```

- **Branch Continuity**: Sequential exports to the same branch maintain commit lineage (`commit 1 -> commit 2`) without destructive force resets (`-B`).
- **File Preservation**: Unrelated files on the Git branch (such as `keep.md`, `README.md`, `.github/`) are strictly preserved. Only files explicitly recorded as `action: "delete"` in AgGit are deleted from Git.
- **Empty Repositories**: Supports root commit creation on empty Git repositories using empty tree plumbing (`git commit-tree 4b825dc...`) without capturing uncommitted files from the workspace.

---

## 📊 Benchmarks

Benchmark executed with **20 concurrent AI agents** under realistic workload on Windows 11 (NVMe PCIe Gen4 SSD, 8 concurrent threads):

| System | Success Rate | Error Rate | Total Time | Throughput (ops/s) | Latency p50 | Latency p95 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Git (Shared Index)** | 7/20 | **65.0%** | 0.56s | 12.61 | 128ms | 187ms |
| **Git (Worktree per Agent)** | 20/20 | 0.0% | 1.26s | 15.83 | 377ms | 389ms |
| **AgGit (Python Engine)** | 20/20 | **0.0%** | 1.12s | 17.89 | 66ms | 283ms |
| **AgGit (Rust Native Engine)** | 20/20 | **0.0%** | **0.10s** | **207.99** | **19ms** | **31ms** |

*All AgGit runs include 100% cryptographic SHA-256 byte verification of all generated files.*

To reproduce benchmarks with exact percentile calculations and verification:
```bash
python benchmark.py
```
See [docs/benchmarks.md](docs/benchmarks.md) for full details.

---

## 🧪 Test Suite

50 comprehensive automated test scenarios across Python and 13 native Rust integration tests validate staging session locking, Windows path normalization & symlink containment, 3-way Git export matrix, conflict identity & lifecycle, JSON recursive merge, auto-healing persistence, pure dry-run CAS isolation, and linear reconciliation scaling:

```bash
# Run all Python regression and deep integrity test suites:
python -m unittest discover -s tests -v

# Or individually:
python -m unittest tests/test_deep_integrity_regressions.py -v
python tests/test_all_regressions.py -v
python tests/test_revisions_export_integrity.py -v

# Run native Rust test suite:
cd aggit-rs && cargo test
```

---

## ⚠️ Known Limitations

1. **POSIX File Attributes**: Advanced permissions (such as `chmod +x` executable bits) and in-repo symlinks are not yet serialized in current CAS blobs.
2. **Continuous Bidirectional Sync**: AgGit supports initial Git commit import and isolated export to Git branches. Branches receiving continuous concurrent commits directly in Git require explicit `import-git` workflows.
3. **GitHub CLI**: Automated Pull Request creation relies on the `gh` utility being installed and authenticated.

---

## 📚 Additional Technical Documentation

- [System Architecture](docs/architecture.md)
- [Storage Format & Schemas](docs/storage-format.md)
- [Git & GitHub Integration Guide](docs/github-integration.md)
- [Benchmark Methodology & Results](docs/benchmarks.md)
- [Format Compatibility & Migration Guide](docs/migration.md)
