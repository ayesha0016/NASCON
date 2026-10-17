"""Standings computed from scratch from event logs (Section 5.10)."""
from app.scoring import project
from app.sign import lots_key
from app.util import E

def _match_rows(state, lg):
    sport = state["sports"][lg["sport_id"]]
    per = sport["periods"]
    rows, remaining = [], {t: 0 for t in lg["teams"]}
    for mid in lg["fixtures"]:
        m = state["matches"][mid]
        pr = project(state, m)
        if pr["status"] != "completed" or pr["outcome"] == "undecided":
            remaining[m["home"]] += 1; remaining[m["away"]] += 1
            continue
        def overs(s):
            if sport["scoring_model"] != "innings": return 0.0
            if s["wickets"] >= per["wickets_per_innings"] * per["innings_per_team"]:
                return float(per["overs_per_innings"] * per["innings_per_team"])
            return s["balls"] / per["balls_per_over"]
        h, a = pr["side"][m["home"]], pr["side"][m["away"]]
        rows.append({"home": m["home"], "away": m["away"], "winner": pr["winner"],
                     "hs": h["score"], "as": a["score"], "hfp": h["fair_play"], "afp": a["fair_play"],
                     "ho": overs(h), "ao": overs(a)})
    return rows, remaining, sport

def _agg(rows, teams, pts, only=None):
    st = {t: {"played": 0, "won": 0, "drawn": 0, "lost": 0, "scored": 0, "conceded": 0,
              "points": 0, "fair_play": 0, "of": 0.0, "ob": 0.0} for t in teams}
    for r in rows:
        if only is not None and not (r["home"] in only and r["away"] in only): continue
        for me, op, ms, os_, mfp, mo, oo in ((r["home"], r["away"], r["hs"], r["as"], r["hfp"], r["ho"], r["ao"]),
                                              (r["away"], r["home"], r["as"], r["hs"], r["afp"], r["ao"], r["ho"])):
            if me not in st: continue
            s = st[me]
            s["played"] += 1; s["scored"] += ms; s["conceded"] += os_; s["fair_play"] += mfp
            s["of"] += mo; s["ob"] += oo
            if r["winner"] is None: s["drawn"] += 1; s["points"] += pts["draw"]
            elif r["winner"] == me: s["won"] += 1; s["points"] += pts["win"]
            else: s["lost"] += 1; s["points"] += pts["loss"]
    return st

def _diff(s, model):
    if model == "innings":
        f = s["scored"] / s["of"] if s["of"] else 0.0
        a = s["conceded"] / s["ob"] if s["ob"] else 0.0
        return round(f - a, 3)
    return s["scored"] - s["conceded"]

def compute(state, lock, league_id):
    with lock:
        lg = state["leagues"].get(league_id)
        if not lg: return E(404, "LEAGUE_NOT_FOUND", "Unknown league", {"league_id": league_id})
        rows, remaining, sport = _match_rows(state, lg)
        model, pts, teams = sport["scoring_model"], lg["points"], lg["teams"]
        overall = _agg(rows, teams, pts)
        order = [c for c in lg["tie_break_order"] if c != "draw_lots"] + ["draw_lots"]

        def crit(c, group):
            if c in ("h2h_points", "h2h_diff"):
                sub = _agg(rows, group, pts, only=set(group))
                return {t: (sub[t]["points"] if c == "h2h_points" else _diff(sub[t], model)) for t in group}
            if c == "overall_diff": return {t: _diff(overall[t], model) for t in group}
            if c == "scored": return {t: overall[t]["scored"] for t in group}
            if c == "fair_play": return {t: overall[t]["fair_play"] for t in group}
            # draw_lots: lower hash is better -> negate ordering via rank
            keys = sorted(group, key=lambda t: lots_key(lg["lots_seed"], league_id, t))
            return {t: -i for i, t in enumerate(keys)}

        sep = {}
        def resolve(group):
            if len(group) == 1: return group
            for c in order:
                vals = crit(c, group)
                if len(set(vals.values())) == 1: continue
                levels = sorted(set(vals.values()), reverse=True)
                out = []
                for i, v in enumerate(levels):
                    sub = resolve([t for t in group if vals[t] == v])
                    if i < len(levels) - 1: sep[sub[-1]] = c
                    out += sub
                return out
            return group

        by_pts = {}
        for t in teams: by_pts.setdefault(overall[t]["points"], []).append(t)
        ranked = []
        for p in sorted(by_pts, reverse=True):
            ranked += resolve(by_pts[p])

        W, k = pts["win"], lg["spots"]
        mx = {t: overall[t]["points"] + W * remaining[t] for t in teams}
        table = []
        for i, t in enumerate(ranked, 1):
            s, others = overall[t], [x for x in teams if x != t]
            if sum(1 for x in others if mx[x] >= s["points"]) < k: status = "clinched"
            elif sum(1 for x in others if overall[x]["points"] > mx[t]) >= k: status = "eliminated"
            else: status = "still_possible"
            table.append({"rank": i, "team_id": t, "played": s["played"], "won": s["won"], "drawn": s["drawn"],
                          "lost": s["lost"], "scored": s["scored"], "conceded": s["conceded"],
                          "diff": _diff(s, model), "points": s["points"], "fair_play": s["fair_play"],
                          "status": status, "resolved_by": sep.get(t)})
        h2h = {}
        for a in teams:
            h2h[a] = {}
            for b in teams:
                if a == b: continue
                sub = _agg(rows, [a, b], pts, only={a, b})[a]
                h2h[a][b] = {"played": sub["played"], "points": sub["points"], "scored": sub["scored"], "conceded": sub["conceded"]}
        return 200, {"league_id": league_id, "sport_id": lg["sport_id"], "qualification_spots": k, "table": table, "h2h": h2h}
