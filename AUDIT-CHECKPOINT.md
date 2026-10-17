# Promptaro engineering checkpoint

Owner: Akshay Dubey. Branch: `audit/promptaro-baseline` (work happens here; main untouched).

## Baseline (recorded 2026-10-16)

- Start commit: `c0e4655` "Initial commit: Tillwright app and Smart Router" (only commit on main).
- Env: macOS, Python 3.14.6, venv at repo root `.venv/` (fastapi 0.143.3, httpx 0.28.1,
  pydantic 2.14.1, pytest 9.1.1, uvicorn 0.54.0, keyring 25.7.0, PyYAML 6.0.3).
- Tests at baseline: `smart-router` 90 pass (2.65s); `tillwright` 59 pass (13.6s). All offline/mocked.
- `smart-router/bench.py` at baseline: worst p99 22.47 ms (<100 ms target met).

## Commits on this branch (oldest → newest)

| Commit | What |
|---|---|
| `5fdd15b` | Spend controls, privacy plumbing, quota accounting, API auth |
| `a3febd7` | Command containment in workspace + local-surface hardening |
| `771a931` | Hard daily spend cap + sticky gateway sessions |
| `44e982e` | Reproducible installs (pinned deps), Windows reinstall detection, Docker/.env fixes |
| `14a2b9f` | Tillwright → Promptaro migration (dirs, keychain, headers, aliases, launchers) |
| `5cc7ac5` | Chat draft restore, router-UI workspace flow, Gatekeeper note |
| `54893bf` | Independent-review fixes (never_ask vetting, ledger coverage, migration unification, stream keys) |

## Verified now

- `pytest promptaro/tests smart-router/tests`: **169 pass** (74 promptaro + 95 smart-router), ~16s.
- `bench.py`: worst p99 **13.45 ms** (was 22.47).
- `git diff --check`: clean. `py_compile` + `bash -n` on scripts: clean.
- Real legacy dirs present on this machine (`~/.akshay-router`, `~/.tillwright`); migration tested
  against fixture copies, NOT the real dirs (first real app launch performs it).

## What changed (summary)

- **Money safety**: free-only fails closed; per-request + daily caps; quota reservations
  (begin/settle/abandon) on primary, forced, and secondary calls; cached-token pricing.
- **Routing**: post-trim estimates, complete cache keys, first-attempt quality signal,
  time-decayed telemetry, escalation on evidence, sticky sessions, exact-name failover.
- **Security**: workspace-confined files/commands, `never_ask`-proof vetting (substitution
  recursion, exfil reads, outside writes, device redirects), scrubbed subprocess envs,
  git-hook hardening, app-header + token auth, short-lived SSE keys, secret redaction.
- **Migration**: `~/.akshay-router` + `~/.tillwright` → `~/.promptaro` once per process,
  never-clobber, desk.db worktree rewrite, dual keychain/file credential fallback,
  `x-tillwright` header + `tillwright-*` aliases + `tw-*` launcher shims kept.
- **Install**: pinned requirements, `ROUTER_HOME` on the Docker volume, `env_file`,
  `.env.example`, PS1 reinstall-stamp fix, Linux chmod, Gatekeeper note.

## Assumptions & limitations

- All tests are offline/mocked; **no live provider calls were made**. Free-tier limits and
  provider behaviors are taken from docs, not verified against live accounts.
- Cost/token savings claimed (cache breakpoints, per-step routing) are structural — measured
  savings need real workloads; routing overhead is the only number measured (<100ms target).
- Real `~/.akshay-router`/`~/.tillwright` migration is covered by tests on fixture dirs but
  has not run against the real dirs yet — first actual app launch will perform it.
- Windows `.bat`/`.ps1` and Linux paths are reviewed but only macOS is exercised on this machine.
- `needs_approval` is a strong policy gate, not a sandbox — agent commands still run as the
  user inside the worktree (documented residual risk).

## Remaining backlog (ordered)

1. Live-provider validation: connect a real key, run free-tier + failover paths end to end.
2. Measure real token/cost savings vs baseline on representative tasks.
3. Semantic cache tier (local embeddings); sampled token counting.
4. Signed/notarized installers; auto-update channel.
5. `checks.py` subprocess network isolation (sandbox-exec/profile) if untrusted repos matter.
