"""Code's single-process premise is a type, not a habit.

Everything Code gives up follows from one fact: the run happens where the human
answering an approval can reach the coroutine that is waiting. No lease, no
reaper, no resumable checkpoint -- and in exchange, a wait that is honest.

A premise that load-bearing must not be a string somebody can widen in a TOML
file. It is a single-valued ``Literal``, so a second locality is a code change
that fails type-checking until every consequence above has been reconsidered.
This test is what keeps that true: the field could be relaxed to a plain
``str`` in one edit, and nothing else in the suite would notice, because every
existing config file would keep passing.

``shell_enabled`` used to be here for a related reason rather than the same
one, and is now ``sandbox_enabled`` and deliberately *not* frozen (ADR-057).
Its freeze was about wiring: a boolean would have let a deployment set it and
get nothing, because no process started the sandbox server and no principal
held its scope. Both are wired, so the honest control is a boolean that is off
by default -- and what it grants was never a shell, but ADR-029's pure
function. The assertion below is inverted for it rather than deleted, so that
re-freezing the field fails here instead of passing silently.
"""

from __future__ import annotations

from typing import Literal, get_args, get_origin, get_type_hints

import pytest

from agent_workbench.bootstrap.settings import CodeSettings

#: Field name -> the single value its ``Literal`` is allowed to hold.
FROZEN_PREMISES = {
    "execution_locality": "in_api_process",
    "coordination": "none",
}

#: Fields that were frozen and are deliberately not any more. Listed rather
#: than forgotten: the interesting failure is somebody re-freezing one to make
#: a test pass, which would take a capability away without an ADR saying so.
DELIBERATELY_UNFROZEN = {"sandbox_enabled": bool}


@pytest.mark.parametrize(
    ("field", "value"),
    sorted(FROZEN_PREMISES.items(), key=lambda item: item[0]),
    ids=sorted(FROZEN_PREMISES),
)
def test_the_single_process_premise_is_a_literal(field: str, value: object) -> None:
    annotation = get_type_hints(CodeSettings)[field]

    assert get_origin(annotation) is Literal
    assert get_args(annotation) == (value,)


@pytest.mark.parametrize(
    ("field", "annotation"),
    sorted(DELIBERATELY_UNFROZEN.items(), key=lambda item: item[0]),
    ids=sorted(DELIBERATELY_UNFROZEN),
)
def test_a_deliberately_unfrozen_premise_is_still_a_plain_type(
    field: str, annotation: type
) -> None:
    """The inverse of the test above, and it exists for the same worry.

    Re-freezing `sandbox_enabled` would silently remove a capability a
    deployment had been granted -- and it would look, in a diff, exactly like
    the tightening this file otherwise rewards. ADR-057 is what unfroze it; a
    later ADR may freeze it again, and will have to edit this test to say so.
    """

    hint = get_type_hints(CodeSettings)[field]

    assert get_origin(hint) is not Literal
    assert hint is annotation


def test_the_premises_are_fields_of_the_section_that_depends_on_them() -> None:
    """The control: a renamed field would make every assertion above vacuous.

    ``get_type_hints`` raises on a name that does not exist, so this could not
    silently pass -- but it would fail with a KeyError that reads like a broken
    test rather than like a premise that moved.
    """

    fields = set(CodeSettings.model_fields)
    assert FROZEN_PREMISES.keys() <= fields
    assert DELIBERATELY_UNFROZEN.keys() <= fields
    # And the old name is gone rather than living beside the new one, which
    # would leave two switches for one question and no way to tell which the
    # deployment meant.
    assert "shell_enabled" not in fields
