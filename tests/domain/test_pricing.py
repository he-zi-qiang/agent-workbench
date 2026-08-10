"""Turning a token count into money, and the one subtraction that is easy to miss.

The arithmetic is small. What is not obvious is whose convention the inputs
follow: ``TokenUsage.input_tokens`` carries the *whole* prompt, cached part
included, because that is what the provider reports and what the adapter passes
through. A pricer that reads the four fields as four disjoint quantities
double-charges every cached token, and the resulting error is invisible in the
direction that matters -- spend looks higher than it is, so a cost ceiling fires
early on exactly the deployments that turned caching on to spend less.
"""

from __future__ import annotations

from agent_workbench.domain.pricing import ModelPrices
from agent_workbench.domain.runs import TokenUsage

#: DeepSeek's published rates at the time of writing, in micro-USD per million
#: tokens. The cache read rate is a tenth of the input rate, which is what makes
#: the double-charging test below able to tell the two apart.
PRICES = ModelPrices(
    input_micro_usd_per_mtok=270_000,
    output_micro_usd_per_mtok=1_100_000,
    cache_read_micro_usd_per_mtok=27_000,
    cache_write_micro_usd_per_mtok=0,
)


def test_an_uncached_turn_costs_the_published_rates() -> None:
    """The baseline every other case here is measured against."""

    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)

    assert PRICES.cost_micro_usd(usage) == 270_000 + 1_100_000


def test_a_cached_prompt_is_charged_once_at_the_cache_rate() -> None:
    """The subtraction. Half the prompt was cached, so half pays each rate.

    Stated as an exact expected value rather than "less than the uncached
    case", because a pricer that subtracted the cached tokens and then forgot
    to charge for them at all would also be less.
    """

    usage = TokenUsage(input_tokens=1_000_000, cache_read_tokens=500_000)

    assert PRICES.cost_micro_usd(usage) == 135_000 + 13_500


def test_caching_a_prompt_costs_less_than_not_caching_it() -> None:
    """The property the subtraction exists to preserve.

    This is the assertion that fails on the natural wrong implementation --
    adding a cache charge on top of an unreduced input charge -- and it fails
    in the readable direction: the cached prompt comes out *more* expensive
    than the identical uncached one, which is the opposite of why caching is
    on.
    """

    prompt = 1_000_000
    uncached = TokenUsage(input_tokens=prompt)
    cached = TokenUsage(input_tokens=prompt, cache_read_tokens=prompt)

    assert PRICES.cost_micro_usd(cached) < PRICES.cost_micro_usd(uncached)


def test_a_fully_cached_prompt_pays_only_the_cache_rate() -> None:
    """The boundary of the case above: nothing is left at the input rate."""

    usage = TokenUsage(input_tokens=800_000, cache_read_tokens=800_000)

    assert PRICES.cost_micro_usd(usage) == 21_600


def test_a_cache_report_larger_than_its_prompt_does_not_go_negative() -> None:
    """Should not happen; must not corrupt the ledger if it does.

    A negative contribution would make a run's spend fall as it worked, and a
    cost ceiling that can be walked back is not a ceiling. Clamped, not
    rejected: this is a provider misreport, and failing a run over it would
    turn somebody else's bug into an outage.
    """

    usage = TokenUsage(input_tokens=100, cache_read_tokens=900)

    assert PRICES.cost_micro_usd(usage) >= 0


def test_a_free_model_costs_nothing() -> None:
    """Zero rates are a price, and the control for every total above.

    Without it, a pricer returning a constant would satisfy the exact-value
    assertions only by coincidence -- this one it cannot satisfy at all.
    """

    free = ModelPrices(
        input_micro_usd_per_mtok=0,
        output_micro_usd_per_mtok=0,
        cache_read_micro_usd_per_mtok=0,
        cache_write_micro_usd_per_mtok=0,
    )
    usage = TokenUsage(input_tokens=10**6, output_tokens=10**6, cache_read_tokens=10**5)

    assert free.cost_micro_usd(usage) == 0


def test_an_empty_turn_costs_nothing() -> None:
    assert PRICES.cost_micro_usd(TokenUsage()) == 0


def test_a_fraction_of_a_micro_usd_rounds_down_rather_than_up() -> None:
    """Pinned because the direction is a decision, not an accident.

    Down keeps a reported total checkable against the price list: it can never
    exceed what the rates say. One token at 270_000 per million is 0.27
    micro-USD, which is zero.
    """

    assert PRICES.cost_micro_usd(TokenUsage(input_tokens=1)) == 0
    # And the control, so this is not merely asserting that small is zero:
    # four tokens cross 1.0 and are charged.
    assert PRICES.cost_micro_usd(TokenUsage(input_tokens=4)) == 1


def test_cache_writes_are_charged_where_a_provider_bills_for_them() -> None:
    """Zero on DeepSeek, so the field needs a rate of its own to be exercised.

    Otherwise the shipped fixture would leave this term untested and a pricer
    that ignored ``cache_write_tokens`` entirely would look correct.
    """

    prices = PRICES.model_copy(update={"cache_write_micro_usd_per_mtok": 500_000})

    assert prices.cost_micro_usd(TokenUsage(cache_write_tokens=1_000_000)) == 500_000
    assert PRICES.cost_micro_usd(TokenUsage(cache_write_tokens=1_000_000)) == 0
