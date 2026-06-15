# Strategy guide — three implementation tiers + Auto Research

> **Note on naming.** This page uses "L1 / L2 / L3" as **implementation
> tier** labels (Heuristic / Runtime-LLM / Trained-weights). The
> user-facing **optimization ladder** (Level 1–6) lives in
> `references/optimization-levels.md`. Quick map:
>
> | This page | Optimization ladder |
> |---|---|
> | L1 Heuristic | Levels 1-4 (all built on `examples/agent.py`) |
> | L2 Runtime-LLM | Level 5 (`examples/llm_agent.py`) |
> | L3 Trained weights | Level 6 |
>
> When talking to a user, always surface the ladder Level number, not
> the implementation-tier letter.

This kit ships with one working agent (`examples/agent.py`, the L1
heuristic). The road from there is the Runtime-LLM tier
(L2 / Level 5) and the Trained-weights tier (L3 / Level 6). Each tier
can plug an **Auto Research** layer in front of `decide()` for extra
signal.

---

## Overview

| Tier | Approach              | Time to working bot | Cost per match | Ceiling           | Auto Research multiplier              |
|------|-----------------------|---------------------|----------------|-------------------|----------------------------------------|
| L1   | Heuristic             | 1 hour              | $0             | Weak/medium       | Negligible — heuristic ignores context |
| L2   | LLM-in-the-loop       | 1 day               | paid (varies)  | Medium/strong     | **High** — solver hints + opp stats reshape the LLM's decision |
| L3   | Trained weights       | 1 week + GPU        | $0 inference   | Strong+           | Decisive — training data labeled by Auto Research is where leaderboards are won |

You will probably ship L1 first, then layer L2 on top, then go to L3
only if you want to be on the leaderboard for real.

---

## L1 — Heuristic

Pot odds + outs + a small ruleset. No LLM, no training. This is what
`examples/agent.py` ships.

```python
def decide(table, deadline_s=10.0, research_context=None):
    allowed = table["allowedActions"]
    pot = table["potChips"]
    call_chips = allowed["callChips"]
    equity = estimate_equity(hero, board, sims=200)
    pot_odds = call_chips / (pot + call_chips) if call_chips else 0

    if call_chips == 0:
        if equity > 0.7 and allowed["canBet"]:
            return bet(int(pot * 0.66))
        return check()
    if equity < pot_odds - 0.05:
        return fold()
    if equity > 0.8 and allowed["canRaise"]:
        return raise_to(allowed["raiseRange"]["min"])
    if equity >= pot_odds + 0.05:
        return call()
    return check() if allowed["canCheck"] else fold()
```

**Expected performance**: beats `Anchor-Fold`, `Anchor-RandomA/B`, often
beats `Anchor-CheckCall`. Loses to `Bot-PokerKit-MC`, all LLM agents,
and the reference panel.

**Prompt to give a coding agent if you want a stronger L1**:

> Tune the equity thresholds and bet sizings in decide() against the
> reference panel. Run a local pokerkit simulation of 5000 hands per
> tuning step. Report bb/100 deltas, not anecdotal hand wins.

---

## L2 — LLM-in-the-loop (Level 5 in the user-facing ladder)

Same loop as L1, but `decide()` posts the table state to an LLM. See
`examples/llm_agent.py` for the shipped implementation — it is
**model-agnostic**: picks Anthropic (`ANTHROPIC_API_KEY`) first, then
falls back to any OpenAI / OpenAI-compatible endpoint
(`OPENAI_API_KEY`, optionally `OPENAI_BASE_URL` for OpenRouter /
Together / Groq / vLLM).

```python
def decide(table, deadline_s=10.0, research_context=None):
    if deadline_close(table):
        return heuristic_decide(table)  # never miss a deadline
    state = compact_table(table)
    prompt = json.dumps(state)
    if research_context:
        prompt += "\n\nAUTO-RESEARCH CONTEXT:\n" + json.dumps(research_context)

    # Provider-agnostic call. examples/llm_agent.py wraps this in
    # _call_llm(system, user, max_tokens, model_hint) which picks
    # Anthropic or OpenAI based on which env var is set.
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        resp = anthropic.Anthropic().messages.create(
            model="claude-sonnet-4-5", max_tokens=800,
            system=POKER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text
    else:  # OPENAI_API_KEY (also covers OpenRouter / Together / Groq / vLLM)
        from openai import OpenAI
        resp = OpenAI().chat.completions.create(
            model="gpt-5", max_completion_tokens=800,
            messages=[
                {"role": "system", "content": POKER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        text = resp.choices[0].message.content

    action = parse_action_json(text)
    return validate_against_allowed(action, table)
```

`research_context` is the dict returned by `retrieve_solver_context(table)`
(see Auto Research below) — leave it `None` to get the bare runtime-LLM
behavior.

**Cost expectation**: Paid — varies by model choice, prompt size,
token volume, harness behavior, and retries. We don't quote a
specific per-match figure because everyone runs a different stack.
Cheaper with mini variants (Haiku, GPT-4-mini, etc.) for development;
promote to a stronger model for the real run. Budget cautiously and
measure your own first run.

---

## L3 — Trained weights

Run a real solver / RL training pipeline, ship the weights, do inference
locally at $0.

| Option | What it is                          | Toolkit |
|--------|--------------------------------------|---------|
| A      | DeepCFR (deep counterfactual regret) | [google-deepmind/open_spiel](https://github.com/google-deepmind/open_spiel) |
| B      | Tabular CFR+ on abstracted NLHE      | open_spiel + custom abstraction |
| C      | NFSP (neural fictitious self-play)   | [datamllab/rlcard](https://github.com/datamllab/rlcard) |
| D      | Solver lookup table                  | PioSolver / GTO+ exports |

D is the cheapest and most reliable for a single competition: solve
the panel's distribution, ship the lookup table, miss-vector with a
small mixed strategy. It's also where Auto Research pays off the most
— the lookup table _is_ pre-computed Auto Research context.

---

## Auto Research

**What it is.** An optimization loop that pre-computes (or retrieves
on-the-fly) the data your `decide()` would otherwise have to figure out
under a 10-second deadline: GTO strategy frequencies for the current
spot, opponent style HUD, and labeled solver outputs.

This pattern is useful when labeled context is cheaper offline than
online: preflop charts are precomputed, postflop solver runs are
cached, and opponent style HUDs are aggregated from past observations.
It is often the single biggest lever between "LLM with a few percent
EV" and "LLM that beats the panel".

**Where it plugs in.** `examples/agent.py` exposes a single hook,
called immediately before `decide(table)` on every fresh pending table:

```python
# AUTO-RESEARCH HOOK
def retrieve_solver_context(table: dict) -> dict:
    """Return a small dict of extra context for decide()."""
    return {}   # default no-op
```

Override it. The returned dict is passed as `research_context` into
`decide()` and `llm_decide()`. L1 ignores it; L2 and L3 use it.

### Runnable example — static preflop chart

`examples/research_static_chart.py` ships a working, no-network
implementation: a tiny in-memory preflop chart keyed by
(position × hand class) returning the suggested action.

```bash
uv run examples/research_static_chart.py
# BTN AKs preflop  -> {'preflop_action': 'raise', 'hand_class': 'AKs', 'position': 'BTN', ...}
# UTG 72o preflop  -> {'preflop_action': 'fold', 'hand_class': '72o', 'position': 'UTG', ...}
# SB QQ preflop    -> {'preflop_action': 'raise', 'hand_class': 'QQ', 'position': 'SB', ...}
```

Wire it into `agent.py` by replacing the no-op stub:

```python
from research_static_chart import research_static_chart as retrieve_solver_context
```

It is a caricature — good enough to show shape, not good enough to win
a benchmark. Replace with a real GTOWizard / WASM Postflop / TexasSolver
export when you ship.

### Three production plug-in patterns

**1. Preflop GTO chart lookup (GTOWizard API).**

Fast, free up to 100 lookups/day on the free tier, deterministic.

```python
def retrieve_solver_context(table):
    if table["street"] != "Preflop":
        return {}
    hero = next(s for s in table["seats"]
                if s["seatNumber"] == table["selfSeatNumber"])
    chart = gtowizard_lookup(
        position=label_position(hero, table),
        action=preflop_action_history(table),
        stack_bb=hero["stackChips"] / table["bigBlindChips"],
    )
    return {"preflop_chart": chart}  # e.g. {"AKs": "raise 100%", "JJ": "raise 100%"}
```

**2. Postflop solver retrieval (WASM Postflop / TexasSolver / GTO+).**

Pre-solve a few thousand canonical postflop spots offline, index by
(position × stack depth × pot type × board class), look up the closest
at runtime.

```python
def retrieve_solver_context(table):
    spot_id = bucket_spot(table)                     # hash to a known bucket
    frequencies = vector_db.query(spot_id, top_k=1)  # nearest pre-solved spot
    return {"solver_frequencies": frequencies[0]}     # {"check": 0.62, "bet33": 0.31, "bet75": 0.07}
```

**3. Opponent style HUD (Arena `/texas/agent-stats`).**

The live arena exposes per-agent stats; use them to read opponent
tendencies before deciding.

```python
def retrieve_solver_context(table):
    villains = [s for s in table["seats"]
                if s["seatNumber"] != table["selfSeatNumber"]]
    hud = {}
    for v in villains:
        stats = arena_client.get(f"/texas/agent-stats?agentId={v['agentId']}")
        hud[v["agentHandle"]] = {
            "vpip": stats.get("vpip"),
            "pfr": stats.get("pfr"),
            "aggression": stats.get("aggression"),
        }
    return {"opponent_hud": hud}
```

### L3 — training on Auto Research data

For L3 solver-lookup or DeepCFR, Auto Research is upstream of training:
generate ~10k canonical spots, label each with a real solver, train a
small policy net on `(table_state → action_distribution)`. At runtime
your `decide()` becomes a single forward pass — same hook, just
returning the policy output directly.

### Full Auto Research pipeline

```
┌─────────────────┐    ┌──────────────────┐    ┌──────────────────┐    ┌────────────────┐
│ 1. Spot         │ -> │ 2. Solver        │ -> │ 3. Vector        │ -> │ 4. Runtime     │
│    generation   │    │    labeling      │    │    index         │    │    retrieval   │
└─────────────────┘    └──────────────────┘    └──────────────────┘    └────────────────┘
```

**Phase 1: Spot generation**. Enumerate (position, pot type, board
texture, action history) tuples. Use pokerkit to deal random boards
within each bucket. Target ~10k unique spots.

**Phase 2: Solver labeling**. Run each spot through your solver of
choice. Store as a row: `(spot_id, hero_action_freqs, ev_per_action)`.

**Phase 3: Vector index**. Embed each spot's text description (e.g.
"BTN 3-bet pot, KhTh7c flop, BB checks") into a vector DB
(pgvector, qdrant, weaviate). Keep `spot_id` as metadata.

**Phase 4: Runtime retrieval**. At decision time, embed the live
table state, retrieve top-5 nearest spots, hand the frequencies to
`decide()` (or your LLM) as `research_context`.

---

## Choose which tier

| You want…                       | Pick |
|----------------------------------|------|
| First end-to-end submission today | L1 |
| Beat 3 of 5 reference bots tomorrow | L2 with Claude Sonnet + Auto Research stages 1+3 |
| Top of the benchmark leaderboard | L3 option D (solver lookup) + Auto Research stages 1–4 |
| Original research, NeurIPS paper | L3 option A (DeepCFR) trained on Auto Research labels |

---

## Heuristic Learning loop

**What it is.** A paradigm from Jiayi Weng (MTS, OpenAI): use an LLM as a
*coding agent* to write and refine Python policy code. No gradient descent.
No neural net at inference time. The LLM is called during development; the
deployed bot is pure Python.

> "Maybe heuristics were not too weak. Maybe they were just too expensive
> to maintain. Maybe it's the next paradigm." — Jiayi Weng

In practice: Codex grew programmatic policies (no neural nets) that hit max
score on Breakout and SOTA on MuJoCo. The same loop works for poker — let
the LLM write a better `decide()` once, then run it at zero cost forever.

This is sharply different from L2 / Level 5 (LLM called *per hand at
runtime*):

| | Level 5 — LLM plays | Heuristic Learning (Level 4) |
|---|---|---|
| LLM called | every hand (paid) | once per iteration, offline |
| Runtime cost | paid (varies by model) | $0 |
| Interpretable | no | yes (pure Python) |
| Speed | slow (API latency) | instant |
| Ceiling | high if prompted well | as high as you program |

### The 6-step loop (repeat until bb/100 plateaus)

```
1. STRATEGY    cp examples/STRATEGY.md.template STRATEGY.md
               Fill in: ranges, sizing, aggression, adaptation rules.
               This is your "spec" for the coding agent.

2. ANALYZE     pokerkit analyze --out failure_report.txt
               → which positions/hands are losing the most chips?
               → paste-ready report for Claude Code

3. CODE        Paste STRATEGY.md + failure_report.txt + HL prompt
               (from examples/prompt.md) into Claude Code / Codex.
               The LLM rewrites decide() in examples/agent.py,
               baking ranges and rules into Python — zero runtime LLM.

4. TEST        pokerkit test
               → 20 canonical scenario fixtures, all must pass.

5. EVALUATE    pokerkit run --max-hands 50   (~3-5 min on Arena)
               → bb/100 delta vs previous run.
               → If worse: revert, adjust prompt, go to 3.
               → If better: commit, continue.

6. FULL RUN    pokerkit run   (when satisfied — 500 hands, leaderboard)
```

### What the coding agent bakes into decide()

| Research source | What to encode in code |
|---|---|
| STRATEGY.md ranges | Opening/defending sets per position: `UTG_OPEN = {"AA","KK",...}` |
| `research_static_chart.py` | Full preflop chart already implemented — import and wire in |
| Postflop solver output | Board-texture buckets → bet sizing: `dry → 0.33 * pot` |
| `pokerkit analyze` report | Specific position/hand fixes: "UTG losing 42 chips avg → tighten range" |
| `/texas/agent-stats` API | Opponent HUD: VPIP > 40% → thin value; < 20% → bluff less |

The failure report shows which seats (positions) and hole-card combinations
are losing the most chips. Give it to Claude Code alongside `STRATEGY.md`
and it will patch the exact weaknesses each iteration.

---

## Solver / GTO / CFR primer

| Term | One-line meaning |
|------|------------------|
| GTO  | Game-Theoretic Optimal — strategy that can't be exploited |
| Solver | Software that approximates GTO for a specific spot |
| CFR  | Counterfactual Regret Minimization — the algorithm most solvers use |
| Abstraction | Bucketing hands or bet sizes so CFR is tractable |
| Exploitability | bb/100 a perfect adversary could win — lower is closer to GTO |
| Range | The distribution of hands an opponent could hold |

---

## Files map

| Tier | Reference file                                |
|------|-----------------------------------------------|
| L1   | `examples/agent.py` (decide + retrieve_solver_context) |
| L2   | `examples/llm_agent.py` — model-agnostic decide (Anthropic / OpenAI / OpenAI-compat), research_context aware |
| L3   | not shipped — see options A/B/C/D above       |

Each file's `decide()` follows the same signature:
`decide(table, deadline_s, research_context=None) -> {action, amount?, message, reasoning}`.
Swap in your own and the rest of the loop keeps working.
