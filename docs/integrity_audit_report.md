# Integrity, Concurrency & Git Export Audit Report (AgGit)

🌐 **Translations / Idiomas:** English | [Português (Brasil)](translations/integrity_audit_report.pt-BR.md) | [Español](translations/integrity_audit_report.es.md)

**Date:** October 15, 2026  
**Execution Environment:** Windows 11 Pro x64, AMD/Intel Multi-Core, NVMe PCIe Gen4 SSD, Python 3.13.7, Rust 1.99.0, Git 2.49.0  
**Working Branch:** `fix/aggit-deep-integrity-windows-and-export`  
**Reference Comparison:** Commit `309df61c45a36357adc0d7326aa4836143e2586e`

---

## 1. Comparative Analysis: HEAD vs. `309df61c45a36357adc0d7326aa4836143e2586e`

| Audited Area | State in `309df61c45a36357adc0d7326aa4836143e2586e` | State in Current HEAD (`fix/aggit-deep-integrity-windows-and-export`) |
| :--- | :--- | :--- |
| **Windows Path Validation** | `_validate_safe_path` entered infinite loops on junction/symlink evaluation because `os.path.dirname("C:\\") == "C:\\"`. Prefix checking (`target.startswith(base)`) allowed directory escapes to sibling folders (e.g. `/work_external` accepted under `/work`). | Implemented `is_path_contained` using `os.path.commonpath` and `os.path.normcase`, ensuring strict component-wise path containment. Added explicit root loop break (`parent == cur`). |
| **Git Export 3-Way Matrix** | Human divergence scenarios on the export branch failed: AgGit deleting a file modified by a human deleted the file without conflict; AgGit modifying a file deleted by a human silently recreated it; conflicting concurrent additions were overwritten; and sequential exports reverted integrated human edits. | Full 3-way divergence matrix implemented in `prepare_export_plan`: detects `modify_delete`, `delete_modify`, and `add_add_divergence` conflicts. Sequential exports perform 3-way merges comparing `last_aggit_bytes` (base), `cur_git_bytes` (Git branch tip with integrated human edits), and `cur_aggit_bytes` (new AgGit state). |
| **Conflict Identity & Lifecycle** | Conflicts used non-deterministic UUIDs or SHAs without sorted version ordering. Resolving an older conflict could mistakenly dismiss newly introduced concurrent conflicts on the same path. | Conflict ID is deterministically generated via SHA-256 (`file_path::internal_path::conflict_type::base_sha::sorted(a_sha, b_sha)`). `resolve_conflict` takes `target_conflict_ids` and reconciliation filters only the targeted conflicts, preserving unresolved concurrent conflicts from other agents. |
| **Auto-Healing Persistence & Pure Dry-Run** | If `journal_index.json` was missing or lagging behind journal files, new engine instances failed to load recent history. Dry-run calls (`preview_export`) caused disk mutations by saving checkpoints or regenerating indices. | Added `read_only` mode to `Journal`. `_load_index` detects missing or out-of-sync indices and auto-heals in memory from transaction files. Dry-run guarantees **zero disk mutations** (no temporary CAS blobs, checkpoints, or indices written). |
| **Reconciliation Scalability** | Sequential transaction reconciliation recomputed full trees from root by scanning the entire journal history ($O(N^2)$ disk reads). | Implemented revision tree memoization and incremental fast-path deltas against the immediate parent commit in `get_revision_tree`, eliminating redundant quadratic transaction reads. |
| **CI Workflow Hardening** | Actions workflow ran hardcoded individual test scripts, failed to discover newly added test suites, and lacked job timeouts. | CI updated with `timeout-minutes: 15` on jobs and steps. Test runner configured for full automatic discovery via `python -m unittest discover -s tests -v`. |
| **Strict Benchmark Verification** | `benchmark.py` relied on weak existence and prefix checks (`exists`, `startswith`) and maintained a fallback to a stale root binary. Rust benchmarks lacked byte verification. | Strict byte-by-byte SHA-256 verification of all output files across Python, Rust, and Git worktrees. Stale binary fallback removed; the native Rust binary is strictly built from sources in `aggit-rs/target/release/aggit.exe`. |

---

## 2. Modified Files & Key Implementations

1. **`aggit/path_utils.py`**:
   - Added `is_path_contained(parent_dir, target_path)` with `os.path.realpath`, `os.path.normcase`, and `os.path.commonpath`.
   - Hardened `sanitize_repo_path` against UNC paths, Windows reserved devices, and drive prefixes.
2. **`aggit/git_bridge.py`**:
   - `_validate_safe_path`: explicit filesystem root break (`parent == curr`) and `is_path_contained` validation.
   - `prepare_export_plan`: 3-way matrix covering scenarios A, B, and C, preserving integrated human edits across successive exports.
   - CAS-compatible SHA computation (`blob <len>\0...`) for export tree blobs.
   - Guarded with `journal.read_only = True` during `preview_export` and `prepare_export_plan`.
3. **`aggit/semantic_merge.py`**:
   - `MergeConflict.compute_conflict_id` with deterministic SHA-256 hash and sorted version SHAs.
   - Assigned `base_sha`, `a_sha`, and `b_sha` across all divergence paths.
4. **`aggit/engine.py`**:
   - `resolve_conflict`: supports `target_conflict_ids` stored in metadata.
   - `reconcile_state`: granular filtering by `target_conflict_ids`, preserving concurrent conflicts.
   - Linearized reconciliation through revision memoization in `get_revision_tree`.
5. **`aggit/journal.py`**:
   - Added `read_only` mode.
   - Auto-healing in `_load_index` when transaction files diverge from `journal_index.json`.
6. **`aggit-rs/src/semantic.rs`**:
   - Implemented `compute_conflict_id` and `recompute_id` with SHA-256 and sorted versions.
   - Attached version metadata across all conflict branches (`modify_delete`, `binary`, `json_key`, text).
7. **`aggit-rs/src/engine.rs`**:
   - Migrated `conflicts_by_path` to `HashMap<String, Vec<MergeConflict>>`.
   - Updated `resolve_conflict` with `target_conflict_ids`.
   - Path sanitization in `materialize` before filesystem operations.
8. **`aggit-rs/src/main.rs`**:
   - Subcommand `benchmark`: strict byte-level verification for each worker payload.
9. **`benchmark.py`**:
   - Binary write mode (`wb`) to eliminate CRLF/LF line-ending translations on Windows.
   - Strict byte-level equality validation for Python, Rust, and Git worktrees.
   - Source compilation from `aggit-rs/target/release/`.
10. **`.github/workflows/ci.yml`**:
    - Automatic discovery with `python -m unittest discover -s tests -v`.
    - Added `timeout-minutes: 15`.
11. **`tests/test_deep_integrity_v2.py`** *(new)*:
    - 10 regression scenarios verifying Windows symlink termination, directory escape blocks, export matrix A/B/C, index auto-healing, pure dry-runs, and linear reconciliation scaling.

---

## 3. Benchmark Methodology & Results

Executed on Windows 11 Pro (NVMe PCIe Gen4 SSD, 8 concurrent threads, Python 3.13, Rust 1.99, Git 2.49):

| Tested System | Success | Error Rate | Total Time (s) | Throughput (ops/s) | Latency p50 | Latency p95 | Content Verified |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Git (Shared Index)** | 4/20 | **80.0%** | 0.52s | 7.71 | 107ms | 182ms | Yes (4 files) |
| **Git (Worktree per Agent)** | 20/20 | 0.0% | 1.23s | 16.27 | 359ms | 417ms | Yes (100% verified) |
| **AgGit (Python Engine)** | 20/20 | **0.0%** | 0.88s | 22.66 | 107ms | 490ms | Yes (100% verified) |
| **AgGit (Rust Native Engine)** | 20/20 | **0.0%** | **0.09s** | **214.50** | **18ms** | **22ms** | Yes (100% verified) |

### Performance Highlights:
- **Git with Shared Index** failed 80% of transactions due to `.git/index.lock` contention.
- **Git with Worktrees** attained 100% success, but file-system directory and branch overhead pushed $p_{95}$ latency to 417ms.
- **AgGit (Python Engine)** outperformed Git worktrees at 22.66 ops/s and 0.88s total duration.
- **AgGit (Rust Native Engine)** achieved **214.50 ops/s** and a $p_{50}$ latency of **18ms** (**13.2x faster** than Git worktrees) with 100% byte-verified data integrity.

---

## 4. Automated Test Suite Coverage

- **Complete Python Test Suite (`python -m unittest discover -s tests -v`):**
  - **50 tests executed, 50 passed** (1 external symlink test skipped due to non-elevated user environment on Windows).
  - Total execution duration: ~22.7 seconds.
- **Native Rust Integration Suite (`cargo test --manifest-path aggit-rs/Cargo.toml`):**
  - **13 integration tests executed, 13 passed**.
  - Total execution duration: ~0.33 seconds.
