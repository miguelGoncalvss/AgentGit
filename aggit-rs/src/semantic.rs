use std::collections::BTreeSet;
use std::process::Command;
use std::fs::File;
use std::io::Write;

use serde::{Serialize, Deserialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MergeConflict {
    pub path: String,
    pub conflict_type: String,
    pub details: String,
    #[serde(default)]
    pub base_sha: Option<String>,
    #[serde(default)]
    pub a_sha: Option<String>,
    #[serde(default)]
    pub b_sha: Option<String>,
    #[serde(default)]
    pub label_a: Option<String>,
    #[serde(default)]
    pub label_b: Option<String>,
    #[serde(default)]
    pub conflict_id: Option<String>,
    #[serde(default)]
    pub file_path: Option<String>,
    #[serde(default)]
    pub internal_path: Vec<String>,
}

impl MergeConflict {
    pub fn compute_conflict_id(
        file_path: &str,
        internal_path: &[String],
        conflict_type: &str,
        base_sha: Option<&str>,
        a_sha: Option<&str>,
        b_sha: Option<&str>,
    ) -> String {
        use sha2::{Sha256, Digest};
        let mut h = Sha256::new();
        let sub = serde_json::to_string(internal_path).unwrap_or_default();
        let mut vers = vec![a_sha.unwrap_or("NONE"), b_sha.unwrap_or("NONE")];
        vers.sort();
        let raw = format!(
            "{}::{}::{}::{}::{}::{}",
            file_path,
            sub,
            conflict_type,
            base_sha.unwrap_or("NONE"),
            vers[0],
            vers[1]
        );
        h.update(raw.as_bytes());
        format!("{:x}", h.finalize())[..16].to_string()
    }

    pub fn new(path: &str, conflict_type: &str, details: &str) -> Self {
        let cid = Self::compute_conflict_id(path, &[], conflict_type, None, None, None);
        Self {
            path: path.to_string(),
            conflict_type: conflict_type.to_string(),
            details: details.to_string(),
            base_sha: None,
            a_sha: None,
            b_sha: None,
            label_a: None,
            label_b: None,
            conflict_id: Some(cid),
            file_path: Some(path.to_string()),
            internal_path: Vec::new(),
        }
    }

    pub fn with_internal(file_path: &str, internal_path: Vec<String>, conflict_type: &str, details: &str) -> Self {
        let cid = Self::compute_conflict_id(file_path, &internal_path, conflict_type, None, None, None);
        Self {
            path: file_path.to_string(),
            conflict_type: conflict_type.to_string(),
            details: details.to_string(),
            base_sha: None,
            a_sha: None,
            b_sha: None,
            label_a: None,
            label_b: None,
            conflict_id: Some(cid),
            file_path: Some(file_path.to_string()),
            internal_path,
        }
    }

    pub fn recompute_id(&mut self) {
        let fpath = self.file_path.as_deref().unwrap_or(&self.path);
        self.conflict_id = Some(Self::compute_conflict_id(
            fpath,
            &self.internal_path,
            &self.conflict_type,
            self.base_sha.as_deref(),
            self.a_sha.as_deref(),
            self.b_sha.as_deref(),
        ));
    }
}

pub struct SemanticMerger;

fn is_binary(bytes: &[u8]) -> bool {
    let check_len = bytes.len().min(8192);
    if bytes[..check_len].contains(&0) {
        return true;
    }
    std::str::from_utf8(bytes).is_err()
}

impl SemanticMerger {
    pub fn merge_files(
        path: &str,
        base_bytes: Option<&[u8]>,
        a_bytes: Option<&[u8]>,
        b_bytes: Option<&[u8]>,
        label_a: &str,
        label_b: &str,
    ) -> (Option<Vec<u8>>, Vec<MergeConflict>) {
        let mut conflicts = Vec::new();

        // 1. Both unchanged
        if a_bytes == base_bytes && b_bytes == base_bytes {
            return (base_bytes.map(|b| b.to_vec()), conflicts);
        }

        // 2. Both sides made identical change (including both deleting)
        if a_bytes == b_bytes {
            return (a_bytes.map(|b| b.to_vec()), conflicts);
        }

        // 3. Only A changed (B unchanged from base)
        if b_bytes == base_bytes {
            return (a_bytes.map(|b| b.to_vec()), conflicts);
        }

        // 4. Only B changed (A unchanged from base)
        if a_bytes == base_bytes {
            return (b_bytes.map(|b| b.to_vec()), conflicts);
        }

        // 5. Both changed differently
        let base_sha = base_bytes.map(|b| {
            use sha2::{Sha256, Digest};
            let mut h = Sha256::new();
            h.update(b);
            format!("{:x}", h.finalize())
        });
        let a_sha = a_bytes.map(|b| {
            use sha2::{Sha256, Digest};
            let mut h = Sha256::new();
            h.update(b);
            format!("{:x}", h.finalize())
        });
        let b_sha = b_bytes.map(|b| {
            use sha2::{Sha256, Digest};
            let mut h = Sha256::new();
            h.update(b);
            format!("{:x}", h.finalize())
        });

        let attach_metadata = |conflicts: &mut Vec<MergeConflict>| {
            for c in conflicts.iter_mut() {
                c.base_sha = base_sha.clone();
                c.a_sha = a_sha.clone();
                c.b_sha = b_sha.clone();
                c.label_a = Some(label_a.to_string());
                c.label_b = Some(label_b.to_string());
                c.recompute_id();
            }
        };

        // Modify / Delete conflicts
        if a_bytes.is_none() && b_bytes.is_some() {
            conflicts.push(MergeConflict::new(path, "modify_delete", "File deleted by A but modified by B"));
            attach_metadata(&mut conflicts);
            return (b_bytes.map(|b| b.to_vec()), conflicts);
        }

        if b_bytes.is_none() && a_bytes.is_some() {
            conflicts.push(MergeConflict::new(path, "modify_delete", "File deleted by B but modified by A"));
            attach_metadata(&mut conflicts);
            return (a_bytes.map(|b| b.to_vec()), conflicts);
        }

        let a = a_bytes.unwrap();
        let b = b_bytes.unwrap();

        // Binary file check
        if is_binary(a) || is_binary(b) || base_bytes.map_or(false, is_binary) {
            conflicts.push(MergeConflict::new(path, "binary", "Binary file modified concurrently with different contents"));
            attach_metadata(&mut conflicts);
            return (Some(b.to_vec()), conflicts);
        }

        let a_str = match std::str::from_utf8(a) {
            Ok(s) => s,
            Err(_) => {
                conflicts.push(MergeConflict::new(path, "binary", "File not valid UTF-8"));
                attach_metadata(&mut conflicts);
                return (Some(b.to_vec()), conflicts);
            }
        };
        let b_str = match std::str::from_utf8(b) {
            Ok(s) => s,
            Err(_) => {
                conflicts.push(MergeConflict::new(path, "binary", "File not valid UTF-8"));
                attach_metadata(&mut conflicts);
                return (Some(b.to_vec()), conflicts);
            }
        };
        let base_str = base_bytes.and_then(|b| std::str::from_utf8(b).ok()).unwrap_or("");

        let ext = path.split('.').last().unwrap_or("").to_lowercase();

        if ext == "json" {
            if let (Ok(val_a), Ok(val_b)) = (serde_json::from_str::<serde_json::Value>(a_str), serde_json::from_str::<serde_json::Value>(b_str)) {
                let val_base_opt = serde_json::from_str::<serde_json::Value>(base_str).ok();
                let (merged_val, json_conflicts) = Self::merge_json_recursive(val_base_opt.as_ref(), &val_a, &val_b, path, &[]);
                conflicts.extend(json_conflicts);
                attach_metadata(&mut conflicts);
                if let Ok(mut json_bytes) = serde_json::to_vec_pretty(&merged_val) {
                    json_bytes.push(b'\n');
                    return (Some(json_bytes), conflicts);
                }
            }
        }

        // Textual 3-way merge
        let (merged_text, text_conflicts) = Self::merge_text_3way(base_str, a_str, b_str, path, label_a, label_b);
        conflicts.extend(text_conflicts);
        attach_metadata(&mut conflicts);

        (Some(merged_text.into_bytes()), conflicts)
    }

    pub fn merge_json_recursive(
        base_val: Option<&serde_json::Value>,
        a_val: &serde_json::Value,
        b_val: &serde_json::Value,
        file_path: &str,
        internal_path: &[String],
    ) -> (serde_json::Value, Vec<MergeConflict>) {
        let mut conflicts = Vec::new();

        if a_val == b_val {
            return (a_val.clone(), conflicts);
        }
        if base_val == Some(a_val) {
            return (b_val.clone(), conflicts);
        }
        if base_val == Some(b_val) {
            return (a_val.clone(), conflicts);
        }

        if let (Some(map_a), Some(map_b)) = (a_val.as_object(), b_val.as_object()) {
            let map_base_opt = base_val.and_then(|v| v.as_object());
            let mut merged = serde_json::Map::new();
            let mut all_keys: BTreeSet<String> = BTreeSet::new();
            if let Some(mb) = map_base_opt {
                all_keys.extend(mb.keys().cloned());
            }
            all_keys.extend(map_a.keys().cloned());
            all_keys.extend(map_b.keys().cloned());

            for k in all_keys {
                let in_base = map_base_opt.and_then(|m| m.get(&k));
                let in_a = map_a.get(&k);
                let in_b = map_b.get(&k);
                let mut sub_internal = internal_path.to_vec();
                sub_internal.push(k.clone());

                match (in_a, in_b) {
                    (Some(va), None) => {
                        if let Some(vbase) = in_base {
                            if vbase == va {
                                // Untouched in A, deleted in B
                            } else {
                                // Modified in A, deleted in B -> Conflict
                                conflicts.push(MergeConflict::with_internal(
                                    file_path,
                                    sub_internal,
                                    "json_key",
                                    &format!("Key '{}' modified in A but deleted in B", k),
                                ));
                                merged.insert(k, va.clone());
                            }
                        } else {
                            // Added only in A
                            merged.insert(k, va.clone());
                        }
                    }
                    (None, Some(vb)) => {
                        if let Some(vbase) = in_base {
                            if vbase == vb {
                                // Untouched in B, deleted in A
                            } else {
                                // Modified in B, deleted in A -> Conflict
                                conflicts.push(MergeConflict::with_internal(
                                    file_path,
                                    sub_internal,
                                    "json_key",
                                    &format!("Key '{}' modified in B but deleted in A", k),
                                ));
                                merged.insert(k, vb.clone());
                            }
                        } else {
                            // Added only in B
                            merged.insert(k, vb.clone());
                        }
                    }
                    (Some(va), Some(vb)) => {
                        let (sub_val, sub_conflicts) = Self::merge_json_recursive(in_base, va, vb, file_path, &sub_internal);
                        conflicts.extend(sub_conflicts);
                        merged.insert(k, sub_val);
                    }
                    (None, None) => {}
                }
            }

            return (serde_json::Value::Object(merged), conflicts);
        }

        // Conflict on scalar / non-matching types
        let key_display = if internal_path.is_empty() {
            file_path.to_string()
        } else {
            internal_path.join("/")
        };
        conflicts.push(MergeConflict::with_internal(
            file_path,
            internal_path.to_vec(),
            "json_key",
            &format!("Conflicting value updates in JSON at {}", key_display),
        ));
        (b_val.clone(), conflicts)
    }

    pub fn merge_text_3way(
        base_text: &str,
        a_text: &str,
        b_text: &str,
        path: &str,
        label_a: &str,
        label_b: &str,
    ) -> (String, Vec<MergeConflict>) {
        // Try git merge-file -p
        let temp_dir = std::env::temp_dir();
        let uid = uuid::Uuid::new_v4();
        let path_a = temp_dir.join(format!("aggit_merge_a_{}.tmp", uid));
        let path_base = temp_dir.join(format!("aggit_merge_base_{}.tmp", uid));
        let path_b = temp_dir.join(format!("aggit_merge_b_{}.tmp", uid));

        let write_ok = (|| -> std::io::Result<()> {
            let mut fa = File::create(&path_a)?;
            fa.write_all(a_text.as_bytes())?;
            let mut fbase = File::create(&path_base)?;
            fbase.write_all(base_text.as_bytes())?;
            let mut fb = File::create(&path_b)?;
            fb.write_all(b_text.as_bytes())?;
            Ok(())
        })();

        if write_ok.is_ok() {
            let output = Command::new("git")
                .args([
                    "merge-file",
                    "-p",
                    &format!("-L{}", label_a),
                    "-LBASE",
                    &format!("-L{}", label_b),
                    path_a.to_str().unwrap(),
                    path_base.to_str().unwrap(),
                    path_b.to_str().unwrap(),
                ])
                .output();

            let _ = std::fs::remove_file(&path_a);
            let _ = std::fs::remove_file(&path_base);
            let _ = std::fs::remove_file(&path_b);

            if let Ok(out) = output {
                let merged = String::from_utf8_lossy(&out.stdout).to_string();
                let has_conflict = out.status.code().unwrap_or(0) != 0 || merged.contains(&format!("<<<<<<< {}", label_a));
                let mut conflicts = Vec::new();
                if has_conflict {
                    conflicts.push(MergeConflict::new(
                        path,
                        "text_overlap",
                        "Text conflict detected via git merge-file",
                    ));
                }
                return (merged, conflicts);
            }
        }

        // Fallback: structured conflict markers if text differs
        let conflict = MergeConflict::new(
            path,
            "text_overlap",
            "Overlapping text modification",
        );
        let merged = format!("<<<<<<< {}\n{}=======\n{}>>>>>>> {}\n", label_a, a_text, b_text, label_b);
        (merged, vec![conflict])
    }
}
