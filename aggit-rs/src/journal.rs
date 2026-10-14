use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use serde::{Deserialize, Serialize};
use chrono::Utc;
use rayon::prelude::*;
use crate::storage::CASStorage;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Transaction {
    pub tx_id: String,
    pub agent_id: String,
    pub timestamp_ns: i64,
    pub message: String,
    pub files: HashMap<String, String>, // path -> blob_sha
    pub parents: Vec<String>,
}

pub struct Journal {
    journal_dir: PathBuf,
}

impl Journal {
    pub fn new<P: AsRef<Path>>(repo_dir: P) -> Self {
        let journal_dir = repo_dir.as_ref().join(".aggit").join("journal");
        fs::create_dir_all(&journal_dir).expect("Failed to create journal directory");
        Self { journal_dir }
    }

    /// Appends a transaction completely lock-free
    pub fn append_transaction(
        &self,
        _storage: &CASStorage,
        agent_id: &str,
        message: &str,
        files: HashMap<String, String>,
        parents: Vec<String>,
    ) -> std::io::Result<Transaction> {
        let now = Utc::now();
        let ts_ns = now.timestamp_nanos_opt().unwrap_or_else(|| now.timestamp_millis() * 1_000_000);
        let tx_id = uuid::Uuid::new_v4().to_string()[..8].to_string();

        let tx = Transaction {
            tx_id: tx_id.clone(),
            agent_id: agent_id.to_string(),
            timestamp_ns: ts_ns,
            message: message.to_string(),
            files,
            parents,
        };

        let filename = format!("{:020}_{}_{}.json", ts_ns, agent_id, tx_id);
        let tx_path = self.journal_dir.join(&filename);
        let tmp_path = self.journal_dir.join(format!(".tmp_{}", filename));

        let json_data = serde_json::to_vec_pretty(&tx)?;
        {
            let mut f = File::create(&tmp_path)?;
            f.write_all(&json_data)?;
            f.sync_all()?;
        }

        fs::rename(&tmp_path, &tx_path)?;
        Ok(tx)
    }

    /// Reads all transactions in parallel using Rayon and returns them in causal chronological order
    pub fn get_all_transactions(&self) -> Vec<Transaction> {
        let mut paths: Vec<PathBuf> = Vec::new();
        if let Ok(entries) = fs::read_dir(&self.journal_dir) {
            for entry in entries.flatten() {
                let p = entry.path();
                if p.extension().map_or(false, |ext| ext == "json") {
                    paths.push(p);
                }
            }
        }

        // Parallel parse across all CPU cores
        let mut txs: Vec<Transaction> = paths
            .par_iter()
            .filter_map(|p| {
                let data = fs::read(p).ok()?;
                serde_json::from_slice(&data).ok()
            })
            .collect();

        // Sort chronologically by nanos
        txs.sort_by_key(|t| t.timestamp_ns);
        txs
    }

    pub fn get_latest_transaction(&self) -> Option<Transaction> {
        let all = self.get_all_transactions();
        all.into_iter().last()
    }
}
