"""
Regression tests for stable refs on the fake accessibility tree.

Covers the failure modes from the batch-form bug report:
duplicate controls, window switching, redraw/refresh, concurrent workflows
and expired/replaced/out-of-scope refs.  Everything runs in-process against
:mod:`tarsier.testing.fake_a11y` – no display server required.
"""

import threading

import pytest

from tarsier.core.ref import (
    NodeRef,
    RefFormatError,
    RefGenerationExpiredError,
    RefOutOfScopeError,
    RefScopeError,
    RefTargetReplacedError,
    SCOPE_MANAGER,
)
from tarsier.core.workflow import Workflow
from tarsier.testing import (
    FakeDesktop,
    FakeNode,
    FakeTreeAdapter,
    FakeWindow,
    build_form_tree,
)


@pytest.fixture
def desktop():
    desk = FakeDesktop()
    desk.add_window(FakeWindow("Batch Form", build_form_tree(prefix="form")))
    desk.add_window(
        FakeWindow("Other Window", build_form_tree(prefix="other")), activate=False
    )
    return desk


@pytest.fixture
def adapter():
    return FakeTreeAdapter()


def _workflow(desktop, adapter, title="Batch Form", scope=None):
    return Workflow(
        adapter,
        root_provider=lambda: desktop.get_window(title).root,
        scope=scope,
    )


# --------------------------------------------------------------------------- #
# Snapshot / ref structure
# --------------------------------------------------------------------------- #
class TestSnapshotRefs:
    def test_ref_encoding_carries_scope_generation_node(self, desktop, adapter):
        with _workflow(desktop, adapter, scope="wf-A") as wf:
            snap = wf.snapshot()
            assert snap.scope == "wf-A"
            assert snap.generation == 1
            for node in snap.root.iter_nodes():
                parsed = NodeRef.parse(node.ref)
                assert parsed.scope == "wf-A"
                assert parsed.generation == 1
                assert parsed.node == node.node_id

    def test_yaml_annotations_match_dict_tree(self, desktop, adapter):
        with _workflow(desktop, adapter) as wf:
            snap = wf.snapshot()
            yaml_refs = {
                line.split("# ", 1)[1].strip()
                for line in snap.yaml_text.splitlines()
                if "# tarsier-ref/" in line
            }
            dict_refs = {n.ref for n in snap.root.iter_nodes()}
            assert yaml_refs == dict_refs

    def test_legacy_yaml_without_refs(self, desktop, adapter):
        with _workflow(desktop, adapter) as wf:
            snap = wf.snapshot()
            plain = snap.to_yaml(include_refs=False)
            assert "tarsier-ref/" not in plain
            assert '- button "Save"' in plain

    def test_yaml_strip_keeps_hash_inside_names(self, adapter):
        # A node name containing '#' must survive ref-annotation stripping.
        root = FakeNode("window", "Editor", node_id="h/window")
        root.add(FakeNode("button", "Save #1", node_id="h/save"))
        desk = FakeDesktop()
        desk.add_window(FakeWindow("Hash", root))
        with Workflow(adapter, lambda: desk.get_window("Hash").root) as wf:
            snap = wf.snapshot()
            plain = snap.to_yaml(include_refs=False)
            assert 'button "Save #1"' in plain
            assert "tarsier-ref/" not in plain
            # And the annotated form still parses to the same node id.
            annotated = snap.to_yaml()
            ref = next(
                line.split("# ", 1)[1].strip()
                for line in annotated.splitlines()
                if "h/save" in line
            )
            assert NodeRef.parse(ref).node == "h/save"

    def test_snapshot_dict_is_json_safe(self, desktop, adapter):
        import json

        with _workflow(desktop, adapter) as wf:
            payload = json.dumps(wf.snapshot().to_dict())
            assert "tarsier-ref/v1/" in payload

    def test_yaml_is_pre_order_parent_before_children(self, desktop, adapter):
        with _workflow(desktop, adapter) as wf:
            lines = [
                line for line in wf.get_ui().splitlines()
                if "tarsier-ref/" in line
            ]
        # The window root must be emitted first, its descendants after it.
        assert "form/window" in lines[0]
        # A child always appears below its parent in the listing.
        top = next(i for i, line in enumerate(lines) if "form/toolbar" in line)
        child = next(i for i, line in enumerate(lines) if "save-toolbar" in line)
        assert top < child
        # Every line's first token is the bullet; the root role is "window".
        assert lines[0].strip().split(" ", 2)[1] == "window"


# --------------------------------------------------------------------------- #
# Duplicate controls
# --------------------------------------------------------------------------- #
class TestDuplicateControls:
    def test_duplicate_save_buttons_get_distinct_refs(self, desktop, adapter):
        with _workflow(desktop, adapter) as wf:
            snap = wf.snapshot()
            saves = [n for n in snap.root.iter_nodes()
                     if n.role == "button" and n.name == "Save"]
            assert len(saves) == 4
            assert len({n.ref for n in saves}) == 4

    def test_ref_clicks_target_only_its_own_control(self, desktop, adapter):
        with _workflow(desktop, adapter) as wf:
            snap = wf.snapshot()
            root = desktop.get_window("Batch Form").root
            inline = root.find_id("form/save-inline")
            toolbar = root.find_id("form/save-toolbar")
            confirm = root.find_id("form/confirm-save")
            inline_ref = next(
                n.ref for n in snap.root.iter_nodes()
                if n.node_id == "form/save-inline"
            )
            wf.click(inline_ref)
            assert inline.click_count == 1
            assert toolbar.click_count == 0
            assert confirm.click_count == 0

            # role/name first-match would hit a different Save; the ref never does.
            wf.click(inline_ref)
            wf.click(inline_ref)
            assert inline.click_count == 3
            assert toolbar.click_count == 0

    def test_ref_type_and_read_on_textbox(self, desktop, adapter):
        with _workflow(desktop, adapter) as wf:
            snap = wf.snapshot()
            user_ref = next(
                n.ref for n in snap.root.iter_nodes() if n.node_id == "form/username"
            )
            wf.type(user_ref, "alice@example.com")
            assert wf.read(user_ref) == "alice@example.com"
            email = desktop.get_window("Batch Form").root.find_id("form/email")
            assert email.value == ""  # no cross-talk between same-role fields

    def test_malformed_ref_is_rejected(self, desktop, adapter):
        with _workflow(desktop, adapter) as wf:
            wf.snapshot()
            with pytest.raises(RefFormatError) as exc:
                wf.click("not-a-ref")
            assert exc.value.code == "ref_format_error"

    def test_tampered_generation_rejected(self, desktop, adapter):
        with _workflow(desktop, adapter) as wf:
            snap = wf.snapshot()
            bad = snap.root.ref.replace("/1/", "/99/")
            with pytest.raises(RefGenerationExpiredError):
                wf.click(bad)


# --------------------------------------------------------------------------- #
# Generations / redraws
# --------------------------------------------------------------------------- #
class TestGenerationsAndRedraw:
    def test_resnapshot_bumps_generation_and_expires_old_ref(self, desktop, adapter):
        with _workflow(desktop, adapter) as wf:
            snap1 = wf.snapshot()
            old_ref = next(
                n.ref for n in snap1.root.iter_nodes() if n.node_id == "form/username"
            )
            snap2 = wf.snapshot()
            assert snap2.generation == 2
            with pytest.raises(RefGenerationExpiredError) as exc:
                wf.click(old_ref)
            assert exc.value.code == "ref_generation_expired"
            assert "fresh snapshot" in str(exc.value)

    def test_target_replaced_after_redraw(self, desktop, adapter):
        with _workflow(desktop, adapter) as wf:
            snap = wf.snapshot()
            old_ref = next(
                n.ref for n in snap.root.iter_nodes()
                if n.node_id == "form/save-inline"
            )
            # Simulate React-style redraw: same position/name, new node id.
            root = desktop.get_window("Batch Form").root
            form = root.find_id("form/form")
            old_btn = root.find_id("form/save-inline")
            new_btn = FakeNode("button", "Save", node_id="form/save-inline-v2")
            form.replace(old_btn, new_btn)

            # Same generation, live re-resolution: the replacement must be
            # detected loudly rather than silently clicked.
            with pytest.raises(RefTargetReplacedError) as exc:
                wf.click(old_ref)
            assert exc.value.code == "ref_target_replaced"
            assert new_btn.click_count == 0  # never silently clicked the replacement

            # After re-snapshot the old ref is generation-expired as well, and
            # the fresh ref targets the replacement node explicitly.
            snap2 = wf.snapshot()
            with pytest.raises(RefGenerationExpiredError):
                wf.click(old_ref)
            new_ref = next(
                n.ref for n in snap2.root.iter_nodes()
                if n.node_id == "form/save-inline-v2"
            )
            wf.click(new_ref)
            assert new_btn.click_count == 1

    def test_full_refresh_replaces_all_ids(self, desktop, adapter):
        with _workflow(desktop, adapter) as wf:
            snap1 = wf.snapshot()
            refs = [n.ref for n in snap1.root.iter_nodes()]
            # Full page refresh: brand-new tree, brand-new ids. Even without a
            # new snapshot the live tree no longer contains any stamped id.
            fresh_root = build_form_tree(prefix="redrawn")
            desktop.get_window("Batch Form").redraw(fresh_root)
            for ref in refs:
                with pytest.raises(RefTargetReplacedError):
                    wf.click(ref)

            # New snapshot's refs work on the new tree.
            snap2 = wf.snapshot()
            new_save = next(
                n.ref for n in snap2.root.iter_nodes()
                if n.node_id == "redrawn/save-inline"
            )
            wf.click(new_save)
            assert fresh_root.find_id("redrawn/save-inline").click_count == 1

    def test_removed_node_fails_target_replaced(self, desktop, adapter):
        with _workflow(desktop, adapter) as wf:
            snap1 = wf.snapshot()
            ref = next(
                n.ref for n in snap1.root.iter_nodes()
                if n.node_id == "form/confirm-cancel"
            )
            root = desktop.get_window("Batch Form").root
            confirm = root.find_id("form/confirm")
            confirm.remove(root.find_id("form/confirm-cancel"))
            with pytest.raises(RefTargetReplacedError):
                wf.click(ref)


# --------------------------------------------------------------------------- #
# Window switching / scopes
# --------------------------------------------------------------------------- #
class TestWindowSwitching:
    def test_ref_scope_isolation_between_windows(self, desktop, adapter):
        with _workflow(desktop, adapter, title="Batch Form", scope="wf-form") as wf_a:
            snap_a = wf_a.snapshot()
            ref_a = next(
                n.ref for n in snap_a.root.iter_nodes()
                if n.node_id == "form/save-toolbar"
            )
            # A second workflow bound to another window.
            with _workflow(
                desktop, adapter, title="Other Window", scope="wf-other"
            ) as wf_b:
                snap_b = wf_b.snapshot()
                ref_b = next(
                    n.ref for n in snap_b.root.iter_nodes()
                    if n.node_id == "other/username"
                )
                # Each workflow only accepts refs minted in its own scope.
                with pytest.raises(RefScopeError) as exc:
                    wf_b.click(ref_a)
                assert exc.value.code == "ref_scope_error"
                with pytest.raises(RefScopeError):
                    wf_a.click(ref_b)

                # Both refs remain independently usable inside their own scope.
                wf_a.click(ref_a)
                assert (
                    desktop.get_window("Batch Form")
                    .root.find_id("form/save-toolbar")
                    .click_count == 1
                )

    def test_scope_root_switch_makes_node_out_of_scope(self, desktop, adapter):
        # Workflow root provider follows the *active* window. A ref taken on
        # window 1 must not resolve after the provider starts returning window 2.
        with Workflow(
            adapter,
            root_provider=lambda: desktop.get_window(desktop.active_window).root,
            scope="wf-switch",
        ) as wf:
            snap = wf.snapshot()
            ref = next(
                n.ref for n in snap.root.iter_nodes()
                if n.node_id == "form/username"
            )
            desktop.switch_to("Other Window")
            # Node id simply doesn't exist in the new scope root.
            with pytest.raises(RefTargetReplacedError):
                wf.click(ref)

    def test_closed_workflow_rejects_refs(self, desktop, adapter):
        wf = _workflow(desktop, adapter, scope="wf-closed")
        snap = wf.snapshot()
        ref = snap.root.ref
        wf.close()
        with pytest.raises(RefScopeError):
            wf.click(ref)
        # Registry removed from the global table as well.
        with pytest.raises(RefScopeError):
            SCOPE_MANAGER.get("wf-closed")


# --------------------------------------------------------------------------- #
# Concurrent workflows
# --------------------------------------------------------------------------- #
class TestConcurrentWorkflows:
    def test_concurrent_workflows_have_independent_generations(self, desktop, adapter):
        errors = []

        def worker(title, prefix, scope):
            try:
                with _workflow(desktop, adapter, title=title, scope=scope) as wf:
                    for _ in range(3):
                        snap = wf.snapshot()
                        target = next(
                            n.ref for n in snap.root.iter_nodes()
                            if n.node_id == f"{prefix}/username"
                        )
                        wf.type(target, f"{scope}-value")
                        assert wf.read(target) == f"{scope}-value"
                    assert wf.registry.generation == 3
            except Exception as exc:  # pragma: no cover - failure reporter
                errors.append(exc)

        t1 = threading.Thread(
            target=worker, args=("Batch Form", "form", "wf-conc-1")
        )
        t2 = threading.Thread(
            target=worker, args=("Other Window", "other", "wf-conc-2")
        )
        t1.start(); t2.start(); t1.join(); t2.join()
        assert errors == []

        # Context managers close workflows on exit, so both scopes are dropped
        # from the global table without interfering with each other.
        assert "wf-conc-1" not in SCOPE_MANAGER._scopes
        assert "wf-conc-2" not in SCOPE_MANAGER._scopes

    def test_cross_workflow_ref_never_resolves(self, desktop, adapter):
        with _workflow(desktop, adapter, scope="wf-x") as wf_x, \
             _workflow(desktop, adapter, title="Other Window", scope="wf-y") as wf_y:
            snap_x = wf_x.snapshot()
            wf_y.snapshot()
            ref_x = next(iter(snap_x.nodes_by_id.values())).ref
            with pytest.raises(RefScopeError):
                wf_y.click(ref_x)

    def test_duplicate_explicit_scope_rejected(self, desktop, adapter):
        _workflow(desktop, adapter, scope="dup").__enter__()
        try:
            with pytest.raises(RefScopeError):
                _workflow(desktop, adapter, scope="dup")
        finally:
            SCOPE_MANAGER.drop_scope("dup")


# --------------------------------------------------------------------------- #
# Legacy role/name compatibility
# --------------------------------------------------------------------------- #
class TestLegacyCompatibility:
    def test_role_name_find_still_works(self, desktop, adapter):
        with _workflow(desktop, adapter) as wf:
            node = wf.find(role="textbox", name="Username")
            assert node.node_id == "form/username"

    def test_role_name_find_raises_when_absent(self, desktop, adapter):
        with _workflow(desktop, adapter) as wf:
            with pytest.raises(ValueError):
                wf.find(role="button", name="Nonexistent")

    def test_ref_error_hierarchy_and_codes(self, desktop, adapter):
        from tarsier.core.ref import TarsierRefError

        for exc_cls, code in [
            (RefFormatError, "ref_format_error"),
            (RefScopeError, "ref_scope_error"),
            (RefGenerationExpiredError, "ref_generation_expired"),
            (RefTargetReplacedError, "ref_target_replaced"),
            (RefOutOfScopeError, "ref_out_of_scope"),
        ]:
            assert issubclass(exc_cls, TarsierRefError)
            assert exc_cls.code == code
