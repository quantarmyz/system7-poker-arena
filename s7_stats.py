"""Advanced stats recorder for System 7 (SQLite, WAL — safe for long/concurrent use).

Records every action with full context → playing ranges, VPIP/PFR/AF by
position/street/hand-class, M3 usage; bankroll snapshots; and per-test-run results
(bb/100 per strategy version). Pick the DB via S7_STATS_DB (PvP uses s7_stats.db,
the Eval test-bench uses s7_test.db). Read it back with s7_report.py.
"""
import json
import os
import sqlite3
import time

DB = os.environ.get("S7_STATS_DB",
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "s7_stats.db"))

_DECISION_COLS = (
    "ts", "table_id", "hand_key", "street", "pos", "ip", "hole", "hand_class",
    "board", "texture", "strength", "spr", "pot", "call_chips", "pot_odds",
    "adj_outs", "n_villains", "archetype", "engine", "action", "amount",
    "voluntary", "preflop_raise", "run_label", "m3_log", "model",
)


def _conn():
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS decisions(
            ts REAL, table_id TEXT, hand_key TEXT, street TEXT, pos TEXT, ip INTEGER,
            hole TEXT, hand_class TEXT, board TEXT, texture TEXT, strength TEXT,
            spr REAL, pot INTEGER, call_chips INTEGER, pot_odds REAL, adj_outs INTEGER,
            n_villains INTEGER, archetype TEXT, engine TEXT, action TEXT, amount INTEGER,
            voluntary INTEGER, preflop_raise INTEGER, run_label TEXT, m3_log TEXT, model TEXT)""")
        # migrate older DBs that predate run_label / m3_log / model
        cols = {r[1] for r in c.execute("PRAGMA table_info(decisions)")}
        if "run_label" not in cols:
            c.execute("ALTER TABLE decisions ADD COLUMN run_label TEXT")
        if "m3_log" not in cols:
            c.execute("ALTER TABLE decisions ADD COLUMN m3_log TEXT")
        if "model" not in cols:
            c.execute("ALTER TABLE decisions ADD COLUMN model TEXT")
        c.execute("""CREATE TABLE IF NOT EXISTS bankroll(
            ts REAL, table_chips INTEGER, hands INTEGER, rebuys INTEGER, note TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS runs(
            ts REAL, run_label TEXT, agent_id TEXT, engine TEXT, hands INTEGER,
            adjusted_bb100 REAL, raw_bb100 REAL, raw_chip_delta INTEGER, m3_calls INTEGER, note TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS equity(
            ts REAL, run_label TEXT, hands INTEGER, raw_chips REAL, adj_chips REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS hand_events(
            hand_key TEXT PRIMARY KEY, ts REAL, seat INTEGER, hole TEXT, board TEXT,
            events TEXT, n_events INTEGER, seats TEXT)""")
        if "seats" not in {r[1] for r in c.execute("PRAGMA table_info(hand_events)")}:
            c.execute("ALTER TABLE hand_events ADD COLUMN seats TEXT")
        c.execute("""CREATE TABLE IF NOT EXISTS hand_results(
            table_id TEXT PRIMARY KEY, ts REAL, board TEXT, winners TEXT, seats_shown TEXT,
            payout INTEGER, chip_delta INTEGER, our_hand TEXT, replay_url TEXT)""")
        if "replay_url" not in {r[1] for r in c.execute("PRAGMA table_info(hand_results)")}:
            c.execute("ALTER TABLE hand_results ADD COLUMN replay_url TEXT")
        c.execute("""CREATE TABLE IF NOT EXISTS agent_stats(
            agent_id TEXT PRIMARY KEY, ts REAL, name TEXT, n INTEGER, vpip REAL, pfr REAL,
            af REAL, bluff_pct REAL, wtsd REAL, wsd REAL, style TEXT)""")
        c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS mllm_runs(
            run_id TEXT PRIMARY KEY, ts REAL, status TEXT, models TEXT, judge TEXT,
            n_hands INTEGER, n_reps INTEGER, note TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mllm_results(
            run_id TEXT, model TEXT, provider TEXT, hand_key TEXT, dec_ts REAL, rep INTEGER,
            action TEXT, amount INTEGER, valid INTEGER, latency_ms INTEGER,
            prompt_tokens INTEGER, completion_tokens INTEGER, answer TEXT, reasoning TEXT,
            think TEXT, judge_score REAL, judge_note TEXT, m3_action TEXT, ts REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mllm_res_run ON mllm_results(run_id)")


def log_decision(d: dict):
    row = tuple(d.get(k) for k in _DECISION_COLS)
    with _conn() as c:
        c.execute(f"INSERT INTO decisions({','.join(_DECISION_COLS)}) "
                  f"VALUES({','.join('?' * len(_DECISION_COLS))})", row)


def log_bankroll(table_chips, hands, rebuys, note=""):
    with _conn() as c:
        c.execute("INSERT INTO bankroll VALUES(?,?,?,?,?)",
                  (time.time(), table_chips, hands, rebuys, note))


def log_run(run_label, agent_id, engine, hands, adjusted_bb100, raw_bb100, raw_chip_delta, m3_calls, note=""):
    with _conn() as c:
        c.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (time.time(), run_label, agent_id, engine, hands,
                   adjusted_bb100, raw_bb100, raw_chip_delta, m3_calls, note))


def log_equity(run_label, hands, raw_chips, adj_chips):
    """Cumulative actual (raw) vs EV (variance-adjusted) chips at a given hand count."""
    with _conn() as c:
        c.execute("INSERT INTO equity VALUES(?,?,?,?,?)",
                  (time.time(), run_label, hands, raw_chips, adj_chips))


def log_hand_events(hand_key, seat, hole, board, events, seats=None):
    """Merge the event timeline across our decisions (dedup by `sequence`) so the full
    hand — all players, every street up to our last action — is preserved, not just one
    20-event sliding window. Keeps the longest board + richest seats snapshot."""
    events = events or []
    if not hand_key or not events:
        return
    with _conn() as c:
        row = c.execute("select board, events, seats from hand_events where hand_key=?", (hand_key,)).fetchone()
        merged, prev_board, prev_seats = {}, "", "[]"
        if row:
            prev_board, prev_seats = (row[0] or ""), (row[2] or "[]")
            for e in (json.loads(row[1]) if row[1] else []):
                merged[e.get("sequence")] = e
        for e in events:
            merged[e.get("sequence")] = e
        ev = sorted(merged.values(), key=lambda e: (e.get("sequence") if e.get("sequence") is not None else 0))
        board = board if len(board or "") >= len(prev_board) else prev_board
        new_seats = json.dumps(seats, default=str) if seats else None
        seats_json = new_seats if (new_seats and new_seats != "[]") else prev_seats
        c.execute("insert or replace into hand_events values(?,?,?,?,?,?,?,?)",
                  (hand_key, time.time(), seat, hole, board,
                   json.dumps(ev, default=str), len(ev), seats_json))


def log_hand_result(table_id, board, winners, seats_shown, payout, chip_delta, our_hand="", replay_url=""):
    """Settled-hand result from /texas/recent-tables (+replays): winners, revealed cards, official replay url."""
    if not table_id:
        return
    with _conn() as c:
        if not replay_url:                       # don't clobber a previously-stored url with ""
            prev = c.execute("select replay_url from hand_results where table_id=?", (str(table_id),)).fetchone()
            if prev and prev[0]:
                replay_url = prev[0]
        c.execute("insert or replace into hand_results values(?,?,?,?,?,?,?,?,?)",
                  (str(table_id), time.time(), board or "",
                   json.dumps(winners or [], default=str), json.dumps(seats_shown or [], default=str),
                   payout, chip_delta, our_hand or "", replay_url or ""))


def log_agent_stats(agent_id, name, st):
    """Persist a villain's official HUD (/texas/agent-stats) for the PLAYERS tab."""
    if not agent_id or not st:
        return
    with _conn() as c:
        c.execute("insert or replace into agent_stats values(?,?,?,?,?,?,?,?,?,?,?)",
                  (str(agent_id), time.time(), name, st.get("N"), st.get("vpip"), st.get("pfr"),
                   st.get("af"), st.get("bluffPct"), st.get("wtsd"), st.get("wsd"),
                   json.dumps(st.get("playingStyle"), default=str)))


def set_meta(k, v):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, json.dumps(v, default=str)))


# ── multiLLM benchmark ──────────────────────────────────────────────────────────
def mllm_start(run_id, models, judge, n_hands, n_reps, note=""):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO mllm_runs VALUES(?,?,?,?,?,?,?,?)",
                  (run_id, time.time(), "running", json.dumps(models, default=str),
                   judge or "", n_hands, n_reps, note))


def mllm_finish(run_id, status="done"):
    with _conn() as c:
        c.execute("UPDATE mllm_runs SET status=? WHERE run_id=?", (status, run_id))


_MLLM_COLS = ["run_id", "model", "provider", "hand_key", "dec_ts", "rep", "action", "amount",
              "valid", "latency_ms", "prompt_tokens", "completion_tokens", "answer", "reasoning",
              "think", "judge_score", "judge_note", "m3_action"]


def log_mllm_result(d: dict):
    with _conn() as c:
        c.execute("INSERT INTO mllm_results(" + ",".join(_MLLM_COLS) + ",ts) VALUES(" +
                  ",".join("?" * len(_MLLM_COLS)) + ",?)",
                  tuple(d.get(k) for k in _MLLM_COLS) + (time.time(),))
