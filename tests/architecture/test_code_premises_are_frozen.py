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

``shell_enabled`` is here for a related reason rather than the same one. It is
frozen ``False`` because turning it on means granting ``sandbox_run``, which
needs a server this process does not start and a permission scope no principal
holds -- so a boolean would let a deployment set it and get nothing, which is
the worst of the three outcomes.
"""

from __future__ import annotations

from typing import Literal, get_args, get_origin, get_type_hints

import pytest

from agent_workbench.bootstrap.settings import CodeSettings

#: Field name -> the single value its ``Literal`` is allowed to hold.
FROZEN_PREMISES = {
    "execution_locality": "in_api_process",
    "coordination": "none",
    "shell_enabled": False,
}


@pytest.mark.parametrize(
    ("field", "value"),
    sorted(FROZEN_PREMISES.items(), key=lambda item: item[0]),
    ids=sorted(FROZEN_PREMISES),
)
def test_the_single_process_premise_is_a_literal(field: str, value: object) -> None:
    annotation = get_type_hints(CodeSettings)[field]

    assert get_origin(annotation) is Literal
    assert get_args(annotation) == (value,)


def test_the_premises_are_fields_of_the_section_that_depends_on_them() -> None:
    """The control: a renamed field would make every assertion above vacuous.

    ``get_type_hints`` raises on a name that does not exist, so this could not
    silently pass -- but it would fail with a KeyError that reads like a broken
    test rather than like a premise that moved.
    """

    assert FROZEN_PREMISES.keys() <= set(CodeSettings.model_fields)
