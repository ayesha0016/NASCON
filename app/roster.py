"""Roster scenarios, transfers/loans/releases with ordered rule checks, immutable audit trail (Section 5.11)."""
import re, copy
from datetime import date, timedelta, datetime, timezone
from app.util import E, VE, is_id, is_int, now_iso, same

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ROLES = {"captain", "vice_captain", "starter", "substitute"}

def pdate(v):
    if not isinstance(v, str) or not DATE_RE.match(v): return None
    try: return date.fromisoformat(v)
    except ValueError: return None

def today():
    return datetime.now(timezone.utc).date()

def setup(state, lock, b):
    if not isinstance(b, dict): return VE("body")
    lg, squads, players = b.get("league"), b.get("squads"), b.get("players")
    mip = b.get("matches_in_progress", []) or []
    bad = []
    if not isinstance(lg, dict) or not is_id(lg.get("league_id")) or not is_id(lg.get("tenant_id")) or not isinstance(lg.get("sport_id"), str):
        bad.append("league")
    else:
        tw = lg.get("transfer_window")
        if not isinstance(tw, dict) or not pdate(tw.get("open")) or not pdate(tw.get("close")) or pdate(tw["open"]) > pdate(tw["close"]):
            bad.append("league.transfer_window")
        if "max_squad_size" in lg and lg["max_squad_size"] is not None and not (is_int(lg["max_squad_size"]) and lg["max_squad_size"] >= 1):
            bad.append("league.max_squad_size")
        rp = lg.get("required_positions")
        if rp is not None and (not isinstance(rp, dict) or any(not is_int(v) or v < 0 for v in rp.values())):
            bad.append("league.required_positions")
    if not isinstance(squads, list) or any(not isinstance(s, dict) or not is_id(s.get("squad_id")) or not is_id(s.get("club_id")) or not is_id(s.get("tenant_id")) for s in squads):
        bad.append("squads")
    elif len({s["squad_id"] for s in squads}) != len(squads):
        bad.append("squads")
    if not isinstance(players, list) or any(not isinstance(p, dict) or not is_id(p.get("player_id")) or not isinstance(p.get("position"), str) for p in players):
        bad.append("players")
    elif "squads" not in bad:
        ids = {s["squad_id"] for s in squads}
        if any(p.get("squad_id") not in ids or p.get("role", "substitute") not in ROLES for p in players) or len({p["player_id"] for p in players}) != len(players):
            bad.append("players")
    if not isinstance(mip, list) or any(not isinstance(m, dict) or not is_id(m.get("match_id")) for m in mip):
        bad.append("matches_in_progress")
    if bad: return VE(*bad)
    with lock:
        sport = state["sports"].get(lg["sport_id"])
        if not sport: return E(404, "SPORT_NOT_FOUND", "Unknown sport", {"sport_id": lg["sport_id"]})
        lid = lg["league_id"]
        old = state["rosters"].get(lid)
        if old:
            for pid in old["players"]:
                state["player_league"].pop(pid, None)
        R = {"league_id": lid, "tenant_id": lg["tenant_id"], "sport_id": lg["sport_id"],
             "open": pdate(lg["transfer_window"]["open"]), "close": pdate(lg["transfer_window"]["close"]),
             "max": lg.get("max_squad_size") or sport["roster"]["max_squad_size"],
             "req": lg["required_positions"] if lg.get("required_positions") is not None else dict(sport["roster"]["required_positions"]),
             "squads": {s["squad_id"]: dict(s) for s in squads}, "players": {}, "mip": [dict(m) for m in mip], "requests": {}}
        for p in players:
            R["players"][p["player_id"]] = {"player_id": p["player_id"], "name": p.get("name"), "position": p["position"],
                                            "squad_id": p["squad_id"], "parent_squad_id": None, "role": p.get("role", "substitute"),
                                            "registration_type": "initial", "start_date": None, "loan_end_date": None,
                                            "audit": []}
            state["player_league"][p["player_id"]] = lid
        state["rosters"][lid] = R
        counts = {s["squad_id"]: 0 for s in squads}
        for p in players: counts[p["squad_id"]] += 1
        return 201, {"league_id": lid, "status": "configured", "squad_counts": counts}

def _audit(state, P, R, action, frm, to, eff, st, who, why):
    state["audit_seq"] += 1
    e = {"audit_id": f"au-{state['audit_seq']:04d}", "action": action, "player_id": P["player_id"], "league_id": R["league_id"],
         "from_squad_id": frm, "to_squad_id": to, "effective_date": eff, "state": st, "who": who, "when": now_iso(), "why": why}
    P["audit"].append(e)
    return e

def materialize(state, R, P, as_of):
    """Automatic loan return: the day after loan_end_date the player is back with the lender (written once)."""
    if P["registration_type"] == "loan" and P["loan_end_date"] and as_of > pdate(P["loan_end_date"]):
        ret = (pdate(P["loan_end_date"]) + timedelta(days=1)).isoformat()
        frm, to = P["squad_id"], P["parent_squad_id"]
        P.update(squad_id=to, parent_squad_id=None, role="substitute", registration_type="loan_return",
                 start_date=ret, loan_end_date=None)
        _audit(state, P, R, "loan_return", frm, to, ret, "active", "system", "Loan ended")

def _count(R, squad):
    return sum(1 for p in R["players"].values() if p["squad_id"] == squad)

def do_transfer(state, b):
    """Caller holds lock."""
    if not isinstance(b, dict): return VE("body")
    bad = []
    typ = b.get("type")
    for k in ("request_id", "league_id", "player_id"):
        if not is_id(b.get(k)): bad.append(k)
    if typ not in ("transfer", "loan", "release"): bad.append("type")
    if not is_id(b.get("from_squad_id")): bad.append("from_squad_id")
    if typ != "release" and not is_id(b.get("to_squad_id")): bad.append("to_squad_id")
    if typ == "release" and b.get("to_squad_id") not in (None,) and not is_id(b.get("to_squad_id")): bad.append("to_squad_id")
    eff = pdate(b.get("effective_date"))
    if not eff: bad.append("effective_date")
    led = None
    if typ == "loan":
        led = pdate(b.get("loan_end_date"))
        if not led or (eff and led <= eff): bad.append("loan_end_date")
    elif b.get("loan_end_date") is not None and not pdate(b.get("loan_end_date")): bad.append("loan_end_date")
    if not isinstance(b.get("reason", ""), str): bad.append("reason")
    if not isinstance(b.get("requested_by", ""), str): bad.append("requested_by")
    if bad: return VE(*bad)

    R = state["rosters"].get(b["league_id"])
    if not R: return E(404, "LEAGUE_NOT_FOUND", "Unknown league", {"league_id": b["league_id"]})
    prev = R["requests"].get(b["request_id"])
    if prev and same(prev["body"], b): return 200, prev["resp"]
    P = R["players"].get(b["player_id"])
    if not P: return E(404, "PLAYER_NOT_FOUND", "Unknown player", {"player_id": b["player_id"]})
    frm, to = b["from_squad_id"], (b.get("to_squad_id") if typ != "release" else None)
    for s in (frm, to):
        if s is not None and s not in R["squads"]: return E(404, "SQUAD_NOT_FOUND", "Unknown squad", {"squad_id": s})
    for s in (frm, to):
        if s is not None and R["squads"][s]["tenant_id"] != R["tenant_id"]:
            return E(409, "CROSS_TENANT_TRANSFER", "Squad belongs to another tenant", {"squad_id": s})
    materialize(state, R, P, eff)
    if P["squad_id"] != frm:
        return E(409, "NOT_IN_SQUAD", "Player is not in from_squad", {"player_id": P["player_id"], "squad_id": P["squad_id"]})
    if typ != "release" and not (R["open"] <= eff <= R["close"]):
        return E(409, "TRANSFER_WINDOW_CLOSED", "Outside transfer window", {"effective_date": eff.isoformat()})
    if P["registration_type"] == "loan":
        return E(409, "PLAYER_ON_LOAN", "Player is on loan", {"player_id": P["player_id"]})
    if typ != "release" and _count(R, to) + 1 > R["max"]:
        return E(409, "SQUAD_FULL", "Destination squad is full", {"squad_id": to, "max_squad_size": R["max"]})
    need = R["req"].get(P["position"])
    if need is not None:
        left = sum(1 for p in R["players"].values() if p["squad_id"] == frm and p["position"] == P["position"]) - 1
        if left < need:
            return E(409, "MIN_POSITION_VIOLATION", "Squad would fall below required position count",
                     {"squad_id": frm, "position": P["position"], "required": need})

    block = next((m["match_id"] for m in R["mip"] if frm in (m.get("home_squad_id"), m.get("away_squad_id"))), None)
    st = "deferred_until_match_end" if block else "active"
    if typ == "release":
        P.update(squad_id=None, parent_squad_id=None, role=None, registration_type="release", start_date=eff.isoformat(), loan_end_date=None)
        reg = None
    else:
        P.update(squad_id=to, parent_squad_id=frm if typ == "loan" else None, role="substitute", registration_type=typ,
                 start_date=eff.isoformat(), loan_end_date=led.isoformat() if led else None)
        reg = {"player_id": P["player_id"], "league_id": R["league_id"], "squad_id": to, "parent_squad_id": P["parent_squad_id"],
               "registration_type": typ, "start_date": eff.isoformat(), "loan_end_date": P["loan_end_date"], "role": "substitute"}
    entry = _audit(state, P, R, typ, frm, to, eff.isoformat(), st, b.get("requested_by"), b.get("reason"))
    resp = {"request_id": b["request_id"], "status": "accepted", "state": st, "blocking_match_id": block,
            "registration": reg, "audit_entry": entry}
    R["requests"][b["request_id"]] = {"body": b, "resp": resp}
    return 201, resp

def transfer(state, lock, b):
    with lock:
        return do_transfer(state, b)

def history(state, lock, player_id, as_of=None):
    with lock:
        lid = state["player_league"].get(player_id)
        if not lid: return E(404, "PLAYER_NOT_FOUND", "Unknown player", {"player_id": player_id})
        d = today() if as_of is None else pdate(as_of)
        if d is None: return VE("as_of")
        R = state["rosters"][lid]; P = R["players"][player_id]
        materialize(state, R, P, d)
        return 200, {"player_id": player_id, "as_of": d.isoformat(), "league_id": lid,
                     "current_squad_id": P["squad_id"], "parent_squad_id": P["parent_squad_id"],
                     "history": copy.deepcopy(P["audit"])}
