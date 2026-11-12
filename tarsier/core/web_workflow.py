"""
Web workflow: stable refs for Playwright-driven pages.

DOM nodes have no persistent identity across redraws, so on every snapshot
the page is scanned in the browser and each semantically meaningful element
is stamped with a ``data-tarsier-id``.  The ref's node id is that token;
re-resolution queries the *current* DOM for the token.  A page reload,
navigation or re-render drops the stamped tokens, so stale refs fail with
:class:`RefTargetReplacedError` instead of silently hitting a replacement
element.

The workflow scope is bound to a specific Playwright page: refs taken on one
tab cannot be resolved after switching to another (``RefScopeError`` /
``RefOutOfScopeError``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from tarsier.core.ref import (
    RefOutOfScopeError,
    RefTargetReplacedError,
    TreeAdapter,
)
from tarsier.core.workflow import Workflow

# Runs entirely inside the browser.  It mirrors the pruning rules of the
# desktop snapshot: keep interactive/ARIA-meaningful elements and anything
# with an accessible name, plus ancestors that contain kept descendants.
_SNAPSHOT_JS = r"""
() => {
    const meaningfulRoles = new Set([
        'button', 'textbox', 'checkbox', 'radio', 'combobox', 'link',
        'menuitem', 'dialog', 'document', 'searchbox', 'switch', 'tab',
        'slider', 'option', 'listbox', 'menubar'
    ]);
    const interactiveTags = new Set([
        'A', 'BUTTON', 'INPUT', 'TEXTAREA', 'SELECT', 'SUMMARY', 'OPTION'
    ]);

    const accName = (el) => {
        try {
            return (el.getAttribute('aria-label') ||
                (el.labels && el.labels.length ? el.labels[0].innerText : '') ||
                el.title || el.innerText || el.value || '').trim();
        } catch (e) { return ''; }
    };
    const ariaRole = (el) => {
        const explicit = el.getAttribute('role');
        if (explicit) return explicit.toLowerCase().split(' ')[0];
        const tag = el.tagName;
        if (tag === 'A') return 'link';
        if (tag === 'BUTTON') return 'button';
        if (tag === 'INPUT') {
            const t = (el.type || 'text').toLowerCase();
            if (t === 'checkbox') return 'checkbox';
            if (t === 'radio') return 'radio';
            if (t === 'submit' || t === 'button') return 'button';
            return 'textbox';
        }
        if (tag === 'TEXTAREA') return 'textbox';
        if (tag === 'SELECT') return 'combobox';
        if (tag === 'NAV') return 'navigation';
        if (['H1','H2','H3','H4','H5','H6'].includes(tag)) return 'heading';
        return tag.toLowerCase();
    };
    const isMeaningful = (el) => {
        const role = ariaRole(el);
        if (meaningfulRoles.has(role)) return true;
        if (interactiveTags.has(el.tagName)) return true;
        if (el.isContentEditable) return true;
        if (accName(el)) return true;
        return false;
    };

    // 1. prune: mark nodes worth keeping
    const keep = new Set();
    const mark = (el) => {
        let keepChild = false;
        for (const child of el.children) {
            if (mark(child)) keepChild = true;
        }
        if (isMeaningful(el) || keepChild) { keep.add(el); return true; }
        return false;
    };
    mark(document.body);

    // 2. stamp stable tokens and build the serializable tree.
    // The per-page random prefix is rolled once per fresh document: after a
    // reload/navigation the new document gets a different prefix, so ids
    // minted before the reload can never collide with re-stamped nodes.
    if (!window.__tarsier_prefix) {
        window.__tarsier_prefix = 'p' + Math.random().toString(36).slice(2, 10);
        window.__tarsier_seq = 0;
    }
    const build = (el, depth) => {
        if (!keep.has(el)) return null;
        const nodeId = el.getAttribute('data-tarsier-id') ||
            ('dom/' + window.__tarsier_prefix + '/' + (window.__tarsier_seq++));
        el.setAttribute('data-tarsier-id', nodeId);
        const role = ariaRole(el);
        const name = accName(el);
        const node = {
            role, name, nodeId, depth,
            domId: el.id || null,
            value: (el.value !== undefined && el.value !== '') ? String(el.value) : null,
            children: []
        };
        for (const child of el.children) {
            const childNode = build(child, depth + 1);
            if (childNode) node.children.push(childNode);
        }
        return node;
    };
    const tree = build(document.body, 0);
    // The body root always participates in the snapshot and must carry a
    // stamp too, otherwise its ref would survive a page reload.
    if (tree && !document.body.getAttribute('data-tarsier-id')) {
        document.body.setAttribute('data-tarsier-id', tree.nodeId);
    }
    return { tree };
}
"""

# Re-resolve one stamped token against the live DOM.
_RESOLVE_JS = r"""
(nodeId) => {
    const el = document.querySelector('[data-tarsier-id="' + nodeId + '"]');
    return el ? true : false;
}
"""


class WebTreeAdapter(TreeAdapter):
    """Adapter over the serializable tree returned by :data:`_SNAPSHOT_JS`.

    The root handle is the JS tree's root node dict with the Playwright
    ``page`` attached under a private key, so the same generic snapshot
    engine can walk the browser tree while :meth:`find_by_id` can still
    re-query the live DOM through the page.
    """

    def root_children(self, root: Dict[str, Any]) -> List[Dict[str, Any]]:
        return list(root.get("children", []))

    def node_role(self, node: Dict[str, Any]) -> str:
        return node.get("role", "unknown")

    def node_name(self, node: Dict[str, Any]) -> str:
        return node.get("name", "") or ""

    def node_id(self, node: Dict[str, Any]) -> str:
        return node["nodeId"]

    def node_value(self, node: Dict[str, Any]) -> Optional[str]:
        return node.get("value")

    def node_extra(self, node: Dict[str, Any]) -> Dict[str, Any]:
        return {"dom_id": node["domId"]} if node.get("domId") else {}

    def find_by_id(self, root: Dict[str, Any], node_id: str) -> Tuple[Any, bool]:
        page = root["_page"]
        try:
            found = page.evaluate(_RESOLVE_JS, node_id)
        except Exception as exc:
            raise RefTargetReplacedError(
                f"Page is not reachable while resolving {node_id!r}: {exc}"
            ) from exc
        if found:
            return {"_page": page, "node_id": node_id}, True
        # Token not on this page: check the other tabs of the same browser
        # context to distinguish "moved to another scope" from "gone entirely".
        try:
            elsewhere = any(
                other is not page and not other.is_closed()
                and _safe_evaluate(other, _RESOLVE_JS, node_id)
                for other in page.context.pages
            )
        except Exception:
            elsewhere = False
        return None, elsewhere


def _safe_evaluate(page: Any, script: str, arg: str) -> bool:
    try:
        return bool(page.evaluate(script, arg))
    except Exception:
        return False


class WebWorkflow:
    """Page-scoped web workflow with stable-ref click/type/read."""

    def __init__(self, web_desktop: Any, scope: Optional[str] = None):
        self._web = web_desktop
        self.adapter = WebTreeAdapter()
        # Bind the scope to the page active at creation time. Refs are page
        # scoped: switching the browser's active tab makes resolution fail
        # with RefOutOfScopeError instead of touching the new page.
        initial_page = getattr(web_desktop, "page", None)
        if initial_page is None or initial_page.is_closed():
            raise RefOutOfScopeError(
                "Cannot start a web workflow without an open active page"
            )
        self._bound_page = initial_page
        self._workflow = Workflow(
            self.adapter,
            root_provider=self._live_root,
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

    def __enter__(self) -> "WebWorkflow":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _page(self) -> Any:
        page = getattr(self._web, "page", None)
        if page is None or page.is_closed():
            raise RefOutOfScopeError(
                f"Active page of scope {self.scope!r} is closed; open a page "
                f"and take a fresh snapshot"
            )
        if page is not self._bound_page:
            raise RefOutOfScopeError(
                f"Scope {self.scope!r} is bound to a different browser tab; "
                f"the active tab was switched. Start a workflow on the new tab "
                f"and take a fresh snapshot"
            )
        if self._bound_page.is_closed():
            raise RefOutOfScopeError(
                f"The tab bound to scope {self.scope!r} was closed"
            )
        return page

    def _live_root(self) -> Dict[str, Any]:
        # Re-evaluate against the live DOM each time; the previous tree is
        # only structural input for pruning/emission inside build_snapshot.
        page = self._page()
        data = page.evaluate(_SNAPSHOT_JS)
        if not data or data.get("tree") is None:
            data = {
                "tree": {
                    "role": "document", "name": "",
                    "nodeId": "dom/body", "children": [],
                },
            }
        tree = data["tree"]
        tree["_page"] = page  # private handle for find_by_id
        return tree

    # -- snapshots ---------------------------------------------------------- #
    def snapshot(self, *, max_depth: int = 15):
        return self._workflow.snapshot(max_depth=max_depth)

    def get_ui(self, *, include_refs: bool = True, max_depth: int = 15) -> str:
        return self._workflow.get_ui(include_refs=include_refs, max_depth=max_depth)

    # -- locator + actions -------------------------------------------------- #
    def _locator_for(self, live: Dict[str, Any]) -> Any:
        page = live["_page"]
        node_id = live["node_id"]
        # CSS.escape equivalent: token chars are alnum / - / _ / / so safe.
        locator = page.locator(f'[data-tarsier-id="{node_id}"]')
        count = locator.count()
        if count == 0:
            raise RefTargetReplacedError(
                f"Node {node_id!r} vanished between validation and action; "
                f"the page redrew. Take a fresh snapshot"
            )
        if count > 1:
            # Duplicate stamps would mean two nodes share identity – never
            # silently pick the first; fail loudly.
            raise RefTargetReplacedError(
                f"Node id {node_id!r} matches {count} elements; identity is "
                f"ambiguous after redraw. Take a fresh snapshot"
            )
        return locator.first

    def _dispatch(self, action: str, live: Dict[str, Any], text: Optional[str]) -> Any:
        locator = self._locator_for(live)
        if action == "click":
            locator.scroll_into_view_if_needed()
            locator.click()
            return locator
        if action == "type":
            locator.scroll_into_view_if_needed()
            locator.fill(text or "")
            return locator
        if action == "read":
            return locator.inner_text()
        raise ValueError(f"unknown action {action!r}")

    def click(self, ref: str) -> Any:
        return self._workflow.click(ref)

    def type(self, ref: str, text: str) -> Any:
        return self._workflow.type(ref, text)

    def read(self, ref: str) -> str:
        return self._workflow.read(ref)

    def resolve(self, ref: str) -> Any:
        live = self._workflow.resolve(ref)
        return self._locator_for(live)

    # -- legacy role/name path ---------------------------------------------- #
    def find(self, role: Optional[str] = None, name: Optional[str] = None) -> Any:
        page = self._page()
        if role and name:
            return page.get_by_role(role, name=name)
        if role:
            return page.get_by_role(role)
        if name:
            return page.get_by_text(name)
        raise ValueError("find requires at least role or name")
