# Promptaro Smart Router

Author: Akshay Dubey

Give one instruction. The router decides which model, which skills and which
checks the task needs, then returns a result that has passed those checks —
using the cheapest, fastest model expected to meet the quality bar and
escalating only when a check fails.

```
instruction ─► classify ─► choose model + ladder ─► pick skills ─► trim context ─► cache?
                 (local, <1 ms typical)                                              │
                                                                                     ▼
        record outcome ◄── escalate on failure ◄── cross-review (only when worth it) ◄── checks ◄── call model
        (improves future routing)
```

## What each feature in the spec maps to

| Feature | Where | How |
|---|---|---|
| Understand the task | `router/classifier.py` | Precompiled rules detect coding, research, writing, document, data or chat; score complexity 0–1; flag needs (JSON, web, code execution, vision, long context). Optional cheap-model classifier for ambiguous requests (`llm_classifier_below_confidence`, off by default). |
| Choose the model | `router/selector.py`, `router/catalog.py` | Every model family on OpenRouter (`vendors: all`, up to 30 families: Anthropic, OpenAI, Google, DeepSeek, xAI, Mistral, Meta Llama, Qwen, …), a small, medium and large current model each, with live prices; size class is capped by price. Per model: expected quality (family reputation prior blended with measured pass rate and human feedback), expected cost and latency. Picks the best cost/latency trade-off among models that clear the task's quality bar; easy requests prefer free models while the daily free allowance lasts; hard needs filter first; a circuit breaker skips failing providers. `RouteDecision.why()` explains the choice (difficulty, bar, options, savings). |
| Choose relevant skills | `router/skills.py`, `skills/*.md` | Each skill declares task types and trigger words; only matching skills are injected, under a token budget (max 3). |
| Control token use | `router/context.py`, `router/store.py`, `router/engine.py` | Large context is trimmed to the most relevant chunks; output length is capped per task type/complexity; identical requests hit a cache; system prompts use provider prompt caching; escalation retries send only failure reasons (plus the previous draft for code). |
| Check quality | `router/checks.py` | Empty/truncated/refusal checks; JSON validity; Python syntax and no placeholder bodies; **pytest runs** for code (generated tests or tests you supply); source links for research. Cross-model review only for high-complexity work that tests can't verify (`cross_review: auto`). |
| Escalate intelligently | `router/engine.py` | Climbs the ladder one step at a time (next-stronger model, or a similar-strength model from another provider), ending at the strongest. Hard tasks start on a strong model immediately. Respects `max_cost_usd`. |
| Improve routing | `router/store.py` | Every attempt is recorded (pass/fail, cost, latency, tokens) in SQLite, and so is human feedback (`Store.feedback`, worth 3 automatic checks each). Expected quality, latency and output length per model × task type × complexity update from real results. `GET /v1/stats` shows them. |
| Free allowance | `router/catalog.py` (`FreeQuota`) | Reads OpenRouter's daily free-request allowance, counts down locally, and pauses free models after a 429 (a minute, or until midnight UTC when the day's allowance is gone); other free providers fill in. |
| Free providers | `router/free_providers.py` | Groq, Cerebras, NVIDIA NIM, Mistral, Codestral, Cohere, Gemini free tier: dated free limits, key prefixes, privacy flags, model picks discovered live. |
| No-spend default | `router/catalog.py`, `router/selector.py` | Paid per-use models are skipped unless `allow_paid` is on; escalation, reviews and named picks follow the same rule, and `NoFreeModel` explains when nothing free is left. |
| Free-tier ledger | `router/quota.py` | Requests/tokens per minute, day, month per provider or model; live rate-limit headers; pauses only the refusing model; refuses requests that can't fit; computes the wait for per-minute limits. |
| Coding-tools gateway | `router/gateway.py` | `/v1/messages` (Anthropic, streaming + tools + images), `/v1/chat/completions`, `/v1/models`, `count_tokens`. Token-protected, free-only by default, failover before the first byte, tool-call arguments repaired. |
| Streaming | `router/providers.py` | `Provider.stream()` for Anthropic and OpenAI-compatible APIs; `Router.run(..., on_delta=...)` streams each attempt. |
| <100 ms routing | `bench.py` | Routing never calls a model or the database on the hot path. Measured below. |

## Getting started (no setup)

1. Unzip the folder anywhere (for example `~/Documents/smart-router`).
2. Double-click **Start Smart Router.command**.
   The first time, macOS may say it's from an unidentified developer: right-click the file → **Open** → **Open**.
   The first start installs what it needs (about a minute); after that it starts in seconds.
3. Your browser opens the app. Click **Sign in with OpenRouter**, log in, approve.
   That one sign-in gives the router Claude, GPT and Gemini. Add a few dollars of credit on OpenRouter if your account is new.
4. Type what you need and press **Run**.

Nothing to fill in: model names, prices and context sizes are pulled live from
OpenRouter's public model list and refreshed daily.

### Where your keys go

- Sign-in uses OpenRouter's standard OAuth (PKCE). The key it issues is stored in
  your **system keychain** — macOS Keychain, Windows Credential Manager (service name `akshay-smart-router`) — never in the project folder.
  Without a system keychain it falls back to `~/.promptaro/credentials.json` (legacy `~/.akshay-router` is migrated) with owner-only permissions.
- The app never displays or returns a stored key. **Disconnect** deletes it.
  You can also revoke it any time from your OpenRouter account.
- The server only listens on `127.0.0.1` and rejects requests addressed to any other host name.

### Optional: direct connections

On the same page you can paste an Anthropic, OpenAI or Google key ("Get key ↗" opens the
right page). Each key is checked with the provider before it is saved. When a direct key
exists, that vendor's models go direct (no OpenRouter fee); everything else still goes
through OpenRouter. Running **Ollama** is detected automatically and its models are used for
cheap, simple tasks.

Why there's no "Log in with Claude": Anthropic does not allow third-party apps to offer
Claude.ai login or use Free/Pro/Max subscription credentials; apps must use API keys
(https://code.claude.com/docs/en/legal-and-compliance). OpenRouter sign-in is the
one-click route that stays within those rules.

### For developers

```bash
python -m router.cli "write a bash script that backs up ~/Documents"      # CLI, same keys
python -m router.cli --route-only "design a multi-region GKE failover plan"
curl -s localhost:8765/v1/run -H 'content-type: application/json' -d '{"instruction":"..."}'
```

API docs at http://localhost:8765/docs. Docker (`docker compose up`) reads keys from
`.env` instead of the keychain, since a container can't see the system keychain.

## Measured routing latency

`python bench.py` (this sandbox, 300 iterations each):

| Case | p50 | p99 |
|---|---|---|
| Short instructions, no context | 0.05–0.18 ms | < 1 ms |
| 258 KB pasted document (context trimming) | ~25 ms | ~45–53 ms |

The model call itself dominates total time; routing is noise next to it.

## Limits worth knowing

- Quality is measured by checks, not guaranteed. Tests prove code runs; they
  don't prove the tests were the right ones. Writing quality is only verified
  by cross-review, which costs an extra call.
- Expected quality starts from the `prior` values you set; routing gets
  meaningfully better once each model has real results (≈10+ per task type).
- Generated code is executed by pytest. Run the router in the provided
  container (unprivileged user, read-only filesystem) or set
  `run_generated_code: false`.
- Web search is only used on direct Anthropic connections (Anthropic's
  server-side web search tool). Through OpenRouter, research answers come from
  the model's own knowledge.
- OpenRouter charges a small fee when you buy credits; a direct key avoids it
  for that vendor.

## Tests

```bash
python -m pytest -q     # 112 tests, fully offline (mocked providers and sign-in)
```

---
Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
