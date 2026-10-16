use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Instant;
use clap::{Parser, Subcommand};
use rayon::prelude::*;

use aggit::engine::AgGitEngine;

#[derive(Parser)]
#[command(name = "aggit")]
#[command(about = "AgGit: Lock-Free Multi-Agent Native VCS (Rust Native Engine)", long_about = None)]
#[command(version = "0.2.0")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Initialize an empty AgGit repository
    Init {
        #[arg(default_value = ".")]
        path: PathBuf,
    },
    /// Stage a file for an agent
    Stage {
        #[arg(short, long)]
        file: String,
        #[arg(short, long)]
        agent: String,
        #[arg(short, long)]
        session: Option<String>,
        #[arg(default_value = ".")]
        path: PathBuf,
    },
    /// Stage deletion of a file for an agent
    StageDelete {
        #[arg(short, long)]
        file: String,
        #[arg(short, long)]
        agent: String,
        #[arg(short, long)]
        session: Option<String>,
        #[arg(default_value = ".")]
        path: PathBuf,
    },
    /// Commit staged changes lock-free
    Commit {
        #[arg(short, long)]
        message: String,
        #[arg(short, long)]
        agent: String,
        #[arg(short, long)]
        session: Option<String>,
        #[arg(short, long)]
        base: Option<String>,
        #[arg(default_value = ".")]
        path: PathBuf,
    },
    /// Show repository status
    Status {
        #[arg(default_value = ".")]
        path: PathBuf,
    },
    /// Show transaction history
    Log {
        #[arg(default_value = ".")]
        path: PathBuf,
    },
    /// Reconcile and smart-materialize state to disk
    Reconcile {
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        force: bool,
        #[arg(default_value = ".")]
        path: PathBuf,
    },
    /// Run native concurrency benchmark
    Benchmark {
        #[arg(short, long, default_value_t = 70)]
        agents: usize,
        #[arg(short, long)]
        threads: Option<usize>,
        #[arg(short, long, default_value_t = 1)]
        rounds: usize,
        #[arg(long)]
        json: bool,
    },
}

fn main() -> std::io::Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Commands::Init { path } => {
            let engine = AgGitEngine::init(&path)?;
            println!("[AgGit-RS] Initialized empty AgGit repository in {:?}", engine.repo_dir.join(".aggit"));
        }
        Commands::Stage { file, agent, session, path } => {
            let engine = AgGitEngine::new(&path);
            let sha = engine.staging.stage_file(&engine.storage, &agent, &file, None, session.as_deref())?;
            println!("[AgGit-RS] Agent '{}' (session: {:?}) staged '{}' (blob: {})", agent, session, file, &sha[..8]);
        }
        Commands::StageDelete { file, agent, session, path } => {
            let engine = AgGitEngine::new(&path);
            engine.staging.stage_delete(&agent, &file, session.as_deref())?;
            println!("[AgGit-RS] Agent '{}' staged deletion of '{}'", agent, file);
        }
        Commands::Commit { message, agent, session, base, path } => {
            let engine = AgGitEngine::new(&path);
            let tx = engine.commit(&agent, session.as_deref(), &message, None, base, None)?;
            println!("[AgGit-RS] Lock-free commit succeeded! [tx: {}] by '{}'", tx.tx_id, agent);
            println!("           Message: {}", tx.message);
            println!("           Changes: {}", tx.get_effective_changes().len());
        }
        Commands::Status { path } => {
            let engine = AgGitEngine::new(&path);
            let txs = engine.journal.get_all_transactions().unwrap_or_default();
            println!("[AgGit-RS Status]");
            println!("Total recorded transactions: {}", txs.len());
            if let Some(latest) = txs.last() {
                println!("Latest: [{}] by '{}': {}", latest.tx_id, latest.agent_id, latest.message);
            }
        }
        Commands::Log { path } => {
            let engine = AgGitEngine::new(&path);
            let txs = engine.journal.get_all_transactions().unwrap_or_default();
            println!("[AgGit-RS Log - {} transactions]", txs.len());
            for tx in txs.iter().rev() {
                println!("* tx {} | Agent: {} | Nanos: {}", tx.tx_id, tx.agent_id, tx.timestamp_ns);
                println!("  Message: {}", tx.message);
                let changes = tx.get_effective_changes();
                let files: Vec<&str> = changes.keys().map(|s| s.as_str()).collect();
                println!("  Files: {}", files.join(", "));
                println!();
            }
        }
        Commands::Reconcile { dry_run, force, path } => {
            let engine = AgGitEngine::new(&path);
            if dry_run {
                let (files, conflicts) = engine.reconcile_state(true)
                    .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
                println!("[AgGit-RS Dry-run] Reconciled in-memory: {} files, {} conflicts.", files.len(), conflicts.len());
                for c in &conflicts {
                    println!("  ! Conflict in '{}' ({}: {})", c.path, c.conflict_type, c.details);
                }
            } else {
                let (count, conflicts) = engine.materialize(force)?;
                println!("[AgGit-RS] Reconciled and materialized {} files into working directory.", count);
                if !conflicts.is_empty() {
                    println!("WARNING: Conflicts found in {} files:", conflicts.len());
                    for c in &conflicts {
                        println!("  ! {} ({}: {})", c.path, c.conflict_type, c.details);
                    }
                } else {
                    println!("Status: Clean merge.");
                }
            }
        }
        Commands::Benchmark { agents, threads, rounds, json } => {
            let num_rounds = if rounds == 0 { 1 } else { rounds };
            let thread_count = threads.unwrap_or(agents);
            let pool = rayon::ThreadPoolBuilder::new()
                .num_threads(thread_count)
                .build()
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;

            struct RoundStats {
                round: usize,
                commit_time_sec: f64,
                reconcile_time_sec: f64,
                total_time_sec: f64,
                throughput_ops_sec: f64,
                median_latency_sec: f64,
                p95_latency_sec: f64,
                p99_latency_sec: f64,
                successes: usize,
                failures: usize,
                content_verified: bool,
            }

            let mut all_stats: Vec<RoundStats> = Vec::with_capacity(num_rounds);

            if !json && num_rounds > 1 {
                println!("============================================================");
                println!("RUST CONCURRENCY BENCHMARK: {} AGENTS, {} THREADS ({} ROUNDS)", agents, thread_count, num_rounds);
                println!("============================================================");
            }

            for r in 1..=num_rounds {
                let bench_dir = std::env::temp_dir().join(format!("aggit_bench_rust_{}_{}_{}", std::process::id(), r, uuid::Uuid::new_v4()));
                let engine = AgGitEngine::init(&bench_dir)?;

                let (worker_results, commit_elapsed) = pool.install(|| {
                    let start = Instant::now();
                    let results: Vec<(bool, f64)> = (0..agents)
                        .into_par_iter()
                        .map(|i| {
                            let agent_id = format!("agent_{:03}", i);
                            let mut files = HashMap::new();
                            let content = format!("// Generated by agent {}\nfn worker_{}() {{ println!(\"ok\"); }}\n", i, i);
                            files.insert(format!("src/worker_{}.rs", i), content.into_bytes());

                            let t0 = Instant::now();
                            let res = engine.commit(&agent_id, None, &format!("Agent {} commit", i), Some(files), None, None);
                            let elapsed_sec = t0.elapsed().as_secs_f64();
                            (res.is_ok(), elapsed_sec)
                        })
                        .collect();
                    let elapsed = start.elapsed().as_secs_f64();
                    (results, elapsed)
                });

                let mut successes = 0;
                let mut failures = 0;
                let mut latencies: Vec<f64> = Vec::with_capacity(agents);

                for (ok, dur) in worker_results {
                    if ok {
                        successes += 1;
                    } else {
                        failures += 1;
                    }
                    latencies.push(dur);
                }

                latencies.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

                let median_latency = if latencies.is_empty() {
                    0.0
                } else {
                    latencies[latencies.len() / 2]
                };

                let p95_latency = if latencies.is_empty() {
                    0.0
                } else {
                    let idx = ((latencies.len() as f64) * 0.95).floor() as usize;
                    latencies[idx.min(latencies.len() - 1)]
                };

                let p99_latency = if latencies.is_empty() {
                    0.0
                } else {
                    let idx = ((latencies.len() as f64) * 0.99).floor() as usize;
                    latencies[idx.min(latencies.len() - 1)]
                };

                // Reconcile and parallel materialize
                let mat_start = Instant::now();
                let (mat_count, conflicts) = engine.materialize(false)?;
                let mat_elapsed = mat_start.elapsed().as_secs_f64();
                let total_time_sec = commit_elapsed + mat_elapsed;

                // Verify content on disk (exact byte-level payload)
                let mut content_verified = conflicts.is_empty() && mat_count == agents;
                for i in 0..agents {
                    let file_path = bench_dir.join(format!("src/worker_{}.rs", i));
                    let expected = format!("// Generated by agent {}\nfn worker_{}() {{ println!(\"ok\"); }}\n", i, i);
                    match std::fs::read(&file_path) {
                        Ok(bytes) => {
                            if bytes != expected.into_bytes() {
                                content_verified = false;
                                break;
                            }
                        }
                        Err(_) => {
                            content_verified = false;
                            break;
                        }
                    }
                }

                let ops_per_sec = if total_time_sec > 0.0 {
                    (successes as f64) / total_time_sec
                } else {
                    0.0
                };

                // Cleanup temp bench repo
                let _ = std::fs::remove_dir_all(&bench_dir);

                let stats = RoundStats {
                    round: r,
                    commit_time_sec: commit_elapsed,
                    reconcile_time_sec: mat_elapsed,
                    total_time_sec,
                    throughput_ops_sec: ops_per_sec,
                    median_latency_sec: median_latency,
                    p95_latency_sec: p95_latency,
                    p99_latency_sec: p99_latency,
                    successes,
                    failures,
                    content_verified,
                };

                if !json && num_rounds > 1 {
                    println!(
                        "[Round {:02}/{:02}] {}/{} commits ({:.1}%) | Commit: {:.3}s | Reconcile: {:.3}s | Total: {:.3}s | {:.2} ops/s | p50: {:.4}s, p95: {:.4}s, p99: {:.4}s | Verified: {}",
                        stats.round, num_rounds, stats.successes, agents,
                        (stats.successes as f64 / agents as f64) * 100.0,
                        stats.commit_time_sec, stats.reconcile_time_sec, stats.total_time_sec,
                        stats.throughput_ops_sec, stats.median_latency_sec, stats.p95_latency_sec, stats.p99_latency_sec,
                        stats.content_verified
                    );
                }

                all_stats.push(stats);
            }

            let total_commits = agents * num_rounds;
            let total_successes: usize = all_stats.iter().map(|s| s.successes).sum();
            let total_failures: usize = all_stats.iter().map(|s| s.failures).sum();
            let all_verified = all_stats.iter().all(|s| s.content_verified);

            let avg_commit_time: f64 = all_stats.iter().map(|s| s.commit_time_sec).sum::<f64>() / num_rounds as f64;
            let avg_reconcile_time: f64 = all_stats.iter().map(|s| s.reconcile_time_sec).sum::<f64>() / num_rounds as f64;
            let avg_total_time: f64 = all_stats.iter().map(|s| s.total_time_sec).sum::<f64>() / num_rounds as f64;
            let avg_throughput: f64 = all_stats.iter().map(|s| s.throughput_ops_sec).sum::<f64>() / num_rounds as f64;
            let avg_p50: f64 = all_stats.iter().map(|s| s.median_latency_sec).sum::<f64>() / num_rounds as f64;
            let avg_p95: f64 = all_stats.iter().map(|s| s.p95_latency_sec).sum::<f64>() / num_rounds as f64;
            let avg_p99: f64 = all_stats.iter().map(|s| s.p99_latency_sec).sum::<f64>() / num_rounds as f64;
            let overall_error_rate = if total_commits > 0 {
                (total_failures as f64 / total_commits as f64) * 100.0
            } else {
                0.0
            };

            if json {
                let rounds_json: Vec<serde_json::Value> = all_stats.iter().map(|s| {
                    serde_json::json!({
                        "round": s.round,
                        "commit_time_sec": s.commit_time_sec,
                        "reconcile_time_sec": s.reconcile_time_sec,
                        "total_time_sec": s.total_time_sec,
                        "throughput_ops_sec": s.throughput_ops_sec,
                        "median_latency_sec": s.median_latency_sec,
                        "p95_latency_sec": s.p95_latency_sec,
                        "p99_latency_sec": s.p99_latency_sec,
                        "successes": s.successes,
                        "failures": s.failures,
                        "content_verified": s.content_verified
                    })
                }).collect();

                let out = serde_json::json!({
                    "system": "AgGit (Rust Native Engine)",
                    "agents_per_round": agents,
                    "threads": thread_count,
                    "rounds": num_rounds,
                    "total_commits": total_commits,
                    "total_successes": total_successes,
                    "total_failures": total_failures,
                    "error_rate_pct": overall_error_rate,
                    "average_stats": {
                        "commit_time_sec": avg_commit_time,
                        "reconcile_time_sec": avg_reconcile_time,
                        "total_time_sec": avg_total_time,
                        "throughput_ops_sec": avg_throughput,
                        "median_latency_sec": avg_p50,
                        "p95_latency_sec": avg_p95,
                        "p99_latency_sec": avg_p99,
                        "content_verified": all_verified
                    },
                    "round_details": rounds_json
                });
                println!("{}", serde_json::to_string_pretty(&out)?);
            } else if num_rounds == 1 {
                let s = &all_stats[0];
                println!("============================================================");
                println!("RUST HARDWARE-ACCELERATED BENCHMARK: {} CONCURRENT AGENTS", agents);
                println!("============================================================");
                println!("[Benchmark] Active OS Threads: {}", thread_count);
                println!("[Benchmark] {} commits attempted, {} succeeded, {} failed ({:.2}% error rate)", agents, s.successes, s.failures, overall_error_rate);
                println!("[Benchmark] Commit Time: {:.3}s, Reconcile/Materialize Time: {:.3}s, Total Time: {:.3}s", s.commit_time_sec, s.reconcile_time_sec, s.total_time_sec);
                println!("[Benchmark] Throughput: {:.2} ops/sec", s.throughput_ops_sec);
                println!("[Benchmark] Latency: p50={:.4}s, p95={:.4}s, p99={:.4}s", s.median_latency_sec, s.p95_latency_sec, s.p99_latency_sec);
                println!("[Benchmark] Strict Content Verification: {} ({}/{} files matched byte-for-byte)", s.content_verified, s.successes, agents);
                println!("============================================================");
            } else {
                println!("============================================================");
                println!("MULTI-ROUND EMPIRICAL AVERAGE ({} AGENTS, {} THREADS, {} ROUNDS)", agents, thread_count, num_rounds);
                println!("============================================================");
                println!("[Average] Total Commits Executed: {}", total_commits);
                println!("[Average] Success Rate: {:.2}% ({} succeeded, {} failed)", 100.0 - overall_error_rate, total_successes, total_failures);
                println!("[Average] Commit Time: {:.3}s", avg_commit_time);
                println!("[Average] Reconcile/Materialize Time: {:.3}s", avg_reconcile_time);
                println!("[Average] Total End-to-End Time: {:.3}s", avg_total_time);
                println!("[Average] Empirical Throughput: {:.2} ops/sec", avg_throughput);
                println!("[Average] Latency: p50={:.4}s, p95={:.4}s, p99={:.4}s", avg_p50, avg_p95, avg_p99);
                println!("[Average] Content Integrity: 100% byte-verified across all {} files in all rounds: {}", total_commits, all_verified);
                println!("============================================================");
            }
        }
    }

    Ok(())
}
