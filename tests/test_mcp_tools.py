"""
Tests for the MCP tool surface.

The real Desktop/WebDesktop cannot be instantiated in CI (no display server
/ native OS APIs), so workflows are injected directly into the server's
workflow table.  These tests pin the externally visible contract:

* ref-bearing tools return recognizable ``[ref_*]`` error strings
* legacy role/name calls still flow through the old code path
* snapshot output can optionally carry refs
"""

import pytest

from tarsier.core import ref as ref_mod
from tarsier.mcp import server
from tarsier.core.workflow import Workflow
from tarsier.testing import FakeDesktop, FakeTreeAdapter, FakeWindow, build_form_tree


class _FakeDesktopBackend:
    """Minimal stand-in for the platform Desktop used by the MCP server."""

    def __init__(self, fake_desktop):
        self._fake = fake_desktop
        self.highlight_actions = False

    def get_window(self, name):
        window = self._fake.get_window(name)

        class _WindowHandle:
            def __init__(self, root):
                self._backend = type("_B", (), {"_control": root})()
                self.name = name

            def find(self, role=None, name=None):
                from collections import deque

                root = self._backend._control
                q = deque([root])
                while q:
                    cur = q.popleft()
                    if cur is not root and cur.role == role and cur.name == name:
                        return _Element(cur)
                    q.extend(cur.children)
                raise ValueError(f"not found {role}/{name}")

        class _Element:
            def __init__(self, node):
                self.node = node

            def click(self):
                self.node.click()

            def type(self, text):
                self.node.type(text)

            def read(self):
                return self.node.read()

        return _WindowHandle(window.root)

    def start_workflow(self, window_name, scope=None):
        self.get_window(window_name)  # mirror fail-fast behavior
        fake = self._fake

        class _DesktopWorkflow:
            def __init__(self):
                self.closed = False
                self._wf = Workflow(
                    FakeTreeAdapter(),
                    root_provider=lambda: fake.get_window(window_name).root,
                    scope=scope,
                )
                self.scope = self._wf.scope

            def get_ui(self, include_refs=True, max_depth=15):
                return self._wf.get_ui(include_refs=include_refs, max_depth=max_depth)

            def click(self, ref):
                self._wf.click(ref)

            def type(self, ref, text):
                self._wf.type(ref, text)

            def read(self, ref):
                return self._wf.read(ref)

            def close(self):
                self._wf.close()
                self.closed = True

        return _DesktopWorkflow()


@pytest.fixture(autouse=True)
def _reset_server():
    server._workflows.clear()
    ref_mod.SCOPE_MANAGER._scopes.clear()
    ref_mod.SCOPE_MANAGER._counter.__init__(1)
    yield
    server._workflows.clear()
    ref_mod.SCOPE_MANAGER._scopes.clear()


@pytest.fixture
def fake_desktop(monkeypatch):
    desk = FakeDesktop()
    desk.add_window(FakeWindow("Batch Form", build_form_tree()))
    backend = _FakeDesktopBackend(desk)
    monkeypatch.setattr(server, "desktop", backend)
    return desk


class TestMcpDesktopWorkflowTools:
    def test_start_snapshot_click_happy_path(self, fake_desktop):
        msg = server.desktop_start_workflow("Batch Form")
        assert "scope=" in msg
        scope = msg.split("scope=")[1].split()[0]

        yaml_text = server.desktop_snapshot(scope)
        assert "tarsier-ref/v1/" in yaml_text

        # Grab the inline Save ref from the annotated snapshot.
        inline_ref = next(
            line.split("# ", 1)[1].strip()
            for line in yaml_text.splitlines()
            if "save-inline" in line
        )
        result = server.ref_click(scope, inline_ref)
        assert "Successfully clicked" in result
        assert (
            fake_desktop.get_window("Batch Form")
            .root.find_id("form/save-inline")
            .click_count == 1
        )

        # include_refs=False preserves the legacy plain YAML (new generation,
        # so do this after the ref actions above).
        plain = server.desktop_snapshot(scope, include_refs=False)
        assert "tarsier-ref/" not in plain

    def test_type_and_read_via_ref(self, fake_desktop):
        scope = server.desktop_start_workflow("Batch Form").split("scope=")[1].split()[0]
        yaml_text = server.desktop_snapshot(scope)
        user_ref = next(
            line.split("# ", 1)[1].strip()
            for line in yaml_text.splitlines()
            if "form/username" in line
        )
        assert "Successfully typed" in server.ref_type(scope, user_ref, "bob")
        assert server.ref_read_text(scope, user_ref) == "bob"

    def test_expired_ref_returns_machine_readable_code(self, fake_desktop):
        scope = server.desktop_start_workflow("Batch Form").split("scope=")[1].split()[0]
        first = server.desktop_snapshot(scope)
        old_ref = next(
            line.split("# ", 1)[1].strip()
            for line in first.splitlines()
            if "form/username" in line
        )
        server.desktop_snapshot(scope)  # second generation
        result = server.ref_click(scope, old_ref)
        assert result.startswith("[ref_generation_expired]")

    def test_unknown_scope_is_reported(self, fake_desktop):
        result = server.ref_click("nope", "tarsier-ref/v1/nope/1/x")
        assert "[ref_scope_error]" in result or "Unknown or closed workflow scope" in result

    def test_malformed_ref_error_code(self, fake_desktop):
        scope = server.desktop_start_workflow("Batch Form").split("scope=")[1].split()[0]
        server.desktop_snapshot(scope)
        result = server.ref_click(scope, "garbage")
        assert result.startswith("[ref_format_error]")

    def test_end_workflow_releases_scope(self, fake_desktop):
        scope = server.desktop_start_workflow("Batch Form").split("scope=")[1].split()[0]
        assert server.end_workflow(scope).startswith("Workflow")
        result = server.desktop_snapshot(scope)
        assert "Unknown or closed workflow scope" in result


class TestMcpLegacyCompatibility:
    def test_legacy_role_name_click_still_works(self, fake_desktop):
        # No workflow involved at all – the original code path is unchanged.
        result = server.desktop_click(
            window_name="Batch Form", role="button", name="Save"
        )
        assert "Successfully clicked the 'Save' button." == result

    def test_ref_and_scope_on_legacy_tool_overrides_role_name(self, fake_desktop):
        scope = server.desktop_start_workflow("Batch Form").split("scope=")[1].split()[0]
        yaml_text = server.desktop_snapshot(scope)
        toolbar_ref = next(
            line.split("# ", 1)[1].strip()
            for line in yaml_text.splitlines()
            if "save-toolbar" in line
        )
        # Even though role/name would match the first Save, the ref wins.
        result = server.desktop_click(
            window_name="Batch Form",
            role="button",
            name="Save",
            ref=toolbar_ref,
            scope=scope,
        )
        assert "Successfully clicked ref" in result
        assert (
            fake_desktop.get_window("Batch Form")
            .root.find_id("form/save-toolbar")
            .click_count == 1
        )

    def test_ref_without_scope_is_usage_error(self, fake_desktop):
        result = server.desktop_click(
            window_name="Batch Form", role="button", name="Save",
            ref="tarsier-ref/v1/x/1/y",
        )
        assert "'scope' is required" in result

    def test_legacy_type_and_read(self, fake_desktop):
        typed = server.desktop_type(
            "Batch Form", "textbox", "Username", "legacy-value"
        )
        assert "Successfully typed" in typed
        content = server.desktop_read_text("Batch Form", "textbox", "Username")
        assert content == "legacy-value"
