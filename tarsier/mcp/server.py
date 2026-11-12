import sys
import os

# Ensure tarsier is in the python path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from typing import Optional
from mcp.server.fastmcp import FastMCP
from tarsier import Desktop
from tarsier.core.ref import TarsierRefError

# Initialize FastMCP Server
mcp = FastMCP("Tarsier Automation")
desktop = Desktop()
web_desktop = None

# Active stable-ref workflows, keyed by scope id.
# Each entry is tagged with its platform ("desktop" | "web").
_workflows = {}


def get_web(headless: bool = False):
    """Lazy loads the WebDesktop so Chromium isn't started unless requested."""
    global web_desktop
    if not web_desktop:
        from tarsier import WebDesktop
        web_desktop = WebDesktop(headless=headless)
    return web_desktop


def _register(workflow, platform: str):
    _workflows[workflow.scope] = (workflow, platform)
    return workflow


def _get_workflow(scope: str):
    entry = _workflows.get(scope)
    if entry is None:
        raise TarsierRefError(
            f"Unknown or closed workflow scope {scope!r}. Start a workflow and "
            f"take a snapshot to obtain fresh refs"
        )
    workflow, _platform = entry
    if workflow.closed:
        raise TarsierRefError(
            f"Workflow scope {scope!r} is closed. Start a workflow and take a "
            f"fresh snapshot to obtain new refs"
        )
    return workflow


def _ref_error(exc: Exception) -> str:
    """Render a ref failure with its stable machine-readable code."""
    if isinstance(exc, TarsierRefError):
        return f"[{exc.code}] {exc}"
    return f"Error: {exc}"


# ==========================================
# STABLE-REF WORKFLOW TOOLS
# ==========================================
@mcp.tool()
def desktop_start_workflow(window_name: str) -> str:
    """
    Opens a stable-ref workflow bound to a desktop window.

    Refs returned by desktop_snapshot are only valid inside this workflow and
    the snapshot generation that produced them. After any redraw/dialog take
    a new snapshot and use the new refs.

    Args:
        window_name: The window title to bind the workflow to.
    """
    try:
        workflow = _register(desktop.start_workflow(window_name), "desktop")
        return (
            f"Workflow started. scope={workflow.scope} window={window_name!r}. "
            f"Call desktop_snapshot with this scope to get refs."
        )
    except Exception as e:
        return f"Error starting workflow: {e}"


@mcp.tool()
def desktop_snapshot(scope: str, include_refs: bool = True) -> str:
    """
    Takes a generationed semantic snapshot of the workflow's window.

    Each node line carries a stable '# tarsier-ref/v1/<scope>/<generation>/<node>'
    annotation when include_refs is true. Taking a new snapshot bumps the
    generation and invalidates refs from earlier snapshots.

    Args:
        scope: Workflow scope id from desktop_start_workflow.
        include_refs: If false, returns plain YAML without ref annotations.
    """
    try:
        workflow = _get_workflow(scope)
        return workflow.get_ui(include_refs=include_refs)
    except Exception as e:
        return _ref_error(e)


@mcp.tool()
def web_start_workflow() -> str:
    """
    Opens a stable-ref workflow bound to the currently active browser page.
    Refs cannot cross tabs; call web_start_workflow again after switching tabs.
    """
    try:
        web = get_web()
        workflow = _register(web.start_workflow(), "web")
        return f"Web workflow started. scope={workflow.scope}."
    except Exception as e:
        return f"Error starting web workflow: {e}"


@mcp.tool()
def web_snapshot(scope: str, include_refs: bool = True) -> str:
    """
    Takes a generationed ARIA snapshot of the workflow's web page, with stable
    tarsier-ref annotations when include_refs is true.

    Args:
        scope: Workflow scope id from web_start_workflow.
        include_refs: If false, returns plain YAML without ref annotations.
    """
    try:
        workflow = _get_workflow(scope)
        return workflow.get_ui(include_refs=include_refs)
    except Exception as e:
        return _ref_error(e)


@mcp.tool()
def end_workflow(scope: str) -> str:
    """
    Closes a workflow and releases its ref registry. Refs from a closed
    workflow can no longer be resolved (ref_scope_error).

    Args:
        scope: Workflow scope id to close.
    """
    entry = _workflows.pop(scope, None)
    if entry is None:
        return f"No active workflow with scope {scope!r}."
    workflow, _platform = entry
    workflow.close()
    return f"Workflow {scope!r} closed."


@mcp.tool()
def ref_click(scope: str, ref: str) -> str:
    """
    Clicks the exact node identified by a stable tarsier ref.

    Fails with a recognizable [ref_*] code (ref_generation_expired,
    ref_target_replaced, ref_out_of_scope, ref_scope_error, ref_format_error)
    instead of falling back to a role/name match.

    Args:
        scope: Workflow scope the ref belongs to.
        ref: Full 'tarsier-ref/v1/...' string from a snapshot.
    """
    try:
        workflow = _get_workflow(scope)
        workflow.click(ref)
        return f"Successfully clicked {ref}."
    except Exception as e:
        return _ref_error(e)


@mcp.tool()
def ref_type(scope: str, ref: str, text: str) -> str:
    """
    Types text into the exact input node identified by a stable tarsier ref.

    Args:
        scope: Workflow scope the ref belongs to.
        ref: Full 'tarsier-ref/v1/...' string from a snapshot.
        text: Text to input.
    """
    try:
        workflow = _get_workflow(scope)
        workflow.type(ref, text)
        return f"Successfully typed into {ref}."
    except Exception as e:
        return _ref_error(e)


@mcp.tool()
def ref_read_text(scope: str, ref: str) -> str:
    """
    Reads the textual content of the exact node identified by a stable ref.

    Args:
        scope: Workflow scope the ref belongs to.
        ref: Full 'tarsier-ref/v1/...' string from a snapshot.
    """
    try:
        workflow = _get_workflow(scope)
        return workflow.read(ref)
    except Exception as e:
        return _ref_error(e)


# ==========================================
# DESKTOP AUTOMATION TOOLS (legacy role/name, unchanged)
# ==========================================
@mcp.tool()
def desktop_open_app(executable: str, window_name: str = None) -> str:
    """
    Opens a desktop application and attaches to it.

    Args:
        executable: The name of the executable (e.g. 'notepad.exe', 'calc.exe', 'code')
        window_name: (Optional) The specific window title to attach to. If None, it grabs the active window.
    """
    try:
        app = desktop.open_app(executable, window_name)
        return f"Successfully opened app and attached to window: '{app.name}'"
    except Exception as e:
        return f"Error opening app: {e}"

@mcp.tool()
def desktop_get_ui(window_name: str) -> str:
    """
    Retrieves the entire semantic UI tree (Desktop DOM) for a specific window in YAML format.
    Use this to see what buttons, textboxes, and menus are currently on the screen.

    Args:
        window_name: The name of the window to inspect.
    """
    try:
        window = desktop.get_window(window_name)
        return window.to_yaml_snapshot()
    except Exception as e:
        return f"Error getting UI: {e}"

@mcp.tool()
def desktop_click(window_name: str, role: str, name: str, ref: Optional[str] = None, scope: Optional[str] = None) -> str:
    """
    Semantically clicks a UI element inside a window.

    Legacy role/name usage (first match) stays supported. When ref + scope are
    supplied, the exact snapshot node is clicked and stale/ambiguous refs fail
    with a [ref_*] code instead of touching another control.

    Args:
        window_name: The name of the window containing the element.
        role: The semantic role of the element (e.g. 'button', 'menuitem', 'tab').
        name: The exact semantic name of the element (e.g. 'Save', 'File').
        ref: Optional stable tarsier ref; when given, role/name are ignored.
        scope: Workflow scope required when ref is given.
    """
    try:
        if ref:
            if not scope:
                return "Error: 'scope' is required when 'ref' is provided."
            workflow = _get_workflow(scope)
            workflow.click(ref)
            return f"Successfully clicked ref {ref}."
        window = desktop.get_window(window_name)
        element = window.find(role=role, name=name)
        element.click()
        return f"Successfully clicked the '{name}' {role}."
    except Exception as e:
        return _ref_error(e)

@mcp.tool()
def desktop_right_click(window_name: str, role: str, name: str) -> str:
    """
    Semantically finds a UI element inside a window and right-clicks it.

    Args:
        window_name: The name of the window containing the element.
        role: The semantic role of the element (e.g. 'button', 'menuitem', 'tab').
        name: The exact semantic name of the element (e.g. 'Save', 'File').
    """
    try:
        window = desktop.get_window(window_name)
        element = window.find(role=role, name=name)
        element.right_click()
        return f"Successfully right-clicked the '{name}' {role}."
    except Exception as e:
        return f"Error right-clicking element: {e}"

@mcp.tool()
def desktop_hover(window_name: str, role: str, name: str) -> str:
    """
    Semantically finds a UI element inside a window and hovers the mouse cursor over it.

    Args:
        window_name: The name of the window containing the element.
        role: The semantic role of the element.
        name: The exact semantic name of the element.
    """
    try:
        window = desktop.get_window(window_name)
        element = window.find(role=role, name=name)
        element.hover()
        return f"Successfully hovered over the '{name}' {role}."
    except Exception as e:
        return f"Error hovering element: {e}"

@mcp.tool()
def desktop_type(window_name: str, role: str, name: str, text: str, ref: Optional[str] = None, scope: Optional[str] = None) -> str:
    """
    Semantically finds a textbox or input field and types text into it.

    Legacy role/name usage stays supported. When ref + scope are supplied the
    exact snapshot node is targeted and stale refs fail with a [ref_*] code.

    Args:
        window_name: The name of the window containing the element.
        role: The semantic role of the element (e.g. 'document', 'edit').
        name: The name of the element.
        text: The text to inject.
        ref: Optional stable tarsier ref; when given, role/name are ignored.
        scope: Workflow scope required when ref is given.
    """
    try:
        if ref:
            if not scope:
                return "Error: 'scope' is required when 'ref' is provided."
            workflow = _get_workflow(scope)
            workflow.type(ref, text)
            return f"Successfully typed into ref {ref}."
        window = desktop.get_window(window_name)
        element = window.find(role=role, name=name)
        element.type(text)
        return f"Successfully typed text into the '{name}' {role}."
    except Exception as e:
        return _ref_error(e)

@mcp.tool()
def desktop_read_text(window_name: str, role: str, name: str, ref: Optional[str] = None, scope: Optional[str] = None) -> str:
    """
    Reads and returns the textual contents of a specific UI element.

    Legacy role/name usage stays supported. When ref + scope are supplied the
    exact snapshot node is read.

    Args:
        window_name: The name of the window containing the element.
        role: The semantic role of the element (e.g. 'document', 'edit').
        name: The exact semantic name of the element.
        ref: Optional stable tarsier ref; when given, role/name are ignored.
        scope: Workflow scope required when ref is given.
    """
    try:
        if ref:
            if not scope:
                return "Error: 'scope' is required when 'ref' is provided."
            workflow = _get_workflow(scope)
            return workflow.read(ref)
        window = desktop.get_window(window_name)
        element = window.find(role=role, name=name)
        content = element.read()
        return content
    except Exception as e:
        return _ref_error(e)

@mcp.tool()
def desktop_hotkey(keys: str) -> str:
    """
    Sends a global OS keyboard shortcut or hotkey.

    Args:
        keys: The hotkey string to send (e.g., '{Ctrl}c', '{Alt}{Tab}', '{LWin}r').
    """
    try:
        desktop.hotkey(keys)
        return f"Successfully sent hotkey: {keys}"
    except Exception as e:
        return f"Error sending hotkey: {e}"

@mcp.tool()
def desktop_drag_and_drop(window_name: str, source_role: str, source_name: str, target_role: str, target_name: str) -> str:
    """
    Semantically finds a source element and drags it to a target element.

    Args:
        window_name: The name of the window containing the elements.
        source_role: The role of the element to drag.
        source_name: The name of the element to drag.
        target_role: The role of the destination element.
        target_name: The name of the destination element.
    """
    try:
        window = desktop.get_window(window_name)
        source_element = window.find(role=source_role, name=source_name)
        target_element = window.find(role=target_role, name=target_name)
        desktop.drag_and_drop(source_element, target_element)
        return f"Successfully dragged '{source_name}' to '{target_name}'."
    except Exception as e:
        return f"Error dragging and dropping: {e}"

@mcp.tool()
def desktop_drag_and_drop_coordinates(start_x: int, start_y: int, end_x: int, end_y: int, move_speed: int = 1, wait_time: float = 0.5) -> str:
    """
    Performs a drag-and-drop gesture from physical coordinates (start_x, start_y) to (end_x, end_y).

    Args:
        start_x: The starting X coordinate.
        start_y: The starting Y coordinate.
        end_x: The ending X coordinate.
        end_y: The ending Y coordinate.
        move_speed: Mouse movement speed (default is 1).
        wait_time: Delay in seconds after mouse up (default is 0.5).
    """
    try:
        desktop.drag_and_drop_coordinates(start_x, start_y, end_x, end_y, move_speed=move_speed, wait_time=wait_time)
        return f"Successfully performed coordinate drag-and-drop from ({start_x}, {start_y}) to ({end_x}, {end_y})."
    except Exception as e:
        return f"Error performing coordinate drag-and-drop: {e}"

@mcp.tool()
def desktop_manage_window(window_name: str, action: str, x: int = None, y: int = None, width: int = None, height: int = None) -> str:
    """
    Manages and manipulates an entire Desktop window.

    Args:
        window_name: The name of the window to manage.
        action: The action to perform. Must be one of: 'maximize', 'minimize', 'restore', 'close', 'move', 'resize'.
        x: The X coordinate (required if action is 'move').
        y: The Y coordinate (required if action is 'move').
        width: The new width (required if action is 'resize').
        height: The new height (required if action is 'resize').
    """
    try:
        window = desktop.get_window(window_name)
        action = action.lower()
        if action == 'maximize':
            window.maximize()
        elif action == 'minimize':
            window.minimize()
        elif action == 'restore':
            window.restore()
        elif action == 'close':
            window.close()
        elif action == 'move':
            if x is None or y is None:
                return "Error: x and y must be provided for 'move' action."
            window.move(x, y)
        elif action == 'resize':
            if width is None or height is None:
                return "Error: width and height must be provided for 'resize' action."
            window.resize(width, height)
        else:
            return f"Error: Unknown action '{action}'"

        return f"Successfully performed '{action}' on window '{window_name}'."
    except Exception as e:
        return f"Error managing window: {e}"

# ==========================================
# WEB AUTOMATION TOOLS (legacy role/name, unchanged)
# ==========================================

@mcp.tool()
def web_start_browser(headless: bool = False) -> str:
    """
    Starts the Playwright Chromium browser session.
    You usually don't need to call this manually unless you specifically want to start it in headless mode,
    as other web_ tools will auto-start it in non-headless mode if not already running.

    Args:
        headless: If True, the browser runs invisibly in the background. Defaults to False so the user can see.
    """
    try:
        get_web(headless=headless)
        mode = "headless" if headless else "visible"
        return f"Successfully started web browser in {mode} mode."
    except Exception as e:
        return f"Error starting browser: {e}"

@mcp.tool()
def web_goto(url: str) -> str:
    """
    Navigates the web browser to a specific URL.

    Args:
        url: The URL to navigate to (e.g., 'https://en.wikipedia.org/wiki/Main_Page')
    """
    try:
        web = get_web()
        page = web.goto(url)
        return f"Successfully navigated to {url}. Page title: '{page.name}'"
    except Exception as e:
        return f"Error navigating: {e}"

@mcp.tool()
def web_get_ui() -> str:
    """
    Retrieves the semantic UI tree (Web DOM) for the current web page.
    This returns a JSON string representing the accessibility tree (ARIA snapshot).
    For stable node refs use web_start_workflow + web_snapshot instead.
    """
    try:
        web = get_web()
        page = web.get_current_page()
        return page.to_json()
    except Exception as e:
        return f"Error getting Web UI: {e}"

@mcp.tool()
def web_click(role: Optional[str] = None, name: Optional[str] = None, selector: Optional[str] = None, ref: Optional[str] = None, scope: Optional[str] = None) -> str:
    """
    Finds a UI element on the current web page and clicks it.

    Legacy role/name/selector usage stays supported. When ref + scope are
    supplied, the exact snapshot node is clicked and stale or relocated refs
    fail with a [ref_*] code.

    Args:
        role: Optional semantic role of the element (e.g. 'button', 'link').
        name: Optional exact semantic name of the element (e.g. 'Search').
        selector: Optional CSS/XPath selector fallback.
        ref: Optional stable tarsier ref; when given, other locators are ignored.
        scope: Workflow scope required when ref is given.
    """
    try:
        if ref:
            if not scope:
                return "Error: 'scope' is required when 'ref' is provided."
            workflow = _get_workflow(scope)
            workflow.click(ref)
            return f"Successfully clicked ref {ref}."
        web = get_web()
        page = web.get_current_page()
        element = page.wait_for_element(role=role, name=name, selector=selector, timeout=5)
        element.click()
        return f"Successfully clicked the element (role={role}, name={name}, selector={selector})."
    except Exception as e:
        return _ref_error(e)

@mcp.tool()
def web_type(role: Optional[str] = None, name: Optional[str] = None, selector: Optional[str] = None, text: str = "", ref: Optional[str] = None, scope: Optional[str] = None) -> str:
    """
    Finds a textbox or input field on the web page and types text into it.

    Legacy role/name/selector usage stays supported. When ref + scope are
    supplied, the exact snapshot node is targeted.

    Args:
        role: Optional semantic role of the element (e.g. 'textbox', 'searchbox').
        name: Optional name of the element.
        selector: Optional CSS/XPath selector fallback.
        text: The text to type.
        ref: Optional stable tarsier ref; when given, other locators are ignored.
        scope: Workflow scope required when ref is given.
    """
    try:
        if ref:
            if not scope:
                return "Error: 'scope' is required when 'ref' is provided."
            workflow = _get_workflow(scope)
            workflow.type(ref, text)
            return f"Successfully typed into ref {ref}."
        web = get_web()
        page = web.get_current_page()
        element = page.wait_for_element(role=role, name=name, selector=selector, timeout=5)
        element.type(text)
        return f"Successfully typed text into the element (role={role}, name={name}, selector={selector})."
    except Exception as e:
        return _ref_error(e)

@mcp.tool()
def web_read_text(role: Optional[str] = None, name: Optional[str] = None, selector: Optional[str] = None, ref: Optional[str] = None, scope: Optional[str] = None) -> str:
    """
    Reads and returns the textual contents of a specific web element.

    Legacy role/name/selector usage stays supported. When ref + scope are
    supplied, the exact snapshot node is read.

    Args:
        role: Optional semantic role of the element (e.g. 'heading', 'main').
        name: Optional exact semantic name of the element.
        selector: Optional CSS/XPath selector fallback.
        ref: Optional stable tarsier ref; when given, other locators are ignored.
        scope: Workflow scope required when ref is given.
    """
    try:
        if ref:
            if not scope:
                return "Error: 'scope' is required when 'ref' is provided."
            workflow = _get_workflow(scope)
            return workflow.read(ref)
        web = get_web()
        page = web.get_current_page()
        element = page.wait_for_element(role=role, name=name, selector=selector, timeout=5)
        return element.read()
    except Exception as e:
        return _ref_error(e)

@mcp.tool()
def web_new_page(url: Optional[str] = None) -> str:
    """
    Opens a new web tab or page.

    Args:
        url: Optional URL to navigate to immediately in the new tab.
    """
    try:
        web = get_web()
        page = web.new_page(url=url)
        return f"Successfully opened new tab. Active page title: '{page.name}'"
    except Exception as e:
        return f"Error opening new tab: {e}"

@mcp.tool()
def web_list_pages() -> str:
    """
    Lists all open browser tabs/pages with their indexes and titles.
    """
    try:
        web = get_web()
        pages_info = []
        for i, p in enumerate(web.pages):
            try:
                pages_info.append(f"[{i}]: {p.page.title()}")
            except Exception:
                pages_info.append(f"[{i}]: (Unable to read title)")
        return "Active Browser Tabs:\n" + "\n".join(pages_info)
    except Exception as e:
        return f"Error listing pages: {e}"

@mcp.tool()
def web_switch_to_page(index: int) -> str:
    """
    Switches active browser context to the tab/page at the specified index.
    Workflow refs are page-scoped: take a new snapshot after switching.

    Args:
        index: The index of the tab (from web_list_pages).
    """
    try:
        web = get_web()
        page = web.switch_to_page(index)
        return f"Successfully switched to tab [{index}]. Active page title: '{page.page.title()}'"
    except Exception as e:
        return f"Error switching tabs: {e}"

@mcp.tool()
def web_close_browser() -> str:
    """
    Safely closes the browser context and stops the Playwright session, releasing resources.
    """
    global web_desktop
    # Ref registries tied to pages of this browser are no longer usable.
    for scope, (workflow, platform) in list(_workflows.items()):
        if platform == "web":
            workflow.close()
            _workflows.pop(scope, None)
    if web_desktop:
        try:
            web_desktop.close()
            web_desktop = None
            return "Successfully closed web browser and stopped Playwright session."
        except Exception as e:
            return f"Error closing web browser: {e}"
    else:
        return "No active browser session to close."


def main():
    """Starts the stdio MCP server."""
    mcp.run()

if __name__ == "__main__":
    main()
