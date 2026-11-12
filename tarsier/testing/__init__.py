"""In-memory fakes for testing Tarsier without a real desktop/browser."""

from tarsier.testing.fake_a11y import (
    FakeDesktop,
    FakeNode,
    FakeTreeAdapter,
    FakeWindow,
    build_form_tree,
)

__all__ = [
    "FakeDesktop",
    "FakeNode",
    "FakeTreeAdapter",
    "FakeWindow",
    "build_form_tree",
]
