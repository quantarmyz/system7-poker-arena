# Heuristic Learning — why we bake strategy into code

> "Maybe heuristics were not too weak. Maybe they were just too
> expensive to maintain. Maybe it's the next paradigm."
> — Jiayi Weng (OpenAI, post-training RL infra)
> https://trinkle23897.github.io/learning-beyond-gradients/

## TL;DR

**Heuristic Learning (HL)** = use a coding agent (Claude Code, Codex,
Cursor, ...) to write and refine the Python `decide()` function
offline. The deployed bot is pure code. **Zero LLM calls at runtime.**

This is different from the **Level 5 runtime-LLM path** (in
`examples/llm_agent.py`), where an LLM is called at every action. HL
is faster, cheaper, deterministic, and the ceiling is as high as you
program it. HL itself is **Level 4** in the optimization ladder
(see `references/optimization-levels.md`).

## Three roles for an LLM (don't conflate)

| Role | Where | When called | Cost / hand |
|---|---|---|---|
| **L1 default (Levels 1-4)** | `examples/agent.py decide()` | runtime | $0 |
| **Level 5 runtime-LLM** | `examples/llm_agent.py decide()` | runtime, every action | paid — varies by model and harness |
| **HL coder (drives Level 4)** | Your coding agent edits `examples/agent.py` | dev time only | dev-tool cost |

HL is the recommended path. Level 5 is offered as a starter for users
who want max strategic depth at runtime — it's paid, and the actual
cost varies by model + harness + token volume.

## The HL loop

```text
1. STRATEGY     Fill in STRATEGY.md (taste-driven, you write this).
                STRATEGY.md is the SPEC. The runtime bot never reads it.
2. CODE         Coding agent reads STRATEGY.md + decide-function.md +
                failure_report.txt (when looping back) and rewrites
                examples/agent.py decide() Python to match. decide() is
                the BUILD ARTIFACT — the only thing the runtime bot sees.
3. TEST         ./pokerkit test              (20 unit scenarios, ~50 ms)
4. SELFPLAY     ./pokerkit selfplay --hands 200 --seed 42  (~1 s)
                → compare bb/100 vs previous run
5. ARENA        ./pokerkit run --max-hands 50              (~3-5 min)
                → real bb/100 vs reference panel
6. ANALYZE      ./pokerkit analyze --out failure_report.txt
                → which positions/hands lost the most chips?
7. LOOP         feed failure_report.txt + STRATEGY.md back to the coding
                agent → step 2. The agent may also propose STRATEGY.md
                edits; user approves, then we re-translate.
```

**Source vs artifact.** STRATEGY.md is what the *user* edits — a
human-readable spec. `examples/agent.py decide()` is what the *bot*
runs — Python the coding agent generated from the spec. There's no
runtime YAML parser; that would be fragile. The translation happens
at dev time, every time STRATEGY.md changes or a loop iteration
completes.

## Why baked-in code beats runtime LLM

- **Speed.** Pure Python: microseconds per decision. LLM: 2-10 seconds.
  Poker Eval has a 60-second per-decision deadline; LLM can still run
  out under retry / long-chain reasoning.
- **Cost.** HL is free at runtime. Level 5 runtime-LLM is paid per
  decision; the per-match total varies by model + harness + token
  volume, so we don't quote a fixed figure.
- **Determinism.** Same input → same output. Tests are reliable.
  LLM sampling is stochastic, hard to regression-test.
- **Inspectability.** You can read the code and understand exactly
  why the bot did X. LLM reasoning is opaque.
- **Adaptability.** Want to change behavior? Edit the code, run
  `pokerkit test`. With L2, you have to re-prompt + re-eval.

## What to bake into `decide()` (what HL produces)

Typical things a coding agent encodes into pure Python:

| Pattern | Example |
|---|---|
| Position-aware opening ranges | `UTG_OPEN = {"AA","KK","QQ","JJ","TT","AKs","AKo","AQs"}` |
| Position-aware defending ranges | `BB_DEFEND_VS_BTN = UTG_OPEN \| {"99","88","77","66","KQs","KJs","QJs","JTs"}` |
| Board-texture detection | `is_dry_board(board) = monotone_count <= 1 and not draw_heavy` |
| Sizing tables | `flop_cbet = {dry: 0.33 * pot, wet: 0.66 * pot}` |
| Hand-strength classes | `pair = hole[0][0] == hole[1][0]`, `top_pair = hero_rank == max(board_ranks)` |
| Opponent profile classes | `vs_loose if villain_vpip > 40 else vs_tight` |
| Deadline fallback | `if deadline_s < 2: return safe_option(table)` |

All of these are deterministic, fast, inspectable, and testable. None
require a runtime LLM.

## When HL stops being enough

The HL ceiling is around `+5 to +10 bb/100` vs the reference panel —
strong but not solver-level. To go higher, you need one of:

1. **Level 5 with research context.** Pass GTOWizard / TexasSolver
   outputs into the LLM at runtime. Paid — varies by model and harness.
2. **Level 6 trained weights.** DeepCFR / NFSP / CFR+ trained on
   labeled spots. Runs at $0/match but takes ~1 week to train + needs
   a GPU. See `docs/strategy.md` "L3 — Trained weights".
3. **Solver lookup table.** Pre-solve canonical spots offline, ship
   the lookup. Bake the table into Python via HL — same paradigm,
   richer data.

The Poker Eval leaderboard's top is in solver-lookup / DeepCFR
territory. HL is enough to be competitive in the top quartile.

## Iteration cadence

A typical HL session looks like:

```
iter 0  baseline (L1 default)        -12.3 bb/100  vs panel
iter 1  add OPENING_RANGES            -4.1
iter 2  add board-texture cbet        +1.8
iter 3  add opponent HUD adjustment   +5.2
iter 4  tighten 3-bet defense          +5.4   ← plateau
```

4-5 iterations, 4-6 hours of dev time, ~$0.50 of Arena API calls
(50-hand previews × 4-5 iterations).

## When to ASK the user (during HL)

The coding agent should ASK the user at:

- **Strategy taste** — "Tight-aggressive vs loose-aggressive?"
- **Validation timing** — "Run 50-hand Arena preview now (3-5 min)?"
- **Submission approval** — "Submit full 500-hand match (~30-40 min)?"
- **Iteration stop** — "bb/100 plateaued at +5.4. Stop here?"

And ACT (without asking) on:

- Editing `examples/agent.py decide()`
- Running `pokerkit test` / `pokerkit selfplay`
- Running `pokerkit analyze`
- Reading reference files in `references/` and `assets/`
