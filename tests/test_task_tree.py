from __future__ import annotations

import json
import shutil
import unittest
import uuid
from pathlib import Path
from typing import Any

from app.task_tree import (
    TaskTreeError,
    delete_task_tree_file,
    list_task_trees,
    parse_task_tree_text,
    read_task_tree,
    read_task_tree_file,
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

    def test_parse_task_tree_strips_mind_map_paragraph_html_from_titles(self) -> None:
        tree = parse_task_tree_text(
            json.dumps(
                {
                    "title": "<p>&lt;p&gt;深层需求&lt;/p&gt;</p>",
                    "nodes": [
                        {
                            "title": "<p>&lt;p&gt;第一层：资料进入&lt;/p&gt;</p>",
                            "children": [],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(tree["title"], "深层需求")
        self.assertEqual(tree["nodes"][0]["title"], "第一层：资料进入")

    def test_parse_task_tree_requires_title_and_nodes(self) -> None:
        with self.assertRaisesRegex(TaskTreeError, "工作树标题"):
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
            loaded = read_task_tree_file("用户给的标题.json", config)
            loaded_by_title = read_task_tree("用户给的标题", config)
            items = list_task_trees(config)

            self.assertEqual(saved.filename, "用户给的标题.json")
            self.assertEqual(loaded.title, "用户给的标题")
            self.assertEqual(loaded_by_title.filename, "用户给的标题.json")
            self.assertEqual(loaded.tree["title"], "JSON 内标题")
            self.assertEqual(loaded.text, saved.path.read_text(encoding="utf-8"))
            self.assertEqual(loaded.tree["nodes"], [{"id": "nodes-0", "title": "阶段一"}])
            self.assertTrue(saved.path.exists())
            self.assertEqual(items[0].filename, "用户给的标题.json")

    def test_list_task_trees_only_uses_root_json_files(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            tree_dir = Path(config["paths"]["task_tree_dir"])
            tree_dir.mkdir(parents=True)
            (tree_dir / "根任务A.json").write_text('{"title":"A","nodes":[]}\n', encoding="utf-8")
            (tree_dir / "根任务B.json").write_text('{"title":"B","nodes":[]}\n', encoding="utf-8")
            (tree_dir / "忽略.txt").write_text("not json\n", encoding="utf-8")
            child_dir = tree_dir / "子目录"
            child_dir.mkdir()
            (child_dir / "子任务.json").write_text('{"title":"子","nodes":[]}\n', encoding="utf-8")

            items = list_task_trees(config)

            self.assertEqual({item.filename for item in items}, {"根任务A.json", "根任务B.json"})

    def test_read_task_tree_file_rejects_subdirectory_filename(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))

            with self.assertRaisesRegex(TaskTreeError, "根目录"):
                read_task_tree_file("子目录/子任务.json", config)

    def test_delete_task_tree_file_removes_root_json_file(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            tree_dir = Path(config["paths"]["task_tree_dir"])
            tree_dir.mkdir(parents=True)
            target = tree_dir / "待删除.json"
            target.write_text('{"title":"待删除","nodes":[]}\n', encoding="utf-8")

            deleted = delete_task_tree_file("待删除.json", config)

            self.assertEqual(deleted.filename, "待删除.json")
            self.assertFalse(target.exists())

    def test_delete_task_tree_file_rejects_missing_or_nested_file(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))

            with self.assertRaisesRegex(TaskTreeError, "工作树不存在"):
                delete_task_tree_file("不存在.json", config)
            with self.assertRaisesRegex(TaskTreeError, "根目录"):
                delete_task_tree_file("子目录/子任务.json", config)


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
