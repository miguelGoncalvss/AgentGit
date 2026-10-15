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
        #[arg(short, long, default_value_t = 20)]
        agents: usize,
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
        Commands::Benchmark { agents, json } => {
            let bench_dir = std::env::temp_dir().join(format!("aggit_bench_rust_{}_{}", std::process::id(), uuid::Uuid::new_v4()));
            let engine = AgGitEngine::init(&bench_dir)?;

            let start = Instant::now();

            // Run concurrent agent commits using Rayon CPU thread pool and record individual latencies
            let worker_results: Vec<(bool, f64)> = (0..agents)
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

            let commit_elapsed = start.elapsed().as_secs_f64();
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

            let error_rate = if agents > 0 {
                (failures as f64 / agents as f64) * 100.0
            } else {
                0.0
            };

            // Cleanup temp bench repo
            let _ = std::fs::remove_dir_all(&bench_dir);

            if json {
                let out = serde_json::json!({
                    "system": "AgGit (Rust Native Engine)",
                    "agents": agents,
                    "concurrency": rayon::current_num_threads(),
                    "total_time_sec": total_time_sec,
                    "commit_time_sec": commit_elapsed,
                    "reconcile_time_sec": mat_elapsed,
                    "successes": successes,
                    "failures": failures,
                    "error_rate_pct": error_rate,
                    "throughput_ops_sec": ops_per_sec,
                    "median_latency_sec": median_latency,
                    "p95_latency_sec": p95_latency,
                    "content_verified": content_verified
                });
                println!("{}", serde_json::to_string_pretty(&out)?);
            } else {
                println!("============================================================");
                println!("RUST HARDWARE-ACCELERATED BENCHMARK: {} CONCURRENT AGENTS", agents);
                println!("============================================================");
                println!("[Benchmark] {} commits attempted, {} succeeded, {} failed", agents, successes, failures);
                println!("[Benchmark] Commit Time: {:.3}s, Reconcile/Materialize Time: {:.3}s, Total Time: {:.3}s", commit_elapsed, mat_elapsed, total_time_sec);
                println!("[Benchmark] Throughput: {:.2} ops/sec", ops_per_sec);
                println!("[Benchmark] Latency: median={:.4}s, p95={:.4}s", median_latency, p95_latency);
                println!("[Benchmark] Content Verified: {}", content_verified);
                println!("============================================================");
            }
        }
    }

    Ok(())
}
