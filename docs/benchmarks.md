# Benchmark Methodology & Performance Evaluation

🌐 **Translations / Idiomas:** English | [Português (Brasil)](translations/benchmarks.pt-BR.md) | [Español](translations/benchmarks.es.md)

This document details the benchmarking procedures, evaluation metrics, and comparative performance analysis between traditional Git and AgGit.

---

## 1. Methodology

The benchmark measures system throughput and latency under high concurrent load with multiple autonomous writers attempting simultaneous commits in the same repository:

1. **Git (Shared Index)**: Simulates standard Git usage where concurrent agents share the same working tree and the monolithic `.git/index`.
2. **Git (Worktree per Agent)**: Creates an isolated physical branch and disk worktree per agent, eliminating index contention at the cost of substantial disk I/O overhead.
3. **AgGit (Python Engine)**: Employs virtual namespaced staging with cross-platform session file locks, an append-only event journal, and incremental DAG reconciliation in Python.
4. **AgGit (Rust Native Engine)**: Leverages hardware-accelerated CAS with pre-compression object existence checks and multithreading via Rayon.

### 1.1 Workload Parameters & Metrics
- **Workload**: 20 autonomous agents running under a concurrency pool of 8 to 12 workers (`ProcessPoolExecutor(max_workers=8)` in Python and Rayon CPU thread pool in Rust).
- **Latency Samples & Percentiles**: Per-agent elapsed time (file generation, staging, and commit) is measured in seconds. Median latency ($p_{50}$) is calculated via `statistics.median(latencies)` and the 95th percentile ($p_{95}$) via quantile interpolation `statistics.quantiles(latencies, n=20)[18]`.
- **Strict Byte-Level Content Verification (`content_verified`)**: Across all systems, an execution is only considered successful if:
  1. All 20 expected physical files exist on disk.
  2. The exact byte contents of each file match the original agent payload via SHA-256.
  3. AgGit reconciliation detects exactly 0 spurious conflicts.
  If any file is missing or corrupted, the benchmark returns a non-zero exit code.

---

## 2. Test Environment Results

Executed on Windows 11 Pro (NVMe PCIe Gen4 SSD, 8 concurrent threads, Python 3.13, Rust 1.99, Git 2.49):

| System | Success | Error Rate | Total Time (s) | Throughput (ops/s) | Latency p50 | Latency p95 | Content Verified |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Git (Shared Index)** | 4/20 | **80.0%** | 0.52s | 7.71 | 107ms | 182ms | Yes (4 files) |
| **Git (Worktree per Agent)** | 20/20 | 0.0% | 1.23s | 16.27 | 359ms | 417ms | Yes (100% verified) |
| **AgGit (Python Engine)** | 20/20 | **0.0%** | 0.88s | 22.66 | 107ms | 490ms | Yes (100% verified) |
| **AgGit (Rust Native Engine)** | 20/20 | **0.0%** | **0.09s** | **214.50** | **18ms** | **22ms** | Yes (100% verified) |

### Comparative Insights:
- **Git with Shared Index** suffers severe lock contention: 80% of commit attempts fail immediately with `.git/index.lock` errors (`Unable to create '.git/index.lock': File exists`).
- **Git with Worktrees** resolves index contention with 100% success, but creating physical branches and copying directories imposes heavy file-system I/O overhead, leading to a $p_{95}$ latency of 417ms.
- **AgGit (Python Engine)** avoids global locks by delegating to session-isolated staging files. Even including full 3-way reconciliation and physical materialization to disk, it outpaces Git Worktrees (22.66 ops/s vs. 16.27 ops/s).
- **AgGit (Rust Native Engine)** dramatically cuts CPU and memory overhead: native CAS and constant-time $O(1)$ journal appending achieve **214.50 ops/s** with a $p_{50}$ latency of just 18ms — **13.2x faster** than Git Worktrees, with 100% byte-verified data integrity.

---

## 3. High-Capacity Scaling: 70 Simultaneous Agents Benchmark (Rust Native Engine)

To stress-test AgGit under massive autonomous agent workloads, the Rust engine was evaluated with an average of **70 autonomous agents committing concurrently across 70 dedicated OS threads**.

The test executed a multi-round empirical evaluation (10 rounds, 700 total atomic transactions) measuring commit speed, DAG reconciliation, disk materialization, throughput, latency percentiles ($p_{50}, p_{95}, p_{99}$), and strict byte-for-byte integrity:

| Metric | Result (70 Concurrent Agents / 70 Threads) |
| :--- | :---: |
| **Total Commits Executed (10 Rounds)** | **700 / 700 (100.0% Success Rate)** |
| **Failed Transactions / Contention Errors** | **0 (0.00% Error Rate)** |
| **Average Commit Phase Time** | **0.211s** |
| **Average Reconcile & Materialize Time** | **0.258s** |
| **Average Total End-to-End Time** | **0.468s** |
| **Empirical Throughput** | **153.31 ops/sec** |
| **Latency $p_{50}$ (Median)** | **168.4ms** (0.1684s) |
| **Latency $p_{95}$** | **205.1ms** (0.2051s) |
| **Latency $p_{99}$** | **209.0ms** (0.2090s) |
| **Byte-Level Payload Verification** | **100% Verified** (all 700 files SHA-256 matched) |

### Key Architectural Factors Enabling 70+ Agent Concurrency:
1. **Lock-Free Transaction Journal**: Transactions are written as independent immutable JSON records (`.aggit/journal/*.json`), completely eliminating staging/commit mutex locks.
2. **Sharded CAS Storage**: Content-addressable blobs are sharded by two-character prefixes (`.aggit/objects/xx/yy`), allowing 70 threads to write distinct blobs simultaneously without directory locking.
3. **Parallel DAG Reconciliation & Materialization**: Rayon thread pools parallelize both SHA-256 validation and file disk materialization, keeping end-to-end total latency well under 0.5s for 70 agents.

---

## 4. Reproduction Commands

```bash
# Run comparative benchmark suite with integrity verification (Python):
python benchmark.py

# Run Rust 70-agent concurrency benchmark (1 round):
cargo run --release --manifest-path aggit-rs/Cargo.toml -- benchmark --agents 70

# Run multi-round empirical average with 70 agents across 70 OS threads:
cargo run --release --manifest-path aggit-rs/Cargo.toml -- benchmark --agents 70 --threads 70 --rounds 10

# Export structured JSON metrics for CI/CD pipelines:
cargo run --release --manifest-path aggit-rs/Cargo.toml -- benchmark --agents 70 --rounds 5 --json
```
Raw and structured JSON metrics are automatically recorded to `benchmark_results.json`.

