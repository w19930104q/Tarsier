"""Integration-style regressions using a simulated UIElement backend.

The real platform backends (Windows/macOS/Linux) build :class:`NodeView`
trees from native accessibility controls.  Here we install a *fake* backend
that records clicks/types/reads and serves NodeView trees directly, which
lets us exercise the exact UIElement facade flow (snapshot -> ref -> action,
generation checks, window replacement) without any desktop session.

A separate AST test guards the MCP server surface because the ``mcp`` package
is not installed in every environment.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tarsier.core.elements import UIElement, UIElementBackend  # noqa: E402
from tarsier.core.snapshot import (  # noqa: E402
    NodeView,
    StaleSnapshotError,
    UnknownReferenceError,
    get_registry,
    reset_registry,
)


class FakeControl:
    """Stand-in for a native accessibility control (uia/atomacos/pyatspi)."""

    def __init__(self, role, name, control_id, visible=True):
        self.role = role
        self.name = name
        self.control_id = control_id
        self.visible = visible
        self.value = ""
        self.clicked = 0
        self.typed = []

    def __repr__(self):
        return f"FakeControl({self.role!r}, {self.name!r}, id={self.control_id})"


def make_tree(spec):
    """Builds (control_tree, view_tree) from a nested dict spec."""
    control = FakeControl(spec["role"], spec["name"], spec["id"],
                          visible=spec.get("visible", True))
    view = NodeView(role=spec["role"], name=spec["name"], node_id=spec["id"],
                    control=control, visible=spec.get("visible", True))
    for child_spec in spec.get("children", []):
        child_control, child_view = make_tree(child_spec)
        control.children = getattr(control, "children", []) + [child_control]
        view.children.append(child_view)
    return control, view


class FakeBackend(UIElementBackend):
    """Mimics the ref flow implemented by the three real platform backends."""

    def __init__(self, root_control, root_view, window_key, highlight_actions=False):
        self._control = root_control
        self._root_view = root_view
        self._window_key = window_key
        self.highlight_actions = highlight_actions

    def _build_view(self, control=None):
        return self._root_view if control is None else _view_for(self._root_view, control)

    def _get_content_root(self, control):
        return self._control

    def _window_key(self):
        return self._window_key

    def to_snapshot(self, max_depth=15):
        from tarsier.core.snapshot import SnapshotBuilder
        store = get_registry().store(self._window_key)
        return store.create(self._build_view(), max_depth=max_depth)

    def to_yaml_snapshot(self, max_depth=15):
        return self.to_snapshot(max_depth).to_yaml()

    def resolve_ref(self, ref):
        from tarsier.core.snapshot import SnapshotError  # noqa: F401
        store = get_registry().store(self._window_key)
        _, _, live_view = store.resolve(ref, live_root=self._build_view())
        return _FakeUIElement(live_view.control, self._root_view, self._window_key)

    def click(self):
        self._control.clicked += 1
        return self

    def type(self, text, waitTime=0.05):
        self._control.typed.append(text)
        self._control.value = text
        return self

    def read(self):
        return self._control.value or self._control.name

    def click_ref(self, ref):
        return self.resolve_ref(ref)._backend.click()

    def type_ref(self, ref, text, waitTime=0.05):
        self.resolve_ref(ref)._backend.type(text, waitTime)
        return self

    def read_ref(self, ref):
        return self.resolve_ref(ref).read()


def _view_for(root_view, control):
    if root_view.control is control:
        return root_view
    for child in root_view.children:
        found = _view_for(child, control)
        if found is not None:
            return found
    return None


class _FakeUIElement:
    """UIElement-style facade that never instantiates a platform backend."""

    def __init__(self, control, root_view, window_key):
        self._backend = FakeBackend(control, root_view, window_key)

    def click(self):
        self._backend.click()
        return self

    def type(self, text, waitTime=0.05):
        self._backend.type(text, waitTime)
        return self

    def read(self):
        return self._backend.read()


ORDER_APP_SPEC = {
    "role": "window", "name": "订单后台", "id": 1,
    "children": [
        {"role": "group", "name": "", "id": 10, "children": [
            {"role": "text", "name": "订单A", "id": 100},
            {"role": "button", "name": "删除", "id": 200},
        ]},
        {"role": "group", "name": "", "id": 11, "children": [
            {"role": "text", "name": "订单B", "id": 101},
            {"role": "button", "name": "删除", "id": 201},
        ]},
        {"role": "group", "name": "", "id": 12, "children": [
            {"role": "text", "name": "订单C", "id": 102},
            {"role": "button", "name": "删除", "id": 202},
            {"role": "textbox", "name": "备注", "id": 300},
        ]},
    ],
}


class RefActionFlowTests(unittest.TestCase):
    def setUp(self):
        reset_registry()
        self.control, self.view = make_tree(ORDER_APP_SPEC)
        self.backend = FakeBackend(self.control, self.view, "win:hwnd:777")
        self.window = UIElement.__new__(UIElement)
        self.window.highlight_actions = False
        self.window._backend = self.backend

    def test_duplicate_buttons_click_distinct_rows(self):
        snapshot = self.window.to_snapshot()
        buttons = [n for n in snapshot.nodes_by_ref.values()
                   if n.role == "button" and n.name == "删除"]
        self.assertEqual(len(buttons), 3)

        # Clicking by ref lands on the exact row; a role/name find alone could
        # only ever return the first match.
        self.window.click_ref(buttons[2].ref)
        clicked_control = self.window.resolve_ref(buttons[2].ref)._backend._control
        self.assertEqual(clicked_control.control_id, 202)
        self.assertEqual(clicked_control.clicked, 1)
        self.assertEqual(
            self.window.resolve_ref(buttons[0].ref)._backend._control.control_id, 200
        )

    def test_type_and_read_through_ref(self):
        snapshot = self.window.to_snapshot()
        textbox = next(n for n in snapshot.nodes_by_ref.values()
                       if n.role == "textbox" and n.name == "备注")
        self.window.type_ref(textbox.ref, "加急处理")
        self.assertEqual(self.window.read_ref(textbox.ref), "加急处理")

    def test_yaml_snapshot_contains_refs_and_hierarchy(self):
        yaml_text = self.window.to_yaml_snapshot()
        self.assertIn("# tarsier snapshot:", yaml_text)
        self.assertEqual(yaml_text.count("# ref=w"), 11)  # 11 emitted nodes
        self.assertIn('button "删除"', yaml_text)
        self.assertIn("订单后台", yaml_text)  # non-ASCII preserved

    def test_stale_ref_after_refresh_is_rejected_before_any_action(self):
        first = self.window.to_snapshot()
        old_ref = next(n.ref for n in first.nodes_by_ref.values()
                       if n.role == "button" and n.name == "删除")
        # Simulated refresh: rebuild tree (fresh ids), re-snapshot.
        new_control, new_view = make_tree(ORDER_APP_SPEC)
        self.backend._control = new_control
        self.backend._root_view = new_view
        self.window.to_snapshot()

        for action in (
            lambda: self.window.click_ref(old_ref),
            lambda: self.window.type_ref(old_ref, "x"),
            lambda: self.window.read_ref(old_ref),
        ):
            with self.assertRaises(StaleSnapshotError):
                action()
        # No click must have happened on the new tree.
        self.assertFalse(
            any(getattr(c, "clicked", 0) for c in _all_controls(new_control))
        )

    def test_window_replacement_isolates_references(self):
        first = self.window.to_snapshot()
        old_ref = next(iter(first.nodes_by_ref.keys()))

        replacement_control, replacement_view = make_tree(ORDER_APP_SPEC)
        replacement_backend = FakeBackend(
            replacement_control, replacement_view, "win:hwnd:999"
        )
        replacement = UIElement.__new__(UIElement)
        replacement.highlight_actions = False
        replacement._backend = replacement_backend

        replacement.to_snapshot()
        with self.assertRaises(UnknownReferenceError):
            replacement.resolve_ref(old_ref)

    def test_role_name_mismatch_blocks_action(self):
        snapshot = self.window.to_snapshot()
        target = next(n for n in snapshot.nodes_by_ref.values()
                      if n.role == "button" and n.name == "删除" and n.node_id == 200)
        # Live tree mutates: the row's button became a link with another name.
        live = _view_for(self.view, _find_control(self.control, 200))
        live.role = "link"
        live.name = "已撤销"
        from tarsier.core.snapshot import ReferenceMismatchError
        with self.assertRaises(ReferenceMismatchError):
            self.window.resolve_ref(target.ref)

    def test_snapshot_json_is_parseable_and_ref_addressable(self):
        import json
        payload = json.loads(self.window.to_snapshot().to_json())
        self.assertEqual(payload["window_key"], "win:hwnd:777")
        refs = []
        stack = [payload]
        while stack:
            current = stack.pop()
            if "ref" in current:
                refs.append(current["ref"])
            stack.extend(current.get("elements", []))
        self.assertEqual(len(refs), len(set(refs)))
        # Every ref in the JSON must resolve on the live tree.
        for ref in refs:
            self.window.resolve_ref(ref)


def _all_controls(control):
    yield control
    for child in getattr(control, "children", []):
        yield from _all_controls(child)


def _find_control(control, control_id):
    if control.control_id == control_id:
        return control
    for child in getattr(control, "children", []):
        found = _find_control(child, control_id)
        if found is not None:
            return found
    return None


class McpServerSurfaceTests(unittest.TestCase):
    """Guards MCP tool signatures without importing the unavailable mcp package."""

    SERVER_PATH = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "tarsier", "mcp", "server.py")
    )

    def setUp(self):
        with open(self.SERVER_PATH, encoding="utf-8") as handle:
            self.tree = ast.parse(handle.read())
        self.tools = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef):
                is_tool = any(
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "mcp"
                    and decorator.func.attr == "tool"
                    for decorator in node.decorator_list
                )
                if is_tool:
                    self.tools[node.name] = node

    def _args(self, name):
        return [arg.arg for arg in self.tools[name].args.args]

    def _defaults(self, name):
        defaults = list(self.tools[name].args.defaults)
        args = self._args(name)
        # defaults align to the tail of the positional args.
        paired = {}
        for arg, default in zip(args[-len(defaults):], defaults):
            paired[arg] = default
        return paired

    def test_action_tools_accept_optional_ref(self):
        for tool in ("desktop_click", "desktop_right_click",
                     "desktop_hover", "desktop_type", "desktop_read_text"):
            self.assertIn(tool, self.tools)
            self.assertIn("ref", self._args(tool))
            self.assertIsInstance(self._defaults(tool)["ref"], ast.Constant)
            self.assertIsNone(self._defaults(tool)["ref"].value)

    def test_legacy_positional_signatures_remain_supported(self):
        # Old callers pass (window_name, role, name) / (..., text) positionally.
        self.assertEqual(
            self._args("desktop_click")[:3], ["window_name", "role", "name"]
        )
        type_args = self._args("desktop_type")
        self.assertEqual(
            type_args[:4], ["window_name", "role", "name", "text"]
        )
        # ref is appended last, so old positional call sites stay valid.
        self.assertEqual(type_args[-1], "ref")

    def test_get_ui_exposes_format_and_depth(self):
        args = self._args("desktop_get_ui")
        self.assertEqual(
            args, ["window_name", "format", "max_depth"]
        )
        defaults = self._defaults("desktop_get_ui")
        self.assertEqual(defaults["format"].value, "yaml")

    def test_snapshot_errors_are_handled_in_tools(self):
        with open(self.SERVER_PATH, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("SnapshotError", text)
        # Every ref-capable tool must translate SnapshotError into a message.
        for tool in ("desktop_click", "desktop_type", "desktop_read_text",
                     "desktop_hover", "desktop_right_click"):
            func = next(n for n in ast.walk(self.tree)
                        if isinstance(n, ast.FunctionDef) and n.name == tool)
            handlers = [
                n for n in ast.walk(func)
                if isinstance(n, ast.ExceptHandler) and n.type is not None
            ]
            self.assertTrue(
                any(isinstance(h.type, ast.Name) and h.type.id == "SnapshotError"
                    for h in handlers),
                f"{tool} must handle SnapshotError",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
