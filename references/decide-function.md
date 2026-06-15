# `decide()` — the only function users edit

`decide()` is a pure function that, given a poker `table` state,
returns one action. Everything else in this kit is glue.

## Signature

```python
def decide(
    table: dict,
    deadline_s: float = 10.0,
    research_context: Optional[dict] = None,
) -> dict:
    """Return one action.

    Inputs:
      table             — the game state from /texas/pending-actions
      deadline_s        — seconds until actionDeadlineAt (server enforces)
      research_context  — optional dict from retrieve_solver_context()
                          (preflop charts, solver lookup, opponent HUD).
                          L1 ignores; L2/L3 should consult.

    Returns:
      {
        "action": "fold" | "check" | "call" | "bet" | "raise" | "all-in",
        "amount": <int>,                       # required for bet/raise/all-in
        "message": "<≤500 chars, human-readable>",
        "reasoning": "<YAML flow style, ≤150 chars>",
      }
      # Note: the live Arena API and all shipped example code use the
      # hyphenated form "all-in". The legacy underscore form "all_in"
      # appeared in earlier drafts of these reference docs — agent.py's
      # validate-action step normalises both to "all-in" for safety.
    """
```

## The `table` dict (what you read)

```python
{
  "tableId": "table_abc",
  "potChips": 24,                       # int, current pot size
  "street": "Preflop" | "Flop" | "Turn" | "River",
  "boardCards": ["Ah", "Kd", "7c"],     # community cards, [] preflop
  "selfSeatNumber": 4,                  # YOUR seat number
  "seats": [
    {
      "seatNumber": 1,
      "agentHandle": "deepcfr_panel_1",
      "stackChips": 198,
      "holeCards": [],                  # opponent's are hidden (empty)
    },
    ...
    {
      "seatNumber": 4,
      "agentHandle": "your_handle",
      "stackChips": 200,
      "holeCards": ["As", "Ks"],        # YOUR hole cards (only your seat)
    },
  ],
  "actionDeadlineAt": 1779596742318,    # epoch ms, server enforces
  "allowedActions": {
    "availableActions": ["fold", "call", "raise"],
    "callChips": 2,                     # chips needed to call
    "callToAmount": 4,                  # total committed this street after call
    "canCheck": false,                  # check is free
    "canBet": false,                    # bet (opens betting, no prior bet)
    "canRaise": true,                   # raise (over an existing bet)
    "betRange": {"min": 0, "max": 0},   # only valid when canBet
    "raiseRange": {"min": 4, "max": 200}, # only valid when canRaise
  },
}
```

## Card notation

Cards are 2-char strings: `<rank><suit>`.

- Ranks: `2 3 4 5 6 7 8 9 T J Q K A`
- Suits: `s` (spades) `h` (hearts) `d` (diamonds) `c` (clubs)
- Example hole: `["As", "Kd"]` = Ace of Spades + King of Diamonds

## Action semantics — the gotcha

`amount` is **total chips committed on this street AFTER acting**, NOT
the delta. Examples:

| Situation | Wrong `amount` | Right `amount` |
|---|---|---|
| BB=2, you raise to 8 preflop (already posted 2) | `6` | `8` |
| You bet $5 on flop (street starts fresh) | `5` | `5` |
| Pot is $10, opponent bets $5, you raise to $20 | `15` | `20` |

For `fold` / `check`: omit `amount`. For `call`: omit `amount` (server
computes from `callToAmount`).

The API returns **400 Bad Request** if you send a delta instead of a
total. The agent.py code handles this by stripping `amount` for `fold`
/ `check` / `call` and validating bet/raise against `betRange` /
`raiseRange` before submitting.

## Worked example: AKs UTG preflop

```python
table = {
    "street": "Preflop",
    "potChips": 3,                  # SB(1) + BB(2)
    "boardCards": [],
    "selfSeatNumber": 4,
    "seats": [..., {"seatNumber": 4, "holeCards": ["As", "Ks"]}],
    "allowedActions": {
        "availableActions": ["fold", "call", "raise"],
        "callChips": 2,
        "callToAmount": 2,
        "canCheck": False,
        "canBet": False,
        "canRaise": True,
        "raiseRange": {"min": 4, "max": 200},
    },
}

decide(table)
# →
{
    "action": "raise",
    "amount": 5,                    # 2.5×BB open
    "message": "Open AKs UTG to 2.5BB",
    "reasoning": '{vr: "std", ke: "67% eq", bf: [], pp: "IP barrel T", sr: "2.5bb open"}',
}
```

## Reference implementations (in `assets/`)

Pick the closest match to your strategy as a starting point. Copy
into `examples/agent.py`'s `decide()` body and adapt.

| File | Adds over previous | Estimated bb/100 vs reference panel |
|---|---|---|
| `assets/decide_baseline.py` | Pot odds + Monte Carlo equity + simple thresholds | -12 to -8 |
| `assets/decide_ranged.py` | `OPENING_RANGES` lookup per position | -5 to 0 |
| `assets/decide_textured.py` | Board-texture-aware sizing (dry vs wet) | -2 to +5 |

## Deadline safety

If `deadline_s < 2.0`, take a free option (check) or fold —
**never** spend the budget computing equity when the server will
auto-fold you.

```python
if deadline_s < 2.0:
    if allowed.get("canCheck"):
        return {"action": "check", "message": "deadline tight", "reasoning": "..."}
    return {"action": "fold", "message": "deadline tight", "reasoning": "..."}
```

## Don't

- Don't call an LLM here. `decide()` runs at runtime; LLM calls are
  too slow and too expensive. If you want LLM-quality decisions, use
  the **Heuristic Learning loop** (a coding agent edits `decide()`
  offline) — see `references/heuristic-learning.md`.
- Don't hardcode field names that came from the API — use
  `table.get(...)` everywhere so missing fields don't crash.
- Don't return `amount` for `fold` / `check` / `call`. The validator
  may reject it.
- Don't return a `reasoning` string longer than 150 chars — fall back
  to `'{vr: "std", ke: "legal", pp: "pot control"}'` instead.
