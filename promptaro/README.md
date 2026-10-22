# Promptaro

> **Status: beta.** The installer is unsigned — macOS may ask you to right-click → **Open**, and Windows SmartScreen may warn on first run.

**A desktop app by Akshay Dubey for AI-powered chats, coding, and agent tasks.**

Author: Akshay Dubey

*Promptaro was previously called Tillwright. Existing installs keep working: settings, keys,
sessions and `tw-*` launchers are read and migrated automatically.*

Chat with any AI model, and hand coding tasks to agents that work on your repos
in parallel, each on its own branch, until you review and merge. Every answer
and every agent step goes through the **Smart Router** (`../smart-router`), which
picks the cheapest model expected to do that step well and moves to a stronger
one when a step fails.

## Install and start

**Double-click one file. Promptaro installs itself and opens in your browser.**

| | |
|---|---|
| **Mac** | Double-click **Install Promptaro.command** once — Promptaro.app lands in Applications (Launchpad, Spotlight, Dock) *and opens*. Next time, just open the app like any other. The app isn't Apple-signed: if macOS blocks it, right-click → **Open**. To run without installing, double-click **Start Promptaro.command**. |
| **Windows** | Double-click **Install Promptaro.bat** once — a Start-menu and desktop shortcut appear *and the app starts*. Next time, click the shortcut. To run without installing, use **Start Promptaro.bat**. |
| **Linux** | Run `packaging/install-linux.sh` once — an app-menu entry appears *and the app starts*. To run without installing, `./launch.sh`. |

**Runs on** macOS 11 or newer (Intel and Apple Silicon), Windows 10/11, and Linux. Python is installed and managed
by the launcher — you don't need anything pre-installed except a browser.

The first start installs what it needs (about a minute). It opens in its own window if Chrome, Edge or Brave is
installed, otherwise in your browser. It keeps running in the background after the window closes, so it opens
instantly next time; **Connections → Quit Promptaro** stops it. First use asks you to accept the **Terms of Use** —
nothing works until you do (§ Terms in the sidebar re-opens them).

**First run** shows three steps, each with its live status:
1. **Sign in with OpenRouter** — a free account that unlocks every model family and 50 free requests a day.
2. **Your Claude or ChatGPT plan** — detected on its own if Claude Code or Codex CLI is installed and signed in.
3. **Optional direct keys** (Anthropic, OpenAI, Google) — no OpenRouter fee. Keys go to your system keychain.

## What it does

| | |
|---|---|
| **Chat** | Like ChatGPT or Claude, with answers streaming in as they're written, but each message is routed: easy questions go to free models, everyday work to the cheapest model that does it well, hard work to the strongest. Every answer shows the model, Free or Paid, and tokens used. Pick a specific model or a priority (lowest cost, fastest, best quality) per chat. |
| **Why this model?** | Under every answer and agent step: how difficult the request looked, the quality it needed, the models considered with their expected quality and cost, why the winner won, and how much less it cost than the strongest model. |
| **Teach it** | Thumbs up or down on any answer. The router learns straight away which models do well at which kind of work for you; thumbs down also offers **Try a stronger model**. Merging an agent's work counts as a thumbs up for the models that did it; discarding finished work as a thumbs down. |
| **Savings** | The sidebar shows what smart routing has saved. Open it for spent vs saved, free / paid / subscription answers, free requests left today, and what the router has learned about each model. |
| **Agent tasks** | Choose a project folder and describe the task. The agent works in a **git worktree on its own branch** (`agent/<task>`), so your working copy is never touched and several agents can work on the same repo at once. It searches, reads, edits, runs your tests, and reports. |
| **Live view** | Each step appears as it happens: what it's doing and why, the files it read or changed, the commands it ran and their output. Send a message while it works and it picks it up at the next step. Stop it any time. |
| **Approvals** | Read-only and test commands run on their own. Anything else (deleting files, `git push`, network installs, redirects) waits for **Allow / Don't allow**. Destructive commands like `sudo` or `rm -rf ~` are always refused. Per task: ask for risky commands, ask for every command, or never ask. |
| **Review** | The Changes panel shows the diff against where the agent started. Then **Merge into main**, **Open pull request** (uses `gh`), **Commit to branch**, **Open in VS Code** for last-mile edits, or **Discard**. |
| **Works without AI** | Review, running commands in a task's copy, commit, merge, PR and discard are all plain code and work with nothing connected. |
| **Project chat** | Bind a chat to a folder and the model reads your code (list/read/search only — it can never change or run anything) to ground its answers. |
| **Your conversations** | Hover ✕ on any sidebar item to delete it; the header's ✕ just closes the view, and ⤓ downloads the whole conversation as Markdown (or `?format=json` for the raw events). |
| **Studio** | Generate images and short videos on free tiers — Google AI Studio, Hugging Face, Together AI and Cloudflare keys (optional), plus Pollinations which needs no key — tried best-first. Convert files locally: make or combine PDFs, and convert images, audio, video and documents between formats. Files never leave your computer. |

## Free tokens for your coding tools

Connect free keys from **Groq, Cerebras, NVIDIA NIM** (don't train on prompts), **Mistral, Codestral** (may train)
or **Cohere** in Connections. Each row shows the provider's free limits and what you've used today.

**Custom endpoints** (Connections) add any OpenAI-compatible API by base URL — Ollama, LM Studio, vLLM or a company
proxy. Models are detected from the endpoint's `/models` or typed by hand; a key is optional and lives in the
keychain. Mark an endpoint **Private** (local or company) and code may use it like the other private providers.

Then open **Coding tools** in the sidebar and click **Add promptaro-claude, promptaro-aider and promptaro-env**:

| Command | What it does |
|---|---|
| `promptaro-claude` | Starts Claude Code on Promptaro's models (not your Claude subscription) |
| `promptaro-aider` | Starts Aider the same way |
| `eval "$(promptaro-env)"` | Sets `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` and the token for any other tool (Cline, Continue, OpenCode, Zed) |

How each request is handled, in under a millisecond and without calling a model:
1. **Difficulty** is scored locally. The model name the tool asks for is a hint (haiku = quick housekeeping, opus = hardest).
2. **Privacy:** code only goes to free providers that don't train on prompts, unless you allow it.
3. **Free limits:** a per-provider ledger tracks requests and tokens per minute, day and month, reads the providers'
   own rate-limit headers, and skips any model the request can't fit (a 25k-token Claude Code turn never goes to a
   model allowed 8k tokens a minute).
4. A model that refuses (rate limit, outage) is paused and the next one answers **before any text is sent**. When only
   a per-minute allowance is in the way, Promptaro waits the few seconds rather than failing.
5. By default coding tools **never spend money**; Coding tools → Spending can allow paid models when free ones can't.

The gateway listens on 127.0.0.1 only and every request needs its token (kept in the system keychain, fetched by
the launchers at run time, never written into them). Codex CLI isn't supported yet (it needs the Responses API).

What realistic free use looks like: Groq + Cerebras + NVIDIA give roughly 2–4 million private tokens a day, about
80–150 Claude Code turns. Open models are weaker than Claude at Claude Code's edit format, so the hardest tasks
are better run on your Claude plan; every request shows which model answered.

## Uses the subscriptions you already pay for

Promptaro checks this computer for the two official coding tools that run on a plan:

| Plan | Tool | How it's detected |
|---|---|---|
| Claude Pro / Max | **Claude Code** | `claude auth status` reports a Claude account sign-in |
| ChatGPT Plus / Pro / Business | **Codex CLI** | `codex login status` reports "Logged in using ChatGPT" |

When either is signed in, **hard agent tasks** run on that plan instead of per-use API billing. If a plan hits its
usage limit, Promptaro rests it for an hour and hands the task to the other plan, or to free models if
neither has room. Nothing to configure: Connections shows what was detected, and the Model menu lets you pick a
plan — or a specific model under it (Claude Sonnet/Opus/Haiku and pinned generations, the GPT-5.x Codex family) —
for a task or a chat. A chat picked this way answers **read-only** on your plan: no writes, edits or commands, and
credential files are denied in the tool's own permission language.

Promptaro never reads or stores either login. It runs the unmodified `claude` or `codex` program in the task's
own branch, feeds it the prompt over standard input (never the command line), and shows its progress. Claude Code
may only edit files and run tests and linters on its own (pushes, deletes, network and deploy commands are
refused). Codex runs in its `workspace-write` sandbox: it can change files inside the task's copy only, with no
network.

## Free models and their limits

Each provider's own free tier, as recorded in `smart-router/router/free_providers.py` (checked
2026-10-15; live rate-limit headers override these numbers whenever a provider sends them):

| Provider | Free allowance | May train on prompts? |
|---|---|---|
| OpenRouter (free models) | 20 requests/minute; 50 requests/day (1,000/day once $10 credit has ever been bought) | depends on the model |
| Groq | 30 requests/minute, 1,000/day, 8,000 tokens/minute, 200,000/day — per model | no |
| Cerebras | 5 requests/minute, 30,000 tokens/minute, 1,000,000 tokens/day — per model | no |
| NVIDIA NIM | 40 requests/minute shared across the account | no |
| Mistral | 60 requests/minute shared across the account | yes (free tier requires it) |
| Codestral (Mistral) | 30 requests/minute, 2,000/day shared across the account | yes |
| Cohere | 20 requests/minute, 1,000/month — trial key, non-commercial use only | no |
| Gemini (AI Studio free tier) | 10 requests/minute, 250/day, 250,000 tokens/minute — per model | yes |

> **Cohere trial keys are non-commercial.** Promptaro's licence allows small businesses to use
> the app commercially, but Cohere's trial tier does not — if you use Promptaro for work, skip
> the Cohere key or use a paid Cohere key instead.

Providers that may train on prompts are only used for code when **Chats and code: any free
model** is chosen; the private-by-default providers (Groq, Cerebras, NVIDIA, Cohere) carry code
without that switch.

## Spending: $0 by default

Promptaro never spends money unless you turn that on. With no subscription it uses free models only. With a Claude
or ChatGPT plan, everyday chats use free models and hard questions, or questions free models couldn't answer, go to
your plan, at no extra cost. Paid per-use models (OpenRouter credit, API keys) are locked in the model menu until you
tick **Allow paid models** in Connections; then a daily cap (starting at $1) limits them. If a free answer fails its
checks, Promptaro tries another free model or your plan, never a paid one. If every free limit is used up and no plan
is signed in, the chat says when limits reset instead of spending.

Free models come from OpenRouter. OpenRouter allows **20 requests a minute** and **50 a day**, or **1000 a day** once
$10 of credit has ever been bought. Promptaro reads your allowance from OpenRouter, counts down as you use it, and
shows "Free requests left today" in Connections and Savings. When the allowance runs out or a free model is
rate-limited, routing skips those models (until midnight UTC, or for a minute) and moves to your other free providers
or your plan. Code (agents, project chats, coding tools) only goes to free providers that don't train on prompts,
unless you choose **Chats and code: any free model**.

## How it saves tokens and money

- **Per-step model choice.** Planning can use a bigger model; routine reads and edits use a small one. The track color beside each step shows which size handled it (teal small, blue medium, violet large).
- **Escalate only on evidence.** Two invalid replies or three failed steps in a row move the task one model up the ladder, not straight to the most expensive one.
- **Prompt caching.** The instructions and the conversation so far are cached with Claude models (about 10× cheaper on repeat reads). OpenAI caches automatically.
- **Small tool outputs.** File reads are paged, command output is clipped, and old tool output is compacted in batches. Batching keeps the cached prefix intact between compactions.
- **Budgets.** Each agent task has a dollar cap (default $2). It stops and tells you when the cap is reached; raise it and say "continue".
- **Every model family.** Anthropic, OpenAI, Google, DeepSeek, xAI, Mistral, Meta Llama, Qwen and more, with live prices. Families without a track record start with less trust, so a cheap unknown model isn't picked on price alone; good results earn it more work.
- **Learning.** Every step's success, cost and speed is recorded, along with your thumbs up and down, so the router learns which models are good at which work for you.

## Safety

- File tools can't leave the task's folder (`../`, absolute paths and symlinks pointing outside are refused), and `.git` is off limits.
- Credential files (`.env*`, `*.env`, `~/.ssh`, key/certificate files) are blocked from the model — in Promptaro's own tools *and* in the plan tools' permission rules.
- Commands run with API keys, tokens and passwords removed from their environment, and with a timeout.
- The server listens on `127.0.0.1` only, rejects requests addressed to other host names, and requires an app header on every change, so a website can't drive it.
- Cache and build junk (`__pycache__`, `node_modules`, `.terraform`, …) never shows up in reviews or commits.

## Privacy

No telemetry, no account with us. Prompts go only to the AI providers you connect. Keys stay in
your system keychain.

## Layout

```
desk/agent.py     agent loop, per-step routing, escalation, compaction
desk/workers.py   Claude Code and Codex CLI on your subscriptions
desk/launchers.py promptaro-claude, promptaro-aider, promptaro-env (+ tw-* shims)
desk/tools.py     list/read/search/write/edit/run + command approval policy
desk/gitops.py    worktrees, diff, commit, merge, PR, discard
desk/desk.py      sessions: chats and agents, many at once
desk/store.py     SQLite (~/.promptaro/desk.db, migrated from ~/.tillwright) + live event fan-out
desk/server.py    HTTP API + live stream (SSE)
desk/ui/          the interface
manifest.json     how Software Hub lists this app
launch.sh         Mac/Linux launcher (Terminal or background)
packaging/        Mac app builder, Windows and Linux installers, icon
```

## Tests

```bash
python -m pytest -q    # 141 tests: real git repos, scripted models, stand-in Claude Code and Codex CLIs
```

## Migrating from Tillwright

First run moves `~/.akshay-router` and `~/.tillwright` into `~/.promptaro` — keys, settings,
sessions, quota ledger and worktrees. Nothing in `~/.promptaro` is overwritten, worktree paths
inside `desk.db` are rewritten, and the old `akshay-smart-router` keychain entries are still
read. `tw-claude`/`tw-aider`/`tw-env` and the `tillwright-*` model aliases keep working
alongside the `promptaro-*` names. Set `PROMPTARO_NO_MIGRATE=1` to opt out, and
`PROMPTARO_HOME` to put the data somewhere else entirely.

## Troubleshooting

| Problem | What to do |
|---|---|
| macOS says the app is damaged/can't be opened | Right-click → **Open**, or `xattr -dr com.apple.quarantine /Applications/Promptaro.app` (it isn't Apple-signed) |
| `promptaro-claude: command not found` | Coding tools → re-run the launcher install. Mac/Linux: add `~/.local/bin` to PATH (`echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc`). Windows: the installer already adds `%USERPROFILE%\.promptaro\bin` — open a new terminal. |
| Old sessions or keys seem missing | Check `~/.promptaro` — migration moves them from `~/.tillwright`/`~/.akshay-router`. Set `TILLWRIGHT_LEGACY_HOME`/`AKSHAY_ROUTER_LEGACY_HOME` if they lived elsewhere. |
| "Not a git repository" starting an agent | Choose **Set it up with git** in the task dialog, or `git init` the folder first |
| No models / "add a key" | Connections → sign in with OpenRouter, or paste a provider key; Ollama is detected when running |
| Free requests exhausted | Providers reset per minute and at midnight UTC: the ledger pauses and moves on to other free providers or your plan. Paid models are only used if you allowed them |
| Agent stopped at budget | Raise the task's dollar cap (or the daily cap in Connections) and send "continue" |
| Something crashed | `~/.promptaro/promptaro.log`; the app auto-offers **Restart now** after a code update |

## Not built yet

- A signed, notarized installer (.dmg / .msi) with automatic updates. Today the app wraps this folder, and updates
  to the folder are picked up with **Restart now**.
- Running agents in the cloud (e.g. on your GKE cluster) so they keep going while the laptop sleeps.
- Accounts and sync across computers.

---
Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
