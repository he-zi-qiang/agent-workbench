"""What a delegating run may hand down, asserted where it cannot be forgotten.

Three properties, and the interesting thing about all three is that they are
properties of *return values* rather than of checks somebody remembered to
write. A caller cannot widen an envelope by passing a different argument,
because there is no argument that widens; a grandchild cannot delegate, because
the tool is not in the list it was handed.

The third section is the one worth deleting a line for: strike the delegation
filter out of ``permitted_child_tools`` and every generation gets the tool that
spawned it, which is an unbounded tree that costs money. The test that goes red
is the reason the filter is not a comment.
"""

from __future__ import annotations

import pytest

from agent_workbench.domain.agents import (
    DELEGATE_TOOL,
    SubAgentCatalogue,
    SubAgentDefinition,
    child_envelope,
    permitted_child_tools,
)
from agent_workbench.domain.policies import AuthorizationEnvelope

RESEARCHER = SubAgentDefinition(
    name="researcher",
    description="reads the knowledge base",
    system_prompt="find out",
    tool_names=("knowledge_search", "external_search"),
)

#: A definition that names the delegation tool itself. Legal to write, and the
#: point of several assertions below: writing it grants nothing.
RECURSIVE = SubAgentDefinition(
    name="recursive",
    description="delegates further",
    system_prompt="delegate",
    tool_names=("knowledge_search", DELEGATE_TOOL),
)


class TestTheIntersectionOnlyNarrows:
    def test_a_child_never_receives_a_tool_its_parent_was_not_allowed(self) -> None:
        """The definition is a ceiling of its own, never a grant.

        ``researcher`` names two tools. The parent holds one of them. The
        answer is one -- not two, and not "two with a warning".
        """

        granted = permitted_child_tools(
            RESEARCHER,
            ("knowledge_search",),
            child_depth=1,
            max_depth=1,
        )

        assert granted == ("knowledge_search",)

    def test_a_parent_holding_more_does_not_widen_the_definition(self) -> None:
        """Narrowing runs in one direction only, and this is the other one."""

        granted = permitted_child_tools(
            RESEARCHER,
            ("knowledge_search", "external_search", "export_artifact"),
            child_depth=1,
            max_depth=1,
        )

        assert granted == ("knowledge_search", "external_search")

    def test_a_child_of_a_parent_holding_nothing_holds_nothing(self) -> None:
        assert permitted_child_tools(RESEARCHER, (), child_depth=1, max_depth=1) == ()


class TestDepthIsWrittenIntoTheToolbox:
    def test_a_child_is_never_handed_the_tool_that_created_it(self) -> None:
        """Depth 1: the child is the last generation, so the tool comes off.

        Both halves of the setup are deliberately generous -- the definition
        names ``delegate_agent`` and the parent is allowed it -- so the only
        thing that can remove it is the depth rule itself.
        """

        granted = permitted_child_tools(
            RECURSIVE,
            ("knowledge_search", DELEGATE_TOOL),
            child_depth=1,
            max_depth=1,
        )

        assert DELEGATE_TOOL not in granted
        assert granted == ("knowledge_search",)

    def test_a_generation_below_the_ceiling_keeps_the_tool(self) -> None:
        """The control. Without it the test above would pass on a function
        that removed the delegation tool unconditionally, which is a different
        (and, for a later tier, wrong) implementation."""

        granted = permitted_child_tools(
            RECURSIVE,
            ("knowledge_search", DELEGATE_TOOL),
            child_depth=1,
            max_depth=2,
        )

        assert DELEGATE_TOOL in granted

    def test_every_depth_at_or_past_the_ceiling_is_the_last_one(self) -> None:
        """Swept rather than sampled: the boundary is an inequality, and an
        off-by-one in it is invisible at a single depth."""

        for ceiling in range(1, 5):
            for depth in range(1, 6):
                granted = permitted_child_tools(
                    RECURSIVE,
                    (DELEGATE_TOOL,),
                    child_depth=depth,
                    max_depth=ceiling,
                )
                may_delegate = DELEGATE_TOOL in granted
                assert may_delegate is (depth < ceiling), (
                    f"depth={depth} ceiling={ceiling} granted={granted}"
                )


class TestTheEnvelopeOnlyDescends:
    def test_a_child_envelope_cannot_raise_the_risk_ceiling(self) -> None:
        """A read-only parent stays read-only however the child is described."""

        parent = AuthorizationEnvelope(
            allowed_tools=("knowledge_search",),
            max_tool_risk="read",
        )

        child = child_envelope(
            parent,
            RESEARCHER,
            child_depth=1,
            max_depth=1,
            risk_ceiling="destructive",
        )

        assert child.max_tool_risk == "read"

    def test_a_child_of_a_writing_parent_is_still_capped_at_read(self) -> None:
        """The other direction: the tier's own ceiling is the binding one."""

        parent = AuthorizationEnvelope(
            allowed_tools=("knowledge_search",),
            max_tool_risk="destructive",
        )

        child = child_envelope(parent, RESEARCHER, child_depth=1, max_depth=1)

        assert child.max_tool_risk == "read"

    def test_a_denial_is_not_dropped_on_the_way_down(self) -> None:
        """Denial is the half of an envelope a copy forgets.

        A child that inherited the allowlist and not the denials would be a way
        to reach, one level down, a tool the submitter explicitly refused.
        """

        parent = AuthorizationEnvelope(
            allowed_tools=("knowledge_search", "external_search"),
            denied_tools=("external_search",),
        )

        child = child_envelope(parent, RESEARCHER, child_depth=1, max_depth=1)

        assert "external_search" in child.denied_tools

    def test_the_approval_list_is_not_shortened_for_a_child(self) -> None:
        """A shorter list would be a way to do unapproved work one level down."""

        parent = AuthorizationEnvelope(
            allowed_tools=("knowledge_search",),
            approval_required_risks=("write", "external", "destructive"),
        )

        child = child_envelope(parent, RESEARCHER, child_depth=1, max_depth=1)

        assert child.approval_required_risks == parent.approval_required_risks


class TestTheCatalogueAnswersAtAssembly:
    def test_two_definitions_under_one_name_stop_the_process(self) -> None:
        """At construction, not at the first delegation.

        A deployment that cannot say which of two definitions a model asking
        for that name would get is one that should not start, and finding out
        on the first delegation means finding out in production.
        """

        with pytest.raises(ValueError, match="share the name"):
            SubAgentCatalogue((RESEARCHER, RESEARCHER))

    def test_an_unregistered_name_is_absent_rather_than_an_error(self) -> None:
        """A model proposed this name, and the proposal still has to become
        exactly one ToolResult -- which the handler cannot write if the lookup
        raises past it."""

        catalogue = SubAgentCatalogue((RESEARCHER,))

        assert catalogue.get("nobody") is None
        assert catalogue.get("researcher") is RESEARCHER

    def test_a_tool_this_process_lacks_is_dropped_from_a_definition(self) -> None:
        """The intersection upstream is with the *envelope*, not the registry.

        An envelope is frozen from configuration, so it can name a tool the
        process failed to assemble -- an MCP server that was down, a search
        provider nobody configured. ``ToolGateway.advertise`` raises for those,
        which would make the child run fail before its first turn rather than
        run with less.
        """

        narrowed = SubAgentCatalogue((RESEARCHER,)).narrowed_to(("knowledge_search",))

        kept = narrowed.get("researcher")
        assert kept is not None
        assert kept.tool_names == ("knowledge_search",)

    def test_a_definition_left_with_none_of_its_tools_is_not_offered(self) -> None:
        """A researcher with no way to search is not a researcher.

        Offering it anyway puts a description in front of the model -- "answers
        from the knowledge base" -- that the run behind it cannot honour, and
        the model spends a whole delegation to be told so.
        """

        narrowed = SubAgentCatalogue((RESEARCHER,)).narrowed_to(())

        assert narrowed.names() == ()

    def test_a_definition_that_never_wanted_tools_survives_any_narrowing(
        self,
    ) -> None:
        """The control. Without it the rule above would also delete the one
        sub-agent whose whole point is that it holds nothing."""

        toolless = SubAgentDefinition(
            name="analyst", description="thinks", system_prompt="think"
        )

        assert SubAgentCatalogue((toolless,)).narrowed_to(()).names() == ("analyst",)

    def test_the_names_keep_the_order_they_were_declared_in(self) -> None:
        """They go into a tool description and a schema enum, both of which are
        frozen into an event stream. An order that varied per process would
        make two identical deployments produce different specs."""

        catalogue = SubAgentCatalogue((RESEARCHER, RECURSIVE))

        assert catalogue.names() == ("researcher", "recursive")
