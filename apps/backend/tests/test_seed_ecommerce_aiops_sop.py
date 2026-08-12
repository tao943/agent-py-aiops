from __future__ import annotations

import ast
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "seed_ecommerce_aiops_sop.py"


def test_sop_seeder_reuses_shared_index_task_client() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    local_functions = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    imported_names = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "knowledge_index_client"
        for alias in node.names
    }
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "_wait_for_index" not in local_functions
    assert {"parse_created_task", "wait_for_index_task"} <= imported_names
    assert {"parse_created_task", "wait_for_index_task"} <= called_names


def test_sop_seeder_builds_the_index_task_query_endpoint() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert 'f"/knowledge-bases/{knowledge_base_id}/documents/{document_id}"' in source
    assert 'f"/index-tasks/{task_id}"' in source
