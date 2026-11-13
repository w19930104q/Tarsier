import sys
import os

# Ensure tarsier is in the python path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from typing import Optional
from mcp.server.fastmcp import FastMCP
from tarsier import Desktop
from tarsier.core.snapshot import SnapshotError

# Initialize FastMCP Server
mcp = FastMCP("Tarsier Automation")
desktop = Desktop()
web_desktop = None


def _resolve_element(window, role: Optional[str], name: Optional[str], ref: Optional[str]):
    """Resolves a target element either by stable snapshot ref or legacy role/name.

    When ``ref`` is supplied it is validated against the live tree (window
    generation, role/name, visibility and ambiguity checks) before any action.
    The role/name path is unchanged for backwards compatibility.
    """
    if ref:
        return window.resolve_ref(ref)
    return window.find(role=role, name=name)


def _describe_target(role: Optional[str], name: Optional[str], ref: Optional[str]) -> str:
    return f"ref='{ref}'" if ref else f"role='{role}', name='{name}'"

def get_web(headless: bool = False):
    """Lazy loads the WebDesktop so Chromium isn't started unless requested."""
    global web_desktop
    if not web_desktop:
        from tarsier import WebDesktop
        web_desktop = WebDesktop(headless=headless)
    return web_desktop

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
def desktop_get_ui(window_name: str, format: str = "yaml", max_depth: int = 15) -> str:
    """
    Retrieves the entire semantic UI tree (Desktop DOM) for a specific window.
    Every actionable and visible text node carries a unique stable reference
    (e.g. 's0n7') shown as '# ref=...' comments in YAML or as 'ref' fields in
    JSON. Pass that ref to desktop_click/desktop_type/desktop_read_text to
    target the exact node even when several elements share the same name.

    Args:
        window_name: The name of the window to inspect.
        format: 'yaml' (default, token-efficient) or 'json' (structured with refs).
        max_depth: Maximum tree depth to emit; deeper subtrees are marked truncated.
    """
    try:
        window = desktop.get_window(window_name)
        snapshot = window.to_snapshot(max_depth=max_depth)
        if format.lower() == "json":
            return snapshot.to_json()
        return snapshot.to_yaml()
    except Exception as e:
        return f"Error getting UI: {e}"

@mcp.tool()
def desktop_click(window_name: str, role: str = None, name: str = None, ref: str = None) -> str:
    """
    Clicks a UI element inside a window, either by its stable snapshot ref or
    by semantic role/name. Prefer ref when the snapshot contains duplicate
    names. A stale, malformed or ambiguous ref returns an explicit error and
    never clicks the wrong row.

    Args:
        window_name: The name of the window containing the element.
        role: Semantic role (e.g. 'button'); ignored when ref is given.
        name: Exact semantic name (e.g. '删除'); ignored when ref is given.
        ref: Stable reference from the latest desktop_get_ui snapshot, e.g. 's0n7'.
    """
    try:
        window = desktop.get_window(window_name)
        element = _resolve_element(window, role, name, ref)
        element.click()
        return f"Successfully clicked the element ({_describe_target(role, name, ref)})."
    except SnapshotError as e:
        return f"Snapshot reference error: {e}"
    except Exception as e:
        return f"Error clicking element: {e}"

@mcp.tool()
def desktop_right_click(window_name: str, role: str = None, name: str = None, ref: str = None) -> str:
    """
    Right-clicks a UI element, identified by stable snapshot ref or role/name.

    Args:
        window_name: The name of the window containing the element.
        role: Semantic role (e.g. 'button'); ignored when ref is given.
        name: Exact semantic name; ignored when ref is given.
        ref: Stable reference from the latest desktop_get_ui snapshot.
    """
    try:
        window = desktop.get_window(window_name)
        element = _resolve_element(window, role, name, ref)
        element.right_click()
        return f"Successfully right-clicked the element ({_describe_target(role, name, ref)})."
    except SnapshotError as e:
        return f"Snapshot reference error: {e}"
    except Exception as e:
        return f"Error right-clicking element: {e}"

@mcp.tool()
def desktop_hover(window_name: str, role: str = None, name: str = None, ref: str = None) -> str:
    """
    Hovers the mouse cursor over a UI element, identified by snapshot ref or role/name.

    Args:
        window_name: The name of the window containing the element.
        role: Semantic role; ignored when ref is given.
        name: Exact semantic name; ignored when ref is given.
        ref: Stable reference from the latest desktop_get_ui snapshot.
    """
    try:
        window = desktop.get_window(window_name)
        element = _resolve_element(window, role, name, ref)
        element.hover()
        return f"Successfully hovered over the element ({_describe_target(role, name, ref)})."
    except SnapshotError as e:
        return f"Snapshot reference error: {e}"
    except Exception as e:
        return f"Error hovering element: {e}"

@mcp.tool()
def desktop_type(window_name: str, role: str = None, name: str = None, text: str = "", ref: str = None) -> str:
    """
    Types text into a textbox/input field, identified by stable snapshot ref
    or semantic role/name. Ref-based typing is validated against the live tree
    before any keystroke is sent.

    Args:
        window_name: The name of the window containing the element.
        role: Semantic role (e.g. 'document', 'edit'); ignored when ref is given.
        name: Name of the element; ignored when ref is given.
        text: The text to inject.
        ref: Stable reference from the latest desktop_get_ui snapshot.
    """
    try:
        window = desktop.get_window(window_name)
        element = _resolve_element(window, role, name, ref)
        element.type(text)
        return f"Successfully typed text into the element ({_describe_target(role, name, ref)})."
    except SnapshotError as e:
        return f"Snapshot reference error: {e}"
    except Exception as e:
        return f"Error typing text: {e}"

@mcp.tool()
def desktop_read_text(window_name: str, role: str = None, name: str = None, ref: str = None) -> str:
    """
    Reads and returns the textual contents of a UI element, identified by
    stable snapshot ref or role/name. Use ref to read a specific row when a
    table contains repeated names.

    Args:
        window_name: The name of the window containing the element.
        role: Semantic role (e.g. 'document', 'edit'); ignored when ref is given.
        name: Exact semantic name; ignored when ref is given.
        ref: Stable reference from the latest desktop_get_ui snapshot.
    """
    try:
        window = desktop.get_window(window_name)
        element = _resolve_element(window, role, name, ref)
        return element.read()
    except SnapshotError as e:
        return f"Snapshot reference error: {e}"
    except Exception as e:
        return f"Error reading text: {e}"

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
# WEB AUTOMATION TOOLS
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
    This returns a YAML string representing the accessibility tree (ARIA snapshot).
    """
    try:
        web = get_web()
        page = web.get_current_page()
        return page.to_json()
    except Exception as e:
        return f"Error getting Web UI: {e}"

@mcp.tool()
def web_click(role: Optional[str] = None, name: Optional[str] = None, selector: Optional[str] = None) -> str:
    """
    Finds a UI element on the current web page (semantically or via CSS selector) and clicks it.
    
    Args:
        role: Optional semantic role of the element (e.g. 'button', 'link', 'checkbox').
        name: Optional exact semantic name of the element (e.g. 'Search', 'Submit').
        selector: Optional CSS/XPath selector fallback (e.g. '.shopping_cart_link') if element lacks clear semantic tags.
    """
    try:
        web = get_web()
        page = web.get_current_page()
        element = page.wait_for_element(role=role, name=name, selector=selector, timeout=5)
        element.click()
        return f"Successfully clicked the element (role={role}, name={name}, selector={selector})."
    except Exception as e:
        return f"Error clicking web element: {e}"

@mcp.tool()
def web_type(role: Optional[str] = None, name: Optional[str] = None, selector: Optional[str] = None, text: str = "") -> str:
    """
    Finds a textbox or input field on the web page (semantically or via CSS selector) and types text into it.
    
    Args:
        role: Optional semantic role of the element (e.g. 'textbox', 'searchbox').
        name: Optional name of the element.
        selector: Optional CSS/XPath selector fallback (e.g. '#search-input') if element lacks clear semantic tags.
        text: The text to type.
    """
    try:
        web = get_web()
        page = web.get_current_page()
        element = page.wait_for_element(role=role, name=name, selector=selector, timeout=5)
        element.type(text)
        return f"Successfully typed text into the element (role={role}, name={name}, selector={selector})."
    except Exception as e:
        return f"Error typing web text: {e}"

@mcp.tool()
def web_read_text(role: Optional[str] = None, name: Optional[str] = None, selector: Optional[str] = None) -> str:
    """
    Reads and returns the textual contents of a specific web element (like a paragraph, heading, or generic container).
    
    Args:
        role: Optional semantic role of the element (e.g. 'heading', 'main', 'article').
        name: Optional exact semantic name of the element.
        selector: Optional CSS/XPath selector fallback (e.g. '.product-price') if element lacks clear semantic tags.
    """
    try:
        web = get_web()
        page = web.get_current_page()
        element = page.wait_for_element(role=role, name=name, selector=selector, timeout=5)
        return element.read()
    except Exception as e:
        return f"Error reading web text: {e}"

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
