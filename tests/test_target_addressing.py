"""What makes a captured target re-findable by an agent rather than merely described."""

from __future__ import annotations

from understudy.interpret.prompt import MAX_LABEL_CHARS, _clip, _step_facts
from understudy.trace.models import Action, AppContext, Element, Step


def step(**target_kwargs) -> Step:
    return Step(
        index=0, t_ms=0, dwell_ms=0, action=Action.CLICK,
        app=AppContext(name="Chrome"),
        target=Element(resolved=True, **target_kwargs),
    )


class TestClipping:
    def test_a_short_label_is_untouched(self):
        assert _clip("Approve") == "Approve"

    def test_a_runaway_label_keeps_its_identifying_head(self):
        clipped = _clip("Row: " + "x" * 400)
        assert clipped.startswith("Row: xxx")
        assert len(clipped) < 200
        assert "+" in clipped and "chars]" in clipped

    def test_whitespace_is_collapsed(self):
        # Accessibility labels arrive full of padding characters and newlines.
        assert _clip("a\n\n  b\t c") == "a b c"

    def test_the_boundary_is_not_clipped(self):
        exact = "y" * MAX_LABEL_CHARS
        assert _clip(exact) == exact


class TestOrdinals:
    def test_an_ordinal_is_reported_when_siblings_are_ambiguous(self):
        facts = _step_facts(step(role="AXGroup", index_in_parent=2, same_role_siblings=9))
        assert "ordinal 2 of 9 AXGroup" in facts

    def test_a_lone_child_gets_no_ordinal(self):
        # Nothing to disambiguate against; saying "1 of 1" is noise.
        facts = _step_facts(step(role="AXButton", title="Save",
                                 index_in_parent=1, same_role_siblings=1))
        assert "ordinal" not in facts

    def test_a_missing_ordinal_is_simply_absent(self):
        assert "ordinal" not in _step_facts(step(role="AXButton", title="Save"))

    def test_an_unlabeled_element_still_reports_its_ordinal(self):
        """The case the field exists for: no label, so position is all there is."""
        facts = _step_facts(step(role="AXGroup", index_in_parent=3, same_role_siblings=31))
        assert "ordinal 3 of 31" in facts
