"""
Stable node references (``tarsier-ref``) for semantic snapshots.

A snapshot taken inside a :class:`Workflow` assigns every interactive /
named node a *stable ref*.  A ref encodes three pieces of information:

* ``scope``      – the workflow the ref belongs to (isolates concurrent
                   workflows and different windows / browser tabs)
* ``generation`` – monotonically increasing snapshot version inside the
                   scope.  Every re-snapshot bumps the generation and
                   invalidates refs handed out by earlier snapshots.
* ``node``       – a stable identifier of the node inside the snapshot
                   (native accessibility runtime id / DOM node id / path).

Refs are opaque, serializable strings of the form::

    tarsier-ref/v1/<scope>/<generation>/<node-id>

so they can travel through MCP / JSON / YAML unchanged.  Resolution is
always performed against the *current* tree of the scope; a ref never
silently falls back to "the first role/name match".  Every failure mode
raises a recognizable :class:`TarsierRefError` subclass carrying a stable
machine-readable ``code``.
"""

from __future__ import annotations

import itertools
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

REF_PREFIX = "tarsier-ref/v1"
_REF_RE = re.compile(
    r"^tarsier-ref/v1/(?P<scope>[A-Za-z0-9_.\-]+)/(?P<generation>\d+)/(?P<node>.+)$"
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class TarsierRefError(Exception):
    """Base class for every stable-ref failure.

    ``code`` is a stable, machine-readable identifier (never translated)
    so callers / MCP clients can branch on it without parsing messages.
    """

    code = "tarsier_ref_error"

    def __init__(self, message: str, *, ref: Optional[str] = None):
        super().__init__(message)
        self.ref = ref


class RefFormatError(TarsierRefError):
    """The string is not a well-formed tarsier ref."""

    code = "ref_format_error"


class RefScopeError(TarsierRefError):
    """The ref belongs to a different / unknown scope (window/tab/workflow)."""

    code = "ref_scope_error"


class RefGenerationExpiredError(TarsierRefError):
    """A newer snapshot exists in this scope; the ref's generation is stale.

    The caller must take a fresh snapshot and obtain a new ref.
    """

    code = "ref_generation_expired"


class RefTargetReplacedError(TarsierRefError):
    """The node id no longer resolves – the target was replaced/removed."""

    code = "ref_target_replaced"


class RefOutOfScopeError(TarsierRefError):
    """The node exists in the application but moved outside this snapshot scope."""

    code = "ref_out_of_scope"


# --------------------------------------------------------------------------- #
# Ref value object
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NodeRef:
    """Parsed / constructable stable node reference."""

    scope: str
    generation: int
    node: str

    def encode(self) -> str:
        return f"{REF_PREFIX}/{self.scope}/{self.generation}/{self.node}"

    def __str__(self) -> str:
        return self.encode()

    @staticmethod
    def parse(value: str) -> "NodeRef":
        if not isinstance(value, str):
            raise RefFormatError(f"Ref must be a string, got {type(value).__name__}", ref=str(value))
        match = _REF_RE.match(value)
        if not match:
            raise RefFormatError(
                f"Malformed tarsier ref {value!r}; expected "
                f"'{REF_PREFIX}/<scope>/<generation>/<node-id>'",
                ref=value,
            )
        return NodeRef(
            scope=match.group("scope"),
            generation=int(match.group("generation")),
            node=match.group("node"),
        )


# --------------------------------------------------------------------------- #
# Snapshot data model
# --------------------------------------------------------------------------- #
@dataclass
class SnapshotNode:
    """One node of a semantic snapshot."""

    role: str
    name: str
    node_id: str
    ref: str
    children: List["SnapshotNode"] = field(default_factory=list)
    value: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"role": self.role, "name": self.name, "ref": self.ref}
        if self.value is not None:
            data["value"] = self.value
        if self.children:
            data["elements"] = [c.to_dict() for c in self.children]
        if self.extra:
            data.update(self.extra)
        return data

    def iter_nodes(self):
        yield self
        for child in self.children:
            yield from child.iter_nodes()


@dataclass
class Snapshot:
    """An immutable semantic snapshot bound to one workflow generation."""

    scope: str
    generation: int
    root: SnapshotNode
    yaml_text: str
    nodes_by_id: Dict[str, SnapshotNode] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "generation": self.generation,
            "ref_prefix": REF_PREFIX,
            "tree": self.root.to_dict(),
        }

    def to_yaml(self, include_refs: bool = True) -> str:
        if include_refs:
            return self.yaml_text
        # strip only the ref annotation suffix, preserving names containing '#'
        marker = "  # " + REF_PREFIX
        lines = []
        for line in self.yaml_text.splitlines():
            idx = line.find(marker)
            lines.append(line[:idx] if idx != -1 else line)
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Tree adapter – platform backends implement this narrow protocol
# --------------------------------------------------------------------------- #
class TreeAdapter:
    """Adapter between a platform tree and the snapshot engine.

    A platform supplies *root handles* (one per scope).  The adapter knows
    how to walk whatever object graph the platform uses (UIA controls,
    AT-SPI accessible objects, fake nodes, Playwright handles) and how to
    re-resolve a previously captured node id against the *live* tree.
    """

    #: node roles that are always considered semantically meaningful
    MEANINGFUL_ROLES = {
        "button", "textbox", "checkbox", "radio", "combobox", "link",
        "menuitem", "window", "document", "edit", "searchbox", "switch",
        "tab", "tablist", "slider", "option", "listbox", "menubar",
    }

    def root_children(self, root: Any) -> List[Any]:
        raise NotImplementedError

    def node_role(self, node: Any) -> str:
        raise NotImplementedError

    def node_name(self, node: Any) -> str:
        raise NotImplementedError

    def node_id(self, node: Any) -> str:
        """Stable id of a *live* node (runtime id / DOM id / fake id)."""
        raise NotImplementedError

    def node_value(self, node: Any) -> Optional[str]:
        return None

    def node_extra(self, node: Any) -> Dict[str, Any]:
        """Optional platform-specific debug metadata copied onto the node."""
        return {}

    def is_meaningful(self, node: Any) -> bool:
        role = self.node_role(node) or ""
        name = self.node_name(node) or ""
        return role in self.MEANINGFUL_ROLES or bool(name.strip())

    def find_by_id(self, root: Any, node_id: str) -> Tuple[Any, bool]:
        """Re-resolve a node id against the live tree rooted at ``root``.

        Returns ``(node, in_scope)``:

        * ``(node, True)``  – the node is alive inside the scope root
        * ``(None, False)`` – not found inside the scope; the adapter may
          set ``in_scope=True`` with ``node=None`` to signal "exists
          elsewhere" (used for the out-of-scope distinction).
        """
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Ref registry – one per workflow scope
# --------------------------------------------------------------------------- #
class RefRegistry:
    """Holds the snapshot generations for a single workflow scope."""

    def __init__(self, scope: str):
        self.scope = scope
        self._generation = 0
        self._lock = threading.RLock()
        #: generation -> {node_id: SnapshotNode}
        self._generations: Dict[int, Dict[str, SnapshotNode]] = {}
        #: generation -> root handle the snapshot was taken from
        self._roots: Dict[int, Any] = {}

    @property
    def generation(self) -> int:
        return self._generation

    def reserve_generation(self, root: Any) -> int:
        """Atomically claim the next generation before the tree is walked.

        Lets the snapshot builder emit final refs in a single pass.
        """
        with self._lock:
            self._generation += 1
            gen = self._generation
            self._roots[gen] = root
            self._generations[gen] = {}
            return gen

    def publish_generation(self, gen: int, nodes: Dict[str, SnapshotNode]) -> None:
        with self._lock:
            self._generations[gen] = nodes

    def current_root(self) -> Any:
        with self._lock:
            return self._roots.get(self._generation)

    def resolve(self, ref: str) -> Tuple[NodeRef, SnapshotNode]:
        """Validate a ref against registry state. Returns (parsed, snapshot node).

        Raises a recognizable :class:`TarsierRefError` on every failure.
        """
        parsed = NodeRef.parse(ref)
        if parsed.scope != self.scope:
            raise RefScopeError(
                f"Ref scope {parsed.scope!r} does not match active scope "
                f"{self.scope!r}; refs cannot be reused across windows/tabs/workflows",
                ref=ref,
            )
        with self._lock:
            if parsed.generation > self._generation:
                raise RefGenerationExpiredError(
                    f"Ref generation {parsed.generation} is unknown to scope "
                    f"{self.scope!r} (current generation {self._generation}); "
                    f"take a fresh snapshot",
                    ref=ref,
                )
            if parsed.generation < self._generation:
                raise RefGenerationExpiredError(
                    f"Ref generation {parsed.generation} expired; a newer snapshot "
                    f"(generation {self._generation}) exists for scope {self.scope!r}. "
                    f"Take a fresh snapshot and obtain a new ref before acting",
                    ref=ref,
                )
            nodes = self._generations.get(parsed.generation, {})
            snap_node = nodes.get(parsed.node)
            if snap_node is None:
                raise RefTargetReplacedError(
                    f"Node {parsed.node!r} is absent from snapshot generation "
                    f"{parsed.generation} of scope {self.scope!r}",
                    ref=ref,
                )
            return parsed, snap_node


# --------------------------------------------------------------------------- #
# Global scope manager – isolates concurrent workflows
# --------------------------------------------------------------------------- #
class ScopeManager:
    """Process-wide table of live workflow scopes and their registries."""

    def __init__(self):
        self._lock = threading.RLock()
        self._scopes: Dict[str, RefRegistry] = {}
        self._counter = itertools.count(1)

    def create_scope(self, scope: Optional[str] = None) -> RefRegistry:
        with self._lock:
            if scope is None:
                scope = f"wf-{next(self._counter):06d}"
            if scope in self._scopes:
                raise RefScopeError(f"Workflow scope {scope!r} already exists")
            registry = RefRegistry(scope)
            self._scopes[scope] = registry
            return registry

    def get(self, scope: str) -> RefRegistry:
        with self._lock:
            registry = self._scopes.get(scope)
            if registry is None:
                raise RefScopeError(
                    f"Unknown workflow scope {scope!r}; the workflow was closed or "
                    f"the ref came from another process/session"
                )
            return registry

    def drop_scope(self, scope: str) -> None:
        with self._lock:
            self._scopes.pop(scope, None)

    def resolve_ref(self, ref: str) -> Tuple[RefRegistry, NodeRef, SnapshotNode]:
        parsed = NodeRef.parse(ref)
        registry = self.get(parsed.scope)
        _, node = registry.resolve(ref)
        return registry, parsed, node


#: process-wide singleton
SCOPE_MANAGER = ScopeManager()


# --------------------------------------------------------------------------- #
# Snapshot construction
# --------------------------------------------------------------------------- #
def _yaml_escape(name: str) -> str:
    return name.replace('"', '\\"')


def build_snapshot(
    adapter: TreeAdapter,
    root: Any,
    registry: RefRegistry,
    *,
    max_depth: int = 15,
) -> Snapshot:
    """Walk a live tree once and build a generationed, ref-bearing snapshot.

    The same traversal produces the SnapshotNode tree, the id->node index
    and the YAML text (with trailing ``# tarsier-ref/...`` annotations),
    guaranteeing all three views agree node-for-node.
    """

    # First pass: prune exactly like the legacy _build_keep_set logic.
    keep_ids: set = set()

    def mark(node: Any) -> bool:
        meaningful = False
        try:
            meaningful = adapter.is_meaningful(node)
        except Exception:
            meaningful = False
        keeps_child = False
        try:
            for child in adapter.root_children(node):
                if mark(child):
                    keeps_child = True
        except Exception:
            pass
        if meaningful or keeps_child:
            try:
                keep_ids.add(adapter.node_id(node))
            except Exception:
                return False
            return True
        return False

    mark(root)

    nodes_by_id: Dict[str, SnapshotNode] = {}
    # Reserve the generation up front so refs are emitted with their final
    # value in a single traversal (no patching / YAML rebuild afterwards).
    gen = registry.reserve_generation(root)

    def build(node: Any, depth: int) -> Optional[SnapshotNode]:
        try:
            node_id = adapter.node_id(node)
        except Exception:
            return None
        if node_id not in keep_ids:
            return None
        try:
            role = adapter.node_role(node) or "unknown"
            name = adapter.node_name(node) or ""
        except Exception:
            return None

        snap = SnapshotNode(
            role=role,
            name=name,
            node_id=node_id,
            ref=NodeRef(registry.scope, gen, node_id).encode(),
        )
        try:
            snap.value = adapter.node_value(node)
        except Exception:
            snap.value = None
        try:
            snap.extra = adapter.node_extra(node) or {}
        except Exception:
            snap.extra = {}
        nodes_by_id[node_id] = snap

        valid_children: List[SnapshotNode] = []
        if depth < max_depth:
            try:
                children = adapter.root_children(node)
            except Exception:
                children = []
            for child in children:
                child_snap = build(child, depth + 1)
                if child_snap is not None:
                    valid_children.append(child_snap)
        snap.children = valid_children
        return snap

    root_snap = build(root, 0)
    if root_snap is None:
        # Root always has an id; defensive fallback so snapshots never crash.
        root_id = adapter.node_id(root)
        root_snap = SnapshotNode(
            role=getattr(adapter, "node_role", lambda _: "unknown")(root) or "unknown",
            name="",
            node_id=root_id,
            ref=NodeRef(registry.scope, gen, root_id).encode(),
        )
        nodes_by_id[root_id] = root_snap

    # Pre-order YAML emission from the finished snapshot tree.
    yaml_lines: List[str] = []

    def emit(snap_node: SnapshotNode, depth: int) -> None:
        indent = "  " * depth
        line = f"{indent}- {snap_node.role}"
        if snap_node.name:
            line += f' "{_yaml_escape(snap_node.name)}"'
        if snap_node.children:
            line += ":"
        line += f"  # {snap_node.ref}"
        yaml_lines.append(line)
        for child in snap_node.children:
            emit(child, depth + 1)

    emit(root_snap, 0)

    registry.publish_generation(gen, nodes_by_id)
    return Snapshot(
        scope=registry.scope,
        generation=gen,
        root=root_snap,
        yaml_text="\n".join(yaml_lines),
        nodes_by_id=nodes_by_id,
    )


# --------------------------------------------------------------------------- #
# Ref resolution against the live tree
# --------------------------------------------------------------------------- #
def resolve_live(
    adapter: TreeAdapter,
    registry: RefRegistry,
    root: Any,
    ref: str,
) -> Tuple[NodeRef, Any]:
    """Validate the ref and re-resolve it against the current live tree.

    Order of checks matters:

    1. format / scope / generation validity (registry)
    2. live re-resolution – a detached or relocated node must fail loudly
       instead of silently operating on a different control.
    """
    parsed, _snap_node = registry.resolve(ref)
    try:
        live_node, in_scope = adapter.find_by_id(root, parsed.node)
    except Exception as exc:  # pragma: no cover - defensive
        raise RefTargetReplacedError(
            f"Failed to re-resolve node {parsed.node!r}: {exc}", ref=ref
        ) from exc
    if live_node is not None:
        return parsed, live_node
    if in_scope:
        raise RefOutOfScopeError(
            f"Node {parsed.node!r} moved outside the active scope "
            f"{registry.scope!r} (window/tab switch or detached subtree)",
            ref=ref,
        )
    raise RefTargetReplacedError(
        f"Node {parsed.node!r} no longer exists in scope {registry.scope!r}; "
        f"the target was replaced or removed. Take a fresh snapshot",
        ref=ref,
    )
