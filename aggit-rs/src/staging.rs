use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use crate::storage::CASStorage;

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

    fn agent_staging_path(&self, agent_id: &str) -> PathBuf {
        let safe_name: String = agent_id
            .chars()
            .map(|c| if c.is_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
            .collect();
        self.staging_dir.join(format!("{}.json", safe_name))
    }

    pub fn get_staged(&self, agent_id: &str) -> HashMap<String, String> {
        let p = self.agent_staging_path(agent_id);
        if !p.exists() {
            return HashMap::new();
        }
        fs::read(&p)
            .ok()
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
            .unwrap_or_default()
    }

    pub fn stage_file(&self, storage: &CASStorage, agent_id: &str, rel_path: &str, content: Option<&[u8]>) -> std::io::Result<String> {
        let norm_path = rel_path.replace('\\', "/");
        let bytes_data: Vec<u8> = match content {
            Some(bytes) => bytes.to_vec(),
            None => {
                let full_path = self.repo_dir.join(&norm_path);
                fs::read(full_path)?
            }
        };

        let blob_sha = storage.store_blob(&bytes_data)?;
        let mut staged = self.get_staged(agent_id);
        staged.insert(norm_path, blob_sha.clone());

        let path = self.agent_staging_path(agent_id);
        let tmp = path.with_extension("tmp");
        let json_bytes = serde_json::to_vec_pretty(&staged)?;
        {
            let mut f = File::create(&tmp)?;
            f.write_all(&json_bytes)?;
        }
        fs::rename(tmp, path)?;

        Ok(blob_sha)
    }

    pub fn clear(&self, agent_id: &str) {
        let path = self.agent_staging_path(agent_id);
        if path.exists() {
            let _ = fs::remove_file(path);
        }
    }
}
