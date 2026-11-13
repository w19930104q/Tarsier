"""
Snapshot-level stable references for Tarsier accessibility trees.

The YAML/JSON snapshots produced by :class:`tarsier.core.elements.UIElementBackend`
used to contain only ``role``/``name`` pairs.  When a window contains several
elements with the same role and name (for example many "删除" buttons in a
table), a plain ``find(role=..., name=...)`` call can only return the first
match and an automation client has no way to target a specific row.  After a
window refresh, re-running that search may silently click the wrong row.

This module implements the platform-neutral part of stable snapshot
references:

* :class:`NodeView` is a lightweight, serialisable description of an
  accessibility node.  The platform backends (Windows/macOS/Linux) build
  ``NodeView`` trees from their native controls; the rest of the engine never
  touches a platform API, which keeps it fully unit-testable off-device.
* :class:`SnapshotBuilder` prunes the tree with the same semantics as the
  historical YAML dumper and assigns every emitted node a unique reference
  (``s<generation>n<index>``) while preserving parent/child hierarchy.
* :class:`SnapshotStore` owns the per-window generation counter and resolves
  references against the *live* tree.  Stale snapshots, role/name mismatches
  and ambiguous matches all raise dedicated, self-explanatory exceptions
  instead of silently operating on the wrong element.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque, OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# A reference always has the opaque shape
# ``w<window-token>:s<generation>n<index>``, e.g. ``w1a2b3c:s0n12``.
# The window token is a short hash of the window identity, so a ref minted for
# one window can never be mistaken for another window's reference even when
# their generation counters happen to align.
REF_PATTERN = re.compile(r"^(?:w([0-9a-f]+):)?s(\d+)n(\d+)$")

# Roles that represent interactive controls.  Mirrors the historical
# ``is_meaningful`` check used by the YAML pruning walk.
ACTIONABLE_ROLES = frozenset(
    [
        "button",
        "textbox",
        "checkbox",
        "radio",
        "combobox",
        "link",
        "menuitem",
        "window",
        "document",
        "edit",
    ]
)

# How many past snapshots a store remembers so that stale references can be
# diagnosed with a precise error message instead of a generic "unknown ref".
DEFAULT_HISTORY_SIZE = 5


def window_token(window_key: str) -> str:
    """Returns the short, filesystem-safe identity token embedded in refs."""
    return hashlib.sha1(window_key.encode("utf-8")).hexdigest()[:8]


def make_ref(window_key: str, generation: int, index: int) -> str:
    """Builds an opaque snapshot reference string."""
    return f"w{window_token(window_key)}:s{generation}n{index}"


def parse_ref(ref: str) -> Tuple[Optional[str], int, int]:
    """Parses a reference into ``(window_token, generation, index)``."""
    match = REF_PATTERN.match(ref or "")
    if not match:
        raise ValueError(f"malformed reference: {ref!r}")
    token, generation, index = match.groups()
    return token, int(generation), int(index)


class SnapshotError(Exception):
    """Base class for every reference/snapshot failure."""


class StaleSnapshotError(SnapshotError):
    """Raised when a reference belongs to an expired snapshot generation."""


class UnknownReferenceError(SnapshotError):
    """Raised when a reference string is malformed or was never issued."""


class AmbiguousReferenceError(SnapshotError):
    """Raised when a reference resolves to more than one live element."""


class ReferenceMismatchError(SnapshotError):
    """Raised when the live element no longer has the recorded role/name."""


class ElementUnavailableError(SnapshotError):
    """Raised when a currently-valid reference points at a vanished element."""


@dataclass
class NodeView:
    """Platform-neutral description of one accessibility node.

    ``node_id`` must be stable and hashable for the lifetime of a single tree
    walk (runtime id on Windows, object identity/id() on macOS/Linux, plain
    integers in tests).  ``control`` optionally carries the native backend
    object so that a resolved view can be acted upon directly.
    """

    role: str
    name: str
    node_id: Any
    children: List["NodeView"] = field(default_factory=list)
    control: Any = None
    actionable: bool = False
    visible: bool = True


@dataclass
class SnapshotNode:
    """One node as recorded inside a :class:`Snapshot`."""

    index: int
    ref: str
    role: str
    name: str
    # Child-index path from the snapshot root through the *full* (unpruned)
    # tree.  Used as the first relocation strategy against the live tree.
    path: Tuple[int, ...]
    node_id: Any
    actionable: bool
    visible: bool
    truncated: bool = False
    children: List["SnapshotNode"] = field(default_factory=list)


def escape_yaml_name(name: str) -> str:
    """Escapes a node name for inclusion inside double quotes in the YAML dump.

    Backslashes are escaped first so the transformation stays reversible, then
    double quotes and control characters.  Non-ASCII characters (CJK, emoji,
    ...) are intentionally left untouched.
    """

    return (
        str(name)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


class Snapshot:
    """An immutable, reference-bearing view of an accessibility tree."""

    def __init__(
        self,
        generation: int,
        window_key: str,
        root: SnapshotNode,
        nodes_by_ref: Dict[str, SnapshotNode],
        max_depth: int,
        truncated: bool,
        created_at: Optional[float] = None,
    ) -> None:
        self.generation = generation
        self.snapshot_id = f"s{generation}"
        self.window_key = window_key
        self.root = root
        self.nodes_by_ref = nodes_by_ref
        self.max_depth = max_depth
        self.truncated = truncated
        self.created_at = created_at if created_at is not None else time.time()

    def get(self, ref: str) -> SnapshotNode:
        """Returns the recorded node for ``ref`` or raises."""

        node = self.nodes_by_ref.get(ref)
        if node is None:
            raise UnknownReferenceError(
                f"Reference '{ref}' does not exist in snapshot {self.snapshot_id} "
                f"(window '{self.window_key}'). Take a fresh snapshot and use one "
                f"of its references."
            )
        return node

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #

    def to_dict(self) -> Dict[str, Any]:
        """Returns a nested ``dict`` suitable for JSON serialisation."""

        def node_dict(node: SnapshotNode) -> Dict[str, Any]:
            data: Dict[str, Any] = {
                "ref": node.ref,
                "role": node.role,
                "name": node.name,
                "actionable": node.actionable,
                "visible": node.visible,
            }
            if node.truncated:
                data["truncated"] = True
            if node.children:
                data["elements"] = [node_dict(child) for child in node.children]
            return data

        result: Dict[str, Any] = {
            "snapshot_id": self.snapshot_id,
            "generation": self.generation,
            "window_key": self.window_key,
            "truncated": self.truncated,
            "max_depth": self.max_depth,
            "created_at": self.created_at,
        }
        result.update(node_dict(self.root))
        return result

    def to_json(self, indent: int = 2) -> str:
        # ``ensure_ascii=False`` keeps non-ASCII names (删除, こんにちは, ...) readable.
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def to_yaml(self) -> str:
        """Renders the hand-rolled, token-efficient YAML snapshot.

        The historical shape (``- role "name":`` with two-space nested lists)
        is preserved; each emitted line gains a ``# ref=...`` comment and
        truncated subtrees are explicitly marked.
        """

        lines = [
            f'# tarsier snapshot: id={self.snapshot_id} generation={self.generation} '
            f'window_key="{escape_yaml_name(self.window_key)}" '
            f'truncated={str(self.truncated).lower()} max_depth={self.max_depth}'
        ]
        self._emit_yaml(self.root, 0, lines)
        return "\n".join(lines)

    def _emit_yaml(self, node: SnapshotNode, depth: int, lines: List[str]) -> None:
        indent = "  " * depth
        line = f"{indent}- {node.role}"
        if node.name:
            line += f' "{escape_yaml_name(node.name)}"'
        if node.children or node.truncated:
            line += ":"
        line += f"  # ref={node.ref}"
        if node.truncated:
            line += " [truncated]"
        lines.append(line)
        for child in node.children:
            self._emit_yaml(child, depth + 1, lines)
        if node.truncated:
            marker_indent = "  " * (depth + 1)
            lines.append(
                f'{marker_indent}- "...[truncated: max_depth={self.max_depth} reached; '
                f're-snapshot with a larger max_depth]"'
            )


class SnapshotBuilder:
    """Builds :class:`Snapshot` objects from :class:`NodeView` trees."""

    def __init__(
        self,
        max_depth: int = 15,
        actionable_roles: Optional[frozenset] = None,
    ) -> None:
        self.max_depth = max_depth
        self.actionable_roles = actionable_roles or ACTIONABLE_ROLES

    def build(
        self,
        root_view: NodeView,
        generation: int,
        window_key: str = "",
    ) -> Snapshot:
        keep_ids = self._compute_keep_set(root_view)
        nodes_by_ref: Dict[str, SnapshotNode] = {}
        counter = {"index": 0}
        root_node, truncated = self._emit(
            view=root_view,
            path=(),
            depth=0,
            keep_ids=keep_ids,
            generation=generation,
            window_key=window_key,
            counter=counter,
            nodes_by_ref=nodes_by_ref,
        )
        return Snapshot(
            generation=generation,
            window_key=window_key,
            root=root_node,
            nodes_by_ref=nodes_by_ref,
            max_depth=self.max_depth,
            truncated=truncated,
        )

    def _is_meaningful(self, view: NodeView) -> bool:
        if view.actionable or view.role in self.actionable_roles:
            return True
        return bool(view.name and view.name.strip())

    def _compute_keep_set(self, view: NodeView) -> set:
        """Returns the node ids that must survive pruning.

        A node is kept when it is meaningful itself or when any descendant is
        kept -- identical semantics to the legacy ``_build_keep_set`` walk.
        """

        keep: set = set()

        def walk(node: NodeView) -> bool:
            kept_child = False
            for child in node.children:
                if walk(child):
                    kept_child = True
            if self._is_meaningful(node) or kept_child:
                keep.add(node.node_id)
                return True
            return False

        walk(view)
        return keep

    def _emit(
        self,
        view: NodeView,
        path: Tuple[int, ...],
        depth: int,
        keep_ids: set,
        generation: int,
        window_key: str,
        counter: Dict[str, int],
        nodes_by_ref: Dict[str, SnapshotNode],
    ) -> Tuple[SnapshotNode, bool]:
        index = counter["index"]
        counter["index"] += 1
        ref = make_ref(window_key, generation, index)

        node = SnapshotNode(
            index=index,
            ref=ref,
            role=view.role,
            name=view.name,
            path=path,
            node_id=view.node_id,
            actionable=bool(view.actionable or view.role in self.actionable_roles),
            visible=bool(view.visible),
        )
        nodes_by_ref[ref] = node

        subtree_truncated = False
        if depth < self.max_depth:
            for child_index, child in enumerate(view.children):
                if child.node_id not in keep_ids:
                    continue
                child_node, child_truncated = self._emit(
                    view=child,
                    path=path + (child_index,),
                    depth=depth + 1,
                    keep_ids=keep_ids,
                    generation=generation,
                    window_key=window_key,
                    counter=counter,
                    nodes_by_ref=nodes_by_ref,
                )
                node.children.append(child_node)
                subtree_truncated = subtree_truncated or child_truncated
        else:
            # Any kept child below the depth limit is silently omitted but the
            # truncation is surfaced explicitly on this node.
            if any(child.node_id in keep_ids for child in view.children):
                node.truncated = True
                subtree_truncated = True

        node.truncated = node.truncated or subtree_truncated
        return node, subtree_truncated


def _iter_bfs(root: NodeView):
    queue = deque([root])
    while queue:
        current = queue.popleft()
        yield current
        queue.extend(current.children)


class SnapshotStore:
    """Per-window snapshot memory: generation counter + bounded history."""

    def __init__(self, window_key: str, history_size: int = DEFAULT_HISTORY_SIZE) -> None:
        self.window_key = window_key
        self.history_size = max(1, history_size)
        self._generation = 0
        self._history: "OrderedDict[int, Snapshot]" = OrderedDict()
        self._current: Optional[Snapshot] = None

    @property
    def current(self) -> Optional[Snapshot]:
        return self._current

    @property
    def generation(self) -> int:
        return self._generation

    def create(self, root_view: NodeView, max_depth: int = 15) -> Snapshot:
        """Takes a new snapshot, bumping the window generation."""

        snapshot = SnapshotBuilder(max_depth=max_depth).build(
            root_view, generation=self._generation, window_key=self.window_key
        )
        self._history[snapshot.generation] = snapshot
        while len(self._history) > self.history_size:
            self._history.popitem(last=False)
        self._current = snapshot
        self._generation += 1
        return snapshot

    def resolve(
        self, ref: str, live_root: Optional[NodeView] = None
    ) -> Tuple[Snapshot, SnapshotNode, Optional[NodeView]]:
        """Resolves ``ref`` and validates it against the live tree.

        Returns ``(snapshot, recorded_node, live_view)``.  ``live_view`` is
        ``None`` when no live tree is supplied.  Raises a dedicated
        :class:`SnapshotError` subclass for every failure mode.
        """

        match = REF_PATTERN.match(ref or "")
        if not match:
            raise UnknownReferenceError(
                f"Malformed reference '{ref}'. References look like "
                f"'w<window-token>:s<generation>n<index>' (e.g. 'w1a2b3c:s0n12') "
                f"and come from a snapshot of window '{self.window_key}'."
            )
        ref_token, ref_generation, _ = match.groups()
        ref_generation = int(ref_generation)
        expected_token = window_token(self.window_key)
        if ref_token is not None and ref_token != expected_token:
            raise UnknownReferenceError(
                f"Reference '{ref}' belongs to a different window "
                f"(token '{ref_token}') and cannot be used with window "
                f"'{self.window_key}' (token '{expected_token}'). Take a fresh "
                f"snapshot of this window."
            )

        snapshot = self._history.get(ref_generation)
        current_generation = self._current.generation if self._current else 0

        if snapshot is None:
            if ref_generation < current_generation:
                raise StaleSnapshotError(
                    f"Reference '{ref}' belongs to expired snapshot generation "
                    f"s{ref_generation}; window '{self.window_key}' is now at "
                    f"generation s{current_generation}. Take a fresh snapshot and "
                    f"use a current reference."
                )
            raise UnknownReferenceError(
                f"Reference '{ref}' was never issued by window '{self.window_key}' "
                f"(current generation: s{current_generation})."
            )

        if self._current is not None and snapshot.generation != self._current.generation:
            raise StaleSnapshotError(
                f"Reference '{ref}' belongs to stale snapshot {snapshot.snapshot_id}; "
                f"window '{self.window_key}' has since been re-snapshotted at "
                f"generation {self._current.snapshot_id}. Take a fresh snapshot and "
                f"use a current reference."
            )

        recorded = snapshot.get(ref)
        if live_root is None:
            return snapshot, recorded, None

        live_view = self._locate(recorded, live_root)
        if live_view.role != recorded.role or live_view.name != recorded.name:
            raise ReferenceMismatchError(
                f"Reference '{ref}' expected {recorded.role} "
                f"'{recorded.name}' but the live element is now "
                f"{live_view.role} '{live_view.name}'. Take a fresh snapshot."
            )
        if not live_view.visible:
            raise ElementUnavailableError(
                f"Reference '{ref}' ({recorded.role} '{recorded.name}') is no longer "
                f"visible in window '{self.window_key}'. Take a fresh snapshot."
            )
        return snapshot, recorded, live_view

    def _locate(self, recorded: SnapshotNode, live_root: NodeView) -> NodeView:
        # 1. Structural path: cheap and exact while the tree is unchanged.
        target = self._walk_path(live_root, recorded.path)
        if target is not None and target.node_id == recorded.node_id:
            return target

        # 2. Stable identity search: tolerates nodes moving to another parent.
        id_matches = [
            view for view in _iter_bfs(live_root) if view.node_id == recorded.node_id
        ]
        if len(id_matches) == 1:
            return id_matches[0]
        if len(id_matches) > 1:
            raise AmbiguousReferenceError(
                f"Reference '{ref_desc(recorded)}' matches {len(id_matches)} live "
                f"nodes with the same identity; take a fresh snapshot."
            )

        # 3. Role/name fallback for trees whose ids are reissued on mutation.
        name_matches = [
            view
            for view in _iter_bfs(live_root)
            if view.role == recorded.role and view.name == recorded.name
        ]
        if len(name_matches) == 1:
            return name_matches[0]
        if len(name_matches) > 1:
            raise AmbiguousReferenceError(
                f"Reference '{ref_desc(recorded)}' is ambiguous: "
                f"{len(name_matches)} live {recorded.role} elements are named "
                f"'{recorded.name}'. Take a fresh snapshot and disambiguate by ref."
            )
        raise ElementUnavailableError(
            f"Reference '{ref_desc(recorded)}' no longer exists in window "
            f"'{self.window_key}'. Take a fresh snapshot."
        )

    @staticmethod
    def _walk_path(root: NodeView, path: Tuple[int, ...]) -> Optional[NodeView]:
        current = root
        for index in path:
            if index >= len(current.children):
                return None
            current = current.children[index]
        return current


def ref_desc(node: SnapshotNode) -> str:
    return f"{node.ref} ({node.role} '{node.name}')"


class SnapshotRegistry:
    """Maps window identities to their :class:`SnapshotStore` instances.

    A window replacement (new native window handle / new window object) yields
    a new key, so references issued against the old window can never be
    replayed against the replacement.
    """

    def __init__(self, history_size: int = DEFAULT_HISTORY_SIZE) -> None:
        self.history_size = history_size
        self._stores: Dict[str, SnapshotStore] = {}

    def store(self, window_key: str) -> SnapshotStore:
        existing = self._stores.get(window_key)
        if existing is None:
            existing = SnapshotStore(window_key, history_size=self.history_size)
            self._stores[window_key] = existing
        return existing

    def has(self, window_key: str) -> bool:
        return window_key in self._stores

    def reset(self, window_key: Optional[str] = None) -> None:
        """Drops snapshot state for one window, or every window."""

        if window_key is None:
            self._stores.clear()
        else:
            self._stores.pop(window_key, None)

    def keys(self) -> List[str]:
        return list(self._stores.keys())


_REGISTRY = SnapshotRegistry()


def get_registry() -> SnapshotRegistry:
    """Returns the process-wide snapshot registry."""

    return _REGISTRY


def reset_registry() -> None:
    """Clears every stored snapshot (mainly useful for tests/teardown)."""

    _REGISTRY.reset()
