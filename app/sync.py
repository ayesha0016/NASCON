"""Offline action replay with per-action conflict resolution (Section 5.14)."""
import copy
from app.util import E, VE, is_id, is_int, same
from app import scoring, roster

def sync(state, lock, b):
    if not isinstance(b, dict) or not is_id(b.get("device_id")): return VE("device_id")
    acts = b.get("actions")
    if not isinstance(acts, list) or not (1 <= len(acts) <= 100): return VE("actions")
    for a in acts:
        if (not isinstance(a, dict) or not is_id(a.get("action_id")) or a.get("type") not in ("score_event", "roster_transfer")
                or not isinstance(a.get("queued_at"), str) or not is_int(a.get("base_version")) or a["base_version"] < 0
                or not isinstance(a.get("payload"), dict)):
            return VE("actions")
    dev = b["device_id"]
    order = sorted(range(len(acts)), key=lambda i: (acts[i]["queued_at"], i))
    results = [None] * len(acts)
    with lock:
        for n, i in enumerate(order, 1):
            results[i] = _one(state, dev, acts[i], n)
    summ = {"applied": 0, "rejected": 0, "duplicate": 0}
    for r in results: summ[r["status"]] += 1
    return 200, {"device_id": dev, "results": results, "summary": summ}

def _res(a, n, status, resolution, code, ver, sig=None):
    return {"action_id": a["action_id"], "status": status, "resolution": resolution, "error_code": code,
            "processed_order": n, "server_version": ver, "signature": sig}

def _one(state, dev, a, n):
    key = (dev, a["action_id"])
    p = a["payload"]
    if a["type"] == "score_event":
        mid = p.get("match_id")
        m = state["matches"].get(mid)
        ver = len(m["events"]) if m else 0
        if key in state["sync"]: return _res(a, n, "duplicate", "skipped_duplicate", None, ver)
        prev = state["events"].get(p.get("event_id"))
        if prev and same(prev["body"], p):
            state["sync"][key] = True
            return _res(a, n, "duplicate", "skipped_duplicate", None, ver)
        if prev:
            return _res(a, n, "rejected", "server_wins", "DUPLICATE_EVENT_ID", ver)
        if not m and p.get("type") != "match_init":
            st, body = scoring.apply_event(state, p)  # yields validation or MATCH_NOT_FOUND
            return _res(a, n, "rejected", "server_wins", body["error"]["code"], ver)
        if a["base_version"] > ver:
            return _res(a, n, "rejected", "server_wins", "INVALID_BASE_VERSION", ver)
        st, body = scoring.apply_event(state, p)
        if st >= 400:
            return _res(a, n, "rejected", "server_wins", body["error"]["code"], ver)
        state["sync"][key] = True
        newv = len(state["matches"][p["match_id"]]["events"])
        return _res(a, n, "applied", "appended" if a["base_version"] == ver else "rebased_append", None, newv, body["signature"])
    # roster_transfer
    lid = state["player_league"].get(p.get("player_id"))
    P = state["rosters"][lid]["players"].get(p.get("player_id")) if lid else None
    ver = len(P["audit"]) if P else 0
    if key in state["sync"]: return _res(a, n, "duplicate", "skipped_duplicate", None, ver)
    R = state["rosters"].get(p.get("league_id"))
    if R and p.get("request_id") in R["requests"] and same(R["requests"][p["request_id"]]["body"], p):
        state["sync"][key] = True
        return _res(a, n, "duplicate", "skipped_duplicate", None, ver)
    if P is None or R is None:
        st, body = roster.do_transfer(state, p)
        return _res(a, n, "rejected", "server_wins", body["error"]["code"] if st >= 400 else "PLAYER_NOT_FOUND", ver)
    if a["base_version"] > ver:
        return _res(a, n, "rejected", "server_wins", "INVALID_BASE_VERSION", ver)
    st, body = roster.do_transfer(state, p)
    newv = len(P["audit"])
    if st >= 400:
        return _res(a, n, "rejected", "server_wins", body["error"]["code"], newv)
    state["sync"][key] = True
    return _res(a, n, "applied", "appended" if a["base_version"] == ver else "rebased_append", None, newv)
