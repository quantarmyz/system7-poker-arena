"""Self-contained HTML replay viewer for past matches.

Fetches your agent's recent settled hands from
`/api/arena/agent/{agentId}/replays` (and per-hand submissions from
`/api/arena/agent/submissions`) and renders a single offline-friendly
`replay.html` file you can open in any browser or email to a friend.

Usage:
    pokerkit replay --latest               # most recent match
    pokerkit replay --match <competitionId> # specific competition
    pokerkit replay --list                  # list your last 10 competitions

Output: writes `replay.html` to the current directory.

The viewer uses Tailwind CDN + Alpine.js inline, no build step required.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from arena_client import (
    ArenaClient,
    ArenaError,
    DEFAULT_BASE,
    CREDS_PATH,
)


def _load_creds() -> tuple[Optional[str], Optional[str]]:
    """Load (api_key, agent_id) from .arena-credentials if present, else env."""
    if CREDS_PATH.exists():
        try:
            creds = json.loads(CREDS_PATH.read_text())
            return (creds.get("apiKey"),
                    creds.get("agentId") or creds.get("id"))
        except Exception:
            pass
    return os.environ.get("ARENA_API_KEY"), os.environ.get("ARENA_AGENT_ID")


def _probe_endpoint(client: ArenaClient, method: str, path: str) -> bool:
    """Check introspection for a (method, path) pair."""
    try:
        schema = client.get("/__introspection")
    except ArenaError:
        return False
    if not isinstance(schema, dict):
        return False
    full = f"/api/arena{path}"
    for ep in schema.get("endpoints") or []:
        if isinstance(ep, dict) and ep.get("method") == method:
            ep_path = ep.get("path", "")
            # path may contain {agentId} — strip path params when matching
            if ep_path == full:
                return True
            if "{" in ep_path:
                # crude match: same number of segments, fixed prefixes line up
                a = ep_path.split("/")
                b = full.split("/")
                if len(a) == len(b) and all(
                        x.startswith("{") or x == y for x, y in zip(a, b)):
                    return True
    return False


def fetch_replays(client: ArenaClient, agent_id: str,
                  competition_id: Optional[str] = None,
                  limit: int = 20) -> list[dict]:
    """Fetch recent settled hands from /agent/{agentId}/replays.
    Returns [] if endpoint missing or call fails."""
    if not _probe_endpoint(client, "GET", "/agent/{agentId}/replays"):
        print("[arena-pokerkit] /agent/{agentId}/replays not present in "
              "introspection — replay endpoint may not be public yet. "
              "Falling back to /agent/submissions for high-level data.",
              file=sys.stderr)
        return []
    qs = f"?limit={limit}"
    if competition_id:
        qs += f"&competitionId={competition_id}"
    try:
        body = client.get(f"/agent/{agent_id}/replays{qs}")
    except ArenaError as e:
        print(f"[arena-pokerkit] replays fetch failed: {e}", file=sys.stderr)
        return []
    if isinstance(body, list):
        return body
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return body["data"]
    return []


def fetch_submissions(client: ArenaClient,
                      agent_id: Optional[str],
                      competition_id: Optional[str] = None,
                      limit: int = 50) -> list[dict]:
    """Fetch per-hand submissions (with hole cards + payouts) for an agent."""
    parts = [f"limit={limit}"]
    if agent_id:
        parts.append(f"agentId={agent_id}")
    if competition_id:
        parts.append(f"competitionId={competition_id}")
    qs = "?" + "&".join(parts)
    try:
        body = client.get(f"/agent/submissions{qs}")
    except ArenaError as e:
        print(f"[arena-pokerkit] submissions fetch failed: {e}", file=sys.stderr)
        return []
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return body["data"]
    return []


def list_recent_competitions(submissions: list[dict]) -> list[dict]:
    """Group submissions by competition. Returns most-recent first."""
    seen: dict[str, dict] = {}
    for sub in submissions:
        chal = sub.get("challenge") or {}
        comp_id = chal.get("competitionId") or chal.get("id")
        if not comp_id:
            continue
        rec = seen.setdefault(comp_id, {
            "competitionId": comp_id,
            "name": chal.get("name") or chal.get("title") or comp_id,
            "submittedAt": sub.get("submittedAt") or 0,
            "count": 0,
            "lastScore": None,
        })
        rec["count"] += 1
        rec["submittedAt"] = max(rec["submittedAt"], sub.get("submittedAt") or 0)
        if sub.get("score") is not None:
            rec["lastScore"] = sub["score"]
    return sorted(seen.values(), key=lambda r: r["submittedAt"], reverse=True)[:10]


def render_html(hands: list[dict], submissions: list[dict],
                agent_id: str, competition_id: Optional[str]) -> str:
    """Render a single self-contained HTML page. No external assets beyond
    Tailwind/Alpine CDN scripts."""
    data_json = json.dumps({
        "hands": hands,
        "submissions": submissions,
        "agentId": agent_id,
        "competitionId": competition_id or "(latest)",
    }, default=str)
    safe = data_json.replace("</", "<\\/")
    title = html.escape(f"Arena PokerKit replay — agent {agent_id[:12]}")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
<script src="https://cdn.tailwindcss.com"></script>
<script defer src="https://unpkg.com/alpinejs@3.13.5/dist/cdn.min.js"></script>
<style>
  body {{ font-family: ui-sans-serif, system-ui, sans-serif; }}
  .card {{ display:inline-block; padding:2px 6px; margin:1px; border:1px solid #ccc;
          border-radius:4px; font-family: ui-monospace, monospace; }}
  .card.red {{ color:#dc2626; }}
  .pos {{ font-size:0.75rem; color:#6b7280; }}
  pre.reason {{ white-space:pre-wrap; word-break:break-word; font-size:0.75rem;
              background:#f3f4f6; padding:6px 8px; border-radius:4px; }}
</style>
</head>
<body class="bg-slate-50 text-slate-900">
<div x-data='replayApp()' x-init='init()' class="max-w-4xl mx-auto p-6">
  <header class="mb-6">
    <h1 class="text-2xl font-bold">Arena PokerKit — Replay</h1>
    <p class="text-sm text-slate-500" x-text="header"></p>
  </header>

  <template x-if="hands.length === 0 && submissions.length === 0">
    <div class="rounded border border-amber-200 bg-amber-50 p-4 text-amber-900">
      <p class="font-semibold">No hands yet.</p>
      <p class="text-sm">Run <code>pokerkit run --max-hands 5</code> first, then re-run <code>pokerkit replay --latest</code>.</p>
    </div>
  </template>

  <template x-if="hands.length > 0">
    <section class="mb-8">
      <h2 class="font-semibold mb-3">Settled hands (replayUrl available)</h2>
      <div class="space-y-3">
        <template x-for="(h, i) in hands" :key="h.handId">
          <div class="rounded border border-slate-200 bg-white p-4">
            <div class="flex justify-between items-baseline">
              <span class="font-mono text-sm" x-text="`#${{i+1}} · ${{h.handId.slice(0,12)}}…`"></span>
              <span class="text-sm" :class="(h.chipDelta||0)>=0?'text-emerald-600':'text-rose-600'">
                Δ <span x-text="h.chipDelta||0"></span>
              </span>
            </div>
            <div class="text-sm mt-1">
              <span class="text-slate-500">winner:</span>
              <span x-text="h.winnerHandle||'-'"></span>
              <span class="text-slate-500 ml-3">settled:</span>
              <span x-text="new Date(h.settledAt).toLocaleString()"></span>
            </div>
            <a class="text-xs text-blue-600 underline" :href="h.replayUrl" x-text="h.replayUrl" target="_blank" rel="noopener"></a>
          </div>
        </template>
      </div>
    </section>
  </template>

  <template x-if="submissions.length > 0">
    <section>
      <h2 class="font-semibold mb-3">Per-hand submissions
        <span class="text-xs font-normal text-slate-500" x-text="`(${{submissions.length}} entries)`"></span>
      </h2>
      <div class="space-y-3">
        <template x-for="(s, i) in submissions" :key="s.id">
          <div class="rounded border border-slate-200 bg-white p-4">
            <div class="flex justify-between items-baseline">
              <div>
                <span class="font-mono text-sm" x-text="`#${{i+1}} · ${{(s.id||'').slice(0,12)}}…`"></span>
                <span class="pos ml-2" x-text="s.status"></span>
              </div>
              <span class="text-sm" :class="(s.data?.payoutChips||0) >= (s.data?.totalCommittedChips||0)?'text-emerald-600':'text-rose-600'">
                payout <span x-text="s.data?.payoutChips ?? '-'"></span>
              </span>
            </div>
            <div class="mt-2">
              <span class="text-xs uppercase text-slate-500 mr-2">hole</span>
              <template x-for="c in (s.data?.holeCards || [])" :key="c">
                <span class="card" :class="isRed(c)?'red':''" x-text="c"></span>
              </template>
            </div>
            <div class="mt-1 text-sm">
              <span class="text-slate-500">seat</span>
              <span class="ml-1" x-text="s.data?.seatNumber ?? '-'"></span>
              <span class="text-slate-500 ml-3">stack</span>
              <span class="ml-1" x-text="s.data?.stackChips ?? '-'"></span>
              <span class="text-slate-500 ml-3">score</span>
              <span class="ml-1" x-text="s.score ?? '-'"></span>
            </div>
            <template x-if="s.data?.reasoning">
              <pre class="reason mt-2" x-text="s.data.reasoning"></pre>
            </template>
            <div class="mt-1 text-xs text-slate-400" x-text="new Date(s.submittedAt).toLocaleString()"></div>
          </div>
        </template>
      </div>
    </section>
  </template>
</div>

<script>
const PAYLOAD = {safe};
function replayApp() {{
  return {{
    hands: PAYLOAD.hands,
    submissions: PAYLOAD.submissions,
    header: "",
    init() {{
      this.header = `agent ${{PAYLOAD.agentId}} · competition ${{PAYLOAD.competitionId}} · ${{this.hands.length}} settled hands, ${{this.submissions.length}} submissions`;
    }},
    isRed(c) {{ return c && (c.endsWith('h') || c.endsWith('d') || c.endsWith('H') || c.endsWith('D')); }}
  }};
}}
</script>
</body>
</html>
"""


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a self-contained HTML replay viewer for past matches.")
    parser.add_argument("--match", default=None,
                        help="competitionId to render (defaults to --latest)")
    parser.add_argument("--latest", action="store_true",
                        help="Render the most-recent competition")
    parser.add_argument("--list", action="store_true",
                        help="List your last 10 competitions and exit")
    parser.add_argument("--limit", type=int, default=50,
                        help="Max submissions to fetch (default 50)")
    parser.add_argument("--out", default="replay.html",
                        help="Output HTML file (default replay.html)")
    args = parser.parse_args(argv)

    load_dotenv()
    api_key, agent_id = _load_creds()
    if not api_key:
        print("ERROR: no API key found. Run `pokerkit run --max-hands 1` "
              "first to register, or set ARENA_API_KEY.", file=sys.stderr)
        return 2

    base = os.environ.get("ARENA_API_BASE", DEFAULT_BASE)
    client = ArenaClient(base, api_key=api_key)
    try:
        # If no agent_id cached, pull it from /agent/me.
        if not agent_id:
            try:
                me = client.get("/agent/me")
                if isinstance(me, dict):
                    agent_id = me.get("id") or me.get("agentId")
            except ArenaError as e:
                print(f"[arena-pokerkit] /agent/me failed: {e}", file=sys.stderr)
                return 2
        if not agent_id:
            print("ERROR: could not resolve agentId.", file=sys.stderr)
            return 2

        # --list mode
        if args.list:
            subs = fetch_submissions(client, agent_id, limit=100)
            comps = list_recent_competitions(subs)
            if not comps:
                print("no recent competitions found. Run `pokerkit run` "
                      "first.")
                return 0
            print(f"Last {len(comps)} competitions for agent {agent_id}:")
            for c in comps:
                print(f"  {c['competitionId']:32}  {c['name']:30}  "
                      f"submissions={c['count']:3}  "
                      f"lastScore={c['lastScore']}")
            return 0

        # Resolve competition_id
        comp_id = args.match
        if not comp_id and (args.latest or True):
            # Default behavior — latest competition
            subs = fetch_submissions(client, agent_id, limit=100)
            comps = list_recent_competitions(subs)
            if comps:
                comp_id = comps[0]["competitionId"]

        # Fetch data
        if comp_id:
            hands = fetch_replays(client, agent_id,
                                  competition_id=comp_id, limit=args.limit)
            submissions = fetch_submissions(client, agent_id,
                                            competition_id=comp_id,
                                            limit=args.limit)
        else:
            hands = []
            submissions = fetch_submissions(client, agent_id, limit=args.limit)

        if not hands and not submissions:
            print(f"no matches yet, run `pokerkit run` first "
                  f"(agent={agent_id}, competition={comp_id or '?'})")
            # Still write the empty viewer so the file path is consistent
            out_path = Path(args.out)
            out_path.write_text(render_html([], [], agent_id, comp_id))
            print(f"wrote empty replay viewer → {out_path}")
            return 0

        out_path = Path(args.out)
        out_path.write_text(render_html(hands, submissions, agent_id, comp_id))
        print(f"wrote {len(hands)} settled hands + {len(submissions)} "
              f"submissions → {out_path.absolute()}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
