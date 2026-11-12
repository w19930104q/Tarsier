"""
Fake accessibility tree for deterministic, platform-independent testing.

The :class:`FakeNode` tree mimics the small subset of a native accessibility
tree (UIA / AT-SPI / AXAPI) that Tarsier's snapshot and ref machinery relies
on: roles, names, stable runtime ids, parent/child navigation, mutable value,
click recording and the ability to *replace* or *relocate* nodes to simulate
page redraws and window switches.

A :class:`FakeDesktop` holds several named :class:`FakeWindow` roots, which
stand in for OS-level windows.  Swapping the active window or mutating a
tree in place reproduces every failure mode the stable-ref design targets,
without touching a real desktop session.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from tarsier.core.ref import TreeAdapter


@dataclass
class FakeNode:
    role: str
    name: str = ""
    node_id: Optional[str] = None
    value: str = ""
    enabled: bool = True
    children: List["FakeNode"] = field(default_factory=list)
    parent: Optional["FakeNode"] = None
    click_count: int = 0
    type_log: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.node_id is None:
            self.node_id = f"n-{id(self):x}"
        for child in self.children:
            child.parent = self

    # -- tree mutation helpers --------------------------------------------- #
    def add(self, child: "FakeNode") -> "FakeNode":
        child.parent = self
        self.children.append(child)
        return child

    def remove(self, child: "FakeNode") -> None:
        self.children.remove(child)
        child.parent = None

    def replace(self, old: "FakeNode", new: "FakeNode") -> None:
        """Simulate a redraw: same structural position, *new* runtime id."""
        idx = self.children.index(old)
        old.parent = None
        new.parent = self
        self.children[idx] = new

    def find_id(self, node_id: str) -> Optional["FakeNode"]:
        if self.node_id == node_id:
            return self
        for child in self.children:
            found = child.find_id(node_id)
            if found is not None:
                return found
        return None

    def contains(self, node: "FakeNode") -> bool:
        if node is self:
            return True
        return any(c.contains(node) for c in self.children)

    # -- fake interaction surface ------------------------------------------ #
    def click(self) -> "FakeNode":
        if not self.enabled:
            raise RuntimeError(f"node {self.node_id!r} is disabled")
        self.click_count += 1
        return self

    def type(self, text: str) -> "FakeNode":
        self.type_log.append(text)
        self.value = text
        return self

    def read(self) -> str:
        return self.value or self.name


class FakeWindow:
    """A scope root: a named window backed by one FakeNode tree."""

    def __init__(self, title: str, root: FakeNode):
        self.title = title
        self.root = root
        self.closed = False

    def redraw(self, new_root: FakeNode) -> None:
        """Replace the whole tree (e.g. page refresh / full re-render)."""
        self.root = new_root


class FakeDesktop:
    """Simulates an OS desktop with multiple windows."""

    def __init__(self):
        self._windows: Dict[str, FakeWindow] = {}
        self.active_window: Optional[str] = None
        self._lock = threading.RLock()

    def add_window(self, window: FakeWindow, *, activate: bool = True) -> FakeWindow:
        with self._lock:
            self._windows[window.title] = window
            if activate or self.active_window is None:
                self.active_window = window.title
            return window

    def get_window(self, title: str) -> FakeWindow:
        with self._lock:
            window = self._windows.get(title)
            if window is None or window.closed:
                raise KeyError(f"no window named {title!r}")
            return window

    def switch_to(self, title: str) -> FakeWindow:
        window = self.get_window(title)
        self.active_window = title
        return window

    def close_window(self, title: str) -> None:
        window = self.get_window(title)
        window.closed = True
        if self.active_window == title:
            remaining = [t for t, w in self._windows.items() if not w.closed]
            self.active_window = remaining[0] if remaining else None

    def window_titles(self) -> List[str]:
        return [t for t, w in self._windows.items() if not w.closed]


class FakeTreeAdapter(TreeAdapter):
    """:class:`TreeAdapter` implementation over :class:`FakeNode` trees."""

    def root_children(self, root: FakeNode) -> List[FakeNode]:
        return list(root.children)

    def node_role(self, node: FakeNode) -> str:
        return node.role

    def node_name(self, node: FakeNode) -> str:
        return node.name

    def node_id(self, node: FakeNode) -> str:
        return node.node_id  # type: ignore[return-value]

    def node_value(self, node: FakeNode) -> Optional[str]:
        return node.value or None

    def find_by_id(self, root: FakeNode, node_id: str):
        node = root.find_id(node_id)
        if node is not None:
            return node, True
        return None, False


def build_form_tree(*, prefix: str = "form") -> FakeNode:
    """A batch-form style tree with DUPLICATE 'Save' buttons and nested
    confirmation dialogs – the exact shape called out in the bug report.
    """
    root = FakeNode("window", "Batch Form", node_id=f"{prefix}/window")
    form = root.add(FakeNode("group", "Entry Form", node_id=f"{prefix}/form"))
    form.add(FakeNode("textbox", "Username", node_id=f"{prefix}/username"))
    form.add(FakeNode("textbox", "Email", node_id=f"{prefix}/email"))

    toolbar = root.add(FakeNode("group", "Toolbar", node_id=f"{prefix}/toolbar"))
    # Two identically-named Save buttons in different containers.
    toolbar.add(FakeNode("button", "Save", node_id=f"{prefix}/save-toolbar"))
    form.add(FakeNode("button", "Save", node_id=f"{prefix}/save-inline"))

    # Multi-layer confirmation dialog with yet another Save button.
    confirm = root.add(FakeNode("window", "Confirm Save", node_id=f"{prefix}/confirm"))
    confirm.add(FakeNode("text", "Save changes?", node_id=f"{prefix}/confirm-text"))
    confirm.add(FakeNode("button", "Cancel", node_id=f"{prefix}/confirm-cancel"))
    confirm.add(FakeNode("button", "Save", node_id=f"{prefix}/confirm-save"))
    second = confirm.add(FakeNode("window", "Final Confirmation", node_id=f"{prefix}/confirm2"))
    second.add(FakeNode("button", "Save", node_id=f"{prefix}/confirm2-save"))
    return root
