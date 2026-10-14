use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use serde::{Deserialize, Serialize};
use crate::storage::CASStorage;
use crate::journal::Journal;
use crate::staging::StagingArea;
use crate::semantic::{SemanticMerger, MergeConflict};
use crate::schema::{ChangeAction, ChangeRecord, Transaction};
use crate::path_utils::sanitize_repo_path;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Checkpoint {
    pub version: u32,
    pub tree: HashMap<String, String>, // path -> blob_sha
    pub processed_tx_ids: Vec<String>,
}

pub struct AgGitEngine {
    pub repo_dir: PathBuf,
    pub aggit_dir: PathBuf,
    pub storage: CASStorage,
    pub journal: Journal,
    pub staging: StagingArea,
    pub checkpoint_dir: PathBuf,
    pub materialized_state_path: PathBuf,
}

impl AgGitEngine {
    pub fn new<P: AsRef<Path>>(repo_dir: P) -> Self {
        let repo_path = repo_dir.as_ref().to_path_buf();
        let aggit_dir = repo_path.join(".aggit");
        let storage = CASStorage::new(&repo_path);
        let journal = Journal::new(&repo_path);
        let staging = StagingArea::new(&repo_path);
        let checkpoint_dir = aggit_dir.join("checkpoints");
        let materialized_state_path = aggit_dir.join("materialized_state.json");
        let _ = fs::create_dir_all(&checkpoint_dir);

        Self {
            repo_dir: repo_path,
            aggit_dir,
            storage,
            journal,
            staging,
            checkpoint_dir,
            materialized_state_path,
        }
    }

    pub fn init<P: AsRef<Path>>(repo_dir: P) -> std::io::Result<Self> {
        let aggit_dir = repo_dir.as_ref().join(".aggit");
        fs::create_dir_all(&aggit_dir)?;
        let config_path = aggit_dir.join("config.json");
        if !config_path.exists() {
            let mut f = File::create(&config_path)?;
            f.write_all(b"{\n  \"version\": \"2.0.0\",\n  \"engine\": \"aggit-rust-native\"\n}\n")?;
        }
        Ok(Self::new(repo_dir))
    }

    pub fn commit(
        &self,
        agent_id: &str,
        session_id: Option<&str>,
        message: &str,
        direct_files: Option<HashMap<String, Vec<u8>>>,
        base_revision: Option<String>,
        parents: Option<Vec<String>>,
    ) -> std::io::Result<Transaction> {
        let staged_doc = self.staging.get_staged_doc(agent_id, session_id);
        let current_generation = staged_doc.generation;

        let effective_base = base_revision.or(staged_doc.base_revision);
        let effective_parents = match parents {
            Some(p) => p,
            None => {
                if let Some(ref base) = effective_base {
                    vec![base.clone()]
                } else if let Some(latest) = self.journal.get_latest_transaction() {
                    vec![latest.tx_id]
                } else {
                    Vec::new()
                }
            }
        };

        let changes = if let Some(files) = direct_files {
            let mut map = HashMap::new();
            for (p, bytes) in files {
                let norm = sanitize_repo_path(&self.repo_dir, &p)
                    .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e))?;
                let sha = self.storage.store_blob(&bytes)?;
                map.insert(norm, ChangeRecord::modify(sha, None));
            }
            map
        } else {
            let entries = self.staging.get_staged_changes(agent_id, session_id);
            if entries.is_empty() {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    format!("Nothing staged for agent '{}'", agent_id),
                ));
            }
            entries
        };

        let tx = self.journal.append_transaction(
            &self.storage,
            agent_id,
            session_id,
            message,
            changes,
            effective_parents,
            effective_base,
        )?;

        // Conditional clear: only wipe if no newer edits occurred
        self.staging.clear_if_generation_le(agent_id, session_id, current_generation);
        Ok(tx)
    }

    fn load_checkpoint(&self) -> Checkpoint {
        let path = self.checkpoint_dir.join("latest.json");
        if let Ok(bytes) = fs::read(&path) {
            if let Ok(cp) = serde_json::from_slice::<Checkpoint>(&bytes) {
                return cp;
            }
        }
        Checkpoint {
            version: 1,
            tree: HashMap::new(),
            processed_tx_ids: Vec::new(),
        }
    }

    fn save_checkpoint(&self, checkpoint: &Checkpoint) -> std::io::Result<()> {
        let path = self.checkpoint_dir.join("latest.json");
        let tmp = path.with_extension(format!("tmp_{}", uuid::Uuid::new_v4()));
        let bytes = serde_json::to_vec_pretty(checkpoint)?;
        {
            let mut f = File::create(&tmp)?;
            f.write_all(&bytes)?;
            f.sync_all()?;
        }
        fs::rename(tmp, path)?;
        Ok(())
    }

    pub fn get_revision_tree(&self, target_tx_id: &str) -> Result<HashMap<String, String>, String> {
        let all_txs = self.journal.get_all_transactions()?;
        let tx_map: HashMap<String, Transaction> = all_txs.into_iter().map(|t| (t.tx_id.clone(), t)).collect();

        if !tx_map.contains_key(target_tx_id) {
            return Err(format!("Revision {} not found", target_tx_id));
        }

        let mut lineage = Vec::new();
        let mut curr = Some(target_tx_id.to_string());
        let mut visited = HashSet::new();

        while let Some(tid) = curr {
            if visited.contains(&tid) {
                break;
            }
            visited.insert(tid.clone());
            if let Some(tx) = tx_map.get(&tid) {
                lineage.push(tx.clone());
                curr = tx.parents.first().cloned();
            } else {
                break;
            }
        }

        lineage.reverse();
        let mut tree = HashMap::new();
        for tx in lineage {
            for (path, change) in tx.get_effective_changes() {
                match change.action {
                    ChangeAction::Delete => {
                        tree.remove(&path);
                    }
                    _ => {
                        if let Some(sha) = change.blob_sha {
                            tree.insert(path, sha);
                        }
                    }
                }
            }
        }

        Ok(tree)
    }

    /// Incremental reconciliation using checkpoints and accurate 3-way ancestor resolution
    pub fn reconcile_state(&self) -> Result<(HashMap<String, Vec<u8>>, Vec<MergeConflict>), String> {
        let mut checkpoint = self.load_checkpoint();
        let mut tree = checkpoint.tree.clone();
        let mut processed_set: HashSet<String> = checkpoint.processed_tx_ids.iter().cloned().collect();
        let all_txs = self.journal.get_all_transactions()?;

        let mut conflicts = Vec::new();

        for tx in all_txs {
            if processed_set.contains(&tx.tx_id) {
                continue;
            }

            let base_tree = if let Some(ref base) = tx.base_revision {
                self.get_revision_tree(base).unwrap_or_default()
            } else if let Some(parent) = tx.parents.first() {
                self.get_revision_tree(parent).unwrap_or_default()
            } else {
                HashMap::new()
            };

            for (path, change) in tx.get_effective_changes() {
                let base_sha = base_tree.get(&path);
                let curr_sha = tree.get(&path);
                let tx_sha = change.blob_sha.as_ref();

                let base_bytes = base_sha.and_then(|s| self.storage.get_blob(s).ok());
                let curr_bytes = curr_sha.and_then(|s| self.storage.get_blob(s).ok());
                let tx_bytes = tx_sha.and_then(|s| self.storage.get_blob(s).ok());

                let (merged_bytes_opt, step_conflicts) = SemanticMerger::merge_files(
                    &path,
                    base_bytes.as_deref(),
                    curr_bytes.as_deref(),
                    tx_bytes.as_deref(),
                    "UNIFIED_HEAD",
                    &tx.agent_id,
                );

                conflicts.extend(step_conflicts);

                match merged_bytes_opt {
                    Some(merged_bytes) => {
                        if let Ok(new_sha) = self.storage.store_blob(&merged_bytes) {
                            tree.insert(path, new_sha);
                        }
                    }
                    None => {
                        tree.remove(&path);
                    }
                }
            }

            processed_set.insert(tx.tx_id.clone());
            checkpoint.processed_tx_ids.push(tx.tx_id);
        }

        checkpoint.tree = tree.clone();
        let _ = self.save_checkpoint(&checkpoint);

        let mut unified_files = HashMap::new();
        for (p, sha) in tree {
            if let Ok(bytes) = self.storage.get_blob(&sha) {
                unified_files.insert(p, bytes);
            }
        }

        Ok((unified_files, conflicts))
    }

    /// Smart materialization: writes only modified files, applies deletions, returns verified count
    pub fn materialize(&self) -> std::io::Result<(usize, Vec<MergeConflict>)> {
        let (unified_files, conflicts) = self.reconcile_state()
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;

        let mut prev_state: HashMap<String, String> = HashMap::new();
        if self.materialized_state_path.exists() {
            if let Ok(bytes) = fs::read(&self.materialized_state_path) {
                if let Ok(map) = serde_json::from_slice(&bytes) {
                    prev_state = map;
                }
            }
        }

        // 1. Remove deleted files
        for old_path in prev_state.keys() {
            if !unified_files.contains_key(old_path) {
                let full = self.repo_dir.join(old_path);
                if full.exists() {
                    let _ = fs::remove_file(full);
                }
            }
        }

        let mut written_count = 0;
        let mut new_state = HashMap::new();

        // 2. Write modified or added files atomically
        for (rel_path, content) in &unified_files {
            let full_path = self.repo_dir.join(rel_path);
            let sha = self.storage.store_blob(content)?;
            new_state.insert(rel_path.clone(), sha.clone());

            if prev_state.get(rel_path) == Some(&sha) && full_path.exists() {
                continue;
            }

            if let Some(parent) = full_path.parent() {
                fs::create_dir_all(parent)?;
            }

            let tmp_path = full_path.with_extension(format!("tmp_{}", uuid::Uuid::new_v4()));
            {
                let mut f = File::create(&tmp_path)?;
                f.write_all(content)?;
                f.sync_all()?;
            }
            fs::rename(tmp_path, &full_path)?;
            written_count += 1;
        }

        // Save updated state
        let tmp_meta = self.materialized_state_path.with_extension(format!("tmp_{}", uuid::Uuid::new_v4()));
        let meta_bytes = serde_json::to_vec_pretty(&new_state)?;
        {
            let mut f = File::create(&tmp_meta)?;
            f.write_all(&meta_bytes)?;
            f.sync_all()?;
        }
        fs::rename(tmp_meta, &self.materialized_state_path)?;

        Ok((written_count, conflicts))
    }
}
