# Promptaro engineering checkpoint

Owner: Akshay Dubey. Branch: `audit/promptaro-baseline` (work happens here; main untouched).

## Baseline (recorded 2026-10-16)

- Start commit: `c0e4655` "Initial commit: Tillwright app and Smart Router" (only commit on main).
- Env: macOS, Python 3.14.6, venv at repo root `.venv/` (fastapi 0.143.3, httpx 0.28.1,
  pydantic 2.14.1, pytest 9.1.1, uvicorn 0.54.0, keyring 25.7.0, PyYAML 6.0.3).
- Tests: `smart-router`: 90 pass (2.65s). `tillwright`: 59 pass (13.6s). All offline/mocked.
- `smart-router/bench.py`: worst p99 22.47 ms (<100 ms target met).
- Two components: `smart-router/` (router pkg: engine, selector, catalog, quota, gateway,
  providers, checks, context, classifier, skills, store, secrets, connect, api, cli) and
  `tillwright/` (desk pkg: desk, agent, workers, tools, gitops, launchers, server, store, build + ui/).

## Findings so far (lead's own read + agent audits; see final report for full list)

Confirmed bugs to fix (ranked):
1. Cache key misses dimensions (system_extra/prefer/quality_target/private/image content) -> wrong
   cached answers. engine.py:221.
2. `private` only honored on gateway path; /v1/run + CLI can send code to trains=True free
   providers. Secondary calls (_maybe_llm_classify, _review) also ignore private.
3. Gateway `plan()` exact-model match bypasses free_only filter -> free-only mode fails OPEN
   when a tool names a paid model. gateway.py:349-351.
4. Context-window/quota/cost checks use untrimmed context estimate -> large-context requests
   hard-fail; budget checks inflated. classifier.py:132 vs engine.py:157.
5. max_cost_usd not enforced on primary attempt (engine.py:238 `i > 0`).
6. tools.py safe-command list reads outside workspace (`cat ~/.ssh/id_rsa` auto-approves);
   _NEVER blacklist bypassable (`rm -rf ~/x`, `rm -rf /etc` under never_ask); Windows: os.killpg
   missing -> timeout path crashes.
7. workers.py: `never_ask` runs Claude Code with bypassPermissions + weak blacklist; SAFE_TOOLS
   `cat:*`/`find:*` allow reading/deleting anywhere on disk.
8. Quota ledger allow() is check-then-act; concurrent requests overshoot minute limits.
9. checks.py runs generated pytest with full user env (secrets, network); on by default.
10. Learning has no decay/self-heal; ~6 fails can exile a model forever.
11. trim_ladder can duplicate the strongest candidate. selector.py:199-205.
12. Classifier: `schema` forces json-mode; zero-signal confidence 0.6 > contested 0.45;
    `go`/`class`/`SELECT .* FROM` misfires; complexity cues uncapped.
13. Anthropic stream() lacks cache_control on last message (cache miss on multi-turn).
14. Branding: product is Promptaro; app still Tillwright everywhere (dirs ~/.tillwright,
    ~/.akshay-router, keychain service, tw-* launchers, x-tillwright headers, model ids
    `tillwright-auto`). Must preserve data/credentials/launchers -> migrate, not blanket rename.

Research-backed improvements to adopt (see comparable-systems report):
- Sticky model per gateway conversation (preserves provider prompt cache; OpenRouter does this).
- Semantic cache tier over exact hash (local embeddings; Portkey/GPTCache contract).
- Tool-output pruning before LLM compaction (OpenCode); warm-prefix-aware compaction (Claude Code).
- Anthropic cache breakpoints (<=4, min-prefix caveat); typed fallbacks (context-overflow up-route).
- Standing daily budget + soft alert (LiteLLM virtual keys pattern).
- Sampled token counting instead of len/4.
- Learned quality estimates from own logs (RouteLLM MF-lite: already have the data).

## Status
Baseline complete. Awaiting remaining audit agents (architecture, providers/gateway, security, UX/branding).
Next: consolidate -> prioritize -> implement on this branch -> tests -> review -> docs.
