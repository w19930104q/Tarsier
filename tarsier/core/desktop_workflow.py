"""
Native accessibility-tree adapter for desktop workflows.

Wraps the platform-specific control objects (Windows UIA, macOS AXAPI,
Linux AT-SPI) behind the :class:`TreeAdapter` protocol used by the ref
machinery, and provides :class:`DesktopWorkflow` – a window-scoped workflow
that hands out stable refs and re-validates them against the *live* tree
before every click / type / read.
"""

from __future__ import annotations

import sys
from typing import Any, List, Optional, Tuple

from tarsier.core.elements import ROLE_MAPPING, UIElement
from tarsier.core.ref import TreeAdapter
from tarsier.core.workflow import Workflow


def _native_role(control: Any) -> str:
    """Best-effort normalized role for a raw platform control."""
    try:
        if sys.platform == "win32":
            raw = control.ControlTypeName.replace("Control", "").lower()
        elif sys.platform == "darwin":
            ax_role = getattr(control, "AXRole", "unknown")
            return ROLE_MAPPING.get(ax_role.lower(), ax_role.replace("AX", "").lower())
        else:
            role_name = control.getRoleName() or "unknown"
            return ROLE_MAPPING.get(role_name.lower(), role_name.replace(" ", "_").lower())
        return ROLE_MAPPING.get(raw, raw)
    except Exception:
        return "unknown"


def _native_name(control: Any) -> str:
    try:
        if sys.platform == "win32":
            return control.Name or ""
        if sys.platform == "darwin":
            return (
                getattr(control, "AXTitle", "")
                or getattr(control, "AXDescription", "")
                or ""
            )
        return control.name or ""
    except Exception:
        return ""


def _native_children(control: Any) -> List[Any]:
    try:
        if sys.platform == "win32":
            return control.GetChildren() or []
        if sys.platform == "darwin":
            return getattr(control, "AXChildren", None) or []
        count = control.childCount
        return [c for c in (control.getChildAtIndex(i) for i in range(count)) if c]
    except Exception:
        return []


def _native_runtime_id(control: Any) -> str:
    """Stable per-tree node id for a raw platform control."""
    try:
        if sys.platform == "win32":
            return "uia:" + "/".join(str(p) for p in control.GetRuntimeId())
        if sys.platform == "darwin":
            # atomacos objects are python wrappers; identity is the best
            # stable handle available between two in-process snapshots.
            return f"ax:{id(control)}"
        return f"atspi:{id(control)}"
    except Exception:
        return f"obj:{id(control)}"


class NativeTreeAdapter(TreeAdapter):
    """Adapter over raw desktop controls (Windows/Mac/Linux)."""

    def root_children(self, root: Any) -> List[Any]:
        return _native_children(root)

    def node_role(self, node: Any) -> str:
        return _native_role(node)

    def node_name(self, node: Any) -> str:
        return _native_name(node)

    def node_id(self, node: Any) -> str:
        return _native_runtime_id(node)

    def find_by_id(self, root: Any, node_id: str) -> Tuple[Any, bool]:
        from collections import deque

        queue = deque([root])
        while queue:
            current = queue.popleft()
            try:
                if _native_runtime_id(current) == node_id:
                    return current, True
            except Exception:
                pass
            try:
                queue.extend(_native_children(current))
            except Exception:
                pass
        return None, False


class DesktopWorkflow:
    """Window-scoped workflow exposing snapshot + stable-ref actions.

    The window is re-fetched through the desktop backend on every snapshot
    and every action, so closing/reopening or switching windows is observed
    rather than cached.  Actions return :class:`UIElement` wrappers to stay
    compatible with the existing public surface.
    """

    def __init__(self, desktop: Any, window_name: str, scope: Optional[str] = None):
        self._desktop = desktop
        self._window_name = window_name
        self._highlight = getattr(desktop, "highlight_actions", False)
        self.adapter = NativeTreeAdapter()
        self._workflow = Workflow(
            self.adapter,
            root_provider=self._live_window_control,
            scope=scope,
            action_dispatcher=self._dispatch,
        )

    # -- lifecycle ---------------------------------------------------------- #
    @property
    def scope(self) -> str:
        return self._workflow.scope

    @property
    def closed(self) -> bool:
        return self._workflow.closed

    def close(self) -> None:
        self._workflow.close()

    def __enter__(self) -> "DesktopWorkflow":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _live_window_control(self) -> Any:
        window = self._desktop.get_window(self._window_name)
        return window._backend._control

    # -- snapshots ---------------------------------------------------------- #
    def snapshot(self, *, max_depth: int = 15):
        return self._workflow.snapshot(max_depth=max_depth)

    def get_ui(self, *, include_refs: bool = True, max_depth: int = 15) -> str:
        return self._workflow.get_ui(include_refs=include_refs, max_depth=max_depth)

    # -- actions ------------------------------------------------------------ #
    def _wrap(self, control: Any) -> UIElement:
        return UIElement(control, highlight_actions=self._highlight)

    def _dispatch(self, action: str, live_control: Any, text: Optional[str]) -> Any:
        element = self._wrap(live_control)
        if action == "click":
            element.click()
        elif action == "type":
            element.type(text or "")
        elif action == "read":
            return element.read()
        else:
            raise ValueError(f"unknown action {action!r}")
        return element

    def click(self, ref: str) -> UIElement:
        return self._workflow.click(ref)

    def type(self, ref: str, text: str) -> UIElement:
        return self._workflow.type(ref, text)

    def read(self, ref: str) -> str:
        return self._workflow.read(ref)

    def resolve(self, ref: str) -> UIElement:
        return self._wrap(self._workflow.resolve(ref))

    # -- legacy role/name path (unchanged first-match semantics) ------------ #
    def find(self, role: Optional[str] = None, name: Optional[str] = None) -> UIElement:
        control = self._workflow.find(role=role, name=name)
        return self._wrap(control)
