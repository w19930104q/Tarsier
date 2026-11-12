"""
Workflow: a scoped snapshot session with stable-ref actions.

A workflow is the unit of agent work.  Every snapshot taken inside a
workflow belongs to its isolated *scope* and receives a monotonically
increasing *generation*.  Refs from one workflow can never be resolved
inside another (concurrent workflows are fully isolated), and refs from an
older generation are rejected as soon as a newer snapshot exists.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from tarsier.core.ref import (
    SCOPE_MANAGER,
    RefRegistry,
    Snapshot,
    TreeAdapter,
    build_snapshot,
    resolve_live,
)


class Workflow:
    """Binds a tree adapter + live-root provider to one ref registry/scope.

    Parameters
    ----------
    adapter:
        Platform adapter that walks and re-resolves the live tree.
    root_provider:
        Zero-argument callable returning the current scope root handle
        (a window control, a fake tree root, a Playwright page wrapper).
        Called on every snapshot and every ref resolution, so window/tab
        switches and redraws are always observed.
    scope:
        Explicit scope id (mainly for tests); otherwise one is generated.
    action_dispatcher:
        Optional hook ``(action, live_node, text) -> result`` letting a
        platform customize how click/type/read touch the live node.  By
        default the node's own ``click()`` / ``type(text)`` / ``read()``
        methods are used, which works for both fake and native nodes.
    """

    def __init__(
        self,
        adapter: TreeAdapter,
        root_provider: Callable[[], Any],
        scope: Optional[str] = None,
        action_dispatcher: Optional[Callable[[str, Any, Optional[str]], Any]] = None,
    ):
        self.adapter = adapter
        self._root_provider = root_provider
        self.registry: RefRegistry = SCOPE_MANAGER.create_scope(scope)
        self.scope = self.registry.scope
        self._closed = False
        self._action_dispatcher = action_dispatcher

    # -- lifecycle ---------------------------------------------------------- #
    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if not self._closed:
            SCOPE_MANAGER.drop_scope(self.scope)
            self._closed = True

    def __enter__(self) -> "Workflow":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- snapshots ---------------------------------------------------------- #
    def snapshot(self, *, max_depth: int = 15) -> Snapshot:
        """Capture a new generationed snapshot (invalidates older refs)."""
        self._ensure_open()
        root = self._root_provider()
        return build_snapshot(self.adapter, root, self.registry, max_depth=max_depth)

    def get_ui(self, *, include_refs: bool = True, max_depth: int = 15) -> str:
        """Convenience: YAML snapshot text, optionally with ref annotations."""
        return self.snapshot(max_depth=max_depth).to_yaml(include_refs=include_refs)

    # -- ref resolution ----------------------------------------------------- #
    def _ensure_open(self) -> None:
        if self._closed:
            from tarsier.core.ref import RefScopeError

            raise RefScopeError(
                f"Workflow scope {self.scope!r} is closed; start a new workflow "
                f"and take a fresh snapshot"
            )

    def resolve(self, ref: str) -> Any:
        """Validate the ref and return the live platform node it points at."""
        self._ensure_open()
        root = self._root_provider()
        _parsed, live = resolve_live(self.adapter, self.registry, root, ref)
        return live

    # -- actions ------------------------------------------------------------ #
    def _dispatch(self, action: str, ref: str, text: Optional[str] = None) -> Any:
        self._ensure_open()
        root = self._root_provider()
        _parsed, live = resolve_live(self.adapter, self.registry, root, ref)
        if self._action_dispatcher is not None:
            return self._action_dispatcher(action, live, text)
        if action == "click":
            return live.click()
        if action == "type":
            if text is None:
                raise ValueError("type action requires text")
            return live.type(text)
        if action == "read":
            return live.read()
        raise ValueError(f"unknown action {action!r}")

    def click(self, ref: str) -> Any:
        return self._dispatch("click", ref, None)

    def type(self, ref: str, text: str) -> Any:
        return self._dispatch("type", ref, text)

    def read(self, ref: str) -> str:
        return self._dispatch("read", ref, None)

    # -- role/name compatibility (legacy semantics, explicitly best-effort) -- #
    def find(self, role: Optional[str] = None, name: Optional[str] = None) -> Any:
        """Find the first matching live node (legacy role/name behaviour).

        Prefer refs for deterministic targeting; this remains for backward
        compatibility with callers that never take a ref-bearing snapshot.
        """
        self._ensure_open()
        root = self._root_provider()
        from collections import deque

        queue = deque([root])
        while queue:
            current = queue.popleft()
            if current is not root:
                try:
                    if _matches(self.adapter, current, role, name):
                        return current
                except Exception:
                    continue
            try:
                queue.extend(self.adapter.root_children(current))
            except Exception:
                pass
        raise ValueError(
            f"Element not found with role={role!r}, name={name!r} in scope {self.scope!r}"
        )


def _matches(adapter: TreeAdapter, node: Any, role: Optional[str], name: Optional[str]) -> bool:
    if role is not None:
        node_role = (adapter.node_role(node) or "").lower()
        if role.lower() not in node_role:
            return False
    if name is not None and adapter.node_name(node) != name:
        return False
    return True
