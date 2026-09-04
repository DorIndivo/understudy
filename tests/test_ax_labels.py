"""Label harvesting, tested against a fake accessibility tree.

The real AX API needs a running app, so the tree walk is exercised by
monkeypatching the two attribute readers every walker goes through.
"""

from __future__ import annotations

import pytest

from understudy.capture import ax
from understudy.trace.models import Element


class Node:
    """A stand-in for an AXUIElement."""

    def __init__(self, role, title=None, value=None, description=None, children=None):
        self.attrs = {
            "AXRole": role, "AXTitle": title, "AXValue": value, "AXDescription": description,
        }
        self.children = children or []
        self.parent = None
        for child in self.children:
            child.parent = self


@pytest.fixture(autouse=True)
def fake_ax(monkeypatch):
    def _attr(node, name):
        if name == "AXChildren":
            return node.children or None
        if name == "AXParent":
            return node.parent
        return node.attrs.get(name)

    def _str_attr(node, name):
        value = node.attrs.get(name)
        return str(value).strip() if value else None

    monkeypatch.setattr(ax, "_attr", _attr)
    monkeypatch.setattr(ax, "_str_attr", _str_attr)


def bare(role="AXGroup", frame=(0, 0, 40, 20)):
    return Element(resolved=True, role=role, frame=frame)


class TestAlreadyLabeled:
    def test_an_element_with_its_own_title_is_left_alone(self):
        node = Node("AXButton", children=[Node("AXStaticText", title="Something Else")])
        element = Element(resolved=True, role="AXButton", title="Approve", frame=(0, 0, 40, 20))
        ax.harvest_label(node, element)
        assert element.title == "Approve"
        assert element.label_source == "element"

    def test_an_identifier_alone_counts_as_labeled(self):
        element = Element(resolved=True, identifier="approve-btn", frame=(0, 0, 40, 20))
        ax.harvest_label(Node("AXButton"), element)
        assert element.label_source == "element"


class TestDescendants:
    def test_icon_button_adopts_the_text_it_contains(self):
        node = Node("AXButton", children=[Node("AXStaticText", value="Approve")])
        element = bare("AXButton")
        ax.harvest_label(node, element)
        assert element.title == "Approve"
        assert element.label_source == "descendant"

    def test_text_is_found_one_level_deeper(self):
        node = Node("AXButton", children=[Node("AXGroup", children=[Node("AXStaticText", value="Send")])])
        element = bare("AXButton")
        ax.harvest_label(node, element)
        assert element.title == "Send"

    def test_the_walk_stops_before_unbounded_depth(self):
        deep = Node("AXStaticText", value="TooDeep")
        for _ in range(ax.MAX_LABEL_DEPTH + 2):
            deep = Node("AXGroup", children=[deep])
        element = bare()
        ax.harvest_label(deep, element)
        assert element.title is None

    def test_non_text_children_are_ignored(self):
        node = Node("AXButton", children=[Node("AXImage", title=None), Node("AXStaticText", value="Ok")])
        element = bare("AXButton")
        ax.harvest_label(node, element)
        assert element.title == "Ok"


class TestSiblings:
    def test_unlabeled_field_adopts_its_caption(self):
        field = Node("AXTextField")
        Node("AXGroup", children=[Node("AXStaticText", value="Invoice ID:"), field])
        element = bare("AXTextField")
        ax.harvest_label(field, element)
        assert element.title == "Invoice ID:"
        assert element.label_source == "sibling"

    def test_descendants_are_preferred_over_siblings(self):
        field = Node("AXTextField", children=[Node("AXStaticText", value="Inner")])
        Node("AXGroup", children=[Node("AXStaticText", value="Outer"), field])
        element = bare("AXTextField")
        ax.harvest_label(field, element)
        assert element.title == "Inner"
        assert element.label_source == "descendant"


class TestPanelGuard:
    """A label only means something for a control; a pane must not adopt one."""

    def test_panel_sized_element_is_not_labeled(self):
        node = Node("AXGroup", children=[Node("AXStaticText", value="Some document text")])
        element = bare("AXGroup", frame=(0, 0, 1200, 800))
        ax.harvest_label(node, element)
        assert element.title is None
        assert element.label_source is None

    def test_control_sized_element_is_labeled(self):
        node = Node("AXGroup", children=[Node("AXStaticText", value="Approve")])
        element = bare("AXGroup", frame=(0, 0, 120, 30))
        ax.harvest_label(node, element)
        assert element.title == "Approve"

    def test_element_without_a_frame_is_still_attempted(self):
        node = Node("AXButton", children=[Node("AXStaticText", value="Go")])
        element = Element(resolved=True, role="AXButton", frame=None)
        ax.harvest_label(node, element)
        assert element.title == "Go"


class TestRobustness:
    def test_nothing_to_harvest_leaves_the_element_bare(self):
        element = bare()
        ax.harvest_label(Node("AXGroup"), element)
        assert element.title is None
        assert element.label_source is None

    def test_a_very_long_label_is_truncated(self):
        node = Node("AXButton", children=[Node("AXStaticText", value="x" * 500)])
        element = bare("AXButton")
        ax.harvest_label(node, element)
        assert len(element.title) == 200
