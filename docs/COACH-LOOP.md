# The self-improving coach loop

System 7 closes the build loop: it plays, records, and then **MiniMax M3 proposes a new strategy
version from its own results**, which you review and A/B against a frozen control. The strategy is
expressed as *data* (validated JSON config), so every proposal is diffable, clamped to safe ranges,
and fully reversible.

## Why strategy-as-data

The decision engine (`decide_system7.py`) reads its opening ranges, 3-bet lists and postflop knobs
from `strategies/<name>.json`, selected by the `S7_STRAT` environment variable. The built-in
baseline is reproduced by an empty config, so:

- A coach proposal is a **delta over a known-good reference** — only the keys it wants to change.
- Bad tokens/values are ignored or clamped, never crash the engine.
- Rolling back is just selecting a different version; nothing is destructive.

### Config schema (`strategies/<name>.json`)

```jsonc
{
  "base": "std",                 // "std" | "wide" — which built-in opening ranges to start from
  "opening_ranges": {            // optional per-position overrides
    "BTN": ["22+", "A2s+", "K9o+", "QTo+", "T9s"],
    "CO":  ["55+", "A8s+", "KTo+"]
  },
  "threebet_value":  ["QQ+", "AKs"],     // value 3-bet classes
  "threebet_bluff":  ["A5s", "KJs"],     // blocker-bluff 3-bet classes
  "knobs": {                     // postflop tuning (see ARCHITECTURE.md → knobs table)
    "open_size_bb": 2.5, "threebet_mult": 3, "value_eq": 0.62,
    "station_mult": 1.2, "cbet_bluff_frac": 0.33, "commit_spr": 3,
    "perejil_flop": 8, "perejil_turn": 10, "perejil_relief": 2,
    "sizing": { "dry": { "flop": 0.33 }, "wet": { "flop": 0.66 } }
  }
}
```

Seed versions shipped: `std.json` (`{}` → baseline), `fijo.json` (`{}` → frozen control = baseline),
`wide.json` (`{"base": "wide"}`). Coach-generated versions are saved as `strategies/coach-<ts>.json`
(git-ignored; they are experiment artifacts).

## The loop

```
   play (s7_test) ──► record (s7_stats) ──► report (s7_report)
        ▲                                          │
        │                                          ▼
   launch version  ◄── you review ◄── M3 proposes strategies/coach-<ts>.json
   (S7_STRAT=<v>)      the proposal      (validated + clamped JSON)
        │
        └────────── A/B vs frozen control `fijo` on the equity curve ──────────┐
                                                                                 ▼
                                                              keep / discard → next iteration
```

1. **Play & record.** Run one or two arms on the eval bench, e.g. the evolving `std` arm and a
   frozen `fijo` control:
   ```bash
   S7_STRAT=std  S7_RUN_LABEL=std  uv run s7_test.py --engine hybrid --matches 5
   S7_STRAT=fijo S7_RUN_LABEL=fijo uv run s7_test.py --engine hybrid --matches 5
   ```
2. **Gate.** Once enough hands accumulate (the dashboard's `COACH_NEED`, default 5000), the
   **COACH** tab unlocks.
3. **Propose.** Click *"pedir consejo a M3"*. The coach feeds `s7_report.report()` + the current
   knobs to M3 and asks for (a) an actionable leak analysis and (b) a JSON block with the keys to
   change. The dashboard parses that block, runs it through `_validate_strat` (whitelists keys,
   clamps every knob to a safe range, caps list sizes), and saves it as `strategies/coach-<ts>.json`.
4. **Review.** The proposal (prose + the saved config + a *"lanzar versión"* button) is shown in the
   COACH tab. **You decide** — nothing auto-applies.
5. **A/B.** Launching the version starts an arm with `S7_STRAT=<version>` and `S7_RUN_LABEL=<version>`
   against the frozen `fijo` control. Compare them on the dashboard's real-vs-EV equity curve and the
   per-engine performance tables.
6. **Iterate.** Keep the winners, discard the rest, ask for the next proposal. Repeat until the bot
   is tournament-ready.

## Guardrails

- **Propose, don't auto-apply.** The coach only ever *writes a candidate file* and surfaces it; a
  human launches it. This keeps the loop reproducible and auditable.
- **Validation & clamping.** `_validate_strat` is the trust boundary between the LLM and the engine:
  unknown keys are dropped, numeric knobs are clamped to sane bounds, range/list sizes are capped.
- **Frozen control.** `fijo` never changes, so the equity comparison always has a stable baseline and
  improvements are measured, not assumed.
- **Regression gate.** `tests/test_system7.py` asserts the default/`std` config still reproduces the
  built-in engine exactly, so the config plumbing can't silently drift the baseline.

## Launching a version by hand

```bash
S7_STRAT=coach-0615-1432 S7_RUN_LABEL=coach-0615-1432 \
  uv run s7_test.py --engine hybrid --matches 5
```

The new label appears as its own series on the equity curve, directly comparable to `fijo`.
