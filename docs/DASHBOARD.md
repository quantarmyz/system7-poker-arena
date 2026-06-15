# Real-time dashboard & hand replayer

`s7_dash.py` is a dense, dependency-free web dashboard (Python stdlib `http.server` only) that reads
the recorded SQLite DB **read-only** and renders live stats, an equity curve, opponent HUDs,
per-engine performance and a step-by-step hand replayer. It also hosts the **COACH** and a **RUN**
control plane.

```bash
uv run s7_dash.py          # serves http://localhost:8787  (binds 0.0.0.0:8787)
```

## Tabs

| Tab | Contents |
|---|---|
| **PANEL** | KPIs (hands, decisions, M3 %), real-vs-EV **equity curve** (toggle per strategy/EV), VPIP grid by hand class, stats by position/street/strength, M3 usage, **average opponent** HUD, **per-engine performance**, and a live decision ticker. |
| **MANOS** | Sortable grid of every hand: time, position, hole cards, board, street reached, moves, SPR (pre/post), and the **net result**. Click a row to open the replayer. |
| **PLAYERS** | Opponent HUDs (VPIP/PFR/AF → archetype) plus an "average enemy" summary. |
| **COACH** | Gated on `COACH_NEED` hands; asks M3 for an analysis + a proposed strategy version with a *"lanzar versión"* button. See [COACH-LOOP.md](COACH-LOOP.md). |
| **RUN** | Launch/stop training arms (engine, strategy version, matches) with a live debug log. Strictly validated, args-as-list, LAN-only. |

## API endpoints (read-only JSON)

`/api/state` (panel aggregate), `/api/hands` (grid), `/api/hand?key=<hand_key>` (one hand for the
replayer), `/api/players`, `/api/runs`, `/api/strats`. All responses are sent with
`Cache-Control: no-store` so a long-lived tab never runs stale JS against an evolved payload.

## The hand replayer

Open any hand to step through it: blinds → actions → streets, with a mini-table (chips, SPR, pot,
dealer button, who's to act), the per-street action log, and our decision "reads" (strength, SPR,
outs, and the M3 reasoning when the LLM was used).

### Honest about a hard API limit

The Arena only delivers the event window (`recentEvents`, ~20 events) **on our own turns**, via
`/texas/pending-actions` — there is no "table-by-id" or hand-history endpoint, and `sequence` resets
per hand. **Therefore the betting that happens after our last action** (villain bets, later streets,
the showdown) **is not recoverable as data.** The replayer is built around this honestly:

1. **`buildTimeline()`** filters noise (`Joined`/`TableStarted`), orders events by `sequence`, and
   **synthesizes our own actions** from the `decisions` table (the window is captured *before* we
   act, so our move is otherwise missing), de-duplicated by seat+street.
2. A final **`RESULTADO`** node is appended; the showdown reveal (final board, revealed holes,
   winners, payouts) is shown **only there** — never spliced onto an earlier event step (the old
   behaviour made the table appear to "teleport" to showdown mid-hand).
3. When the result is on a later street than our last seen action, a **gap note** explains it
   ("after your fold the hand continued without you…") instead of pretending we saw it.
4. The settled hand's **official `replayUrl`** (from `/agent/{id}/replays`) is stored and surfaced as
   a *"▶ repro oficial"* link — the faithful source for the unseen portion.

This means the in-app replay always reflects exactly what we observed, plus a clearly-marked jump to
the verified final result, with a one-click path to the arena's own full replay.

## Notes

- **Read-only DB access.** The dashboard opens the WAL database with `mode=ro`, so it never blocks
  the live writers (`s7_test.py` / the runners).
- **No build step, no dependencies.** It's a single Python file serving inline HTML/CSS/JS.
- **LAN/tailnet only.** It binds `0.0.0.0:8787`; keep it behind your private network — the RUN tab is
  a control plane and is intentionally not exposed publicly.
