"""Sports config, event-sourced scoring engine. No sport/event names are hard-coded."""
import re, copy
from app.util import E, VE, is_id, is_int, same
from app.sign import sign

TB_ALL = ["h2h_points", "h2h_diff", "overall_diff", "scored", "fair_play", "draw_lots"]
SPORT_RE = re.compile(r"^[a-z0-9_-]{2,40}$")
EVT_RE = re.compile(r"^[a-z0-9_]{1,40}$")
OT_POLICIES = {"none", "extra_time", "shootout", "golden_score", "super_over"}

def _pos_int(v, mn=1):
    return is_int(v) and v >= mn

# ---------------- sports ----------------
def validate_sport(b):
    bad = []
    if not isinstance(b.get("sport_id"), str) or not SPORT_RE.match(b["sport_id"]): bad.append("sport_id")
    nm = b.get("name")
    if not isinstance(nm, str) or not (1 <= len(nm) <= 80): bad.append("name")
    model = b.get("scoring_model")
    if model not in ("event", "innings", "round"): bad.append("scoring_model")
    p = b.get("periods")
    if not isinstance(p, dict): bad.append("periods")
    elif model == "event" and not (_pos_int(p.get("count")) and _pos_int(p.get("duration_minutes"))): bad.append("periods")
    elif model == "innings" and not all(_pos_int(p.get(k)) for k in ("innings_per_team", "overs_per_innings", "balls_per_over", "wickets_per_innings")): bad.append("periods")
    elif model == "round" and not (_pos_int(p.get("best_of")) and p["best_of"] % 2 == 1): bad.append("periods")
    se = b.get("scoring_events")
    if not isinstance(se, dict) or not se: bad.append("scoring_events")
    else:
        for k, v in se.items():
            ok = isinstance(k, str) and EVT_RE.match(k) and isinstance(v, dict)
            if ok and model == "event": ok = _pos_int(v.get("points"))
            elif ok and model == "innings": ok = (is_int(v.get("runs")) and v["runs"] >= 0 and isinstance(v.get("legal_ball"), bool) and isinstance(v.get("wicket"), bool))
            elif ok and model == "round": ok = _pos_int(v.get("rounds"))
            if not ok: bad.append("scoring_events"); break
    if not isinstance(b.get("draw_allowed"), bool): bad.append("draw_allowed")
    ot = b.get("overtime")
    if not isinstance(ot, dict) or ot.get("policy") not in OT_POLICIES or ("duration_minutes" in ot and not (is_int(ot["duration_minutes"]) and ot["duration_minutes"] >= 0)):
        bad.append("overtime")
    tb = b.get("tie_break_order")
    if tb is not None and (not isinstance(tb, list) or len(set(map(str, tb))) != len(tb) or any(t not in TB_ALL for t in tb)):
        bad.append("tie_break_order")
    r = b.get("roster")
    if (not isinstance(r, dict) or not _pos_int(r.get("min_squad_size")) or not _pos_int(r.get("max_squad_size"))
            or r["min_squad_size"] > r["max_squad_size"] or not isinstance(r.get("required_positions"), dict)
            or any(not (isinstance(k, str) and is_int(v) and v >= 0) for k, v in r["required_positions"].items())):
        bad.append("roster")
    return bad

def create_sport(state, lock, b):
    if not isinstance(b, dict): return VE("body")
    bad = validate_sport(b)
    if bad: return VE(*bad)
    with lock:
        if b["sport_id"] in state["sports"]:
            return E(409, "SPORT_EXISTS", "Sport already exists", {"sport_id": b["sport_id"]})
        cfg = copy.deepcopy(b)
        if "tie_break_order" not in cfg: cfg["tie_break_order"] = list(TB_ALL)
        state["sports"][b["sport_id"]] = cfg
        return 201, {"sport_id": b["sport_id"], "status": "created", "config": cfg}

# ---------------- projection ----------------
def project(state, m):
    sport = state["sports"][m["sport_id"]]
    model = sport["scoring_model"]
    se = sport["scoring_events"]
    comp = {}  # target_id -> compensation event_id
    for ev in m["events"]:
        if ev["type"] == "compensation":
            comp[ev["payload"]["compensates"]] = ev["event_id"]
    side = {t: {"score": 0, "wickets": 0, "balls": 0, "fair_play": 0} for t in (m["home"], m["away"])}
    status, tb = "upcoming", None
    for ev in m["events"]:
        t, p = ev["type"], ev["payload"]
        if t == "match_start": status = "in_progress"
        elif t == "match_end": status = "completed"
        if ev["event_id"] in comp or t == "compensation": continue
        if t == "score":
            cfg = se[p["event_type"]]; s = side[p["team_id"]]
            if model == "event": s["score"] += cfg["points"]
            elif model == "round": s["score"] += cfg["rounds"]
            else:
                s["score"] += cfg["runs"]
                if cfg["legal_ball"]: s["balls"] += 1
                if cfg["wicket"]: s["wickets"] += 1
        elif t == "discipline": side[p["team_id"]]["fair_play"] += p["points"]
        elif t == "tiebreak_winner": tb = p["team_id"]
    h, a = side[m["home"]], side[m["away"]]
    winner = None
    if h["score"] > a["score"]: winner = m["home"]
    elif a["score"] > h["score"]: winner = m["away"]
    elif tb: winner = tb
    if status != "completed": outcome = "pending"
    elif winner == m["home"]: outcome = "home_win"
    elif winner == m["away"]: outcome = "away_win"
    elif sport["draw_allowed"]: outcome = "draw"
    else: outcome = "undecided"
    return {"status": status, "side": side, "tb": tb, "winner": winner, "outcome": outcome, "comp": comp, "model": model, "sport": sport}

def score_view(state, lock, match_id):
    with lock:
        m = state["matches"].get(match_id)
        if not m: return E(404, "MATCH_NOT_FOUND", "Unknown match", {"match_id": match_id})
        pr = project(state, m)
        bpo = pr["sport"]["periods"].get("balls_per_over")
        def team(tid):
            s = pr["side"][tid]
            inn = pr["model"] == "innings"
            return {"team_id": tid, "score": s["score"],
                    "wickets": s["wickets"] if inn else None, "balls": s["balls"] if inn else None,
                    "overs": f"{s['balls'] // bpo}.{s['balls'] % bpo}" if inn else None,
                    "fair_play": s["fair_play"]}
        evs = [{"sequence": e["sequence"], "event_id": e["event_id"], "type": e["type"], "payload": e["payload"],
                "signature": e["signature"], "compensated": e["event_id"] in pr["comp"],
                "compensated_by": pr["comp"].get(e["event_id"])} for e in m["events"]]
        return 200, {"match_id": match_id, "sport_id": m["sport_id"], "league_id": m.get("league_id"),
                     "status": pr["status"], "outcome": pr["outcome"], "winner_team_id": pr["winner"] if pr["status"] == "completed" or pr["winner"] else pr["winner"],
                     "tiebreak_winner_team_id": pr["tb"], "home": team(m["home"]), "away": team(m["away"]),
                     "version": len(m["events"]), "events": evs}

# ---------------- events ----------------
PAYLOAD_SCHEMA = {
    "match_init": None, "match_start": {}, "match_end": {},
    "score": {"team_id": str, "event_type": str}, "discipline": {"team_id": str, "points": int},
    "tiebreak_winner": {"team_id": str}, "compensation": {"compensates": str, "reason": str},
}

def apply_event(state, b):
    """Caller must hold the lock. Returns (status, body)."""
    if not isinstance(b, dict): return VE("body")
    bad = []
    eid, mid, typ, p = b.get("event_id"), b.get("match_id"), b.get("type"), b.get("payload")
    if not is_id(eid): bad.append("event_id")
    if not is_id(mid): bad.append("match_id")
    if typ not in PAYLOAD_SCHEMA: bad.append("type")
    if not isinstance(p, dict): bad.append("payload")
    elif typ == "match_init":
        for k in ("sport_id", "home_team_id", "away_team_id"):
            if not is_id(p.get(k)): bad.append("payload." + k)
        if "league_id" in p and p["league_id"] is not None and not is_id(p["league_id"]): bad.append("payload.league_id")
        if not bad and p["home_team_id"] == p["away_team_id"]: bad.append("payload.away_team_id")
    elif typ in PAYLOAD_SCHEMA:
        for k, tp in PAYLOAD_SCHEMA[typ].items():
            v = p.get(k)
            if tp is int and not is_int(v): bad.append("payload." + k)
            if tp is str and (not isinstance(v, str) or not v): bad.append("payload." + k)
    if bad: return VE(*bad)

    prev = state["events"].get(eid)
    if prev:
        if same(prev["body"], b): return 200, prev["resp"]
        return E(409, "DUPLICATE_EVENT_ID", "event_id reused with different body", {"event_id": eid})

    if typ == "match_init":
        if p["sport_id"] not in state["sports"]:
            return E(404, "SPORT_NOT_FOUND", "Unknown sport", {"sport_id": p["sport_id"]})
        if mid in state["matches"]:
            return E(409, "MATCH_EXISTS", "Match already exists", {"match_id": mid})
        m = {"match_id": mid, "sport_id": p["sport_id"], "league_id": p.get("league_id"),
             "home": p["home_team_id"], "away": p["away_team_id"], "events": []}
        state["matches"][mid] = m
    else:
        m = state["matches"].get(mid)
        if not m: return E(404, "MATCH_NOT_FOUND", "Unknown match", {"match_id": mid})
        pr = project(state, m)
        st = pr["status"]
        if typ in ("score", "discipline", "tiebreak_winner") and p["team_id"] not in (m["home"], m["away"]):
            return VE("payload.team_id")
        if typ == "score" and p["event_type"] not in pr["sport"]["scoring_events"]:
            return E(400, "UNKNOWN_EVENT_TYPE", "Event type not defined for sport", {"event_type": p["event_type"]})
        if typ == "compensation":
            tgt_id = p["compensates"]
            tgt = state["events"].get(tgt_id)
            if not tgt: return E(404, "EVENT_NOT_FOUND", "Unknown event", {"event_id": tgt_id})
            if tgt["match_id"] != mid or tgt["type"] not in ("score", "discipline", "tiebreak_winner") or tgt_id in pr["comp"]:
                return E(409, "INVALID_COMPENSATION", "Cannot compensate this event", {"event_id": tgt_id})
        elif typ == "match_start":
            if st != "upcoming": return E(409, "ILLEGAL_TRANSITION", f"Cannot start from {st}", {"from": st, "to": "in_progress"})
        elif typ == "match_end":
            if st != "in_progress": return E(409, "ILLEGAL_TRANSITION", f"Cannot end from {st}", {"from": st, "to": "completed"})
            h, a = pr["side"][m["home"]], pr["side"][m["away"]]
            if h["score"] == a["score"] and not pr["sport"]["draw_allowed"] and not pr["tb"]:
                return E(409, "TIE_NOT_ALLOWED", "Level score and draws not allowed", {"match_id": mid})
        else:
            if st != "in_progress":
                return E(409, "MATCH_NOT_IN_PROGRESS", "Match is not in progress", {"match_id": mid, "status": st})
            if typ == "score":
                sp = pr["sport"]; per = sp["periods"]; s = pr["side"][p["team_id"]]
                if pr["model"] == "innings":
                    if (s["balls"] >= per["innings_per_team"] * per["overs_per_innings"] * per["balls_per_over"]
                            or s["wickets"] >= per["innings_per_team"] * per["wickets_per_innings"]):
                        return E(409, "INNINGS_COMPLETE", "Innings complete", {"team_id": p["team_id"]})
                elif pr["model"] == "round":
                    need = (per["best_of"] + 1) // 2
                    if any(x["score"] >= need for x in pr["side"].values()):
                        return E(409, "SERIES_ALREADY_DECIDED", "Series already decided", {"match_id": mid})

    seq = len(m["events"]) + 1
    sig = sign(eid, p)
    m["events"].append({"sequence": seq, "event_id": eid, "type": typ, "payload": p, "signature": sig})
    resp = {"event_id": eid, "match_id": mid, "type": typ, "sequence": seq, "match_version": seq, "signature": sig}
    state["events"][eid] = {"body": b, "resp": resp, "match_id": mid, "type": typ}
    return 201, resp

def post_event(state, lock, b):
    with lock:
        return apply_event(state, b)

# ---------------- leagues ----------------
def create_league(state, lock, b):
    if not isinstance(b, dict): return VE("body")
    bad = []
    lid, sid, teams, fx = b.get("league_id"), b.get("sport_id"), b.get("teams"), b.get("fixtures")
    if not is_id(lid): bad.append("league_id")
    if not isinstance(sid, str): bad.append("sport_id")
    if not isinstance(teams, list) or not (2 <= len(teams) <= 40) or not all(is_id(t) for t in teams) or len(set(teams)) != len(teams):
        bad.append("teams")
    if not isinstance(fx, list) or any(not isinstance(f, dict) or not is_id(f.get("match_id")) or not is_id(f.get("home")) or not is_id(f.get("away")) for f in fx):
        bad.append("fixtures")
    elif "teams" not in bad:
        if any(f["home"] == f["away"] or f["home"] not in teams or f["away"] not in teams for f in fx): bad.append("fixtures")
        if len({f["match_id"] for f in fx}) != len(fx): bad.append("fixtures")
    pts = b.get("points") or {}
    if not isinstance(pts, dict) or any(k in pts and not is_int(pts[k]) for k in ("win", "draw", "loss")): bad.append("points")
    tb = b.get("tie_break_order")
    if tb is not None and (not isinstance(tb, list) or len(set(map(str, tb))) != len(tb) or any(t not in TB_ALL for t in tb)): bad.append("tie_break_order")
    q = b.get("qualification")
    if not isinstance(q, dict) or not is_int(q.get("spots")) or (isinstance(teams, list) and not (1 <= q["spots"] <= len(teams))): bad.append("qualification")
    if "lots_seed" in b and b["lots_seed"] is not None and not is_int(b["lots_seed"]): bad.append("lots_seed")
    if bad: return VE(*dict.fromkeys(bad))
    with lock:
        if sid not in state["sports"]: return E(404, "SPORT_NOT_FOUND", "Unknown sport", {"sport_id": sid})
        if lid in state["leagues"]: return E(409, "LEAGUE_EXISTS", "League exists", {"league_id": lid})
        for f in fx:
            if f["match_id"] in state["matches"]: return E(409, "MATCH_EXISTS", "Match exists", {"match_id": f["match_id"]})
        sport = state["sports"][sid]
        state["leagues"][lid] = {
            "league_id": lid, "sport_id": sid, "teams": list(teams), "fixtures": [f["match_id"] for f in fx],
            "points": {"win": pts.get("win", 3), "draw": pts.get("draw", 1), "loss": pts.get("loss", 0)},
            "tie_break_order": list(tb) if tb is not None else list(sport.get("tie_break_order") or TB_ALL),
            "spots": q["spots"], "lots_seed": b.get("lots_seed") if b.get("lots_seed") is not None else state["seed"]}
        for f in fx:
            state["matches"][f["match_id"]] = {"match_id": f["match_id"], "sport_id": sid, "league_id": lid,
                                               "home": f["home"], "away": f["away"], "events": []}
        return 201, {"league_id": lid, "status": "created", "team_count": len(teams), "fixture_count": len(fx)}
