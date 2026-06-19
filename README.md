# System 7 — a self-improving poker bot for the dev.fun Poker Arena

[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-3776ab)](pyproject.toml)
[![Engine](https://img.shields.io/badge/engine-heuristic%20%2B%20MiniMax%20M3-success)](docs/ARCHITECTURE.md)
[![Status](https://img.shields.io/badge/status-active%20research-orange)](#status--results)

**System 7** is a No-Limit Hold'em 6-max agent for the
[dev.fun Poker Arena](https://github.com/devfun-org/poker-arena-starter-kit) *Poker Eval*
benchmark. It pairs a fast, fully deterministic **heuristic engine** with an on-demand
**LLM brain** (MiniMax **M3**) for the hard spots, records every decision into SQLite, and
ships a **real-time web dashboard** with a hand replayer and an **M3 "coach" that proposes
new strategy versions from its own play** — turning the build loop into a measurable,
reproducible A/B experiment.

> Built on top of the [dev.fun Poker Arena Starter Kit](https://github.com/devfun-org/poker-arena-starter-kit)
> (MIT). System 7 is the engine, instrumentation, dashboard and coach layered on the kit's
> client/loop. See [Acknowledgements](#acknowledgements).

> **The dashboard is a three-zone pipeline** — *LAB* (build + evaluate) → *COACH*
> (diagnose + adjust) → *PRODUCTION* (deploy + monitor live play), with separate
> cash/tournament contexts. Full walkthrough in **[docs/PIPELINE.md](docs/PIPELINE.md)**.

---

## Highlights

- 🎯 **Deterministic heuristic core** — position-aware opening ranges, 3-bet value/bluff
  construction, SPR-driven postflop, adjusted-outs draw math, texture-based c-bet sizing and
  a disciplined bluff gate. Pure Python, **no network at decision time** → microsecond decisions.
- 🧠 **Hybrid escalation** — cheap heuristic by default; only genuinely *hard* nodes are routed
  to **MiniMax M3** (OpenAI-compatible API), with the model's reasoning captured for review.
- 🧩 **Versioned strategy as data** — opening ranges, 3-bet lists and ~10 postflop knobs live in
  `strategies/<name>.json`, selected at runtime with `S7_STRAT`. The default is byte-for-byte the
  built-in baseline (regression-tested), so configs are safe, diffable and reversible.
- 🔁 **Self-improving coach loop** — after enough hands, M3 reads the full stats report and
  **proposes a concrete new strategy version** (validated, clamped JSON). You review it and launch
  it against a frozen control arm. Propose → review → A/B → iterate. See [docs/COACH-LOOP.md](docs/COACH-LOOP.md).
- 📊 **Real-time dashboard** — stdlib-only web UI: live decisions, equity (real vs EV) curve,
  opponent HUDs, per-engine performance, and a **step-by-step hand replayer**. See [docs/DASHBOARD.md](docs/DASHBOARD.md).
- 🧪 **Eval test-bench** — registers throwaway agents and plays full *Poker Eval* matches against
  the fixed DeepCFR opponent panel, persisting decisions, events, equity and results for analysis.

---

## Architecture

```
                       ┌──────────────────────────────────────────────┐
   /texas/pending      │                 decide(table)                 │
   -actions  ───────►  │                                               │
   (table state)       │   hybrid_system7._is_hard(table, deadline)?   │
                       │        │ no                    │ yes           │
                       │        ▼                       ▼               │
                       │  decide_system7        llm_system7 (M3)        │
                       │  (heuristic engine)    OpenAI-compatible chat  │
                       │        │                       │               │
                       │        └────────► action ◄─────┘               │
                       └───────────────────┬──────────────────────────-┘
                                            │ action + features
                          ┌─────────────────▼─────────────────┐
                          │ s7_stats.py  →  SQLite (WAL)        │
                          │ decisions · hand_events · equity ·  │
                          │ runs · hand_results · agent_stats   │
                          └───────┬──────────────────┬─────────┘
                                  │ read-only (ro)   │ report()
                        ┌─────────▼────────┐  ┌──────▼─────────────────┐
                        │ s7_dash.py       │  │ s7_report.py → M3 coach │
                        │ web dashboard +  │  │ proposes strategies/    │
                        │ hand replayer    │  │ coach-<ts>.json         │
                        └──────────────────┘  └────────────────────────┘
```

| Module | Role |
|---|---|
| `decide_system7.py` | Deterministic heuristic engine. Position, ranges, SPR, outs, sizing, bluff gate, commit logic. Pure Python. |
| `hybrid_system7.py` | Router: heuristic vs. escalate-to-M3 based on spot difficulty and remaining deadline. |
| `llm_system7.py` | MiniMax **M3** integration (OpenAI-compatible). Builds the prompt, parses reasoning, strips `<think>`. |
| `s7_strat.py` + `strategies/*.json` | Versioned strategy config layer. `S7_STRAT` selects a version; missing keys fall back to baseline. |
| `s7_reads.py` | Opponent HUD / reads (VPIP / PFR / AF → archetype) used to adjust lines. |
| `s7_stats.py` | SQLite recorder (decisions, hand events, equity, runs, hand results, agent stats). |
| `s7_test.py` | Eval test-bench: registers throwaway agents, plays matches, records everything. |
| `s7_report.py` | Renders the text stats report consumed by the coach. |
| `s7_dash.py` | Real-time dashboard + hand replayer (stdlib `http.server`, port 8787). |
| `run_system7.py` / `run_hybrid_system7.py` / `run_pvp.py` | Live runners (heuristic, hybrid, PvP loop). |

Deeper technical write-up: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## How it works

### 1 · Heuristic engine (`decide_system7.py`)
A node-locking, GTO-flavoured ruleset (EducaPoker methodology): it computes position, effective
stack and **SPR**, hand strength and board texture, **adjusted outs** for draws (discounted EV),
then picks an action — open-raise from range, 3-bet for value or with blocker bluffs, c-bet sized
by texture, barrel or give up via an outs-based bluff gate, and commit/stack-off when SPR is low.
It reads opponent tendencies (`s7_reads`) to widen value vs. calling stations and tighten vs. nits.
It is **deterministic and offline** — the same `table` always yields the same action.

### 2 · Hybrid escalation (`hybrid_system7.py` + `llm_system7.py`)
Most decisions are trivial and handled by the heuristic for free. `_is_hard()` flags the small
fraction of high-leverage, ambiguous nodes and routes *those* to **MiniMax M3**, subject to the
turn deadline. M3's natural-language reasoning is stored alongside the decision so you can audit
exactly why it chose a line.

### 3 · Strategy as versioned data (`s7_strat.py`)
Ranges and postflop thresholds are **not hardcoded** — they live in `strategies/<name>.json`
(`base`, `opening_ranges`, `threebet_value`, `threebet_bluff`, `knobs`). Pick a version with
`S7_STRAT=<name>`. The default is identical to the built-in baseline, so every experiment is a
clean, reversible diff over a known-good reference.

### 4 · Self-improving coach loop
The eval bench records full game state. Once enough hands accumulate, the dashboard's **COACH** tab
asks M3 to analyse the report and **emit a new strategy version** as a validated JSON config. You
review the proposal and launch it (`S7_STRAT=<version>`) against a **frozen control arm** (`fijo`)
to A/B it on the equity curve and per-engine tables. Repeat until the bot is tournament-ready.
Full description: **[docs/COACH-LOOP.md](docs/COACH-LOOP.md)**.

### 5 · Real-time dashboard (`s7_dash.py`)
A dense, dependency-free web UI on `:8787` with tabs for live **PANEL** stats, a sortable **MANOS**
(hands) grid, opponent **PLAYERS** HUDs, the **COACH**, and a **RUN** control plane to launch
training arms. Includes a step-by-step **hand replayer**. Details + the honest API-limitation note:
**[docs/DASHBOARD.md](docs/DASHBOARD.md)**.

---

## Quickstart

> Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). System 7 uses the starter kit's
> `ArenaClient`, so the standard kit setup applies.

```bash
# 1. install deps
uv sync                      # or: uv run <script> auto-resolves

# 2. configure — copy the template and fill in your keys (never commit .env)
cp .env.example .env
#   ARENA_API_KEY / ARENA_COMPETITION_ID   (Arena)
#   OPENAI_API_KEY + OPENAI_BASE_URL       (MiniMax M3, OpenAI-compatible)

# 3. run the offline tests (deterministic engine regression)
uv run --with pytest python -m pytest tests/test_system7.py -q

# 4. play a Poker Eval match with the hybrid engine
uv run run_hybrid_system7.py

# 5. run the eval test-bench (records to s7_test.db) and open the dashboard
uv run s7_test.py --engine hybrid --matches 5     # 5 × 500 = 2500 hands
uv run s7_dash.py                                  # http://localhost:8787
```

### Running modes

| Command | What it does |
|---|---|
| `uv run run_system7.py` | Live Eval with the **heuristic** engine only. |
| `uv run run_hybrid_system7.py` | Live Eval with the **hybrid** engine (heuristic + M3). |
| `uv run run_pvp.py` | Continuous **PvP** loop with stats recording. |
| `uv run s7_test.py --engine {hybrid,heur} --matches N` | Eval **test-bench** (records everything). |
| `S7_STRAT=<v> uv run s7_test.py ...` | Run a specific **strategy version**. |
| `uv run s7_dash.py` | Real-time **dashboard** + replayer on `:8787`. |

---

## 🐳 Deploy with Docker

The whole thing ships as a container — no systemd, no host setup. The dashboard launches/stops/reads
training runs itself (a built-in subprocess backend auto-replaces systemd), so **the full UI works inside
the container**: the RUN tab, the COACH strategy generator, the clasificatoria batches and the multiLLM
benchmark all run as tracked subprocesses.

```bash
# 1. clone + configure
git clone https://github.com/quantarmyz/system7-poker-arena.git
cd system7-poker-arena
cp .env.example .env          # fill ARENA_API_KEY, OPENAI_API_KEY + OPENAI_BASE_URL (MiniMax M3), …

# 2. up
docker compose up -d          # builds the image + starts the dashboard

# 3. open the dashboard
#    http://localhost:8787   → PANEL / MANOS / PLAYERS / COACH / RUN / RANK / multiLLM
```

Launch matches from the **RUN** tab (or COACH → "generar/lanzar versión"); they appear live in RANK and the
equity curve. Everything persists in **`./data`** (SQLite DBs, `strategies/`, `.clasif/` claim creds, job
logs) — survives `docker compose down && up -d`. Secrets stay in `.env` / `./data`, never in the image or repo.

**Optional always-on workers** (off by default):

```bash
docker compose --profile bench up -d   # continuous Eval test-bench vs the near-GTO panel
docker compose --profile pvp   up -d   # PvP Playground loop (run_pvp.py)
```

> The same code runs under **systemd** on a bare host (the backend auto-detects `systemd-run`/`journalctl`);
> set `S7_RUN_BACKEND=systemd|subprocess` to force it.

---

## Project layout

```
decide_system7.py      heuristic engine (deterministic, offline)
hybrid_system7.py      heuristic ↔ M3 routing
llm_system7.py         MiniMax M3 (OpenAI-compatible) integration
s7_strat.py            versioned strategy loader      strategies/*.json (incl. s7-opus)
s7_reads.py            opponent HUD / reads
s7_stats.py            SQLite recorder                s7_test.py  eval bench
s7_report.py           report for the coach           s7_dash.py  dashboard + replayer
s7_mllm.py             multiLLM benchmark runner      s7_batch.py  wave runner (clasificatorias)
s7_jobs.py             run backend (systemd | subprocess, auto-detected)
run_system7.py · run_hybrid_system7.py · run_pvp.py   live runners
system7_prompt.md      M3 decision/coach prompt
Dockerfile · docker-compose.yml · docker/entrypoint.sh   container deploy
tests/                 test_system7.py (engine regression) + kit tests
docs/                  ARCHITECTURE · COACH-LOOP · DASHBOARD
examples/              starter-kit client/loop (ArenaClient, agent, llm_agent, …)
```

---

## Methodology

System 7's heuristics follow an **EducaPoker / GTO node-locking** philosophy: play a sound,
position-aware baseline, then exploit measured population tendencies. Core concepts encoded in the
engine include **SPR** (stack-to-pot ratio) for commitment decisions, **adjusted outs** (EV-discounted
draw counting), blocker-aware 3-bet bluffing, and texture-dependent bet sizing. The LLM is used
surgically — as a tie-breaker on hard nodes and as an offline coach — never as a per-hand crutch.

---

## Status & results

This is **active research**. The current focus is the coach A/B loop: a `std` arm (evolves with
coach proposals) against a frozen `fijo` control, measured on the dashboard's real-vs-EV equity
curve and per-engine tables. Numbers shift as strategies are tuned, so the repository deliberately
does **not** ship a headline winrate — the dashboard is the source of truth. The opponent panel is
a fixed set of near-GTO DeepCFR bots.

---

## Acknowledgements

System 7 is built on the **[dev.fun Poker Arena Starter Kit](https://github.com/devfun-org/poker-arena-starter-kit)**
(MIT) by devfun-org — it provides the `ArenaClient`, the game loop, the *Poker Eval* harness and the
poker primitives in `examples/`. All System 7 modules (`decide_system7`, `hybrid_system7`,
`llm_system7`, `s7_*`, the strategy layer, the dashboard and the coach) are original work layered on
that foundation. LLM reasoning is provided by **MiniMax M3** via an OpenAI-compatible endpoint.

## License

[MIT](LICENSE) — same as the upstream starter kit.
