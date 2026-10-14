use std::collections::HashMap;
use serde::{Deserialize, Serialize};
use chrono::Utc;

pub const CURRENT_SCHEMA_VERSION: u32 = 2;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ChangeAction {
    Add,
    Modify,
    Delete,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChangeRecord {
    pub action: ChangeAction,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub blob_sha: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub old_sha: Option<String>,
}

impl ChangeRecord {
    pub fn add(blob_sha: String) -> Self {
        Self {
            action: ChangeAction::Add,
            blob_sha: Some(blob_sha),
            old_sha: None,
        }
    }

    pub fn modify(blob_sha: String, old_sha: Option<String>) -> Self {
        Self {
            action: ChangeAction::Modify,
            blob_sha: Some(blob_sha),
            old_sha,
        }
    }

    pub fn delete(old_sha: Option<String>) -> Self {
        Self {
            action: ChangeAction::Delete,
            blob_sha: None,
            old_sha,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Transaction {
    #[serde(default = "default_schema_version")]
    pub schema_version: u32,
    pub tx_id: String,
    pub agent_id: String,
    #[serde(default = "default_empty_string")]
    pub session_id: String,
    #[serde(default)]
    pub base_revision: Option<String>,
    #[serde(default)]
    pub parents: Vec<String>,
    #[serde(default = "default_iso_timestamp")]
    pub timestamp: String,
    pub timestamp_ns: i64,
    pub message: String,
    #[serde(default)]
    pub changes: HashMap<String, ChangeRecord>,
    #[serde(default)]
    pub files: HashMap<String, String>, // Legacy compatibility
    #[serde(default)]
    pub metadata: serde_json::Value,
}

fn default_schema_version() -> u32 {
    CURRENT_SCHEMA_VERSION
}

fn default_empty_string() -> String {
    String::new()
}

fn default_iso_timestamp() -> String {
    Utc::now().to_rfc3339()
}

impl Transaction {
    pub fn new(
        agent_id: &str,
        session_id: Option<&str>,
        message: &str,
        changes: HashMap<String, ChangeRecord>,
        parents: Vec<String>,
        base_revision: Option<String>,
    ) -> Self {
        let now = Utc::now();
        let ts_ns = now.timestamp_nanos_opt().unwrap_or_else(|| now.timestamp_millis() * 1_000_000);
        let tx_id = uuid::Uuid::new_v4().to_string();

        let mut files_legacy = HashMap::new();
        for (path, change) in &changes {
            if let Some(sha) = &change.blob_sha {
                files_legacy.insert(path.clone(), sha.clone());
            }
        }

        Self {
            schema_version: CURRENT_SCHEMA_VERSION,
            tx_id,
            agent_id: agent_id.to_string(),
            session_id: session_id.unwrap_or(agent_id).to_string(),
            base_revision,
            parents,
            timestamp: now.to_rfc3339(),
            timestamp_ns: ts_ns,
            message: message.to_string(),
            changes,
            files: files_legacy,
            metadata: serde_json::Value::Object(Default::default()),
        }
    }

    /// Normalizes transaction so that changes map is guaranteed populated even for legacy v1 records
    pub fn get_effective_changes(&self) -> HashMap<String, ChangeRecord> {
        if !self.changes.is_empty() {
            return self.changes.clone();
        }
        // Fallback for legacy v1 format
        let mut map = HashMap::new();
        for (path, sha) in &self.files {
            map.insert(path.clone(), ChangeRecord {
                action: ChangeAction::Modify,
                blob_sha: Some(sha.clone()),
                old_sha: None,
            });
        }
        map
    }
}
