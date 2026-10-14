use std::collections::{BTreeSet, HashMap};

pub struct SemanticMerger;

impl SemanticMerger {
    pub fn merge_content(base: &str, a: &str, b: &str, filename: &str) -> (String, bool) {
        if a == base {
            return (b.to_string(), false);
        }
        if b == base {
            return (a.to_string(), false);
        }
        if a == b {
            return (a.to_string(), false);
        }

        let ext = filename.split('.').last().unwrap_or("").to_lowercase();

        match ext.as_str() {
            "py" => Self::merge_python_like(base, a, b),
            "json" => Self::merge_json(base, a, b),
            _ => Self::merge_text_hunks(base, a, b),
        }
    }

    fn merge_json(base: &str, a: &str, b: &str) -> (String, bool) {
        let val_base: serde_json::Value = serde_json::from_str(base).unwrap_or(serde_json::Value::Object(Default::default()));
        let val_a: serde_json::Value = serde_json::from_str(a).unwrap_or(serde_json::Value::Object(Default::default()));
        let val_b: serde_json::Value = serde_json::from_str(b).unwrap_or(serde_json::Value::Object(Default::default()));

        if let (Some(map_base), Some(map_a), Some(map_b)) = (val_base.as_object(), val_a.as_object(), val_b.as_object()) {
            let mut merged = map_base.clone();
            let mut has_conflict = false;

            let all_keys: BTreeSet<String> = map_base.keys().chain(map_a.keys()).chain(map_b.keys()).cloned().collect();

            for k in all_keys {
                let in_a = map_a.get(&k);
                let in_b = map_b.get(&k);
                let in_base = map_base.get(&k);

                match (in_a, in_b) {
                    (Some(va), None) => {
                        if in_base == in_b {
                            // deleted in b
                            merged.remove(&k);
                        } else {
                            merged.insert(k, va.clone());
                        }
                    }
                    (None, Some(vb)) => {
                        if in_base == in_a {
                            // deleted in a
                            merged.remove(&k);
                        } else {
                            merged.insert(k, vb.clone());
                        }
                    }
                    (Some(va), Some(vb)) => {
                        if va == vb {
                            merged.insert(k, va.clone());
                        } else if Some(va) == in_base {
                            merged.insert(k, vb.clone());
                        } else if Some(vb) == in_base {
                            merged.insert(k, va.clone());
                        } else {
                            // Conflict
                            let mut conflict_obj = serde_json::Map::new();
                            conflict_obj.insert("<<<<<<< AGENT_A".to_string(), va.clone());
                            conflict_obj.insert("=======".to_string(), serde_json::Value::String("CONFLICT".to_string()));
                            conflict_obj.insert(">>>>>>> AGENT_B".to_string(), vb.clone());
                            merged.insert(k, serde_json::Value::Object(conflict_obj));
                            has_conflict = true;
                        }
                    }
                    (None, None) => {
                        merged.remove(&k);
                    }
                }
            }
            return (serde_json::to_string_pretty(&merged).unwrap(), has_conflict);
        }

        Self::merge_text_hunks(base, a, b)
    }

    fn merge_python_like(base: &str, a: &str, b: &str) -> (String, bool) {
        let (imports_a, blocks_a) = Self::extract_py_components(a);
        let (imports_b, blocks_b) = Self::extract_py_components(b);
        let (imports_base, blocks_base) = Self::extract_py_components(base);

        // Union of imports
        let mut all_imports = BTreeSet::new();
        all_imports.extend(imports_base);
        all_imports.extend(imports_a);
        all_imports.extend(imports_b);

        let mut all_block_keys: Vec<String> = Vec::new();
        for k in blocks_base.keys().chain(blocks_a.keys()).chain(blocks_b.keys()) {
            if !all_block_keys.contains(k) {
                all_block_keys.push(k.clone());
            }
        }

        let mut merged_blocks = Vec::new();
        let mut has_conflict = false;

        for k in all_block_keys {
            let in_base = blocks_base.get(&k);
            let in_a = blocks_a.get(&k);
            let in_b = blocks_b.get(&k);

            match (in_base, in_a, in_b) {
                (None, Some(ca), None) => merged_blocks.push(ca.clone()),
                (None, None, Some(cb)) => merged_blocks.push(cb.clone()),
                (None, Some(ca), Some(cb)) => {
                    if ca == cb {
                        merged_blocks.push(ca.clone());
                    } else {
                        merged_blocks.push(format!("# <<<<<<< AGENT_A\n{}\n# =======\n{}\n# >>>>>>> AGENT_B", ca, cb));
                        has_conflict = true;
                    }
                }
                (Some(cbase), Some(ca), Some(cb)) => {
                    if ca == cb {
                        merged_blocks.push(ca.clone());
                    } else if ca == cbase {
                        merged_blocks.push(cb.clone());
                    } else if cb == cbase {
                        merged_blocks.push(ca.clone());
                    } else {
                        merged_blocks.push(format!("# <<<<<<< AGENT_A\n{}\n# =======\n{}\n# >>>>>>> AGENT_B", ca, cb));
                        has_conflict = true;
                    }
                }
                (Some(cbase), None, Some(cb)) => {
                    if cb != cbase {
                        merged_blocks.push(cb.clone());
                    }
                }
                (Some(cbase), Some(ca), None) => {
                    if ca != cbase {
                        merged_blocks.push(ca.clone());
                    }
                }
                (Some(_), None, None) => {}
                _ => {}
            }
        }

        let mut parts = Vec::new();
        if !all_imports.is_empty() {
            let imp_vec: Vec<String> = all_imports.into_iter().collect();
            parts.push(imp_vec.join("\n"));
        }
        if !merged_blocks.is_empty() {
            parts.push(merged_blocks.join("\n\n"));
        }

        (parts.join("\n\n") + "\n", has_conflict)
    }

    fn extract_py_components(src: &str) -> (BTreeSet<String>, HashMap<String, String>) {
        let mut imports = BTreeSet::new();
        let mut blocks = HashMap::new();

        let mut cur_block_name = None;
        let mut cur_block_lines: Vec<&str> = Vec::new();

        for line in src.lines() {
            let trimmed = line.trim();
            if trimmed.starts_with("import ") || trimmed.starts_with("from ") {
                imports.insert(trimmed.to_string());
                continue;
            }

            if trimmed.starts_with("def ") || trimmed.starts_with("async def ") || trimmed.starts_with("class ") {
                if let Some(name) = cur_block_name.take() {
                    blocks.insert(name, cur_block_lines.join("\n"));
                    cur_block_lines.clear();
                }

                let sig_start = if trimmed.starts_with("async def ") { 10 } else if trimmed.starts_with("def ") { 4 } else { 6 };
                let rest = &trimmed[sig_start..];
                let name = rest.split('(').next().unwrap_or(rest).split(':').next().unwrap_or(rest).trim().to_string();
                cur_block_name = Some(name);
                cur_block_lines.push(line);
            } else if cur_block_name.is_some() {
                cur_block_lines.push(line);
            }
        }

        if let Some(name) = cur_block_name {
            blocks.insert(name, cur_block_lines.join("\n"));
        }

        (imports, blocks)
    }

    fn merge_text_hunks(_base: &str, a: &str, b: &str) -> (String, bool) {
        if a == b {
            (a.to_string(), false)
        } else {
            (format!("{}\n{}", a, b), false)
        }
    }
}
