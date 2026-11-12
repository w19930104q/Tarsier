"""
Local Playwright regression tests for web stable refs.

Drives a real headless Chromium against HTML written to temp files, so the
suite is fully offline and deterministic. Covers duplicate controls, tab
switching, page refresh, redraw (node replacement), concurrent workflows
and every recognizable ref error.
"""

from pathlib import Path

import pytest

from tarsier.core.ref import (
    NodeRef,
    RefGenerationExpiredError,
    RefScopeError,
    RefTargetReplacedError,
    SCOPE_MANAGER,
)
from tarsier.core.web import WebDesktop

pytestmark = pytest.mark.playwright


FORM_HTML = """
<!doctype html>
<html><head><meta charset="utf-8"><title>Batch Form</title></head>
<body>
  <form id="entry">
    <label>Username <input id="username" type="text" aria-label="Username"></label>
    <label>Email <input id="email" type="text" aria-label="Email"></label>
    <!-- inline duplicate Save -->
    <button id="save-inline" type="button">Save</button>
  </form>
  <div id="toolbar">
    <button id="save-toolbar" type="button">Save</button>
  </div>
  <dialog id="confirm" open>
    <p>Save changes?</p>
    <button id="confirm-cancel" type="button">Cancel</button>
    <button id="confirm-save" type="button">Save</button>
  </dialog>
  <script>
    // Redraw helper: replace the inline Save with a brand-new node (new id),
    // reproducing a framework re-render of a same-named control.
    window.redrawInlineSave = function () {
      const old = document.getElementById('save-inline');
      const fresh = document.createElement('button');
      fresh.id = 'save-inline-v2';
      fresh.type = 'button';
      fresh.textContent = 'Save';
      old.replaceWith(fresh);
    };
    // An unload listener forces a non-bfcache navigation so reload() rebuilds
    // the DOM from source (and drops runtime data-tarsier-id stamps), matching
    // a real form-page refresh.
    window.addEventListener('unload', () => {});
  </script>
</body></html>
"""

SECOND_HTML = """
<!doctype html>
<html><head><meta charset="utf-8"><title>Second Page</title></head>
<body>
  <h1>Second tab</h1>
  <button id="ping" type="button">Ping</button>
  <input id="note" type="text" aria-label="Note">
</body></html>
"""


@pytest.fixture
def page_dir(tmp_path: Path) -> Path:
    (tmp_path / "form.html").write_text(FORM_HTML, encoding="utf-8")
    (tmp_path / "second.html").write_text(SECOND_HTML, encoding="utf-8")
    return tmp_path


@pytest.fixture
def web(page_dir):
    desk = WebDesktop(headless=True)
    desk.goto((page_dir / "form.html").as_uri())
    yield desk
    desk.close()


def _ref_for(snapshot, element_id):
    for node in snapshot.root.iter_nodes():
        if node.extra.get("dom_id") == element_id:
            return node.ref
    raise AssertionError(f"no snapshot node for {element_id}")


class TestWebSnapshot:
    def test_snapshot_refs_are_stable_and_unique_per_save(self, web):
        with web.start_workflow() as wf:
            snap = wf.snapshot()
            assert snap.generation == 1
            saves = [n for n in snap.root.iter_nodes()
                     if n.role == "button" and n.name == "Save"]
            # Three Save buttons (inline, toolbar, confirm) – distinct refs.
            assert len(saves) == 3
            assert len({n.ref for n in saves}) == 3
            for node in snap.root.iter_nodes():
                parsed = NodeRef.parse(node.ref)
                assert parsed.scope == wf.scope
                assert parsed.generation == 1

    def test_yaml_carries_ref_annotations(self, web):
        with web.start_workflow() as wf:
            text = wf.get_ui()
            assert "tarsier-ref/v1/" in text
            assert 'button "Save"' in text
            plain = wf.get_ui(include_refs=False)
            assert "tarsier-ref/" not in plain

    def test_legacy_role_name_path_still_works(self, web):
        with web.start_workflow() as wf:
            locator = wf.find(role="button", name="Ping")  # absent here
            assert locator.count() == 0
        # And the pre-workflow WebElement API is untouched.
        page = web.get_current_page()
        btn = page.button("Save")
        assert btn._locator.first.count() >= 1


class TestWebRefActions:
    def test_ref_click_hits_exact_duplicate_button(self, web):
        with web.start_workflow() as wf:
            snap = wf.snapshot()
            inline_ref = _ref_for(snap, "save-inline")
            toolbar_ref = _ref_for(snap, "save-toolbar")

            web.page.evaluate(
                "() => { document.getElementById('save-inline')."
                "addEventListener('click', () => window.__inline = (window.__inline||0)+1);"
                "document.getElementById('save-toolbar')."
                "addEventListener('click', () => window.__toolbar = (window.__toolbar||0)+1); }"
            )
            wf.click(inline_ref)
            wf.click(inline_ref)
            assert web.page.evaluate("() => window.__inline") == 2
            assert web.page.evaluate("() => window.__toolbar || 0") == 0

            wf.click(toolbar_ref)
            assert web.page.evaluate("() => window.__toolbar") == 1

    def test_ref_type_and_read(self, web):
        with web.start_workflow() as wf:
            snap = wf.snapshot()
            user_ref = _ref_for(snap, "username")
            email_ref = _ref_for(snap, "email")
            wf.type(user_ref, "alice")
            wf.type(email_ref, "a@example.com")
            assert web.page.input_value("#username") == "alice"
            assert web.page.input_value("#email") == "a@example.com"
            assert "alice" not in wf.read(email_ref)

    def test_dom_carries_tarsier_id_after_snapshot(self, web):
        with web.start_workflow() as wf:
            wf.snapshot()
            stamped = web.page.evaluate(
                "() => document.querySelectorAll('[data-tarsier-id]').length"
            )
            assert stamped >= 5


class TestWebRedrawAndRefresh:
    def test_redraw_replacing_node_fails_ref(self, web):
        with web.start_workflow() as wf:
            snap = wf.snapshot()
            old_ref = _ref_for(snap, "save-inline")
            # Framework re-render: same name/role, new DOM identity.
            web.page.evaluate("() => window.redrawInlineSave()")
            with pytest.raises(RefTargetReplacedError):
                wf.click(old_ref)

            # Fresh snapshot yields a working ref to the replacement.
            snap2 = wf.snapshot()
            new_ref = _ref_for(snap2, "save-inline-v2")
            wf.click(new_ref)  # must not raise

    def test_page_refresh_expires_every_ref(self, web):
        with web.start_workflow() as wf:
            snap = wf.snapshot()
            refs = [n.ref for n in snap.root.iter_nodes()]
            web.page.reload(wait_until="load")
            # Stamped data-tarsier-id attributes are gone with the DOM.
            for ref in refs:
                with pytest.raises(RefTargetReplacedError):
                    wf.click(ref)
            snap2 = wf.snapshot()
            assert snap2.generation == 2
            user_ref = _ref_for(snap2, "username")
            wf.type(user_ref, "after-reload")
            assert web.page.input_value("#username") == "after-reload"

    def test_resnapshot_generation_expiry(self, web):
        with web.start_workflow() as wf:
            snap1 = wf.snapshot()
            old_ref = _ref_for(snap1, "username")
            wf.snapshot()  # gen 2
            with pytest.raises(RefGenerationExpiredError):
                wf.type(old_ref, "stale")
            assert web.page.input_value("#username") == ""


class TestWebTabSwitching:
    def test_ref_cannot_cross_tabs(self, web, page_dir):
        with web.start_workflow(scope="web-tab-1") as wf:
            snap = wf.snapshot()
            form_ref = _ref_for(snap, "username")

            second = web.context.new_page()
            second.goto((page_dir / "second.html").as_uri())
            web.page = second  # workflow now resolves against the new active page

            # The node still lives in the first tab but is outside this
            # workflow's active page scope: recognizable out-of-scope error,
            # never a silent click on a second-page element.
            from tarsier.core.ref import RefOutOfScopeError

            with pytest.raises(RefOutOfScopeError):
                wf.click(form_ref)

            # A fresh workflow on the second tab works independently.
            with web.start_workflow(scope="web-tab-2") as wf2:
                snap2 = wf2.snapshot()
                ping_ref = _ref_for(snap2, "ping")
                wf2.click(ping_ref)
                # And the first workflow's ref is unknown to the second scope.
                with pytest.raises(RefScopeError):
                    wf2.click(form_ref)
            second.close()

    def test_closed_page_is_reported_as_out_of_scope(self, web, page_dir):
        from tarsier.core.ref import RefOutOfScopeError

        wf = web.start_workflow(scope="web-close")
        wf.snapshot()
        web.page.close()
        with pytest.raises(RefOutOfScopeError):
            wf.snapshot()
        wf.close()


class TestWebConcurrentWorkflows:
    def test_concurrent_workflows_on_separate_pages(self, web, page_dir):
        # Playwright's sync API is not thread-safe by design, so two workflows
        # drive two pages interleaved on one thread; the point under test is
        # registry/generation isolation between concurrently-open scopes.
        second = web.context.new_page()
        second.goto((page_dir / "second.html").as_uri())
        first_page = web.context.pages[0]

        local_a = WebDesktop.__new__(WebDesktop)
        local_a.page = first_page
        local_a.context = web.context
        local_a.highlight_actions = False

        local_b = WebDesktop.__new__(WebDesktop)
        local_b.page = second
        local_b.context = web.context
        local_b.highlight_actions = False

        with local_a.start_workflow(scope="c-web-1") as wa, \
             local_b.start_workflow(scope="c-web-2") as wb:
            assert SCOPE_MANAGER.get("c-web-1") is not None
            assert SCOPE_MANAGER.get("c-web-2") is not None

            sa = wa.snapshot()
            sb = wb.snapshot()
            ref_user = _ref_for(sa, "username")
            ref_note = _ref_for(sb, "note")

            # Cross-scope refs are always rejected.
            with pytest.raises(RefScopeError):
                wb.click(ref_user)
            with pytest.raises(RefScopeError):
                wa.click(ref_note)

            # Independent generations: bumping A never invalidates B's refs.
            wa.snapshot()
            wb.type(ref_note, "n2")
            assert second.input_value("#note") == "n2"
            with pytest.raises(RefGenerationExpiredError):
                wa.type(ref_user, "stale")
            fresh_a = wa.snapshot()
            wa.type(_ref_for(fresh_a, "username"), "u1")
            assert first_page.input_value("#username") == "u1"
        second.close()

    def test_two_workflows_same_page_isolated_generations(self, web):
        with web.start_workflow(scope="iso-a") as a, web.start_workflow(scope="iso-b") as b:
            sa = a.snapshot()
            b.snapshot()
            a.snapshot()  # gen 2 for A only; B stays on gen 1
            ref_a = _ref_for(sa, "username")
            # A's gen-1 ref is expired inside A but rejected by scope inside B.
            with pytest.raises(RefGenerationExpiredError):
                a.click(ref_a)
            with pytest.raises(RefScopeError):
                b.click(ref_a)
