#!/usr/bin/env python
"""System 7 — report generator over the advanced-stats DB (s7_stats.db).

    uv run s7_report.py            # full text report to stdout
    uv run s7_report.py --md out.md

Covers: overview, preflop ranges by position (VPIP/PFR + which hands), preflop
hand-class table, postflop profile by street/strength, opponents/HUD, M3 usage,
and leak notes for tuning.
"""
import os
import sqlite3
import sys
import time

DB = os.environ.get("S7_STATS_DB",
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "s7_stats.db"))
_POS_ORDER = ["UTG", "MP", "CO", "BTN", "SB", "BB", "?"]


def _c():
    return sqlite3.connect(DB)


def _q(c, sql, args=()):
    return c.execute(sql, args).fetchall()


def _pct(n, d):
    return (100.0 * n / d) if d else 0.0


def report():
    if not os.path.exists(DB):
        return "no hay datos todavía (s7_stats.db no existe)."
    c = _c()
    out = []
    P = out.append

    total_dec = _q(c, "select count(*) from decisions")[0][0]
    if not total_dec:
        return "s7_stats.db sin decisiones aún."
    hands = _q(c, "select count(distinct hand_key) from decisions")[0][0]
    pf = _q(c, "select count(*) from decisions where street='preflop'")[0][0]
    t0, t1 = _q(c, "select min(ts), max(ts) from decisions")[0]
    eng = dict(_q(c, "select engine, count(*) from decisions group by engine"))
    m3 = eng.get("M3", 0)
    span_h = (t1 - t0) / 3600.0 if t0 and t1 else 0

    P("=" * 64)
    P("  SYSTEM 7 (system7-llm) — REPORT PvP Playground")
    P("=" * 64)
    P(f"Manos jugadas        : {hands}")
    P(f"Decisiones           : {total_dec}  (preflop {pf}, postflop {total_dec-pf})")
    P(f"Periodo              : {span_h:.1f} h  ({hands/span_h:.0f} manos/h)" if span_h > 0 else f"Periodo: n/a")
    P(f"Motor                : heurístico {eng.get('heur',0)} | MiniMax M3 {m3} "
      f"({_pct(m3,total_dec):.1f}% de decisiones)")

    # bankroll / bb-100 (best-effort)
    br = _q(c, "select table_chips, hands, rebuys, ts from bankroll where table_chips is not null order by ts")
    if br:
        rebuys = br[-1][2] or 0
        last_stack = br[-1][0] or 0
        net = last_stack - 1000 * (1 + rebuys)
        bb100 = (net / 2) / (hands / 100.0) if hands else 0
        P(f"Bankroll (aprox)     : stack {last_stack}, rebuys {rebuys}, neto ~{net:+d} fichas "
          f"→ ~{bb100:+.1f} bb/100 (aprox, play-money)")

    # ── Preflop ranges by position ──────────────────────────────────────────
    P("\n" + "-" * 64)
    P("  PREFLOP — RANGOS POR POSICIÓN  (qué manos juego)")
    P("-" * 64)
    P(f"{'pos':<5}{'manos':>7}{'VPIP%':>8}{'PFR%':>8}   abre/3bet (top)")
    rows = _q(c, """select pos,
                    count(distinct hand_key),
                    sum(voluntary), sum(preflop_raise), count(*)
                  from decisions where street='preflop' group by pos""")
    rmap = {r[0]: r for r in rows}
    for pos in _POS_ORDER:
        if pos not in rmap:
            continue
        _, dh, vol, pfr, n = rmap[pos]
        raised = _q(c, """select hand_class, count(*) from decisions
                          where street='preflop' and pos=? and preflop_raise=1
                          and hand_class!='' group by hand_class order by 2 desc limit 12""", (pos,))
        rr = " ".join(f"{h}" for h, _ in raised)
        P(f"{pos:<5}{dh:>7}{_pct(vol,n):>8.1f}{_pct(pfr,n):>8.1f}   {rr[:60]}")

    # ── Preflop hand-class table ────────────────────────────────────────────
    P("\n" + "-" * 64)
    P("  PREFLOP — POR CLASE DE MANO (acción dominante)")
    P("-" * 64)
    P(f"{'mano':<6}{'veces':>7}{'%raise':>8}{'%call':>8}{'%fold':>8}")
    hc = _q(c, """select hand_class,
                  count(*),
                  sum(case when action in ('bet','raise') then 1 else 0 end),
                  sum(case when action='call' then 1 else 0 end),
                  sum(case when action='fold' then 1 else 0 end)
                from decisions where street='preflop' and hand_class!='' group by hand_class
                order by 2 desc limit 30""")
    for h, n, r, ca, f in hc:
        P(f"{h:<6}{n:>7}{_pct(r,n):>8.0f}{_pct(ca,n):>8.0f}{_pct(f,n):>8.0f}")

    # ── Postflop profile ────────────────────────────────────────────────────
    P("\n" + "-" * 64)
    P("  POSTFLOP — acción por calle y por fuerza")
    P("-" * 64)
    for st in ("flop", "turn", "river"):
        acts = dict(_q(c, "select action, count(*) from decisions where street=? group by action", (st,)))
        tot = sum(acts.values())
        if not tot:
            continue
        agg = acts.get("bet", 0) + acts.get("raise", 0) + acts.get("all-in", 0)
        pas = acts.get("call", 0)
        af = (agg / pas) if pas else float("inf")
        P(f"{st:<6} n={tot:<5} fold {_pct(acts.get('fold',0),tot):4.0f}% "
          f"check {_pct(acts.get('check',0),tot):4.0f}% call {_pct(acts.get('call',0),tot):4.0f}% "
          f"bet/raise {_pct(agg,tot):4.0f}%  AF~{af:.2f}")
    P("\n  fuerza postflop → acción:")
    sr = _q(c, """select strength,
                  count(*),
                  sum(case when action in ('bet','raise','all-in') then 1 else 0 end),
                  sum(case when action='call' then 1 else 0 end),
                  sum(case when action in ('check','fold') then 1 else 0 end)
                from decisions where street!='preflop' and strength!='' group by strength order by 2 desc""")
    for s, n, agg, ca, pasv in sr:
        P(f"   {s:<5} n={n:<5} agg {_pct(agg,n):4.0f}%  call {_pct(ca,n):4.0f}%  check/fold {_pct(pasv,n):4.0f}%")

    # ── Opponents / HUD / M3 ────────────────────────────────────────────────
    P("\n" + "-" * 64)
    P("  RIVALES (arquetipo HUD visto) y USO DE M3")
    P("-" * 64)
    arc = _q(c, "select archetype, count(*) from decisions group by archetype order by 2 desc")
    P("  arquetipos enfrentados: " + ", ".join(f"{a}:{n}" for a, n in arc))
    m3st = _q(c, "select street, count(*) from decisions where engine='M3' group by street order by 2 desc")
    P("  M3 llamado por calle  : " + (", ".join(f"{s}:{n}" for s, n in m3st) or "ninguno aún"))
    m3sr = _q(c, "select strength, count(*) from decisions where engine='M3' and strength!='' group by strength order by 2 desc")
    P("  M3 por fuerza         : " + (", ".join(f"{s}:{n}" for s, n in m3sr) or "—"))

    # ── Notes for tuning ────────────────────────────────────────────────────
    P("\n" + "-" * 64)
    P("  NOTAS PARA MEJORAR (heurísticas)")
    P("-" * 64)
    notes = []
    # overall VPIP/PFR
    vol = _q(c, "select sum(voluntary), sum(preflop_raise), count(*) from decisions where street='preflop'")[0]
    if vol[2]:
        v, p, n = vol
        notes.append(f"VPIP global ~{_pct(v,n):.0f}% / PFR ~{_pct(p,n):.0f}% (gap {_pct(v,n)-_pct(p,n):.0f}). "
                     f"Referencia 6-max reg: ~22/18.")
    ff = _q(c, "select sum(case when action='fold' then 1 else 0 end), count(*) from decisions where street='flop' and call_chips>0")[0]
    if ff[1]:
        notes.append(f"Fold-to-bet en flop ~{_pct(ff[0],ff[1]):.0f}% (n={ff[1]}). >55% = explotable (over-fold).")
    if m3 == 0 and total_dec > 50:
        notes.append("M3 no se ha llamado aún: o no hubo spots difíciles, o el deadline cae bajo el gate (S7_LLM_MIN_DEADLINE).")
    if not notes:
        notes.append("Aún pocos datos para conclusiones; deja correr más manos.")
    for nz in notes:
        P("  • " + nz)
    P("=" * 64)
    c.close()
    return "\n".join(out)


def main(argv):
    txt = report()
    if "--md" in argv:
        path = argv[argv.index("--md") + 1]
        with open(path, "w") as f:
            f.write(txt + "\n")
        print("escrito", path)
    else:
        print(txt)


if __name__ == "__main__":
    main(sys.argv[1:])
