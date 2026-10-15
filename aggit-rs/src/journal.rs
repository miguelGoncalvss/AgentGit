use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use rayon::prelude::*;
use crate::storage::CASStorage;
use crate::schema::{ChangeRecord, Transaction};

pub struct Journal {
    journal_dir: PathBuf,
    _index_path: PathBuf,
}

impl Journal {
    pub fn new<P: AsRef<Path>>(repo_dir: P) -> Self {
        let aggit_dir = repo_dir.as_ref().join(".aggit");
        let journal_dir = aggit_dir.join("journal");
        let index_path = aggit_dir.join("journal_index.json");
        fs::create_dir_all(&journal_dir).expect("Failed to create journal directory");
        Self { journal_dir, _index_path: index_path }
    }

    /// Appends a transaction completely lock-free.
    /// Temp file uses .tmp suffix to ensure concurrent readers never inspect partial transactions.
    pub fn append_transaction(
        &self,
        _storage: &CASStorage,
        agent_id: &str,
        session_id: Option<&str>,
        message: &str,
        changes: HashMap<String, ChangeRecord>,
        parents: Vec<String>,
        base_revision: Option<String>,
    ) -> std::io::Result<Transaction> {
        let tx = Transaction::new(agent_id, session_id, message, changes, parents, base_revision);

        let filename = format!("{:020}_{}_{}.json", tx.timestamp_ns, tx.agent_id, tx.tx_id);
        let tx_path = self.journal_dir.join(&filename);
        let tmp_path = self.journal_dir.join(format!(".tmp_tx_{}_{}.tmp", std::process::id(), uuid::Uuid::new_v4()));

        let json_data = serde_json::to_vec_pretty(&tx)?;
        {
            let mut f = File::create(&tmp_path)?;
            f.write_all(&json_data)?;
            f.sync_all()?;
        }

        fs::rename(&tmp_path, &tx_path)?;
        Ok(tx)
    }

    pub fn append_transaction_with_metadata(
        &self,
        _storage: &CASStorage,
        agent_id: &str,
        session_id: Option<&str>,
        message: &str,
        changes: HashMap<String, ChangeRecord>,
        parents: Vec<String>,
        base_revision: Option<String>,
        metadata: serde_json::Value,
    ) -> std::io::Result<Transaction> {
        let tx = Transaction::new_with_metadata(agent_id, session_id, message, changes, parents, base_revision, metadata);

        let filename = format!("{:020}_{}_{}.json", tx.timestamp_ns, tx.agent_id, tx.tx_id);
        let tx_path = self.journal_dir.join(&filename);
        let tmp_path = self.journal_dir.join(format!(".tmp_tx_{}_{}.tmp", std::process::id(), uuid::Uuid::new_v4()));

        let json_data = serde_json::to_vec_pretty(&tx)?;
        {
            let mut f = File::create(&tmp_path)?;
            f.write_all(&json_data)?;
            f.sync_all()?;
        }

        fs::rename(&tmp_path, &tx_path)?;
        Ok(tx)
    }

    /// Reads all published transactions in parallel using Rayon and returns them sorted by timestamp_ns.
    /// Only reads complete files with extension 'json' that do not start with '.tmp'.
    pub fn get_all_transactions(&self) -> Result<Vec<Transaction>, String> {
        let mut paths: Vec<PathBuf> = Vec::new();
        if let Ok(entries) = fs::read_dir(&self.journal_dir) {
            for entry in entries.flatten() {
                let p = entry.path();
                let fname = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
                if fname.starts_with(".tmp") {
                    continue;
                }
                if p.extension().map_or(false, |ext| ext == "json") {
                    paths.push(p);
                }
            }
        }

        // Parallel parse across all CPU cores
        let results: Result<Vec<Transaction>, String> = paths
            .par_iter()
            .map(|p| {
                let data = fs::read(p).map_err(|e| format!("Failed to read {}: {}", p.display(), e))?;
                serde_json::from_slice::<Transaction>(&data)
                    .map_err(|e| format!("Corrupt transaction record in {}: {}", p.display(), e))
            })
            .collect();

        let mut txs = results?;
        // Sort chronologically and causally by nanos
        txs.sort_by_key(|t| t.timestamp_ns);
        Ok(txs)
    }

    /// Fast lookup for the latest transaction
    pub fn get_latest_transaction(&self) -> Option<Transaction> {
        let mut paths: Vec<PathBuf> = Vec::new();
        if let Ok(entries) = fs::read_dir(&self.journal_dir) {
            for entry in entries.flatten() {
                let p = entry.path();
                let fname = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
                if !fname.starts_with(".tmp") && p.extension().map_or(false, |ext| ext == "json") {
                    paths.push(p);
                }
            }
        }

        if paths.is_empty() {
            return None;
        }

        // Filenames are prefixed with {:020} timestamp_ns, so lexicographical sort matches timestamp order
        paths.sort();
        let latest_path = paths.last()?;
        let data = fs::read(latest_path).ok()?;
        serde_json::from_slice::<Transaction>(&data).ok()
    }
}
