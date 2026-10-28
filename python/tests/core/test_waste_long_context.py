"""Marginal waste prices each request at its own long-context tier."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentplay._core.ingestion.coding_agent.session_pure import _marginal_waste_usd
from agentplay._core.ingestion.coding_agent.spans import SpanData


def _turn(n: int, *requests: tuple[int, int]) -> SpanData:
    """A turn whose ``(input, output)`` requests are its whole billed usage."""
    now = datetime.now(UTC)
    events = [{"model": "gpt-6-luna", "input_tokens": inp, "output_tokens": out} for inp, out in requests]
    return SpanData(
        span_id=f"s{n}",
        trace_id="t",
        parent_id="root",
        name=f"Turn {n}",
        span_kind="LLM",
        status_code="OK",
        start_time=now,
        end_time=now,
        attributes={
            "llm.token_count.input_fresh": sum(inp for inp, _ in requests),
            "llm.token_count.completion": sum(out for _, out in requests),
            "llm.cost_events": events,
        },
    )


def _retry_usd(*turns: SpanData) -> float:
    return _marginal_waste_usd(list(turns), [], {1}, set(), "gpt-6-luna")["retry_waste_marginal_usd"]


def test_a_long_request_in_a_retry_turn_is_priced_at_the_long_context_tier():
    # gpt-6-luna: $0.10 in / $0.50 out per 1M; above 272K input the whole
    # request bills at 2x input and 1.5x output ($0.20 / $0.75).
    usd = _retry_usd(_turn(1, (300_000, 10_000)), _turn(2, (1_000, 1_000)))
    assert usd == pytest.approx(300_000 * 0.20 / 1e6 + 10_000 * 0.75 / 1e6)


def test_requests_below_the_threshold_stay_at_base_rates_even_when_the_turn_is_long():
    # Two 200K requests: the turn sums to 400K but neither request crosses 272K.
    usd = _retry_usd(_turn(1, (200_000, 0), (200_000, 0)), _turn(2, (1_000, 1_000)))
    assert usd == pytest.approx(400_000 * 0.10 / 1e6)


def test_a_request_exactly_at_the_threshold_is_not_long_context():
    usd = _retry_usd(_turn(1, (272_000, 0)), _turn(2, (1_000, 1_000)))
    assert usd == pytest.approx(272_000 * 0.10 / 1e6)
