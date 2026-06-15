# Architecture

System 7 is a layered NLHE 6-max agent: a deterministic heuristic core, an optional LLM brain for
hard nodes, a SQLite instrumentation layer, and a dashboard/coach built on top of the recorded data.
This document covers the decision path and the data model. For the strategy/coach workflow see
[COACH-LOOP.md](COACH-LOOP.md); for the UI see [DASHBOARD.md](DASHBOARD.md).

## The decision entry point

Every turn, the Arena gives us a `table` dict (via `/texas/pending-actions`) describing seats,
stacks, board, pot, blinds, our hole cards, the legal actions and a sliding window of
`recentEvents`. The agent must return `{action, amount?, message?, reasoning?}` before the deadline.

```
decide(table, deadline_s, research_context) ──► hybrid_system7 routes:
    _is_hard(table, deadline)  ?  llm_system7 (M3)   :   decide_system7 (heuristic)
```

`run_system7.py` wires the heuristic only; `run_hybrid_system7.py` wires the router; `s7_test.py`
does the same inside the Eval test-bench and records every decision.

## 1 · Heuristic engine — `decide_system7.py`

A single pure-Python function over the `table` dict. **No network, no global state, deterministic.**
The pipeline:

1. **Position & stacks** — `_position()` maps our seat to UTG/MP/CO/BTN/SB/BB from the blinds and
   seat order; `_eff_stack()` / `_spr()` compute the effective stack and the **stack-to-pot ratio**.
2. **Hand & board** — strength bucket, board texture (dry/wet/paired), and draw enumeration.
3. **Adjusted outs** — draws are counted then **discounted** (EducaPoker-style EV discount tables)
   so a flush draw on a paired board isn't valued like a clean one.
4. **Preflop** — open from the position's range (`OPENING_RANGES`), 3-bet for value
   (`_3BET_VALUE`) or as a blocker bluff (`_3BET_BLUFF_BLOCKERS`), size via `open_size_bb` /
   `threebet_mult`.
5. **Postflop** — c-bet sizing by texture (`SIZING`), value threshold (`value_eq`), an outs-based
   **bluff gate** ("perejil" — only barrel when outs/relief clear `perejil_flop` / `perejil_turn`),
   and **commit logic** when `spr <= commit_spr`.
6. **Reads adjustment** — `_villain_reads()` (from `s7_reads`) widens value vs. calling stations
   (`station_mult`) and tightens vs. nits.

All of the tunable numbers above are **knobs** exposed to the strategy layer (see below), so the
engine's behaviour can be changed as *data* without touching code.

### Tunable knobs (defaults)

| Knob | Default | Meaning |
|---|---|---|
| `open_size_bb` | 2.5 | Open-raise size in big blinds |
| `threebet_mult` | 3 | 3-bet sizing multiple of the call amount |
| `value_eq` | 0.62 | Equity threshold to bet/raise for value postflop |
| `station_mult` | 1.2 | Value-widening factor vs. calling stations |
| `cbet_bluff_frac` | 0.33 | C-bet bluff frequency vs. over-folders |
| `commit_spr` | 3 | SPR at/below which we commit the stack |
| `perejil_flop` / `perejil_turn` | 8 / 10 | Minimum adjusted outs to barrel as a bluff |
| `perejil_relief` | 2 | Outs "relief" allowance for the bluff gate |
| `sizing` | texture×street map | Bet-size fractions by board texture and street |

## 2 · Hybrid routing — `hybrid_system7.py` + `llm_system7.py`

`hybrid_system7._is_hard(table, deadline_s)` returns `True` only for the small set of genuinely
ambiguous, high-leverage nodes that benefit from deeper reasoning (and only if there's enough time
left on the clock). Those go to **MiniMax M3**:

- `llm_system7._minimax_call(system, user, max_tokens, model)` is a thin OpenAI-compatible chat
  call. Base URL defaults to `https://api.minimax.io/v1` and is overridable with `OPENAI_BASE_URL`;
  the key is `OPENAI_API_KEY`. The helper strips `<think>…</think>` blocks and returns clean text.
- The decision path builds a structured prompt (`system7_prompt.md`) from the table and reads, asks
  M3 for an action + reasoning, parses it defensively, and **falls back to the heuristic** on any
  error or timeout. The model's reasoning is persisted with the decision (`model` column) for audit.

Everything else stays on the free, instant heuristic — the LLM is a scalpel, not a crutch.

## 3 · Strategy layer — `s7_strat.py` + `strategies/*.json`

`decide_system7` reads its ranges and knobs from a config selected by `S7_STRAT`:

```python
import s7_strat
_CFG = s7_strat.load()        # strategies/<S7_STRAT>.json  (or {} → built-in baseline)
```

`load(name)` reads `strategies/<name>.json`; `names()` lists versions; `save(name, cfg)` writes one.
A config may set `base` (`std`|`wide`), `opening_ranges` (per position), `threebet_value`,
`threebet_bluff` and `knobs`. **Missing keys fall back to the baseline**, so `strategies/std.json`
== `{}` reproduces the built-in engine byte-for-byte (verified by `tests/test_system7.py`). This is
what makes coach proposals safe and reversible. See [COACH-LOOP.md](COACH-LOOP.md).

## 4 · Instrumentation — `s7_stats.py` (SQLite, WAL)

Every run records to a WAL-mode SQLite DB (`s7_test.db` for the bench). The dashboard opens it
**read-only** (`mode=ro`) so reads never block the writers.

| Table | Contents |
|---|---|
| `decisions` | one row per action: street, position, hole, board, strength, texture, SPR, pot, call, outs, action, amount, engine, `model`, `m3_log`, `run_label` |
| `hand_events` | per-hand event timeline (merged across our turns, deduped + ordered by `sequence`) + seat snapshot |
| `hand_results` | settled-hand result: board, winners, revealed holes, payouts, **net `chip_delta`**, `replay_url` |
| `equity` | cumulative real (`raw`) vs variance-adjusted (`adj`) chips per `run_label` over hand count |
| `runs` | per-match summary (engine, hands, adjusted bb/100, m3 calls) |
| `agent_stats` | opponent HUD snapshots (VPIP/PFR/AF/WTSD/WSD/style) |

`s7_report.py` renders a text report over these tables; it is what the coach reads.

### Net result accounting
`hand_results.chip_delta` is the **net** result of a hand for us = `payout − our contribution`,
reconstructed from the event stream (`_our_invested`) because the API does not expose our committed
chips. So a won pot shows profit excluding the chips we put in, and a fold shows the blinds/bets lost.

## Determinism & testing

The heuristic engine is deterministic and offline, which makes it unit-testable without the network.
`tests/test_system7.py` pins the engine's decisions on a battery of representative spots and asserts
that the default strategy config (and `S7_STRAT=std`) reproduces the baseline exactly — the
regression gate for any strategy or refactor change.
