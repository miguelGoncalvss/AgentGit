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
}

impl MergeConflict {
    pub fn new(path: &str, conflict_type: &str, details: &str) -> Self {
        Self {
            path: path.to_string(),
            conflict_type: conflict_type.to_string(),
            details: details.to_string(),
            base_sha: None,
            a_sha: None,
            b_sha: None,
            label_a: None,
            label_b: None,
            conflict_id: Some(uuid::Uuid::new_v4().to_string()[..16].to_string()),
        }
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
        // Modify / Delete conflicts
        if a_bytes.is_none() && b_bytes.is_some() {
            conflicts.push(MergeConflict::new(path, "modify_delete", "File deleted by A but modified by B"));
            return (b_bytes.map(|b| b.to_vec()), conflicts);
        }

        if b_bytes.is_none() && a_bytes.is_some() {
            conflicts.push(MergeConflict::new(path, "modify_delete", "File deleted by B but modified by A"));
            return (a_bytes.map(|b| b.to_vec()), conflicts);
        }

        let a = a_bytes.unwrap();
        let b = b_bytes.unwrap();

        // Binary file check
        if is_binary(a) || is_binary(b) || base_bytes.map_or(false, is_binary) {
            conflicts.push(MergeConflict::new(path, "binary", "Binary file modified concurrently with different contents"));
            return (Some(b.to_vec()), conflicts);
        }

        let a_str = match std::str::from_utf8(a) {
            Ok(s) => s,
            Err(_) => return (Some(b.to_vec()), conflicts),
        };
        let b_str = match std::str::from_utf8(b) {
            Ok(s) => s,
            Err(_) => return (Some(b.to_vec()), conflicts),
        };
        let base_str = base_bytes.and_then(|b| std::str::from_utf8(b).ok()).unwrap_or("");

        let ext = path.split('.').last().unwrap_or("").to_lowercase();

        if ext == "json" {
            if let (Ok(val_a), Ok(val_b)) = (serde_json::from_str::<serde_json::Value>(a_str), serde_json::from_str::<serde_json::Value>(b_str)) {
                let val_base = serde_json::from_str::<serde_json::Value>(base_str).unwrap_or(serde_json::Value::Object(Default::default()));
                let (merged_val, json_conflicts) = Self::merge_json_recursive(&val_base, &val_a, &val_b, path);
                conflicts.extend(json_conflicts);
                if let Ok(mut json_bytes) = serde_json::to_vec_pretty(&merged_val) {
                    json_bytes.push(b'\n');
                    return (Some(json_bytes), conflicts);
                }
            }
        }

        // Textual 3-way merge
        let (merged_text, text_conflicts) = Self::merge_text_3way(base_str, a_str, b_str, path, label_a, label_b);
        conflicts.extend(text_conflicts);
        (Some(merged_text.into_bytes()), conflicts)
    }

    pub fn merge_json_recursive(
        base_val: &serde_json::Value,
        a_val: &serde_json::Value,
        b_val: &serde_json::Value,
        path: &str,
    ) -> (serde_json::Value, Vec<MergeConflict>) {
        let mut conflicts = Vec::new();

        if a_val == b_val {
            return (a_val.clone(), conflicts);
        }
        if a_val == base_val {
            return (b_val.clone(), conflicts);
        }
        if b_val == base_val {
            return (a_val.clone(), conflicts);
        }

        if let (Some(map_base), Some(map_a), Some(map_b)) = (base_val.as_object(), a_val.as_object(), b_val.as_object()) {
            let mut merged = serde_json::Map::new();
            let all_keys: BTreeSet<String> = map_base.keys().chain(map_a.keys()).chain(map_b.keys()).cloned().collect();

            for k in all_keys {
                let in_base = map_base.get(&k);
                let in_a = map_a.get(&k);
                let in_b = map_b.get(&k);

                match (in_a, in_b) {
                    (Some(va), None) => {
                        if let Some(vbase) = in_base {
                            if vbase == va {
                                // Untouched in A, deleted in B
                            } else {
                                // Modified in A, deleted in B -> Conflict
                                conflicts.push(MergeConflict::new(
                                    &format!("{}.{}", path, k),
                                    "json_key",
                                    "Key modified in A but deleted in B",
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
                                conflicts.push(MergeConflict::new(
                                    &format!("{}.{}", path, k),
                                    "json_key",
                                    "Key modified in B but deleted in A",
                                ));
                                merged.insert(k, vb.clone());
                            }
                        } else {
                            // Added only in B
                            merged.insert(k, vb.clone());
                        }
                    }
                    (Some(va), Some(vb)) => {
                        let empty_obj = serde_json::Value::Object(Default::default());
                        let vbase = in_base.unwrap_or(&empty_obj);
                        let (sub_val, sub_conflicts) = Self::merge_json_recursive(vbase, va, vb, &format!("{}.{}", path, k));
                        conflicts.extend(sub_conflicts);
                        merged.insert(k, sub_val);
                    }
                    (None, None) => {}
                }
            }

            return (serde_json::Value::Object(merged), conflicts);
        }

        // Conflict on scalar / non-matching types
        conflicts.push(MergeConflict::new(
            path,
            "json_key",
            &format!("Conflicting value updates in JSON at {}", path),
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
