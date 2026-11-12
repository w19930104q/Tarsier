"""
Stable-ref workflow demo on the in-memory fake accessibility tree.

Runs fully offline (no display server / browser) and shows the complete
lifecycle on a batch form containing duplicate "Save" buttons and nested
confirmation dialogs:

1. open a scoped workflow and snapshot
2. act on the *exact* duplicate control via its stable ref
3. observe the recognizable errors when a target is replaced, a window is
   switched or a ref's generation expires
4. recover by taking a fresh snapshot

Run:  python examples/stable_ref_workflow_demo.py
"""

from tarsier.core.ref import (
    RefGenerationExpiredError,
    RefScopeError,
    RefTargetReplacedError,
)
from tarsier.core.workflow import Workflow
from tarsier.testing import (
    FakeDesktop,
    FakeNode,
    FakeTreeAdapter,
    FakeWindow,
    build_form_tree,
)


def main() -> None:
    desktop = FakeDesktop()
    desktop.add_window(FakeWindow("Batch Form", build_form_tree()))
    desktop.add_window(
        FakeWindow("Other Window", build_form_tree(prefix="other")),
        activate=False,
    )
    adapter = FakeTreeAdapter()

    with Workflow(
        adapter,
        root_provider=lambda: desktop.get_window("Batch Form").root,
        scope="demo-form",
    ) as wf:
        # 1. Snapshot: every node carries a scope/generation ref.
        snapshot = wf.snapshot()
        print("=== Snapshot (generation 1) ===")
        print(snapshot.to_yaml())

        save_refs = [
            node.ref
            for node in snapshot.root.iter_nodes()
            if node.role == "button" and node.name == "Save"
        ]
        print(f"\n{len(save_refs)} 'Save' buttons, {len(set(save_refs))} distinct refs")

        # 2. Click the inline Save specifically, never the toolbar/dialog one.
        inline_ref = next(
            node.ref
            for node in snapshot.root.iter_nodes()
            if node.node_id == "form/save-inline"
        )
        wf.click(inline_ref)
        inline = desktop.get_window("Batch Form").root.find_id("form/save-inline")
        toolbar = desktop.get_window("Batch Form").root.find_id("form/save-toolbar")
        print(f"\nRef click -> inline clicks: {inline.click_count}, "
              f"toolbar clicks: {toolbar.click_count}")

        # 3a. Target replaced in place (framework redraw) while generation is
        # still current: loud RefTargetReplacedError, no silent mis-click.
        root = desktop.get_window("Batch Form").root
        form = root.find_id("form/form")
        form.replace(
            root.find_id("form/save-inline"),
            FakeNode("button", "Save", node_id="form/save-inline-v2"),
        )
        try:
            wf.click(inline_ref)
        except RefTargetReplacedError as exc:
            print(f"\nReplaced target -> [{exc.code}] {exc}")

        # 3b. Re-snapshot: old generation is now expired.
        wf.snapshot()
        try:
            wf.click(inline_ref)
        except RefGenerationExpiredError as exc:
            print(f"Stale generation -> [{exc.code}]")

        # 4. Recover: fresh ref from the new snapshot works again.
        fresh = wf.snapshot()
        new_ref = next(
            node.ref
            for node in fresh.root.iter_nodes()
            if node.node_id == "form/save-inline-v2"
        )
        wf.click(new_ref)
        print("Recovered with a fresh snapshot; replacement clicked exactly once")

    # 5. Scopes isolate windows/workflows.
    with Workflow(
        adapter,
        root_provider=lambda: desktop.get_window("Other Window").root,
        scope="demo-other",
    ) as other:
        other.snapshot()
        try:
            other.click(new_ref)
        except RefScopeError as exc:
            print(f"\nCross-window ref -> [{exc.code}]")

    print("\nAll stable-ref guarantees demonstrated.")


if __name__ == "__main__":
    main()
