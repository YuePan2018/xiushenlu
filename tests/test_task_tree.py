from __future__ import annotations

import json
import shutil
import unittest
import uuid
from pathlib import Path
from typing import Any

from app.task_tree import (
    TaskTreeError,
    list_task_trees,
    parse_task_tree_text,
    read_task_tree,
    save_task_tree,
    task_tree_filename,
)


class TaskTreeTests(unittest.TestCase):
    def test_parse_task_tree_accepts_fenced_json_and_drops_node_metadata(self) -> None:
        tree = parse_task_tree_text(
            """```json
{
  "version": 1,
  "title": "写一本书",
  "summary": "长期写作计划",
  "nodes": [
    {
      "title": "每天写 500 字",
      "kind": "habit",
      "cadence": "daily",
      "status": "doing",
      "note": "旧版节点说明",
      "tags": ["每日重复"],
      "children": []
    }
  ]
}
```"""
        )

        self.assertEqual(tree["title"], "写一本书")
        node = tree["nodes"][0]
        self.assertEqual(
            node,
            {
                "id": "nodes-0",
                "title": "每天写 500 字",
                "content": "旧版节点说明",
            },
        )
        self.assertEqual(node["title"], "每天写 500 字")

    def test_parse_task_tree_keeps_children_as_structure_only(self) -> None:
        tree = parse_task_tree_text(
            json.dumps(
                {
                    "title": "长期计划",
                    "nodes": [
                        {
                            "id": "parent",
                            "title": "父节点",
                            "content": "父节点正文",
                            "kind": "phase",
                            "children": [
                                {
                                    "id": "child",
                                    "title": "子节点",
                                    "content": "子节点正文",
                                    "status": "done",
                                    "children": [],
                                }
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(
            tree["nodes"],
            [
                {
                    "id": "parent",
                    "title": "父节点",
                    "content": "父节点正文",
                    "children": [
                        {
                            "id": "child",
                            "title": "子节点",
                            "content": "子节点正文",
                        }
                    ],
                }
            ],
        )

    def test_parse_task_tree_requires_title_and_nodes(self) -> None:
        with self.assertRaisesRegex(TaskTreeError, "任务树标题"):
            parse_task_tree_text('{"nodes": []}')
        with self.assertRaisesRegex(TaskTreeError, "nodes 数组"):
            parse_task_tree_text('{"title": "缺少节点"}')

    def test_title_becomes_sanitized_json_filename(self) -> None:
        self.assertEqual(task_tree_filename("  修身炉/长期:计划*  "), "修身炉长期计划.json")
        with self.assertRaisesRegex(TaskTreeError, "标题不能为空"):
            task_tree_filename(" /:* ")

    def test_save_read_and_list_task_tree_uses_title_filename(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            text = json.dumps(
                {
                    "title": "JSON 内标题",
                    "nodes": [
                        {
                            "title": "阶段一",
                            "kind": "phase",
                            "cadence": "phase",
                            "status": "doing",
                            "children": [],
                        }
                    ],
                },
                ensure_ascii=False,
            )

            saved = save_task_tree("用户给的标题", text, config)
            loaded = read_task_tree("用户给的标题", config)
            items = list_task_trees(config)

            self.assertEqual(saved.filename, "用户给的标题.json")
            self.assertEqual(loaded.title, "用户给的标题")
            self.assertEqual(loaded.tree["title"], "JSON 内标题")
            self.assertEqual(loaded.tree["nodes"], [{"id": "nodes-0", "title": "阶段一"}])
            self.assertTrue(saved.path.exists())
            self.assertEqual(items[0].filename, "用户给的标题.json")


def _test_config(root: Path) -> dict[str, Any]:
    paths = {
        "task_tree_dir": str(root / "task_tree"),
    }
    return {
        "paths": paths,
        "safety": {
            "allowed_dirs": list(paths.values()),
            "protected_files": [],
        },
    }


class _temporary_directory:
    def __enter__(self) -> str:
        parent = Path("workspace") / "test_task_tree"
        parent.mkdir(parents=True, exist_ok=True)
        self.path = parent / uuid.uuid4().hex
        self.path.mkdir(parents=True)
        return str(self.path)

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
