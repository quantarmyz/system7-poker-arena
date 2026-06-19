# The System 7 Pipeline

> Official documentation for the **three-zone pipeline** that drives System 7 — from
> authoring an agent, to evaluating it, to coaching it, to running it live on the
> [dev.fun Poker Arena](https://github.com/devfun-org/poker-arena-starter-kit).
> See also [ARCHITECTURE.md](ARCHITECTURE.md) (engine internals) and
> [COACH-LOOP.md](COACH-LOOP.md) (the self-improvement loop).

System 7 is organised around the **lifecycle of an agent**. The web dashboard
(`s7_dash.py`, served at `:8787`) is split into three zones that read left-to-right:

```
  🧪 LAB                →   🎓 COACH              →   📡 PRODUCTION
  build + evaluate          diagnose + adjust         deploy + monitor live
```

A header toggle — **♠ CASH / 🏆 TOURNAMENT** — switches the entire context (separate
databases, agents, hands and rankings per game type). Pick the game type *before* you start.

---

## 1. Core concepts

| Concept | What it is |
|---|---|
| **Game type** | `cash` or `tournament`. Each has its own SQLite DB (`s7_cash.db` / `s7_tourney.db`), its own ranges and its own leaderboard. Cash play has fixed blinds; tournament play escalates them. |
| **Agent profile** | The full bundle a user designs: `{name, title, strategy, engine, model, provider, hud, tracker, game, note}`. Stored in `data/agents/<name>.json` (`s7_agents.py`). The *strategy* is what the engine plays; the *engine* decides how. |
| **Strategy** | A versioned `strategies/<name>.json`: opening ranges per position (cash) or per BB-bucket (tournament), 3-bet value/bluff sets, post-flop sizing matrix and tunable *knobs*. Loaded by `decide_system7.py` via `S7_STRAT`. |
| **Engine** | `heur` = pure heuristic (deterministic, instant, free). `hybrid` = heuristic on trivial spots + the **MiniMax M3** LLM on the hard ones (gated by `S7_LLM_MIN_DEADLINE`, ~27 s/call). |
| **Identity model** | dev.fun is **one account = one agent**. The Eval registers a *fresh throwaway* agent per match; live PvP runs under a single persistent identity (its credentials file). A claimed agent is your one official entity and can play any competition. |

### Cash modalities & tournament buckets

- **Cash** has three modalities chosen by `mode`: **AGR** (aggressive/wide), **STD**
  (balanced), **NIT** (tight). Changing the modality re-seeds the 13×13 grids.
- **Tournament** plays by **effective stack depth** in four buckets
  (`BB_BUCKETS = [40,20,10]`): `deep` (>40bb) / `mid` (20–40) / `short` (10–20) /
  `push` (<10bb → pure **shove/fold**). `decide_system7._active_ranges(table)` selects the
  ranges from the live stack depth.

---

## 2. 🧪 LAB — build and evaluate

### 2.1 Build an agent (visual builder)

Click **+ cash** or **+ torneo**. The builder is the *only* place agents are created.
Fields:

- **name (id)** — slug (`a-z0-9_-`), used for the strategy/profile filenames.
- **nombre visible (title)** + **descripción (note)** — free-text label and notes.
- **game** + (cash) **modalidad** — AGR/STD/NIT, or (tournament) the per-bucket tabs.
- **engine** (`heur`/`hybrid`), **model** + **provider** (the LLM for the hybrid engine).
- **HUD / tracker** — use opponent profiles for node-locking.
- **13×13 grids** — click a hand to add/remove it from the opening range (per position for
  cash, per BB-bucket for tournament). The backend expands range *tokens* (`22+`, `A2s+`) to
  explicit combos via `decide_system7._expand`; the grids are never re-implemented in JS.
- **Postflop** — a *sizing matrix* (pot fraction × texture × street) plus *knobs*
  (value equity, c-bet bluff fraction, commit-SPR, "perejil", …).
- **3-bet value / bluff** — the hands you re-raise for value or as a bluff.

Saving writes both the strategy (`/api/strats/save`) and the profile (`/api/agent/save`).

### 2.2 Evaluate

Pick an agent, a **group label** (so several runs of the same agent are distinguishable),
and how many agents to launch. **▶ lanzar evaluación** spins up *N* disposable agents that
each play the **Poker Eval S1** (500 hands vs the reference panel) concurrently, capped by
`S7_EVAL_MAXC` to avoid rate-limiting. Each run registers a throwaway `s7t-*` agent,
saves its credentials (so it is claimable + rankable) and writes every decision to the
game DB.

### 2.3 Per-task monitor

Select a **group** to watch its evaluation *live* (refreshes every ~15 s):

- **counters** — hands, decisions, M3 %, bb/100 ± CI.
- **distribution** — a **Gaussian** over the per-agent bb/100 (histogram + fitted normal),
  so you read the *spread*, not a single noisy number.
- **preflop heatmap** (13×13 VPIP) and **VPIP/PFR by position**.
- **live log** — the active match's stdout (joins, hands, errors, time-outs).
- **hand history** filtered to the task → click opens the replayer.

The **⏹ parar evaluación** button stops the batch and all its children.

### 2.4 Report

Per strategy: bb/100 **aggregated across the N runs with a 95 % confidence interval**.
A single 500-hand Eval carries ±20 bb/100 of noise, so the report treats the aggregate +
CI as the signal — not any one run.

---

## 3. 🎓 COACH — diagnose and adjust

For an agent that already has data:

- **Your play vs the best play** — VPIP / PFR / gap / flop c-bet against GTO 6-max bands,
  with a ✓/⚠/✗ verdict, plus your real bb/100 vs the panel.
- **Advice** — actionable leaks to fix, by position.
- **AI coach (M3)** — an analysis + a proposed version.
- **Pro-player generator** — *fix my leaks* or *ideal from scratch* produces a complete
  strategy that opens in the LAB builder to review, name and save.

The loop closes: evaluate → coach → adjust → re-evaluate.

---

## 4. 📡 PRODUCTION — deploy and monitor live

### 4.1 My account agent

The **🏅 Mi agente (cuenta)** card reads the account's credentials, confirms the claimed
agent + owner, and shows **its position in every leaderboard** it is registered in
(Eval / Playground / Tournament) by paginating `/competition/leaderboard` for your agentId.

### 4.2 Play an event

Pick an **agent** + an **event** (Eval, Playground, Tournament — discovered live via
`/competition/list-active`) and press **▶ jugar**:

- **One active at a time + a queue.** Deploying while one is active enqueues the rest.
- **Dedupe** — the same agent+event can't be queued twice.
- **Continuous jobs** — Playground/Tournament (`run_pvp`) loops indefinitely, so it is
  flagged *"continuo · la cola espera"*: it blocks the queue until you stop it. Evals are
  one-shot, so a queue of Evals drains by itself.

> **Eval deploys** run `s7_test.py` (one-shot, claimable). **PvP deploys** run `run_pvp.py`
> under the persistent credentials file (`S7_CREDS_FILE`). Swap that file to change which
> account the live play counts for.

### 4.3 Monitor

- **🟢 En vivo** — the active agent(s), real hand count, stop / claim.
- **📜 Log del agente en vivo** — the active job's stdout, near-real-time (jobs run with
  `PYTHONUNBUFFERED=1` and stderr captured, so 403s, time-outs and tracebacks surface here).
- **Manos en vivo** — every hand, with columns for **modo** (`LLM-m3`/`HEUR`),
  per-street actions (**flop/turn/river**), where it **folded**, pot and result. A
  **🤖 solo LLM** toggle filters to LLM hands. Click a hand to open the replayer.
- **Reproductor** — the **official dev.fun replayer** (`replayUrl`, captured by both the
  Eval flow and `run_pvp`'s heartbeat) plus a local reconstruction, and a
  **🤖 Razonamiento del LLM** panel with M3's full `answer`/`think` per decision.
- **Equity en vivo** — REAL vs EV curve. **Sesión en juego** — preflop 13×13 + postflop
  action split + session stats. Both filter to a selected agent (👁) or show the global
  session.
- **🏆 Ranking** — scored agents (Eval bb/100, claimable 🏆) merged with PvP deploys, each
  **tagged by type** (`Eval` / `Playground` / `Tournament`) so an Eval result is never
  confused with live PvP play. 🗑 removes an entry.

### 4.4 Claiming

🏆 links a registered agent to a dev.fun account (verify via X). It works for Eval agents
(per-agent credentials) and for PvP agents (resolved through the shared credentials file).

---

## 5. The Poker Eval & scoring

The Eval (`seed_poker_eval_s1`) is a **500-hand, one-shot-per-agent** benchmark against a
fixed near-GTO reference panel. The score is `adjustedBbPer100`. Because each agent only
gets one Eval, agents are disposable — you register many, evaluate them, and **claim** the
ones you want on your account. The official leaderboard ranks by cumulative score.

---

## 6. Data model & job backend

- **SQLite, one DB per game type.** Tables: `decisions` (every action + features + the M3
  reasoning blob), `runs` (per-match bb/100), `equity` (REAL vs EV curve), `hand_results`
  (settled hands + revealed cards + official `replay_url`), `hand_events` (timeline),
  `own_hands` / `opp_hands` / `opp_profiles` (the PokerTracker/HM-style archive).
  WAL is bounded (`journal_size_limit`, `wal_autocheckpoint`).
- **Jobs** (`s7_jobs.py`) run as background processes (systemd *or* plain subprocess; the
  Docker image auto-selects subprocess with per-job log files). Launch/stop/log all flow
  through the dashboard.
- **Tracker** (`s7_tracker.py`) harvests `/agent/submissions`, `/texas/recent-tables`,
  `/agent/{id}/replays` and `/texas/agent-stats` into the archive so the HUD serves
  aggregated opponent reads.

---

## 7. Deployment

```bash
docker compose up -d dashboard          # the web UI + job backend, on :8787
```

The image bundles the engine, the dashboard and the vendored front-end (`web/`). Live data
lives in the `./data` volume (DBs, agent profiles, credentials — **never** committed).
Production agents are subprocess children of the dashboard container, so **recreating the
container stops any in-flight job** — drain or expect to relaunch.

---

*System 7 is built on the dev.fun Poker Arena Starter Kit (MIT). This document describes the
pipeline; for the heuristic/LLM engine internals see [ARCHITECTURE.md](ARCHITECTURE.md).*
