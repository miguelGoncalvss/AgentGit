use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use crate::storage::CASStorage;
use crate::path_utils::sanitize_repo_path;
use crate::schema::{ChangeAction, ChangeRecord};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StagingDoc {
    pub generation: u64,
    pub agent_id: String,
    pub session_id: String,
    pub base_revision: Option<String>,
    pub entries: HashMap<String, ChangeRecord>,
}

pub struct StagingArea {
    repo_dir: PathBuf,
    staging_dir: PathBuf,
}

impl StagingArea {
    pub fn new<P: AsRef<Path>>(repo_dir: P) -> Self {
        let repo_buf = repo_dir.as_ref().to_path_buf();
        let staging_dir = repo_buf.join(".aggit").join("staging");
        fs::create_dir_all(&staging_dir).expect("Failed to create staging directory");
        Self {
            repo_dir: repo_buf,
            staging_dir,
        }
    }

    fn agent_staging_filename(&self, agent_id: &str, session_id: Option<&str>) -> String {
        let effective_session = session_id.unwrap_or(agent_id);
        let ident = format!("{}::{}", agent_id, effective_session);
        let mut hasher = Sha256::new();
        hasher.update(ident.as_bytes());
        let hash_prefix = &format!("{:x}", hasher.finalize())[..12];

        let safe_prefix: String = agent_id
            .chars()
            .take(24)
            .map(|c| if c.is_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
            .collect();
        format!("{}_{}.json", safe_prefix, hash_prefix)
    }

    fn agent_staging_path(&self, agent_id: &str, session_id: Option<&str>) -> PathBuf {
        self.staging_dir.join(self.agent_staging_filename(agent_id, session_id))
    }

    pub fn get_staged_doc(&self, agent_id: &str, session_id: Option<&str>) -> StagingDoc {
        let p = self.agent_staging_path(agent_id, session_id);
        if !p.exists() {
            return StagingDoc {
                generation: 0,
                agent_id: agent_id.to_string(),
                session_id: session_id.unwrap_or(agent_id).to_string(),
                base_revision: None,
                entries: HashMap::new(),
            };
        }

        if let Ok(bytes) = fs::read(&p) {
            if let Ok(doc) = serde_json::from_slice::<StagingDoc>(&bytes) {
                return doc;
            }
            // Legacy migration: file was HashMap<String, String> (path -> sha)
            if let Ok(legacy_map) = serde_json::from_slice::<HashMap<String, String>>(&bytes) {
                let mut entries = HashMap::new();
                for (path, sha) in legacy_map {
                    entries.insert(path, ChangeRecord {
                        action: ChangeAction::Modify,
                        blob_sha: Some(sha),
                        old_sha: None,
                    });
                }
                return StagingDoc {
                    generation: 1,
                    agent_id: agent_id.to_string(),
                    session_id: session_id.unwrap_or(agent_id).to_string(),
                    base_revision: None,
                    entries,
                };
            }
        }

        StagingDoc {
            generation: 0,
            agent_id: agent_id.to_string(),
            session_id: session_id.unwrap_or(agent_id).to_string(),
            base_revision: None,
            entries: HashMap::new(),
        }
    }

    pub fn get_staged_changes(&self, agent_id: &str, session_id: Option<&str>) -> HashMap<String, ChangeRecord> {
        self.get_staged_doc(agent_id, session_id).entries
    }

    pub fn set_base_revision(&self, agent_id: &str, session_id: Option<&str>, base_revision: &str) -> std::io::Result<()> {
        let mut doc = self.get_staged_doc(agent_id, session_id);
        doc.base_revision = Some(base_revision.to_string());
        doc.generation += 1;
        self.write_staging_doc(agent_id, session_id, &doc)
    }

    fn write_staging_doc(&self, agent_id: &str, session_id: Option<&str>, doc: &StagingDoc) -> std::io::Result<()> {
        let path = self.agent_staging_path(agent_id, session_id);
        let tmp_path = path.with_extension(format!("tmp_{}_{}", std::process::id(), uuid::Uuid::new_v4()));
        let json_bytes = serde_json::to_vec_pretty(doc)?;
        {
            let mut f = File::create(&tmp_path)?;
            f.write_all(&json_bytes)?;
            f.sync_all()?;
        }
        fs::rename(tmp_path, path)?;
        Ok(())
    }

    /// Stages a batch of additions/modifications and deletions atomically in a single write.
    pub fn stage_batch(
        &self,
        storage: &CASStorage,
        agent_id: &str,
        session_id: Option<&str>,
        files_to_stage: Option<HashMap<String, Option<Vec<u8>>>>,
        files_to_delete: Option<Vec<String>>,
        base_revision: Option<String>,
    ) -> std::io::Result<HashMap<String, ChangeRecord>> {
        let mut doc = self.get_staged_doc(agent_id, session_id);
        if let Some(base) = base_revision {
            doc.base_revision = Some(base);
        }

        if let Some(files) = files_to_stage {
            for (rel_path, content_opt) in files {
                let norm_path = sanitize_repo_path(&self.repo_dir, &rel_path)
                    .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e))?;

                let bytes = match content_opt {
                    Some(b) => b,
                    None => {
                        let full = self.repo_dir.join(&norm_path);
                        fs::read(full)?
                    }
                };

                let blob_sha = storage.store_blob(&bytes)?;
                let old_sha = doc.entries.get(&norm_path).and_then(|c| c.blob_sha.clone());
                let action = if old_sha.is_none() { ChangeAction::Add } else { ChangeAction::Modify };

                doc.entries.insert(norm_path, ChangeRecord {
                    action,
                    blob_sha: Some(blob_sha),
                    old_sha,
                });
            }
        }

        if let Some(deletions) = files_to_delete {
            for rel_path in deletions {
                let norm_path = sanitize_repo_path(&self.repo_dir, &rel_path)
                    .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e))?;
                let old_sha = doc.entries.get(&norm_path).and_then(|c| c.blob_sha.clone());
                doc.entries.insert(norm_path, ChangeRecord {
                    action: ChangeAction::Delete,
                    blob_sha: None,
                    old_sha,
                });
            }
        }

        doc.generation += 1;
        self.write_staging_doc(agent_id, session_id, &doc)?;
        Ok(doc.entries)
    }

    pub fn stage_file(
        &self,
        storage: &CASStorage,
        agent_id: &str,
        rel_path: &str,
        content: Option<&[u8]>,
    ) -> std::io::Result<String> {
        let mut map = HashMap::new();
        map.insert(rel_path.to_string(), content.map(|c| c.to_vec()));
        let res = self.stage_batch(storage, agent_id, None, Some(map), None, None)?;
        let norm = sanitize_repo_path(&self.repo_dir, rel_path)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e))?;
        let sha = res.get(&norm).and_then(|c| c.blob_sha.clone()).unwrap_or_default();
        Ok(sha)
    }

    pub fn stage_delete(&self, agent_id: &str, rel_path: &str, session_id: Option<&str>) -> std::io::Result<()> {
        self.stage_batch(&CASStorage::new(&self.repo_dir), agent_id, session_id, None, Some(vec![rel_path.to_string()]), None)?;
        Ok(())
    }

    pub fn clear_if_generation_le(&self, agent_id: &str, session_id: Option<&str>, max_generation: u64) {
        let doc = self.get_staged_doc(agent_id, session_id);
        if doc.generation <= max_generation {
            let path = self.agent_staging_path(agent_id, session_id);
            if path.exists() {
                let _ = fs::remove_file(path);
            }
        }
    }

    pub fn clear(&self, agent_id: &str) {
        let path = self.agent_staging_path(agent_id, None);
        if path.exists() {
            let _ = fs::remove_file(path);
        }
    }
}
