use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use rayon::prelude::*;
use crate::storage::CASStorage;
use crate::journal::{Journal, Transaction};
use crate::staging::StagingArea;
use crate::semantic::SemanticMerger;

pub struct AgGitEngine {
    pub repo_dir: PathBuf,
    pub storage: CASStorage,
    pub journal: Journal,
    pub staging: StagingArea,
}

impl AgGitEngine {
    pub fn new<P: AsRef<Path>>(repo_dir: P) -> Self {
        let repo_path = repo_dir.as_ref().to_path_buf();
        let storage = CASStorage::new(&repo_path);
        let journal = Journal::new(&repo_path);
        let staging = StagingArea::new(&repo_path);

        Self {
            repo_dir: repo_path,
            storage,
            journal,
            staging,
        }
    }

    pub fn init<P: AsRef<Path>>(repo_dir: P) -> std::io::Result<Self> {
        let aggit_dir = repo_dir.as_ref().join(".aggit");
        fs::create_dir_all(&aggit_dir)?;
        let config_path = aggit_dir.join("config.json");
        if !config_path.exists() {
            let mut f = File::create(&config_path)?;
            f.write_all(b"{\n  \"version\": \"1.0.0\",\n  \"engine\": \"aggit-rust-native\"\n}\n")?;
        }
        Ok(Self::new(repo_dir))
    }

    pub fn commit(
        &self,
        agent_id: &str,
        message: &str,
        direct_files: Option<HashMap<String, Vec<u8>>>,
    ) -> std::io::Result<Transaction> {
        if let Some(files) = direct_files {
            for (path, content) in files {
                self.staging.stage_file(&self.storage, agent_id, &path, Some(&content))?;
            }
        }

        let staged = self.staging.get_staged(agent_id);
        if staged.is_empty() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!("Nothing staged for agent '{}'", agent_id),
            ));
        }

        let latest = self.journal.get_latest_transaction();
        let parents = latest.map(|t| vec![t.tx_id]).unwrap_or_default();

        let tx = self.journal.append_transaction(&self.storage, agent_id, message, staged, parents)?;
        self.staging.clear(agent_id);
        Ok(tx)
    }

    pub fn reconcile_state(&self) -> (HashMap<String, Vec<u8>>, Vec<String>) {
        let txs = self.journal.get_all_transactions();
        let mut unified: HashMap<String, Vec<u8>> = HashMap::new();
        let mut conflicts: Vec<String> = Vec::new();

        for tx in txs {
            for (rel_path, blob_sha) in tx.files {
                let new_bytes = match self.storage.get_blob(&blob_sha) {
                    Ok(b) => b,
                    Err(_) => continue,
                };

                if let Some(curr_bytes) = unified.get(&rel_path) {
                    if curr_bytes == &new_bytes {
                        continue;
                    }
                    if let (Ok(curr_str), Ok(new_str)) = (std::str::from_utf8(curr_bytes), std::str::from_utf8(&new_bytes)) {
                        let (merged, conflict) = SemanticMerger::merge_content("", curr_str, new_str, &rel_path);
                        unified.insert(rel_path.clone(), merged.into_bytes());
                        if conflict {
                            conflicts.push(rel_path);
                        }
                    } else {
                        // Binary file: last-write-wins
                        unified.insert(rel_path, new_bytes);
                    }
                } else {
                    unified.insert(rel_path, new_bytes);
                }
            }
        }

        (unified, conflicts)
    }

    /// Parallel materialization into working directory using Rayon
    pub fn materialize(&self) -> std::io::Result<(usize, Vec<String>)> {
        let (unified, conflicts) = self.reconcile_state();
        let repo_dir = &self.repo_dir;

        // Write files to disk in parallel across CPU threads
        unified.par_iter().for_each(|(rel_path, bytes)| {
            let full_path = repo_dir.join(rel_path);
            if let Some(parent) = full_path.parent() {
                let _ = fs::create_dir_all(parent);
            }
            let _ = fs::write(full_path, bytes);
        });

        Ok((unified.len(), conflicts))
    }
}
