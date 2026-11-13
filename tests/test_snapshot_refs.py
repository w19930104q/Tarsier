"""Unit regressions for snapshot-level stable references.

These tests drive the platform-neutral snapshot engine with a *simulated*
accessibility tree (plain :class:`tarsier.core.snapshot.NodeView` objects).
No desktop session, display server or platform accessibility library is
required, so the suite runs anywhere, including CI without a UI.

Coverage:
  * duplicate role/name nodes (the "many 删除 buttons" case)
  * nodes moving within one generation (path relocation)
  * window refresh (new generation -> stale snapshot error)
  * window replacement (new window key -> isolated store)
  * non-ASCII names (CJK, emoji, quotes, backslashes, newlines)
  * pruning semantics, preorder numbering, depth truncation
  * YAML escaping and JSON round-trip
  * malformed / unknown / ambiguous / invisible references
  * legacy exact/regex find API compatibility
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tarsier.core.snapshot import (  # noqa: E402
    ACTIONABLE_ROLES,
    AmbiguousReferenceError,
    ElementUnavailableError,
    NodeView,
    ReferenceMismatchError,
    SnapshotBuilder,
    SnapshotRegistry,
    SnapshotStore,
    StaleSnapshotError,
    UnknownReferenceError,
    escape_yaml_name,
)


def node(role, name, node_id, children=None, visible=True, actionable=False):
    """Builds a simulated accessibility node; ids are simple ints in tests."""
    return NodeView(
        role=role,
        name=name,
        node_id=node_id,
        children=list(children or []),
        control=("control", node_id),
        visible=visible,
        actionable=actionable,
    )


def table_tree():
    """A window with three rows, each exposing an identically named 删除 button."""
    rows = []
    for row_index in range(3):
        label = node("text", f"订单{row_index + 1}", node_id=100 + row_index)
        delete = node("button", "删除", node_id=200 + row_index)
        rows.append(node("group", "", node_id=10 + row_index, children=[label, delete]))
    return node("window", "订单管理", node_id=1, children=rows)


class DuplicateNameTests(unittest.TestCase):
    def setUp(self):
        self.store = SnapshotStore("orders")
        self.tree = table_tree()
        self.snapshot = self.store.create(self.tree)

    def test_every_emitted_node_has_unique_ref(self):
        refs = list(self.snapshot.nodes_by_ref.keys())
        self.assertEqual(len(refs), len(set(refs)))
        # root + 3 groups + 3 labels + 3 delete buttons = 10 emitted nodes
        self.assertEqual(len(refs), 10)
        for ref in refs:
            self.assertRegex(ref, r"^w[0-9a-f]{8}:s0n\d+$")

    def test_duplicate_delete_buttons_are_individually_addressable(self):
        delete_refs = [
            ref
            for ref, n in self.snapshot.nodes_by_ref.items()
            if n.role == "button" and n.name == "删除"
        ]
        self.assertEqual(len(delete_refs), 3)

        # Each ref must resolve to a distinct live control.
        controls = []
        for ref in delete_refs:
            _, _, live = self.store.resolve(ref, live_root=self.tree)
            controls.append(live.control)
        self.assertEqual(len(set(controls)), 3)

    def test_hierarchy_is_preserved_in_yaml(self):
        yaml_text = self.snapshot.to_yaml()
        lines = [line for line in yaml_text.splitlines() if line.strip()]
        # Header plus ten node lines.
        self.assertTrue(lines[0].startswith("# tarsier snapshot:"))
        self.assertEqual(len(lines), 11)
        # Indentation hierarchy: window -> group -> button.
        window_line = next(line for line in lines if "- window" in line)
        group_line = next(line for line in lines if "- group" in line)
        button_line = next(line for line in lines if "- button" in line)
        self.assertLess(len(window_line) - len(window_line.lstrip()),
                        len(group_line) - len(group_line.lstrip()))
        self.assertLess(len(group_line) - len(group_line.lstrip()),
                        len(button_line) - len(button_line.lstrip()))
        # Every node line carries its ref comment.
        self.assertTrue(all("# ref=" in line for line in lines[1:]))

    def test_refs_follow_preorder_traversal(self):
        ordered = list(self.snapshot.nodes_by_ref.values())
        self.assertEqual(ordered[0].role, "window")
        self.assertEqual(ordered[1].role, "group")
        self.assertEqual(ordered[2].role, "text")
        self.assertEqual(ordered[3].role, "button")
        self.assertEqual([n.index for n in ordered], list(range(10)))

    def test_json_carries_refs_and_round_trips(self):
        data = json.loads(self.snapshot.to_json())
        self.assertEqual(data["snapshot_id"], "s0")
        self.assertEqual(data["generation"], 0)
        self.assertEqual(data["role"], "window")
        first_group = data["elements"][0]
        self.assertIn("ref", first_group)
        self.assertEqual(first_group["elements"][1]["name"], "删除")
        self.assertFalse(data["truncated"])
        # Non-ASCII characters are emitted literally, not \u-escaped.
        self.assertIn("删除", self.snapshot.to_json())


class NodeMovementTests(unittest.TestCase):
    def test_node_moving_parent_still_resolves_within_generation(self):
        store = SnapshotStore("orders")
        tree = table_tree()
        snapshot = store.create(tree)
        target_ref = next(
            ref for ref, n in snapshot.nodes_by_ref.items()
            if n.role == "button" and n.name == "删除"
        )
        target_id = snapshot.get(target_ref).node_id

        # Reorder rows: the button physically moves to a different index path.
        root = tree
        root.children = [root.children[2], root.children[0], root.children[1]]
        _, _, live = store.resolve(target_ref, live_root=tree)
        self.assertEqual(live.node_id, target_id)

    def test_role_name_change_is_rejected(self):
        store = SnapshotStore("orders")
        tree = table_tree()
        snapshot = store.create(tree)
        target_ref = next(
            ref for ref, n in snapshot.nodes_by_ref.items()
            if n.role == "button" and n.name == "删除"
        )
        # The live element changed role/name (e.g. became a disabled link).
        tree.children[0].children[1].role = "link"
        tree.children[0].children[1].name = "已删除"
        with self.assertRaises(ReferenceMismatchError):
            store.resolve(target_ref, live_root=tree)

    def test_ambiguous_role_name_fallback_raises(self):
        store = SnapshotStore("orders")
        tree = table_tree()
        snapshot = store.create(tree)
        # Recorded node points at the first 删除 button.
        target_ref = next(
            ref for ref, n in snapshot.nodes_by_ref.items()
            if n.role == "button" and n.name == "删除"
        )
        # Ids are all reissued on refresh and none match anymore, while three
        # buttons still share role/name -> ambiguity must be reported, not guessed.
        def reissue(view):
            view.node_id = ("new", view.node_id)
            for child in view.children:
                reissue(child)
        reissue(tree)
        with self.assertRaises(AmbiguousReferenceError):
            store.resolve(target_ref, live_root=tree)

    def test_disappeared_node_raises_unavailable(self):
        store = SnapshotStore("orders")
        tree = table_tree()
        snapshot = store.create(tree)
        target_ref = next(
            ref for ref, n in snapshot.nodes_by_ref.items()
            if n.role == "button" and n.name == "删除"
        )
        # Remove all buttons and give everything fresh ids.
        def reissue(view):
            view.node_id = ("new", view.node_id)
            for child in view.children:
                reissue(child)
        reissue(tree)
        for row in tree.children:
            row.children = [row.children[0]]
        with self.assertRaises(ElementUnavailableError):
            store.resolve(target_ref, live_root=tree)

    def test_invisible_node_raises_unavailable(self):
        store = SnapshotStore("orders")
        tree = table_tree()
        snapshot = store.create(tree)
        target_ref = next(
            ref for ref, n in snapshot.nodes_by_ref.items()
            if n.role == "button" and n.name == "删除"
        )
        tree.children[0].children[1].visible = False
        with self.assertRaises(ElementUnavailableError):
            store.resolve(target_ref, live_root=tree)


class RefreshAndReplacementTests(unittest.TestCase):
    def test_refresh_makes_old_snapshot_stale(self):
        store = SnapshotStore("orders")
        first = store.create(table_tree())
        old_ref = next(
            ref for ref, n in first.nodes_by_ref.items()
            if n.role == "button" and n.name == "删除"
        )
        # Window refresh: same window, fresh tree, new generation.
        store.create(table_tree())
        self.assertEqual(store.generation, 2)
        with self.assertRaises(StaleSnapshotError) as ctx:
            store.resolve(old_ref, live_root=table_tree())
        self.assertIn("stale", str(ctx.exception).lower())

    def test_unknown_generation_is_distinct_from_stale(self):
        store = SnapshotStore("orders", history_size=1)
        store.create(table_tree())
        store.create(table_tree())  # evicts generation 0
        own_token = list(store._history.values())[0].window_key
        from tarsier.core.snapshot import make_ref
        stale_ref = make_ref(own_token, 0, 3)
        future_ref = make_ref(own_token, 99, 3)
        with self.assertRaises(StaleSnapshotError):
            store.resolve(stale_ref, live_root=table_tree())
        with self.assertRaises(UnknownReferenceError):
            store.resolve(future_ref, live_root=table_tree())

    def test_malformed_refs_are_rejected(self):
        store = SnapshotStore("orders")
        store.create(table_tree())
        for bad in ("", None, "n7", "s0", "s0n", "button:删除",
                    "wxyz:s0n1", "w12345678:s0", "w12345678:n1"):
            with self.assertRaises(UnknownReferenceError):
                store.resolve(bad, live_root=table_tree())

    def test_window_replacement_uses_isolated_store(self):
        registry = SnapshotRegistry()
        old_store = registry.store("win:hwnd:111")
        old_snapshot = old_store.create(table_tree())
        old_ref = next(iter(old_snapshot.nodes_by_ref.keys()))

        # New native window (different hwnd) -> independent generation counter.
        new_store = registry.store("win:hwnd:222")
        self.assertIsNot(new_store, old_store)
        new_snapshot = new_store.create(table_tree())
        self.assertEqual(new_snapshot.generation, 0)
        # A reference minted by the replaced window can never be replayed here:
        # the new window's store rejects foreign refs explicitly.
        with self.assertRaises(UnknownReferenceError):
            new_store.resolve(old_ref, live_root=table_tree())
        self.assertEqual(
            set(registry.keys()), {"win:hwnd:111", "win:hwnd:222"}
        )
        registry.reset("win:hwnd:111")
        self.assertFalse(registry.has("win:hwnd:111"))


class PruningOrderAndTruncationTests(unittest.TestCase):
    def test_pruning_keeps_meaningful_subtree_and_drops_decorative(self):
        decorative = node("pane", "", node_id=99)  # no actionable role, no name
        kept_button = node("button", "确定", node_id=42)
        container = node("pane", "", node_id=5, children=[decorative, kept_button])
        root = node("window", "w", node_id=1, children=[container])

        snapshot = SnapshotBuilder(max_depth=15).build(root, generation=0)
        emitted_ids = {n.node_id for n in snapshot.nodes_by_ref.values()}
        self.assertIn(1, emitted_ids)       # root always kept
        self.assertIn(5, emitted_ids)       # ancestor of a kept node
        self.assertIn(42, emitted_ids)      # actionable
        self.assertNotIn(99, emitted_ids)   # decorative leaf pruned

    def test_text_nodes_with_names_are_kept(self):
        text = node("text", "提示信息", node_id=7)
        root = node("window", "w", node_id=1, children=[text])
        snapshot = SnapshotBuilder().build(root, generation=0)
        self.assertIn(7, {n.node_id for n in snapshot.nodes_by_ref.values()})

    def test_truncation_marks_nodes_past_max_depth(self):
        deep = node("button", "深层按钮", node_id=99)
        for depth in range(6):
            deep = node("group", f"层级{depth}", node_id=50 + depth, children=[deep])
        root = node("window", "w", node_id=1, children=[deep])

        snapshot = SnapshotBuilder(max_depth=3).build(root, generation=0)
        self.assertTrue(snapshot.truncated)
        truncated_nodes = [n for n in snapshot.nodes_by_ref.values() if n.truncated]
        self.assertTrue(truncated_nodes)
        yaml_text = snapshot.to_yaml()
        self.assertIn("[truncated]", yaml_text)
        self.assertIn("re-snapshot", yaml_text)
        # The deep button itself must not have been emitted.
        self.assertFalse(
            any(n.node_id == 99 for n in snapshot.nodes_by_ref.values())
        )

    def test_actionable_flag_is_recorded(self):
        edit = node("edit", "", node_id=3)
        plain = node("text", "只读文本", node_id=4)
        root = node("window", "w", node_id=1, children=[edit, plain])
        snapshot = SnapshotBuilder().build(root, generation=0)
        flags = {n.node_id: n.actionable for n in snapshot.nodes_by_ref.values()}
        self.assertTrue(flags[3])
        self.assertFalse(flags[4])

    def test_snapshot_header_contains_generation_and_window(self):
        snapshot = SnapshotBuilder().build(
            table_tree(), generation=2, window_key="win:hwnd:42"
        )
        header = snapshot.to_yaml().splitlines()[0]
        self.assertIn("generation=2", header)
        self.assertIn("id=s2", header)
        self.assertIn("win:hwnd:42", header)
        # Refs inside generation 2 must use the s2 prefix with the window token.
        from tarsier.core.snapshot import window_token
        expected_prefix = f"w{window_token('win:hwnd:42')}:s2n"
        self.assertTrue(all(n.ref.startswith(expected_prefix) for n in
                            snapshot.nodes_by_ref.values()))


class EscapingAndNonAsciiTests(unittest.TestCase):
    def test_yaml_escaping_is_reversible_ordering(self):
        # Backslash must be escaped before the quote, otherwise unescaping breaks.
        self.assertEqual(escape_yaml_name('a\\b'), 'a\\\\b')
        self.assertEqual(escape_yaml_name('say "hi"'), 'say \\"hi\\"')
        self.assertEqual(escape_yaml_name("line1\nline2\t!"), "line1\\nline2\\t!")
        self.assertEqual(escape_yaml_name("回车\r"), "回车\\r")

    def test_special_characters_render_safely_in_yaml(self):
        tricky = node(
            "button",
            '路径 C:\\新文件夹 与 "引号" 和\n换行',
            node_id=8,
        )
        root = node("window", "窗口", node_id=1, children=[tricky])
        yaml_text = SnapshotBuilder().build(root, generation=0).to_yaml()
        # A literal backslash is doubled and embedded quotes are backslash-escaped.
        self.assertIn("C:\\\\新文件夹", yaml_text)
        self.assertIn('\\"引号\\"', yaml_text)
        self.assertIn("\\n", yaml_text)
        # CJK stays literal.
        self.assertIn("新文件夹", yaml_text)

    def test_non_ascii_names_json_and_yaml(self):
        for name in ["删除", "こんにちは", "안녕하세요", "🎉 提交", "Ωμέγα"]:
            root = node("window", name, node_id=1,
                        children=[node("button", name, node_id=2)])
            snapshot = SnapshotBuilder().build(root, generation=0)
            self.assertIn(name, snapshot.to_yaml())
            data = json.loads(snapshot.to_json())
            self.assertEqual(data["name"], name)
            self.assertEqual(data["elements"][0]["name"], name)

    def test_resolve_with_non_ascii_duplicate_names(self):
        store = SnapshotStore("cjk")
        rows = [
            node("group", "", node_id=10 + i, children=[
                node("text", f"项目{i}", node_id=30 + i),
                node("button", "删除", node_id=50 + i),
            ])
            for i in range(2)
        ]
        tree = node("window", "管理后台", node_id=1, children=rows)
        snapshot = store.create(tree)
        buttons = [n for n in snapshot.nodes_by_ref.values()
                   if n.role == "button" and n.name == "删除"]
        self.assertEqual(len(buttons), 2)
        _, _, live = store.resolve(buttons[1].ref, live_root=tree)
        self.assertEqual(live.node_id, 51)


class LegacyApiCompatibilityTests(unittest.TestCase):
    def test_find_semantics_are_handled_unchanged_outside_store(self):
        # The snapshot engine never replaces find(); refs are additive.
        # Simulate the legacy "first match wins" behaviour explicitly to lock
        # in that refs are what disambiguate, not changed search semantics.
        tree = table_tree()
        all_buttons = [v for v in _walk(tree)
                       if v.role == "button" and v.name == "删除"]
        self.assertEqual(len(all_buttons), 3)
        first = all_buttons[0]
        # A plain role/name lookup (as find() implements) would land here;
        # snapshot refs instead address each button distinctly.
        store = SnapshotStore("orders")
        snapshot = store.create(tree)
        distinct = {
            store.resolve(n.ref, live_root=tree)[2].node_id
            for n in snapshot.nodes_by_ref.values()
            if n.role == "button" and n.name == "删除"
        }
        self.assertEqual(distinct, {b.node_id for b in all_buttons})
        self.assertIn(first.node_id, distinct)

    def test_yaml_line_shape_remains_parseable_by_simple_scanner(self):
        snapshot = SnapshotBuilder().build(table_tree(), generation=0)
        yaml_text = snapshot.to_yaml()
        node_lines = [line for line in yaml_text.splitlines()
                      if re.match(r"^\s*-\s+\w+", line)]
        # Every legacy-style node line still starts with "- role".
        self.assertEqual(len(node_lines), 10)
        # Names still appear quoted right after the role.
        quoted = [line for line in node_lines if '"' in line]
        self.assertTrue(any("删除" in line for line in quoted))

    def test_actionable_roles_match_historical_meaningful_set(self):
        self.assertEqual(
            ACTIONABLE_ROLES,
            frozenset(["button", "textbox", "checkbox", "radio", "combobox",
                       "link", "menuitem", "window", "document", "edit"]),
        )


def _walk(root):
    yield root
    for child in root.children:
        yield from _walk(child)


if __name__ == "__main__":
    unittest.main(verbosity=2)
