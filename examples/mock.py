"""Dry-run mock infrastructure for `--dry-run` flows.

This module is only imported when `--dry-run` is set on agent.py /
llm_agent.py. It wires httpx.MockTransport into ArenaClient so the
full happy path runs end-to-end with zero outbound network.

Scenarios (--dry-run-scenario):
  - instant: pending-actions immediately serves one table, then empties (default)
  - queued:  status returns phase=panel_acting for 2 polls before serving a table
  - stale:   first /texas/action returns 409 (stale), agent retries successfully
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any, Optional

import httpx

from arena_client import (
    ArenaClient,
    ArenaError,
    MOCK_BASE,
    assert_endpoints,
    fetch_introspection,
    load_or_register,
    resolve_terminal_phases,
)


MOCK_COMPETITION_ID = "comp_dryrun"


def respx_active() -> bool:
    """True iff respx has installed its global httpx patcher. Used by the
    dry-run path so tests can still mock individual routes with respx."""
    try:
        from respx.mocks import HTTPCoreMocker  # type: ignore
    except Exception:
        return False
    try:
        return bool(list(HTTPCoreMocker.routers))
    except Exception:
        return False


def mock_table(competition_id: str) -> dict:
    """Synthetic table identical in shape to a live pending-actions row:
    hero faces a $100 bet on Ah Kd 7c with AsKs."""
    return {
        "id": "tbl_dry",
        "tableId": "tbl_dry",
        "tableNumber": 1,
        "competitionId": competition_id,
        "status": "Active",
        "street": "Flop",
        "potChips": 300,
        "currentBet": 100,
        "minRaiseTo": 200,
        "startedAt": 1700000000000,
        "endedAt": None,
        "countdownEndsAt": None,
        "actionDeadlineAt": None,
        "currentSeatNumber": 1,
        "boardCards": ["Ah", "Kd", "7c"],
        "smallBlindChips": 10,
        "bigBlindChips": 20,
        "buyInChips": 2000,
        "winners": [],
        "seats": [
            {"seatId": "s1", "seatNumber": 1, "agentId": "me",
             "agentName": "Me", "agentHandle": "me", "status": "Active",
             "stackChips": 1800, "currentBetChips": 0,
             "totalCommittedChips": 0, "payoutChips": None,
             "holeCards": ["As", "Ks"]},
            {"seatId": "s2", "seatNumber": 2, "agentId": "opp",
             "agentName": "Opp", "agentHandle": "opp", "status": "Active",
             "stackChips": 1700, "currentBetChips": 100,
             "totalCommittedChips": 100, "payoutChips": None,
             "holeCards": None},
        ],
        "actingSeatNumber": 1,
        "selfSeatNumber": 1,
        "allowedActions": {
            "canFold": True, "canCheck": False, "canCall": True,
            "canBet": False, "canRaise": True,
            "callAmount": 100, "callChips": 100, "callToAmount": 100,
            "minBet": None, "minRaiseTo": 200, "maxCommit": 1800,
            "allInToAmount": 1800,
            "betRange": None,
            "raiseRange": {"min": 200, "max": 1800},
            "canAllIn": True,
            "availableActions": ["fold", "call", "raise", "all-in"],
            "amountSemantics": "toAmount",
            "amountHint": "total committed this street",
            "actionHint": "fold/call/raise to >= 200 / all-in 1800",
        },
        "recentEvents": [],
    }


def mock_introspection_schema() -> dict:
    """Tiny introspection skeleton that satisfies assert_endpoints() and
    resolve_terminal_phases() in dry-run mode."""
    return {
        "endpoints": [
            {"method": "POST", "path": "/api/arena/auth/register", "auth": False},
            {"method": "GET",  "path": "/api/arena/agent/me", "auth": True},
            {"method": "GET",  "path": "/api/arena/__introspection", "auth": True},
            {"method": "POST", "path": "/api/arena/texas/benchmark/start", "auth": True,
             "output": {
                 "properties": {
                     "match": {
                         "properties": {
                             "phase": {"enum": ["queued", "panel_acting",
                                                "waiting_user", "completed",
                                                "cancelled", "failed"]},
                             "status": {"enum": ["Running", "Completed",
                                                 "Cancelled", "Failed"]},
                         }
                     }
                 }
             }},
            {"method": "GET",  "path": "/api/arena/texas/benchmark/status", "auth": True},
            {"method": "GET",  "path": "/api/arena/texas/pending-actions", "auth": True},
            {"method": "POST", "path": "/api/arena/texas/action", "auth": True},
        ]
    }


def run_mock_benchmark(args: argparse.Namespace,
                       decide_fn: Optional[Any] = None,
                       retrieve_solver_context: Optional[Any] = None) -> int:
    """In-process dry-run: wire httpx.MockTransport into ArenaClient so the
    full happy path runs end-to-end with zero network access.

    decide_fn lets the L2 dry-run inject `llm_decide` so --dry-run actually
    exercises the LLM path. retrieve_solver_context is the Auto Research hook
    from agent.py (passed in to avoid a hard import cycle)."""
    # Late import to avoid circular dependency at module import time.
    if decide_fn is None or retrieve_solver_context is None:
        import agent as agent_mod  # type: ignore
        if decide_fn is None:
            decide_fn = agent_mod.decide
        if retrieve_solver_context is None:
            retrieve_solver_context = agent_mod.retrieve_solver_context

    competition_id = (args.competition_id
                      or os.environ.get("ARENA_COMPETITION_ID")
                      or MOCK_COMPETITION_ID)
    table_state = mock_table(competition_id)
    target_hands = max(int(args.max_hands or 1), 1)

    scenario = getattr(args, "dry_run_scenario", "instant") or "instant"

    # State for the mock — pending_idx walks tables → empty;
    # status_idx walks running → completed after the action lands.
    pending_idx = {"i": 0}
    status_idx = {"i": 0}
    action_landed = {"v": False}
    stale_served = {"v": False}

    # Scenario tunings.
    # queued: first 2 pending polls return empty + status reports panel_acting,
    #         then the table appears.
    # stale:  first action submission returns 409, retry succeeds.
    queued_warmup_polls = 2 if scenario == "queued" else 0

    def _status_payload(phase: str, completed: int, table: Optional[dict]) -> dict:
        return {
            "match": {
                "id": "m_dry", "competitionId": competition_id, "agentId": "agent_dry",
                "status": "Running" if phase != "completed" else "Completed",
                "phase": phase,
                "targetHands": target_hands, "completedHands": completed,
                "rawChipDelta": 250 if phase == "completed" else 0,
                "rawBbPer100": 12.5 if phase == "completed" else 0.0,
                "adjustedChipDelta": 200.0 if phase == "completed" else None,
                "adjustedBbPer100": 10.0 if phase == "completed" else None,
                "currentTableId": (table.get("tableId") if table else None),
                "startedAt": 1700000000000,
                "endedAt": 1700000010000 if phase == "completed" else None,
                "error": None,
            },
            "table": table,
            "participant": None,
        }

    action_log: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/auth/register"):
            return httpx.Response(200, json={
                "agentId": "agent_dry", "apiKey": "dry_key_xxx",
                "handle": args.handle, "name": args.name,
            })
        if path.endswith("/agent/me"):
            return httpx.Response(200, json={
                "id": "agent_dry", "agentId": "agent_dry",
                "handle": args.handle, "name": args.name,
            })
        if path.endswith("/__introspection"):
            return httpx.Response(200, json=mock_introspection_schema())
        if path.endswith("/texas/benchmark/start"):
            phase = "panel_acting" if scenario == "queued" else "queued"
            return httpx.Response(200, json=_status_payload(phase, 0, None))
        if path.endswith("/texas/pending-actions"):
            i = pending_idx["i"]
            pending_idx["i"] += 1
            # queued scenario: empty for the warmup polls.
            if i < queued_warmup_polls:
                return httpx.Response(200, json={"tables": []})
            effective_i = i - queued_warmup_polls
            if effective_i == 0 and not action_landed["v"]:
                return httpx.Response(200, json={"tables": [table_state]})
            return httpx.Response(200, json={"tables": []})
        if path.endswith("/texas/benchmark/status"):
            i = status_idx["i"]
            status_idx["i"] += 1
            if action_landed["v"]:
                return httpx.Response(200, json=_status_payload("completed", target_hands, None))
            # queued scenario: report panel_acting during warmup.
            if scenario == "queued" and i < queued_warmup_polls:
                return httpx.Response(200, json=_status_payload("panel_acting", 0, None))
            return httpx.Response(200, json=_status_payload("queued", 0, None))
        if path.endswith("/texas/action"):
            try:
                action_log.append(json.loads(request.content.decode()))
            except Exception:
                pass
            # stale scenario: first action 409, retry succeeds. Reset
            # pending_idx so the next /pending-actions re-serves the table.
            if scenario == "stale" and not stale_served["v"]:
                stale_served["v"] = True
                pending_idx["i"] = queued_warmup_polls
                return httpx.Response(409, json={
                    "error": "table state is stale, re-poll pending-actions",
                })
            action_landed["v"] = True
            return httpx.Response(200, json={"table": table_state, "participant": None})
        return httpx.Response(404, json={"error": f"unmocked {path}"})

    client = ArenaClient(MOCK_BASE, api_key="dry_key_xxx")
    if not respx_active():
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(_handler),
                                      timeout=10.0, trust_env=False)

    try:
        creds = load_or_register(client, args.handle, args.name, args.quote)
        print(f"[arena-pokerkit] (dry-run, scenario={scenario}) "
              f"registered agent={creds.get('agentId', '?')} base={MOCK_BASE}")

        schema = fetch_introspection(client)
        assert_endpoints(schema)
        terminal_phases, terminal_statuses = resolve_terminal_phases(schema)

        start_resp = client.post("/texas/benchmark/start",
                                 {"competitionId": competition_id})
        if not isinstance(start_resp, dict):
            raise ArenaError(0, str(start_resp)[:200], "benchmark/start malformed")
        match = start_resp.get("match") or {}
        print(f"[arena-pokerkit] (dry-run) benchmark started: phase={match.get('phase')} "
              f"target={match.get('targetHands')}")

        # P2-5: delegate to the shared live loop so dry-run does not drift.
        # This gives the mock 400-fallback, 401/403 repair, malformed-input
        # validation, --max-hands honoring, and a heartbeat before decide().
        from agent import _run_benchmark_loop  # late import to avoid cycle
        rc = _run_benchmark_loop(
            client=client,
            args=args,
            competition_id=competition_id,
            decide_fn=decide_fn,
            retrieve_fn=retrieve_solver_context,
            terminal_phases=terminal_phases,
            terminal_statuses=terminal_statuses,
            label=" (dry-run)",
        )
        if action_log:
            chosen = action_log[-1]
            print(f"[arena-pokerkit] (dry-run) decided "
                  f"action={chosen.get('action')} "
                  f"amount={chosen.get('amount')} "
                  f"reasoning={chosen.get('reasoning')!r}")
        return rc
    finally:
        client.close()
