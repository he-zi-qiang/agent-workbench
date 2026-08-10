"""What a run's tokens cost, so a cost ceiling can be more than a wish.

``RunBudget`` has carried ``max_cost_micro_usd`` since it was written, and the
runtime has refused every request that set one: nothing turned tokens into
money, so the ceiling would have sat at zero for the whole run and never
fired. Refusing was right -- a limit that cannot be enforced must not be
accepted as one -- but it left the budget with no ceiling that grows with the
work, which is the ceiling a node doing real work actually needs (ADR-030).

This module is the missing arithmetic, and nothing more. It performs no I/O,
consults no provider, and does not discover prices: a deployment states what
its models charge and this converts a token count into micro-USD. Prices that
drifted from the provider's are a stale config file, which is visible, rather
than a silently mispriced ceiling.

Micro-USD is the unit because the domain counts money in integers. Floating
point accumulating over hundreds of turns is a ceiling that depends on the
order the turns arrived in.
"""

from __future__ import annotations

from pydantic import Field

from agent_workbench.domain.runs import TokenUsage
from agent_workbench.domain.schema import DomainModel

#: Rates are quoted per million tokens because that is how providers publish
#: them, and copying a published number without arithmetic is the transcription
#: least likely to go wrong.
TOKENS_PER_RATE_UNIT = 1_000_000


class ModelPrices(DomainModel):
    """What one model charges, in micro-USD per million tokens.

    No defaults here. This is not the operator-facing surface -- it is built
    from a projection that always supplies all four -- and a default at this
    layer would only make it possible to construct a half-filled price list in
    code and have it look complete. The one rate an operator may leave out,
    ``cache_write``, defaults where operators write it and arrives here filled
    in.
    """

    input_micro_usd_per_mtok: int = Field(ge=0)
    output_micro_usd_per_mtok: int = Field(ge=0)
    #: Usually far below the input rate; that discount is the whole reason
    #: prompt caching is on by default.
    cache_read_micro_usd_per_mtok: int = Field(ge=0)
    #: Zero on providers that do not bill separately for writing the cache.
    #: Zero because it is free, which is a price, not because it is unknown.
    cache_write_micro_usd_per_mtok: int = Field(ge=0)

    def cost_micro_usd(self, usage: TokenUsage) -> int:
        """What ``usage`` costs at these rates.

        ``input_tokens`` includes the cached ones. That is the provider's
        convention rather than this project's -- DeepSeek reports
        ``prompt_tokens`` as the whole prompt and ``prompt_cache_hit_tokens``
        as the part of it that was cached, and the adapter passes both through
        unchanged (pinned by ``tests/contracts/test_deepseek_model.py``:
        ``prompt_tokens=118`` with ``prompt_cache_hit_tokens=64`` arrives as
        ``input_tokens=118, cache_read_tokens=64``). So the cached part is
        subtracted before the full input rate is applied. Charging both rates
        over the same tokens would bill the cached prompt at more than an
        uncached one, and a cost ceiling would fire earliest exactly on the
        deployments that configured caching to spend less.

        A cache report larger than the prompt it belongs to is clamped rather
        than allowed to go negative. It should not happen; if a provider ever
        sends it, a run that overcharges slightly is a better failure than one
        whose spend goes down as it works.
        """

        uncached_input = max(0, usage.input_tokens - usage.cache_read_tokens)
        return (
            _at(uncached_input, self.input_micro_usd_per_mtok)
            + _at(usage.cache_read_tokens, self.cache_read_micro_usd_per_mtok)
            + _at(usage.output_tokens, self.output_micro_usd_per_mtok)
            + _at(usage.cache_write_tokens, self.cache_write_micro_usd_per_mtok)
        )


def _at(tokens: int, micro_usd_per_mtok: int) -> int:
    """Integer micro-USD, rounded down.

    Down rather than to-nearest so that a spend total is never larger than what
    the rates say, which keeps "this run cost X" a statement the price list can
    be checked against. The residue is under one micro-USD per rate per turn --
    a millionth of a dollar against rates in the hundreds of thousands per
    million tokens -- so it cannot accumulate into a ceiling that misses.
    """

    return tokens * micro_usd_per_mtok // TOKENS_PER_RATE_UNIT


__all__ = ["TOKENS_PER_RATE_UNIT", "ModelPrices"]
