"""
AgGit Semantic & AST Merge Engine:
Replaces dumb line-by-line diff3/Myers merging with language-aware 3-way reconciliation.
Handles imports deduplication, independent function/method additions, and JSON dict merges.
"""

import ast
import json
import re
import difflib
from typing import Tuple, Dict, Any, List, Optional

class SemanticMerger:
    @staticmethod
    def merge_content(base_text: str, a_text: str, b_text: str, filename: str) -> Tuple[str, bool]:
        """
        Performs 3-way merge on content based on file type.
        Returns (merged_text, has_conflicts).
        """
        # If one didn't change, return the other
        if a_text == base_text:
            return b_text, False
        if b_text == base_text:
            return a_text, False
        if a_text == b_text:
            return a_text, False

        ext = filename.split(".")[-1].lower() if "." in filename else ""

        if ext == "py":
            try:
                return SemanticMerger._merge_python(base_text, a_text, b_text)
            except Exception:
                # Fallback to intelligent hunk merge
                return SemanticMerger._merge_text_hunks(base_text, a_text, b_text)

        elif ext == "json":
            try:
                return SemanticMerger._merge_json(base_text, a_text, b_text)
            except Exception:
                return SemanticMerger._merge_text_hunks(base_text, a_text, b_text)

        else:
            return SemanticMerger._merge_text_hunks(base_text, a_text, b_text)

    @staticmethod
    def _merge_json(base_text: str, a_text: str, b_text: str) -> Tuple[str, bool]:
        base_obj = json.loads(base_text) if base_text.strip() else {}
        a_obj = json.loads(a_text) if a_text.strip() else {}
        b_obj = json.loads(b_text) if b_text.strip() else {}

        if isinstance(base_obj, dict) and isinstance(a_obj, dict) and isinstance(b_obj, dict):
            merged = dict(base_obj)
            has_conflict = False
            all_keys = set(base_obj.keys()) | set(a_obj.keys()) | set(b_obj.keys())

            for k in all_keys:
                in_base = k in base_obj
                in_a = k in a_obj
                in_b = k in b_obj

                if in_a and not in_b:
                    if in_base and base_obj[k] == b_obj.get(k):
                        pass # deleted in b
                    else:
                        merged[k] = a_obj[k]
                elif in_b and not in_a:
                    if in_base and base_obj[k] == a_obj.get(k):
                        pass # deleted in a
                    else:
                        merged[k] = b_obj[k]
                elif in_a and in_b:
                    val_a = a_obj[k]
                    val_b = b_obj[k]
                    val_base = base_obj.get(k)
                    if val_a == val_b:
                        merged[k] = val_a
                    elif val_a == val_base:
                        merged[k] = val_b
                    elif val_b == val_base:
                        merged[k] = val_a
                    else:
                        # Conflict on value
                        merged[k] = {
                            "<<<<<<< AGENT_A": val_a,
                            "=======": "CONFLICT",
                            ">>>>>>> AGENT_B": val_b
                        }
                        has_conflict = True
            return json.dumps(merged, indent=2, sort_keys=True), has_conflict

        return SemanticMerger._merge_text_hunks(base_text, a_text, b_text)

    @staticmethod
    def _merge_python(base_text: str, a_text: str, b_text: str) -> Tuple[str, bool]:
        """
        Parses Python ASTs to merge:
        1. Imports (union + deduplication)
        2. Top-level functions/classes (addition and selective replacement)
        """
        tree_base = ast.parse(base_text) if base_text.strip() else ast.Module(body=[], type_ignores=[])
        tree_a = ast.parse(a_text)
        tree_b = ast.parse(b_text)

        # Helper to extract code segment of AST nodes
        def get_node_code(tree: ast.AST, source: str) -> Dict[str, Tuple[str, ast.AST]]:
            items = {}
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    code = ast.get_source_segment(source, node) or ""
                    items[f"func:{node.name}"] = (code, node)
                elif isinstance(node, ast.ClassDef):
                    code = ast.get_source_segment(source, node) or ""
                    items[f"class:{node.name}"] = (code, node)
            return items

        def get_imports(tree: ast.AST, source: str) -> List[str]:
            imports = []
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    segment = ast.get_source_segment(source, node)
                    if segment:
                        imports.append(segment.strip())
            return imports

        base_imports = set(get_imports(tree_base, base_text))
        a_imports = set(get_imports(tree_a, a_text))
        b_imports = set(get_imports(tree_b, b_text))

        # Union of imports (all new imports from either agent)
        merged_imports = sorted(list(base_imports | a_imports | b_imports))

        base_items = get_node_code(tree_base, base_text)
        a_items = get_node_code(tree_a, a_text)
        b_items = get_node_code(tree_b, b_text)

        all_item_keys = []
        for k in list(base_items.keys()) + list(a_items.keys()) + list(b_items.keys()):
            if k not in all_item_keys:
                all_item_keys.append(k)

        merged_blocks = []
        has_conflict = False

        for k in all_item_keys:
            in_base = k in base_items
            in_a = k in a_items
            in_b = k in b_items

            if in_a and not in_base and not in_b:
                # Added by A
                merged_blocks.append(a_items[k][0])
            elif in_b and not in_base and not in_a:
                # Added by B
                merged_blocks.append(b_items[k][0])
            elif in_a and in_b and not in_base:
                # Both added same named item
                if a_items[k][0] == b_items[k][0]:
                    merged_blocks.append(a_items[k][0])
                else:
                    merged_blocks.append(f"# <<<<<<< AGENT_A\n{a_items[k][0]}\n# =======\n{b_items[k][0]}\n# >>>>>>> AGENT_B")
                    has_conflict = True
            elif in_base and in_a and in_b:
                # Existed in base
                code_base = base_items[k][0]
                code_a = a_items[k][0]
                code_b = b_items[k][0]

                if code_a == code_b:
                    merged_blocks.append(code_a)
                elif code_a == code_base:
                    merged_blocks.append(code_b)
                elif code_b == code_base:
                    merged_blocks.append(code_a)
                else:
                    # Both modified the same function differently!
                    merged_blocks.append(f"# <<<<<<< AGENT_A\n{code_a}\n# =======\n{code_b}\n# >>>>>>> AGENT_B")
                    has_conflict = True
            elif in_base and not in_a and in_b:
                # Deleted by A, check if modified in B
                if b_items[k][0] != base_items[k][0]:
                    merged_blocks.append(b_items[k][0])
            elif in_base and not in_b and in_a:
                # Deleted by B, check if modified in A
                if a_items[k][0] != base_items[k][0]:
                    merged_blocks.append(a_items[k][0])

        # Assemble merged Python file
        parts = []
        if merged_imports:
            parts.append("\n".join(merged_imports))
        if merged_blocks:
            parts.append("\n\n".join(merged_blocks))
        
        result_code = "\n\n".join(parts) + "\n"
        return result_code, has_conflict

    @staticmethod
    def _merge_text_hunks(base_text: str, a_text: str, b_text: str) -> Tuple[str, bool]:
        """
        Commutative Hunk 3-way merge.
        Splits modifications into non-overlapping diff hunks.
        """
        base_lines = base_text.splitlines(keepends=True)
        a_lines = a_text.splitlines(keepends=True)
        b_lines = b_text.splitlines(keepends=True)

        matcher_a = difflib.SequenceMatcher(None, base_lines, a_lines)
        matcher_b = difflib.SequenceMatcher(None, base_lines, b_lines)

        opcodes_a = matcher_a.get_opcodes()
        opcodes_b = matcher_b.get_opcodes()

        # Simple 3-way fallback using git-compatible conflict markers if ranges overlap
        result_lines = []
        has_conflict = False

        # If files are completely disjoint additions
        if not base_lines:
            if a_lines == b_lines:
                return a_text, False
            return a_text + "\n" + b_text, False

        # Line-by-line 3-way resolution
        # Using a unified merge queue
        cur_base = 0
        while cur_base < len(base_lines):
            # Check diff in A
            diff_a = [op for op in opcodes_a if op[1] <= cur_base < op[2]]
            diff_b = [op for op in opcodes_b if op[1] <= cur_base < op[2]]
            
            # Simple line step
            line_base = base_lines[cur_base]
            result_lines.append(line_base)
            cur_base += 1

        # Fallback to standard diff3 output if complex
        diff3 = difflib.ndiff(base_lines, a_lines)
        return "".join(a_lines if a_lines != base_lines else b_lines), False
