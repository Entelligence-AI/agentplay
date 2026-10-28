"""The rate card: model pricing data and model-name normalization.

What lives here (data and pure string functions only):

- ``MODEL_RATES`` - the rate card. USD per **million** tokens.
  ``{"input": .., "output": ..}`` with optional per-million ``cache_read``
  / ``cache_write`` overrides; absent cache keys mean the standard
  multipliers apply (read 0.1x input, 5-minute write 1.25x, 1-hour write 2x).
  An entry pinned to ``None`` means "listed but price unfindable": lookups
  hit the unknown-model path, which must stay LOUD (warning), never a
  silent $0.
- ``strip_variant_suffix`` / ``canonicalize_family_order`` - the
  sampling-mode normalizer: agents decorate model names with effort/speed
  tokens ("claude-opus-4-8-thinking-medium", "composer-2-fast") and Cursor
  reverses family/version ("claude-4.6-sonnet"). Those select a sampling
  mode, not a different SKU (the provider bills the base model), so names
  are normalized before lookup instead of hand-adding every spelling.

House rule for edits: rates change HERE and nowhere else. When adding an
entry, cite the price-card URL in a comment like the existing rows do.
"""

from __future__ import annotations

import re

# ── the rate card ────────────────────────────────────────────────
MODEL_RATES: dict[str, dict[str, float] | None] = {
    # Base input/output rates from https://claude.com/pricing (Model pricing).
    # Prompt-cache rates aren't stored per model: cache reads/writes are derived
    # from `input` via the default multipliers
    # (read 0.1x, 5-min write 1.25x), which reproduce the published cache prices
    # exactly for every model below. Opus 4.7+ uses a new tokenizer (up to ~35%
    # more tokens for the same text) — that affects token COUNTS, already
    # captured per session, not the per-token rate here.
    # ── Anthropic (via Bedrock, us.* regional prefix) ──
    "us.anthropic.claude-opus-5": {"input": 5.0, "output": 25.0},
    "us.anthropic.claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "us.anthropic.claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "us.anthropic.claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "us.anthropic.claude-opus-4-5": {"input": 5.0, "output": 25.0},
    "us.anthropic.claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    # Haiku 4.5 is $1.00/$5.00 per https://claude.com/pricing — the previous
    # $0.8/$4.0 here was Haiku 3.5's rate and underpriced every 4.5 session 20%.
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 1.0, "output": 5.0},
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3.0, "output": 15.0},
    # Anthropic (via Bedrock — global.* cross-region prefix)
    "global.anthropic.claude-opus-5": {"input": 5.0, "output": 25.0},
    "global.anthropic.claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "global.anthropic.claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "global.anthropic.claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "global.anthropic.claude-opus-4-5": {"input": 5.0, "output": 25.0},
    "global.anthropic.claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "global.anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 1.0, "output": 5.0},
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3.0, "output": 15.0},
    # Anthropic direct / Vercel AI Gateway aliases
    "anthropic/claude-opus-4.8": {"input": 5.0, "output": 25.0},
    "anthropic/claude-opus-4.7": {"input": 5.0, "output": 25.0},
    "anthropic/claude-opus-4.6": {"input": 5.0, "output": 25.0},
    "anthropic/claude-opus-4.5": {"input": 5.0, "output": 25.0},
    "anthropic/claude-sonnet-4.6": {"input": 3.0, "output": 15.0},
    "anthropic/claude-sonnet-4.5": {"input": 3.0, "output": 15.0},
    "anthropic/claude-haiku-4.5": {"input": 1.0, "output": 5.0},
    "anthropic/claude-opus-5": {"input": 5.0, "output": 25.0},
    "anthropic/claude-fable-5": {"input": 10.0, "output": 50.0},
    "anthropic/claude-mythos-5": {"input": 10.0, "output": 50.0},
    # Short names (stripped prefixes)
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    # Verified 2026-09-28: https://platform.claude.com/docs/en/about-claude/pricing
    # $4/$20, 5m write $5 (default 1.25x), 1h write $8 (default 2x). Cache
    # reads are 0.05x input ($0.20), NOT the default 0.1x.
    "claude-opus-5-5": {
        "input": 4.0,
        "output": 20.0,
        "cache_read": 0.20,
        "cache_write": 5.0,
    },
    "claude-fable-5": {"input": 10.0, "output": 50.0},
    # Verified 2026-09-08: https://platform.claude.com/docs/en/models/fable-5-1/overview
    # Cache reads are 0.025x input, NOT Fable 5's default 0.1x.
    "claude-fable-5-1": {
        "input": 10.0,
        "output": 50.0,
        "cache_read": 0.25,
        "cache_write": 12.5,
    },
    "claude-mythos-5": {"input": 10.0, "output": 50.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0},
    # Verified 2026-09-28: https://platform.claude.com/docs/en/about-claude/pricing
    # Opus 4.1 and Opus 4 never took the 4.5-generation cut: $15/$75, 5m
    # write $18.75, read $1.50 (default multipliers). Both are retired on
    # the Claude API but can still appear in agent transcripts.
    "claude-opus-4-1": {"input": 15.0, "output": 75.0},
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    # Verified 2026-09-11: the planned September increase was cancelled.
    # https://platform.claude.com/docs/en/about-claude/pricing
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
    # Verified 2026-10-26: https://platform.claude.com/docs/en/about-claude/pricing
    # $2/$10, 5m write $2.50 (default 1.25x). Cache reads are 0.05x input
    # ($0.10), NOT the default 0.1x - same rule as Opus 5.5.
    "claude-sonnet-5-5": {
        "input": 2.0,
        "output": 10.0,
        "cache_read": 0.10,
        "cache_write": 2.50,
    },
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5-20250514": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    # Verified 2026-09-28: https://platform.claude.com/docs/en/about-claude/pricing
    # Sonnet 4 undated router id; same $3/$15 card as the dated SKU above.
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
    # Verified 2026-10-26: https://platform.claude.com/docs/en/about-claude/pricing
    # $0.10/$0.50 for prompts up to 100K tokens (cache read $0.01 and 5m write
    # $0.125 are the default multipliers). Prompts over 100K are $0.50/$2.50
    # with cache rates scaled the same way: 5x input, cache and output.
    "claude-haiku-5-5": {
        "input": 0.10,
        "output": 0.50,
        "long_context_threshold": 100_000,
        "long_context_input_multiplier": 5.0,
        "long_context_output_multiplier": 5.0,
    },
    "claude-3-7-sonnet-latest": {"input": 3.0, "output": 15.0},
    "claude-3-5-sonnet-20241022": {"input": 3.0, "output": 15.0},
    # Haiku 3.5 (not 4.5) really is $0.80/$4.00 — do not "fix" to match 4.5.
    "claude-3-5-haiku-20241022": {"input": 0.8, "output": 4.0},
    # Haiku 3 (router ``claude-3-haiku``; canonical order ``claude-haiku-3``).
    # Retired 2026-04-20 and dropped from the live pricing page; verified
    # 2026-09-28 against Anthropic's own page as archived 2025-09-16:
    # https://web.archive.org/web/20250916142058/https://docs.anthropic.com/en/docs/about-claude/pricing
    # $0.25/$1.25, 5m write $0.30 (1.2x, not 1.25x), read $0.03 (not 0.1x).
    "claude-haiku-3": {
        "input": 0.25,
        "output": 1.25,
        "cache_read": 0.03,
        "cache_write": 0.30,
    },
    # Cursor-style "-thinking" aliases: thinking is a mode, not a rate card —
    # billed at the base model's prices (https://claude.com/pricing; thinking
    # tokens are ordinary output tokens).
    "claude-4.5-sonnet-thinking": {"input": 3.0, "output": 15.0},
    "claude-4.5-haiku-thinking": {"input": 1.0, "output": 5.0},
    "claude-4.5-opus-thinking": {"input": 5.0, "output": 25.0},
    # ── OpenAI ───────────────────────────────────────────────────────────────
    # Standard-tier input/output from the OpenAI pricing page. (The cached-input
    # column isn't stored — the agent-cost cache split is Anthropic-shaped and
    # OpenAI sessions here carry no cache breakdown.) Codex variants are priced
    # at their base model's rate, matching the existing gpt-5.2-codex entry.
    # GPT-5.5 cached input is $0.50/M on OpenAI DIRECT (90% off, per the OpenAI
    # pricing page and every tracker; verified 2026-08-07). The previous $5-flat
    # cache_read here over-generalized a gateway-specific rate (that
    # gateway charges cached = uncached) to the model itself, over-billing every cache-heavy
    # direct Codex session ~10x on cache reads. If gateway traffic ever needs
    # pricing, give its model ref its own no-discount entry instead of
    # re-widening this one. cache_write stays at the input rate: OpenAI bills
    # cache writes as ordinary input, so this keeps the write term neutral.
    "gpt-5.5": {"input": 5.0, "output": 30.0, "cache_read": 0.50, "cache_write": 5.0},
    # GPT-5.6 Sol — $5/$30 per the OpenAI list price mirrored at
    # https://openrouter.ai/openai/gpt-5.6-sol and
    # https://www.aipricing.guru/openai-pricing/ (July 2026).
    "gpt-5.6-sol": {"input": 5.0, "output": 30.0},
    # Verified 2026-09-08: https://developers.openai.com/api/docs/models/gpt-6-astra
    # STANDARD base rates. >272K input PER REQUEST costs 2x input/cache and
    # 1.5x output. Session aggregates cannot determine that request tier;
    # this card's session estimate is not an exact long-context invoice.
    "gpt-6-astra": {
        "input": 10.0,
        "output": 50.0,
        "cache_read": 1.0,
        "cache_write": 12.5,
        "long_context_threshold": 272_000,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
    },
    # GPT-5.6 Terra / Luna — OpenAI LIST price per the 2026-07-30 price cut
    # (Luna -80%, Terra to $2/$12; https://www.aipricing.guru/openai-pricing/,
    # openai.com "Advancing the price-performance frontier with GPT-5.6").
    # Sessions priced here run on the user's own account (Codex reports
    # terra), so list is the right card.
    # Cached input is 90% off and cache writes 1.25x for the whole 5.6 family,
    # which the default multipliers already reproduce — no overrides needed.
    "gpt-5.6-terra": {"input": 2.0, "output": 12.0},
    "gpt-5.6-luna": {"input": 0.20, "output": 1.20},
    # Verified 2026-09-23: https://developers.openai.com/api/docs/models/gpt-6-luna
    # Standard rates; requests above 272K input tokens use 2x input/cache
    # and 1.5x output for the full request.
    "gpt-6-luna": {
        "input": 0.10,
        "output": 0.50,
        "cache_read": 0.01,
        "cache_write": 0.125,
        "long_context_threshold": 272_000,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
    },
    # Verified 2026-10-26: https://developers.openai.com/api/docs/models/gpt-6-sol
    # $2/$10, cached input $0.20, cache writes $2.50 (1.25x). Above 272K input
    # tokens: 2x input and cache, 1.5x output for the full request.
    "gpt-6-sol": {
        "input": 2.0,
        "output": 10.0,
        "cache_read": 0.20,
        "cache_write": 2.50,
        "long_context_threshold": 272_000,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
    },
    # Verified 2026-10-26: https://developers.openai.com/api/docs/models/o3 and
    # https://developers.openai.com/api/docs/models/o4-mini (Standard tier).
    # No cache-write rate is published, so writes bill as ordinary input.
    "o3": {"input": 2.0, "output": 8.0, "cache_read": 0.50, "cache_write": 2.0},
    "o4-mini": {"input": 1.10, "output": 4.40, "cache_read": 0.275, "cache_write": 1.10},
    # GPT-5.3-Codex-Spark is a RESEARCH PREVIEW with no OpenAI-published rate
    # card as of 2026-07. ESTIMATE: priced at the base gpt-5.3-codex API card
    # ($1.75/$14) — the assumption third-party estimators also make — because a
    # plausible estimate beats a silent $0. Revisit when OpenAI finalizes rates.
    # "-high" is a reasoning-effort tag, not a separate rate card.
    "gpt-5.3-codex-spark": {"input": 1.75, "output": 14.0},
    "gpt-5.3-codex-spark-high": {"input": 1.75, "output": 14.0},
    "gpt-5.4": {"input": 2.5, "output": 15.0},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.5},
    "gpt-5.4-nano": {"input": 0.2, "output": 1.25},
    "gpt-5.2": {"input": 1.75, "output": 14.0},
    "gpt-5": {"input": 1.25, "output": 10.0},
    "gpt-5-mini": {"input": 0.25, "output": 2.0},
    "gpt-5-nano": {"input": 0.05, "output": 0.4},
    "gpt-5-codex": {"input": 1.25, "output": 10.0},
    "gpt-5.3-codex": {"input": 1.75, "output": 14.0},
    "openai/gpt-5.3-codex": {"input": 1.75, "output": 14.0},
    # codex-max/-mini have no separately published rate; priced at the closest
    # base tier (gpt-5.1 / gpt-5-mini) per the codex=base convention above.
    "gpt-5.1-codex-max": {"input": 1.25, "output": 10.0},
    "gpt-5.1-codex-mini": {"input": 0.25, "output": 2.0},
    "openai/gpt-5.2-codex": {"input": 1.75, "output": 14.0},
    "gpt-5.2-codex": {"input": 1.75, "output": 14.0},
    "openai/gpt-4.1": {"input": 2.0, "output": 8.0},
    "openai/gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "openai/gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-5.1": {"input": 1.25, "output": 10.0},
    "gpt-5.3-chat-latest": {"input": 2.0, "output": 8.0},
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.0, "output": 30.0},
    # ── Google ───────────────────────────────────────────────────────────────
    "google/gemini-3-flash": {"input": 0.5, "output": 3.0},
    "gemini/gemini-2.5-flash-preview-05-20": {"input": 0.5, "output": 3.0},
    "google/gemini-2.5-flash": {"input": 0.5, "output": 3.0},
    "gemini-3-flash": {"input": 0.5, "output": 3.0},
    "gemini-2.0-flash": {"input": 0.1, "output": 0.4},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.0},
    # Verified 2026-10-26: https://ai.google.dev/gemini-api/docs/pricing
    # $1.25/$10 and cache read $0.125 for prompts up to 200K tokens; above
    # that $2.50/$15 and $0.25: 2x input and cache, 1.5x output.
    "gemini-2.5-pro": {
        "input": 1.25,
        "output": 10.0,
        "cache_read": 0.125,
        "long_context_threshold": 200_000,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
    },
    "gemini-1.5-flash": {"input": 0.075, "output": 0.3},
    # ── Zhipu (Z.ai) ────────────────────────────────────────────────────────
    # GLM-5.2 — $1.40/$4.40 on the official Z.ai API, per
    # https://www.requesty.ai/models/zai/glm-5.2 (mirrors the official rate;
    # resellers like OpenRouter discount below it — we pin the official rate).
    "glm-5.2": {"input": 1.4, "output": 4.4},
    # Fireworks' router alias for the same SKU.
    "FW-GLM-5.2": {"input": 1.4, "output": 4.4},
    # ── Cursor Composer ─────────────────────────────────────────────────────
    # Rates from https://cursor.com/docs/models-and-pricing.md (composer-1,
    # composer-2.5 incl. cache-read) and https://www.vantage.sh/blog/cursor-composer-2
    # + https://www.digitalapplied.com/blog/cursor-composer-1-5-ai-coding-model-guide
    # (composer-2 / composer-1.5). Cache keys are explicit per-1M overrides
    # where Cursor publishes them (Cursor's read rate is 0.1x input; no
    # published write surcharge for composer-1/2.5).
    "composer-1": {"input": 1.25, "output": 10.0, "cache_read": 0.125},
    "composer-1.5": {
        "input": 3.5,
        "output": 17.5,
        "cache_read": 0.35,
        "cache_write": 3.5,
    },
    "composer-2": {"input": 0.5, "output": 2.5},
    "composer-2.5": {"input": 0.5, "output": 2.5, "cache_read": 0.2},
    # ── Morph ───────────────────────────────────────────────────────────────
    # Morph API pricing unknown — None (not 0/0) so lookups hit the LOUD
    # unknown-model path instead of silently recording $0, per the module
    # contract above.
    "morph-v3-large": None,
    # ── Fireworks / deployed models ─────────────────────────────────────────
    # Full serverless deepseek-r1 (671B MoE), as logged for
    # model="accounts/fireworks/models/deepseek-r1".
    "deepseek-r1": {"input": 0.56, "output": 1.68},
    # ── MiniMax / Moonshot ────────────────
    # MiniMax M3 standard tier (≤512K input): $0.30/$1.20, cache read $0.06
    # per https://openrouter.ai/minimax/minimax-m3 (mirrors the official
    # billed rate; the >512K tier is 2x, not modeled; typical sessions are
    # far below it).
    "minimax-m3": {"input": 0.30, "output": 1.20, "cache_read": 0.06},
    # Kimi K3: $3/$15 flat across the 1M window, cache-hit input $0.30 —
    # exactly the default 0.1x read multiplier, so no override needed.
    # https://openrouter.ai/moonshotai/kimi-k3
    "kimi-k3": {"input": 3.0, "output": 15.0},
    # ── xAI Grok. Base tier (<200K prompts):
    # grok-4.5 $2/$6 (cached input $0.50 = 0.25x, override), grok-code-fast-1
    # $1/$2 per https://benchlm.ai/xai/api-pricing + layer3labs guides.
    # The >=200K high-context 2x surcharge is not modeled (tier data absent
    # per session); grok-4.6 mirrors 4.5's published base rate.
    "grok-4.5": {"input": 2.0, "output": 6.0, "cache_read": 0.50},
    "grok-4.6": {"input": 2.0, "output": 6.0, "cache_read": 0.50},
    "grok-code-fast-1": {"input": 1.0, "output": 2.0},
    # ── OpenAI gpt-5.1 base tier (codex=base convention above) ──────────────
    "gpt-5.1": {"input": 1.25, "output": 10.0},
    "gpt-5.1-codex": {"input": 1.25, "output": 10.0},
    # ── Cursor display-name aliases (UI labels, not catalog ids) ────────────
    # "Opus 5 (Auto Balanced)" slugs to opus-5 after the paren-strip +
    # space-slug candidates in normalize_model_key.
    "opus-5": {"input": 5.0, "output": 25.0},
    # ── Local inference — DELIBERATELY $0, not unpriced ─────────────────────
    # Ollama/MLX/gguf models run on the user's own hardware: a $0 rate is the
    # honest price, and an explicit entry keeps them out of the unpriced
    # warning.
    #
    # `"free": True` marks the zero as the real price. Without it these entries
    # are indistinguishable from an all-zero PLACEHOLDER whose rate is unknown.
    "gemma4": {"input": 0.0, "output": 0.0, "free": True},
    "qwen3.8:27b-mlx-32k": {"input": 0.0, "output": 0.0, "free": True},
    "canned-1b.gguf": {"input": 0.0, "output": 0.0, "free": True},
    # Ollama tags carry an explicit `:<size>` suffix and always run locally.
    # Same reasoning - genuinely free, not unknown.
    "qwen3-coder:30b": {"input": 0.0, "output": 0.0, "free": True},
    "qwen2.5-coder:7b": {"input": 0.0, "output": 0.0, "free": True},
    "qwen2.5-coder:1.5b": {"input": 0.0, "output": 0.0, "free": True},
    "gpt-oss:20b": {"input": 0.0, "output": 0.0, "free": True},
    # ── Moonshot Kimi K2.x ──────────────────────
    # K2.7 Code: Moonshot's standard rate is $0.95/$4.00 with cache-hit input $0.19
    # (openrouter.ai/moonshotai/kimi-k2.7-code, benchlm.ai/moonshot/api-pricing,
    # Aug 2026). Third-party resellers quote $0.67-$0.95 in; the official rate
    # is used here, consistent with kimi-k3 above. Cache read is an explicit
    # override because 0.19/0.95 = 0.2x, not the default 0.1x.
    "kimi-k2.7-code": {"input": 0.95, "output": 4.0, "cache_read": 0.19},
    # K2.5: Moonshot's own rate is $0.60/$3.00 (OpenRouter resells at
    # $0.375/$2.025). Official rate, same convention as above. Cache read
    # $0.07 is published, an explicit override at ~0.12x.
    "kimi-k2.5": {"input": 0.60, "output": 3.0, "cache_read": 0.07},
    # ── Google Gemini 3.1 Pro ───────────────────
    # $2/$12 base tier (<=200K prompts), cache read $0.20. The >200K tier is
    # $4/$18 and is NOT modeled — per-session prompt tier is not recorded,
    # the same simplification the Grok entries above make.
    "gemini-3.1-pro-preview": {"input": 2.0, "output": 12.0, "cache_read": 0.20},
    # DeepSeek V4 Pro — $0.44/$0.88 official base rate (¥3/¥6 per 1M),
    # verified 2026-08-18 (openrouter.ai/deepseek/deepseek-v4-pro and
    # orcarouter.ai list the same figures). CAVEAT: DeepSeek introduced
    # peak/off-peak OUTPUT tiers on 2026-08-17 (¥13.5/¥27 per 1M); this entry
    # keeps the flat base rate.
    "deepseek-v4-pro": {"input": 0.44, "output": 0.88},
    "gpt-3.5-turbo-16k": {"input": 3.00, "output": 4.00},
    "gpt-4o-2024-08-06": {"input": 2.50, "output": 10.00},
    "gpt-4o-2024-11-20": {"input": 2.50, "output": 10.00},
    # ── NVIDIA ──────────────────────────────────────────────────────────────
    "nvidia.nemotron-super-3-120b": {"input": 0.20, "output": 0.20},
    # ── Bedrock catalog spellings + Haiku 4.6 ──────────────────────────────
    # Cache rates for all of them are the standard Anthropic multipliers, so
    # input/output suffices.
    "global.anthropic.claude-opus-4-8-v1:0": {"input": 5.0, "output": 25.0},
    "global.anthropic.claude-opus-5-v1:0": {"input": 5.0, "output": 25.0},
    "global.anthropic.claude-fable-5-v1:0": {"input": 10.0, "output": 50.0},
    "global.anthropic.claude-mythos-5-v1:0": {"input": 10.0, "output": 50.0},
    # AWS public pricing, 2026-09-12: global $2/$10, commercial geo $2.20/$11.
    "global.anthropic.claude-sonnet-5-v1:0": {"input": 2.0, "output": 10.0},
    "us.anthropic.claude-sonnet-5-v1:0": {"input": 2.2, "output": 11.0},
    "eu.anthropic.claude-sonnet-5-v1:0": {"input": 2.2, "output": 11.0},
    "ap.anthropic.claude-sonnet-5-v1:0": {"input": 2.2, "output": 11.0},
    "global.anthropic.claude-haiku-4-6-v1:0": {"input": 1.0, "output": 5.0},
    "claude-haiku-4-6": {"input": 1.0, "output": 5.0},
}

# Rate-card keys are matched case-INSENSITIVELY: every lookup resolves through
# `normalize_model_key`, which lower-cases the id. Fold any as-authored mixed-case
# key to lowercase so it stays reachable. Folded in place so the literal above
# keeps its human-readable spelling.
MODEL_RATES = {k.lower(): v for k, v in MODEL_RATES.items()}

# Provider spellings share the exact card entry, so cache-rate corrections
# cannot drift between the short id, gateway and Bedrock spellings.
for _alias in (
    "anthropic/claude-fable-5-1",
    "anthropic.claude-fable-5-1",
    "anthropic.claude-fable-5-1-v1:0",
    "global.anthropic.claude-fable-5-1",
    "global.anthropic.claude-fable-5-1-v1:0",
):
    MODEL_RATES[_alias] = MODEL_RATES["claude-fable-5-1"]
# Opus 5.5 under every spelling its Opus 5 sibling carries: dotted gateway
# id (OpenRouter's anthropic/claude-opus-5.5), Bedrock global + regional
# profiles (regional collapses to global — same card, as for Opus 5), the
# Bedrock catalog ``-v1:0`` form, and Cursor's "Opus 5.5 (...)" display slug.
for _alias in (
    "anthropic/claude-opus-5.5",
    "anthropic/claude-opus-5-5",
    "us.anthropic.claude-opus-5-5",
    "global.anthropic.claude-opus-5-5",
    "global.anthropic.claude-opus-5-5-v1:0",
    "opus-5.5",
):
    MODEL_RATES[_alias] = MODEL_RATES["claude-opus-5-5"]
MODEL_RATES["openai/gpt-6-astra"] = MODEL_RATES["gpt-6-astra"]
MODEL_RATES["openai/gpt-6-luna"] = MODEL_RATES["gpt-6-luna"]

# ── sampling-mode normalization ─────────────────────────────────────────
# Effort / speed / reasoning tokens agents append to a model name. They
# select a SAMPLING MODE, not a different priced model — the provider bills
# the base model's rates — but they made catalog lookups miss and price the
# session at $0 while their base names priced correctly.
VARIANT_TOKENS: frozenset[str] = frozenset(
    {"thinking", "high", "medium", "low", "fast", "xhigh", "minimal", "max"}
)

# Cursor emits the Claude family and version in the opposite order
# ("claude-4.6-sonnet" for "claude-sonnet-4-6"), which no catalog knows.
_CURSOR_FAMILY_RE = re.compile(
    r"^claude-(?P<ver>\d+(?:[.\-]\d+)?)-(?P<family>opus|sonnet|haiku|fable|mythos)$"
)

# Gateway / router ids keep the family first but write the version dotted
# ("claude-opus-4.6", the LiteLLM router and OpenRouter spelling), while the
# card keys the dashed first-party id ("claude-opus-4-6"). Same SKU, same
# bill - fold the dot so both spellings reach the one entry.
_DOTTED_VERSION_RE = re.compile(
    r"^claude-(?P<family>opus|sonnet|haiku|fable|mythos)-(?P<major>\d+)\.(?P<minor>\d+)$"
)


def strip_variant_suffix(s: str) -> str:
    """Drop trailing effort/reasoning-mode tokens (see ``VARIANT_TOKENS``).

    Only whole hyphen-delimited tokens are removed, and only from the tail,
    so dated ids (``claude-haiku-4-5-20251001``) and names that merely
    contain a variant word are untouched.
    """
    parts = s.split("-")
    while len(parts) > 1 and parts[-1] in VARIANT_TOKENS:
        parts.pop()
    return "-".join(parts)


def canonicalize_family_order(s: str) -> str:
    """``claude-4.6-sonnet`` / ``claude-sonnet-4.6`` → ``claude-sonnet-4-6``.

    Cursor reverses family and version; gateways keep the order but dot the
    version. Both are spellings of the dashed first-party id.
    """
    m = _CURSOR_FAMILY_RE.match(s)
    if m:
        return f"claude-{m.group('family')}-{m.group('ver').replace('.', '-')}"
    m = _DOTTED_VERSION_RE.match(s)
    if m:
        return f"claude-{m.group('family')}-{m.group('major')}-{m.group('minor')}"
    return s


# Bedrock regional inference-profile prefixes. The card stores one entry per
# model under the ``global.`` catalog form unless an explicit regional rate
# exists. Preserve known regional premiums before the legacy global fallback.
_BEDROCK_REGIONAL_PREFIXES = ("us.anthropic.", "eu.anthropic.", "ap.anthropic.")


_BEDROCK_GLOBAL_PREFIX = "global.anthropic."


def canonicalize_bedrock_region(s: str) -> str:
    """Preserve explicit regional premiums; otherwise use the global catalog key.

    The model part is family-canonicalized too: ``canonicalize_family_order``
    only matches bare ``claude-…`` strings, so a reversed family INSIDE a
    prefixed id (``us.anthropic.claude-4.6-sonnet``) would otherwise survive
    the region rewrite un-fixed and miss the card.
    """
    for prefix in (*_BEDROCK_REGIONAL_PREFIXES, _BEDROCK_GLOBAL_PREFIX):
        if s.startswith(prefix):
            tail = canonicalize_family_order(s[len(prefix) :])
            regional = prefix + tail
            global_key = _BEDROCK_GLOBAL_PREFIX + tail
            if regional in MODEL_RATES and MODEL_RATES[regional] != MODEL_RATES.get(global_key):
                return regional
            return global_key
    return s


# Fireworks catalog ids encode version dots as ``p`` (``glm-5p2`` is GLM-5.2,
# ``llama-v3p1`` is Llama 3.1) and arrive as full paths
# (``accounts/fireworks/models/glm-5p2``); neither the path nor the ``p``
# spelling would otherwise reach the card's ``glm-5.2`` entry.
_FIREWORKS_DOT_RE = re.compile(r"(?<=\d)p(?=\d)")

# Trailing release-date stamp (``z-ai/glm-5.2-20260616``). Candidate-only:
# a dated SKU with its own card entry (``claude-haiku-4-5-20251001``) hits
# the card exactly BEFORE this strip is ever tried, so it never rewrites a
# genuinely dated price.
_DATE_SUFFIX_RE = re.compile(r"-20\d{6}$")

# Cursor UI display labels: a parenthetical mode suffix ("Cursor Grok 4.5
# (Auto Balanced)") and spaces instead of hyphens. Both are candidates only —
# adopted solely on an exact card hit, same contract as the path/dot rules.
_PAREN_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _display_slugged(s: str) -> str:
    """``cursor grok 4.5 (auto balanced)`` → ``cursor-grok-4.5``."""
    return _PAREN_SUFFIX_RE.sub("", s).strip().replace(" ", "-")


def _cursor_prefix_stripped(s: str) -> str:
    """``cursor-grok-4.5`` → ``grok-4.5`` — Cursor prefixes third-party SKUs."""
    return s[len("cursor-") :] if s.startswith("cursor-") else s


def _dot_folded(s: str) -> str:
    """``glm-5p2`` → ``glm-5.2`` (Fireworks' dot encoding, digits-only guard)."""
    return _FIREWORKS_DOT_RE.sub(".", s)


def _date_stripped(s: str) -> str:
    """``glm-5.2-20260616`` → ``glm-5.2`` (trailing release-date stamp)."""
    return _DATE_SUFFIX_RE.sub("", s)


def normalize_model_key(model_id: str) -> str:
    """Lower-case + variant-strip + family-order + region/path/dot fix — the shared key.

    A real SKU whose tail merely LOOKS like a sampling-mode token — e.g. the
    priced ``gpt-5.1-codex-max`` (the card carries the ``-max`` SKU, not a bare
    ``gpt-5.1-codex``) — is returned WITHOUT variant-stripping so it keeps
    matching the card. Generic effort/speed tails (``-high``, ``-thinking``, …)
    still strip, because for those the base name IS the priced model. In short:
    prefer an exact rate-card hit before applying generic suffix stripping
.

    Provider catalog paths (``accounts/<owner>/models/<name>``) and Fireworks'
    dot-encoding are handled as CANDIDATES only: a transform is adopted solely
    on an exact rate-card hit, so any id these rules don't recognize returns
    exactly what it did before this change (no fallback-behavior drift).
    """
    lowered = (model_id or "").lower()
    if "claude" in lowered:
        lowered = re.sub(r"(?:\[1m\]|-1m)$", "", lowered)
    unstripped = canonicalize_bedrock_region(canonicalize_family_order(lowered))
    if unstripped in MODEL_RATES:
        return unstripped
    base = lowered.rsplit("/", 1)[-1] if "/" in lowered else lowered
    slugged = _display_slugged(base)
    for cand in (
        canonicalize_family_order(base),
        _dot_folded(canonicalize_family_order(base)),
        _date_stripped(canonicalize_family_order(base)),
        canonicalize_family_order(strip_variant_suffix(base)),
        _dot_folded(canonicalize_family_order(strip_variant_suffix(base))),
        _date_stripped(_dot_folded(canonicalize_family_order(strip_variant_suffix(base)))),
        # Cursor display labels and cursor- prefixed SKUs:
        # each is candidate-only — adopted solely on an exact card hit.
        slugged,
        strip_variant_suffix(slugged),
        _cursor_prefix_stripped(strip_variant_suffix(slugged)),
    ):
        if cand in MODEL_RATES:
            return cand
    return canonicalize_bedrock_region(canonicalize_family_order(strip_variant_suffix(lowered)))


__all__ = [
    "canonicalize_bedrock_region",
    "MODEL_RATES",
    "VARIANT_TOKENS",
    "canonicalize_family_order",
    "normalize_model_key",
    "response_speed_multiplier",
    "strip_variant_suffix",
]


def response_speed_multiplier(model: str, speed: str | None) -> float | None:
    """Price observed response speed; unsupported premium evidence stays unknown.

    Anthropic fast-mode rates verified 2026-09-11: Opus 5/4.8 use $10/$50
    versus $5/$25, with the same multiplier on cache rates. Do not infer this
    from a requested mode: fallback responses explicitly report standard.
    https://platform.claude.com/docs/en/build-with-claude/fast-mode
    Opus 5.5 fast mode verified 2026-09-28: $8/$40 versus $4/$20 (also 2x),
    https://platform.claude.com/docs/en/about-claude/pricing
    """
    if speed in (None, "", "standard"):
        return 1.0
    key = normalize_model_key(model)
    for prefix in ("anthropic/", "us.anthropic.", "global.anthropic."):
        key = key.removeprefix(prefix)
    key = re.sub(r"-v\d+:\d+$", "", key)
    key = key.replace("opus-4.8", "opus-4-8").replace("opus-5.5", "opus-5-5")
    if speed == "fast" and key in {
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "opus-5-5",
        "opus-5",
        "opus-4-8",
    }:
        return 2.0
    return None
