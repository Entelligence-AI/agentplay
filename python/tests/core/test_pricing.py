from __future__ import annotations

import pytest

from agentplay import builder as sm
from agentplay._core.pricing import MODEL_RATES, normalize_model_key

M = 1_000_000

# Published per-1M prices (input, cache read, output), copied from each
# provider's page rather than derived from the card, so a wrong multiplier
# fails here. A second row is the long-context tier where one exists.
PUBLISHED = {
    "claude-sonnet-5-5": [(2.0, 0.10, 10.0)],
    "claude-haiku-5-5": [(0.10, 0.01, 0.50), (0.50, 0.05, 2.50)],
    "gpt-6-sol": [(2.0, 0.20, 10.0), (4.0, 0.40, 15.0)],
    "o3": [(2.0, 0.50, 8.0)],
    "o4-mini": [(1.10, 0.275, 4.40)],
    "gemini-2.5-pro": [(1.25, 0.125, 10.0), (2.50, 0.25, 15.0)],
}


@pytest.mark.parametrize("model", PUBLISHED)
def test_model_resolves_to_a_rate(model):
    assert MODEL_RATES.get(normalize_model_key(model))


@pytest.mark.parametrize("model", PUBLISHED)
def test_base_tier_matches_the_published_price(model):
    # 10K input tokens: below every long-context threshold.
    inp, read, out = PUBLISHED[model][0]
    n = 10_000
    assert sm.estimate_cost_usd(model, n, 0, 0, 0) == pytest.approx(inp * n / M)
    assert sm.estimate_cost_usd(model, 0, 0, n, 0) == pytest.approx(read * n / M)
    assert sm.estimate_cost_usd(model, 0, 0, 0, M) == pytest.approx(out)


@pytest.mark.parametrize("model", [m for m, rows in PUBLISHED.items() if len(rows) > 1])
def test_long_context_tier_matches_the_published_price(model):
    # One request one token over the threshold is billed entirely at the
    # published long-context prices; one at the threshold is not.
    threshold = MODEL_RATES[normalize_model_key(model)]["long_context_threshold"]
    for tokens, (inp, read, out) in ((threshold, PUBLISHED[model][0]), (threshold + 1, PUBLISHED[model][1])):
        cost = sm.estimate_cost_usd(model, 1, 0, tokens - 1, M)
        assert cost == pytest.approx((inp + (tokens - 1) * read + M * out) / M)
