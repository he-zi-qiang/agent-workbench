"""What turning delegation on actually changes, from the configuration inward.

Three separate gates have to agree before a model can start another run, and
they are deliberately not the same gate. The tool has to be **registered** (a
process fact), the Task's **envelope** has to allow the name (frozen at
submission), and the **profile** has to be offered it (a per-agent ceiling). A
deployment with the switch off must fail all three, and the way it fails matters:
``ToolGateway.advertise`` raises for a name nothing registered, so a profile that
named the tool statically would turn an off switch into a node that fails on
every Task.

That last one is why the tool is a ``DynamicToolSource`` rather than an entry in
``AgentProfile.tool_names``, and it is what the last section pins.
"""

from __future__ import annotations

import pytest

from agent_workbench.bootstrap.projections import task_authorization_envelope
from agent_workbench.bootstrap.settings import MultiAgentSettings
from agent_workbench.domain.agents import DELEGATE_TOOL
from agent_workbench.workflows.agent_profiles import (
    profile_for,
    profile_with_dynamic_tools,
)


class TestTheEnvelopeCarriesTheNameOnlyWhenAsked:
    def test_a_deployment_with_delegation_off_never_names_the_tool(self) -> None:
        envelope = task_authorization_envelope(external_search=False)

        assert DELEGATE_TOOL not in envelope.allowed_tools

    def test_turning_it_on_adds_the_name_and_nothing_else(self) -> None:
        """The one widening that touches a single axis.

        ``external_search`` and ``sandbox_run`` both declare ``risk="external"``
        and so require the ceiling to move as well; a delegation is ``read``,
        which is below every ceiling these variants set. There is no second half
        to forget here, and this pins that there is not.
        """

        without = task_authorization_envelope(external_search=False)
        with_delegation = task_authorization_envelope(
            external_search=False, delegation=True
        )

        assert DELEGATE_TOOL in with_delegation.allowed_tools
        assert set(with_delegation.allowed_tools) - {DELEGATE_TOOL} == set(
            without.allowed_tools
        )

    def test_it_composes_with_every_other_widening(self) -> None:
        envelope = task_authorization_envelope(
            external_search=True,
            mcp_tools=("word_render",),
            sandbox=True,
            delegation=True,
        )

        assert DELEGATE_TOOL in envelope.allowed_tools
        assert "word_render" in envelope.allowed_tools


class TestTheConfiguredTreeMustFitTheTaskBudget:
    def test_a_tree_wider_than_the_task_allowance_stops_the_process(self) -> None:
        """Computed at startup from two numbers an operator typed.

        The worst case is exponential in the depth. Working it out here is what
        stops a deployment from discovering halfway through a run that the bill
        was never bounded by the setting that claimed to bound it.
        """

        with pytest.raises(ValueError, match="delegation tree"):
            MultiAgentSettings(
                delegation_enabled=True,
                max_delegation_depth=3,
                max_children_per_run=8,
                max_agent_invocation_attempts_per_task=12,
            )

    def test_the_same_numbers_are_accepted_while_delegation_is_off(self) -> None:
        """The check is about what delegation would do, so it does not fire on a
        deployment that is not delegating."""

        settings = MultiAgentSettings(
            max_delegation_depth=3,
            max_children_per_run=8,
            max_agent_invocation_attempts_per_task=12,
        )

        assert settings.delegation_enabled is False

    def test_the_shipped_defaults_fit(self) -> None:
        """A guard that refused this project's own defaults would be a guard
        nobody could turn the feature on behind."""

        settings = MultiAgentSettings(delegation_enabled=True)

        assert settings.max_children_per_run**settings.max_delegation_depth <= (
            settings.max_agent_invocation_attempts_per_task
        )


class TestAProfileGetsTheToolOnlyFromTheProcessThatHasIt:
    def test_an_empty_catalog_leaves_the_profile_exactly_as_it_was(self) -> None:
        """The failure mode this indirection exists for.

        A profile that named ``delegate_agent`` in ``tool_names`` would ask
        ``advertise`` for it on every deployment, and ``advertise`` raises for a
        name the registry does not hold. The switch being off has to leave the
        profile untouched, not merely refuse the call later.
        """

        profile = profile_for("work")

        assert DELEGATE_TOOL not in profile.tool_names
        assert profile_with_dynamic_tools(profile, {}) is profile

    def test_a_process_that_registered_it_hands_it_to_the_declaring_profile(
        self,
    ) -> None:
        profile = profile_for("work")

        granted = profile_with_dynamic_tools(profile, {"delegation": (DELEGATE_TOOL,)})

        assert DELEGATE_TOOL in granted.tool_names

    def test_a_profile_that_did_not_declare_the_audience_does_not_get_it(
        self,
    ) -> None:
        """The catalog is offered to every profile; subscription is what selects.

        ``synthesize`` is the node that writes the deliverable, and giving the
        report's author a way to start runs it did not plan is a bigger change
        than this tier is making.
        """

        writer = profile_for("synthesize")

        granted = profile_with_dynamic_tools(writer, {"delegation": (DELEGATE_TOOL,)})

        assert DELEGATE_TOOL not in granted.tool_names

    def test_a_v1_research_node_is_not_where_this_belongs(self) -> None:
        """Where a deployment wired retrieval, that node never runs an agent.

        ``task_handlers.research_internal`` calls ``research.internal.gather()``
        and returns -- no model, no tool loop. A delegation source declared
        there would be offered to nobody, and the switch would look on while
        nothing could reach it. Pinned as a test because the mistake is
        invisible from the profile itself: it reads exactly like a node that
        would use the tool.
        """

        researcher = profile_for("research_internal")

        assert "delegation" not in researcher.dynamic_tool_sources
