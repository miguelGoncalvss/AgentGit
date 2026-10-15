use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use serde::{Deserialize, Serialize};

use crate::storage::CASStorage;
use crate::staging::StagingArea;
use crate::journal::Journal;
use crate::schema::{ChangeRecord, ChangeAction, Transaction};
use crate::semantic::{SemanticMerger, MergeConflict};
use crate::path_utils::sanitize_repo_path;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Checkpoint {
    pub version: u32,
    pub tree: HashMap<String, String>,
    pub processed_tx_ids: Vec<String>,
    #[serde(default)]
    pub pending_conflicts: Vec<MergeConflict>,
}

pub struct AgGitEngine {
    pub repo_dir: PathBuf,
    pub storage: CASStorage,
    pub journal: Journal,
    pub staging: StagingArea,
    checkpoint_dir: PathBuf,
    materialized_state_path: PathBuf,
}

impl AgGitEngine {
    pub fn new<P: AsRef<Path>>(repo_dir: P) -> Self {
        let repo_dir = repo_dir.as_ref().to_path_buf();
        let storage = CASStorage::new(&repo_dir);
        let journal = Journal::new(&repo_dir);
        let staging = StagingArea::new(&repo_dir);
        let aggit_dir = repo_dir.join(".aggit");
        let checkpoint_dir = aggit_dir.join("checkpoints");
        let materialized_state_path = aggit_dir.join("materialized_state.json");

        let _ = fs::create_dir_all(&checkpoint_dir);

        Self {
            repo_dir,
            storage,
            journal,
            staging,
            checkpoint_dir,
            materialized_state_path,
        }
    }

    pub fn init<P: AsRef<Path>>(repo_dir: P) -> std::io::Result<Self> {
        let repo_dir = repo_dir.as_ref().to_path_buf();
        let aggit_dir = repo_dir.join(".aggit");
        fs::create_dir_all(&aggit_dir)?;
        let config_path = aggit_dir.join("config.json");
        if !config_path.exists() {
            fs::write(config_path, b"{\"version\": \"2.0.0\", \"engine\": \"aggit-rs\"}\n")?;
        }
        Ok(Self::new(repo_dir))
    }

    /// Commit transaction lock-free.
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

        let is_direct = direct_files.is_some();
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
            changes.clone(),
            effective_parents,
            effective_base,
        )?;

        if !is_direct {
            self.staging.consume_committed_changes(agent_id, session_id, &changes, current_generation);
        }
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
            version: 2,
            tree: HashMap::new(),
            processed_tx_ids: Vec::new(),
            pending_conflicts: Vec::new(),
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

    /// Incremental reconciliation using checkpoints and accurate 3-way ancestor resolution.
    /// Preserves pending conflicts across calls and restarts.
    pub fn reconcile_state(&self, dry_run: bool) -> Result<(HashMap<String, Vec<u8>>, Vec<MergeConflict>), String> {
        let mut checkpoint = self.load_checkpoint();
        let mut tree = checkpoint.tree.clone();
        let mut processed_set: HashSet<String> = checkpoint.processed_tx_ids.iter().cloned().collect();
        let all_txs = self.journal.get_all_transactions()?;

        // Track pending conflicts by path
        let mut conflicts_by_path: HashMap<String, Vec<MergeConflict>> = HashMap::new();
        for c in checkpoint.pending_conflicts {
            let p = c.file_path.clone().unwrap_or_else(|| c.path.clone());
            conflicts_by_path.entry(p).or_default().push(c);
        }

        let mut virtual_blobs: HashMap<String, Vec<u8>> = HashMap::new();

        for tx in &all_txs {
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
                let norm = sanitize_repo_path(&self.repo_dir, &path)
                    .unwrap_or_else(|_| path.clone());

                let explicit_resolution_meta = tx.metadata.as_object().and_then(|m| {
                    if let Some(res) = m.get("resolution").and_then(|v| v.as_object()) {
                        let path_match = res.get("resolved_path").and_then(|v| v.as_str()) == Some(&norm);
                        if path_match {
                            return Some(res);
                        }
                    }
                    if m.get("resolved_conflict_path").and_then(|v| v.as_str()) == Some(&norm) {
                        return Some(m);
                    }
                    None
                });

                if let Some(res_meta) = explicit_resolution_meta {
                    let target_ids: Vec<String> = res_meta.get("target_conflict_ids")
                        .or_else(|| res_meta.get("target_conflicts"))
                        .and_then(|v| v.as_array())
                        .map(|arr| arr.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect())
                        .unwrap_or_default();

                    if !target_ids.is_empty() {
                        if let Some(existing) = conflicts_by_path.get_mut(&norm) {
                            existing.retain(|c| {
                                if let Some(ref cid) = c.conflict_id {
                                    !target_ids.contains(cid)
                                } else {
                                    false
                                }
                            });
                            if existing.is_empty() {
                                conflicts_by_path.remove(&norm);
                            }
                        }
                    } else {
                        conflicts_by_path.remove(&norm);
                    }

                    match change.action {
                        ChangeAction::Delete => {
                            tree.remove(&norm);
                        }
                        _ => {
                            if let Some(sha) = change.blob_sha {
                                tree.insert(norm, sha);
                            }
                        }
                    }
                    continue;
                }

                let base_sha = base_tree.get(&norm);
                let curr_sha = tree.get(&norm);
                let tx_sha = change.blob_sha.as_ref();

                let base_bytes = base_sha.and_then(|s| virtual_blobs.get(s).cloned().or_else(|| self.storage.get_blob(s).ok()));
                let curr_bytes = curr_sha.and_then(|s| virtual_blobs.get(s).cloned().or_else(|| self.storage.get_blob(s).ok()));
                let tx_bytes = tx_sha.and_then(|s| virtual_blobs.get(s).cloned().or_else(|| self.storage.get_blob(s).ok()));

                let (merged_bytes_opt, step_conflicts) = SemanticMerger::merge_files(
                    &norm,
                    base_bytes.as_deref(),
                    curr_bytes.as_deref(),
                    tx_bytes.as_deref(),
                    "UNIFIED_HEAD",
                    &tx.agent_id,
                );

                if !step_conflicts.is_empty() {
                    for sc in step_conflicts {
                        let p = sc.file_path.clone().unwrap_or_else(|| sc.path.clone());
                        conflicts_by_path.entry(p).or_default().push(sc);
                    }
                }

                match merged_bytes_opt {
                    Some(merged_bytes) => {
                        let new_sha = if dry_run {
                            use sha2::{Sha256, Digest};
                            let mut h = Sha256::new();
                            h.update(format!("blob {}\0", merged_bytes.len()).as_bytes());
                            h.update(&merged_bytes);
                            let s = format!("{:x}", h.finalize());
                            virtual_blobs.insert(s.clone(), merged_bytes);
                            s
                        } else {
                            self.storage.store_blob(&merged_bytes)
                                .map_err(|e| format!("Failed to store blob: {}", e))?
                        };
                        tree.insert(norm, new_sha);
                    }
                    None => {
                        tree.remove(&norm);
                    }
                }
            }

            processed_set.insert(tx.tx_id.clone());
            checkpoint.processed_tx_ids.push(tx.tx_id.clone());
        }

        let mut all_conflicts: Vec<MergeConflict> = Vec::new();
        for list in conflicts_by_path.values() {
            all_conflicts.extend(list.clone());
        }

        if !dry_run {
            checkpoint.tree = tree.clone();
            checkpoint.pending_conflicts = all_conflicts.clone();
            let _ = self.save_checkpoint(&checkpoint);
        }

        let mut unified_files = HashMap::new();
        for (p, sha) in tree {
            let content_opt = virtual_blobs.get(&sha).cloned()
                .or_else(|| self.storage.get_blob(&sha).ok());
            if let Some(bytes) = content_opt {
                unified_files.insert(p, bytes);
            }
        }

        Ok((unified_files, all_conflicts))
    }

    /// Explicitly resolves a pending conflict on path.
    pub fn resolve_conflict(
        &self,
        path: &str,
        resolved_bytes: Option<Vec<u8>>,
        agent_id: &str,
        message: &str,
        target_conflict_ids: Option<Vec<String>>,
    ) -> std::io::Result<Transaction> {
        let norm = sanitize_repo_path(&self.repo_dir, path)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e))?;

        let mut changes = HashMap::new();
        match resolved_bytes {
            Some(bytes) => {
                let sha = self.storage.store_blob(&bytes)?;
                changes.insert(norm.clone(), ChangeRecord::modify(sha, None));
            }
            None => {
                changes.insert(norm.clone(), ChangeRecord::delete(None));
            }
        }

        let latest = self.journal.get_latest_transaction();
        let parents = latest.map(|t| vec![t.tx_id]).unwrap_or_default();

        let mut meta = serde_json::Map::new();
        meta.insert("resolved_conflict_path".to_string(), serde_json::Value::String(norm.clone()));
        let mut res_map = serde_json::Map::new();
        res_map.insert("resolved_path".to_string(), serde_json::Value::String(norm.clone()));
        if let Some(ref tids) = target_conflict_ids {
            let arr: Vec<serde_json::Value> = tids.iter().map(|s| serde_json::Value::String(s.clone())).collect();
            res_map.insert("target_conflict_ids".to_string(), serde_json::Value::Array(arr));
        }
        meta.insert("resolution".to_string(), serde_json::Value::Object(res_map));

        let tx = self.journal.append_transaction_with_metadata(
            &self.storage,
            agent_id,
            None,
            message,
            changes,
            parents,
            None,
            serde_json::Value::Object(meta),
        )?;

        let _ = self.reconcile_state(false);
        Ok(tx)
    }

    /// Smart materialization: writes only modified files, pre-validates to preserve local edits,
    /// blocks if conflicts exist unless force is specified.
    pub fn materialize(&self, force: bool) -> std::io::Result<(usize, Vec<MergeConflict>)> {
        let (unified_files, conflicts) = self.reconcile_state(false)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;

        if !conflicts.is_empty() && !force {
            return Err(std::io::Error::new(
                std::io::ErrorKind::Other,
                format!("Materialization blocked: {} unresolved conflict(s) pending", conflicts.len()),
            ));
        }

        let mut prev_state: HashMap<String, String> = HashMap::new();
        if self.materialized_state_path.exists() {
            if let Ok(bytes) = fs::read(&self.materialized_state_path) {
                if let Ok(map) = serde_json::from_slice(&bytes) {
                    prev_state = map;
                }
            }
        }

        // -------------------------------------------------------------
        // PHASE 1: Pre-validation of all files
        // -------------------------------------------------------------
        let mut all_paths = HashSet::new();
        for k in unified_files.keys() {
            all_paths.insert(k.clone());
        }
        for k in prev_state.keys() {
            all_paths.insert(k.clone());
        }

        let mut preserved_local_deletions = HashSet::new();

        for p in &all_paths {
            let full_path = self.repo_dir.join(p);
            let disk_bytes = fs::read(&full_path).ok();
            let disk_sha = disk_bytes.as_ref().map(|b| {
                use sha2::{Sha256, Digest};
                let mut h = Sha256::new();
                h.update(b);
                format!("{:x}", h.finalize())
            });

            let prev_sha = prev_state.get(p);
            let desired = unified_files.get(p);
            let desired_sha = desired.map(|b| {
                use sha2::{Sha256, Digest};
                let mut h = Sha256::new();
                h.update(b);
                format!("{:x}", h.finalize())
            });

            if disk_sha == desired_sha {
                continue;
            }

            // Scheduled for delete
            if desired_sha.is_none() {
                if disk_sha.is_some() && prev_sha.is_some() && disk_sha.as_ref() != prev_sha && !force {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::Other,
                        format!("Local modification would be deleted: '{}'", p),
                    ));
                }
            }

            // Scheduled for write
            if desired_sha.is_some() {
                if disk_sha.is_some() {
                    if prev_sha.is_none() && !force {
                        return Err(std::io::Error::new(
                            std::io::ErrorKind::Other,
                            format!("Untracked file would be overwritten: '{}'", p),
                        ));
                    } else if prev_sha.is_some() && disk_sha.as_ref() != prev_sha && !force {
                        return Err(std::io::Error::new(
                            std::io::ErrorKind::Other,
                            format!("Local modification would be overwritten: '{}'", p),
                        ));
                    }
                } else {
                    // File does not exist on disk
                    if let Some(prev) = prev_sha {
                        if Some(prev) == desired_sha.as_ref() {
                            // User deleted it locally and no agent modified it -> PRESERVE local deletion!
                            preserved_local_deletions.insert(p.clone());
                        } else if !force {
                            return Err(std::io::Error::new(
                                std::io::ErrorKind::Other,
                                format!("Local deletion conflict: '{}' was deleted locally but agent updated it", p),
                            ));
                        }
                    }
                }
            }
        }

        // -------------------------------------------------------------
        // PHASE 2: Safe atomic execution
        // -------------------------------------------------------------
        // 1. Remove deleted files (propagate error)
        for old_path in prev_state.keys() {
            if !unified_files.contains_key(old_path) {
                if let Ok(safe_old) = sanitize_repo_path(&self.repo_dir, old_path) {
                    let full = self.repo_dir.join(safe_old);
                    if full.exists() {
                        fs::remove_file(&full)?;
                    }
                }
            }
        }

        let mut written_count = 0;
        let mut new_state = HashMap::new();

        // 2. Write modified or added files atomically
        for (rel_path, content) in &unified_files {
            let safe_rel = sanitize_repo_path(&self.repo_dir, rel_path)
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e))?;
            let full_path = self.repo_dir.join(&safe_rel);
            let content_sha = {
                use sha2::{Sha256, Digest};
                let mut h = Sha256::new();
                h.update(content);
                format!("{:x}", h.finalize())
            };
            new_state.insert(rel_path.clone(), content_sha.clone());

            if preserved_local_deletions.contains(rel_path) && !force {
                continue;
            }

            if prev_state.get(rel_path) == Some(&content_sha) && full_path.exists() {
                continue;
            }

            if let Some(parent) = full_path.parent() {
                fs::create_dir_all(parent)?;
            }

            let tmp_path = full_path.with_extension(format!("tmp_{}_{:?}_{}.tmp", std::process::id(), std::thread::current().id(), uuid::Uuid::new_v4()));
            {
                let mut f = File::create(&tmp_path)?;
                f.write_all(content)?;
                f.sync_all()?;
            }
            fs::rename(&tmp_path, &full_path)?;
            written_count += 1;
        }

        // Save updated state (propagate error)
        let tmp_meta = self.materialized_state_path.with_extension(format!("tmp_{}_{}.tmp", std::process::id(), uuid::Uuid::new_v4()));
        let meta_bytes = serde_json::to_vec_pretty(&new_state)?;
        {
            let mut f = File::create(&tmp_meta)?;
            f.write_all(&meta_bytes)?;
            f.sync_all()?;
        }
        fs::rename(&tmp_meta, &self.materialized_state_path)?;

        Ok((written_count, conflicts))
    }
}
