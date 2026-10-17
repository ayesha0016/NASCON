import threading
from app import roster, sync, scoring
from tests.test_store_scoring import fresh, ev

def setup(st, lock, mip=None):
    return roster.setup(st, lock, {
        "league": {"league_id": "lg-r1", "tenant_id": "tenant-alpha", "sport_id": "football",
                   "transfer_window": {"open": "2026-09-01", "close": "2026-09-30"}, "max_squad_size": 3, "required_positions": {"gk": 1}},
        "squads": [{"squad_id": "sq-a", "club_id": "club-a", "tenant_id": "tenant-alpha", "name": "A"},
                   {"squad_id": "sq-b", "club_id": "club-b", "tenant_id": "tenant-alpha", "name": "B"},
                   {"squad_id": "sq-x", "club_id": "club-x", "tenant_id": "tenant-beta", "name": "X"}],
        "players": [{"player_id": "p1", "name": "Ali", "position": "gk", "squad_id": "sq-a", "role": "captain"},
                    {"player_id": "p2", "name": "Bilal", "position": "df", "squad_id": "sq-a", "role": "starter"}],
        "matches_in_progress": mip or []})

def tr(rid, pid="p2", typ="transfer", frm="sq-a", to="sq-b", eff="2026-09-30", led=None):
    return {"request_id": rid, "league_id": "lg-r1", "player_id": pid, "type": typ, "from_squad_id": frm,
            "to_squad_id": to, "effective_date": eff, "loan_end_date": led, "reason": "Tactical move", "requested_by": "user-77"}

def code(r): return r[1]["error"]["code"] if r[0] >= 400 else r[0]

def test_rule_order_and_transfer():
    st, lock = fresh(), threading.Lock()
    s, r = setup(st, lock)
    assert s == 201 and r["squad_counts"] == {"sq-a": 2, "sq-b": 0, "sq-x": 0}
    assert code(roster.transfer(st, lock, tr("t0", typ="loan"))) == "VALIDATION_ERROR"
    assert code(roster.transfer(st, lock, tr("t1", pid="zz"))) == "PLAYER_NOT_FOUND"
    assert code(roster.transfer(st, lock, tr("t2", to="sq-x"))) == "CROSS_TENANT_TRANSFER"
    assert code(roster.transfer(st, lock, tr("t3", frm="sq-b", to="sq-a"))) == "NOT_IN_SQUAD"
    assert code(roster.transfer(st, lock, tr("t4", eff="2026-10-01"))) == "TRANSFER_WINDOW_CLOSED"
    assert code(roster.transfer(st, lock, tr("t5", pid="p1"))) == "MIN_POSITION_VIOLATION"
    s, r = roster.transfer(st, lock, tr("t6"))
    assert s == 201 and r["state"] == "active" and r["registration"]["role"] == "substitute" and r["audit_entry"]["audit_id"] == "au-0001"
    assert roster.transfer(st, lock, tr("t6")) == (200, r)
    assert roster.history(st, lock, "p2", "2026-10-01")[1]["current_squad_id"] == "sq-b"

def test_loan_return_and_deferred():
    st, lock = fresh(), threading.Lock()
    setup(st, lock, mip=[{"match_id": "m-live", "home_squad_id": "sq-a", "away_squad_id": "sq-b"}])
    s, r = roster.transfer(st, lock, tr("l1", typ="loan", eff="2026-09-10", led="2026-12-31"))
    assert s == 201 and r["state"] == "deferred_until_match_end" and r["blocking_match_id"] == "m-live"
    assert r["registration"]["parent_squad_id"] == "sq-a"
    assert code(roster.transfer(st, lock, tr("l2", frm="sq-b", to="sq-a", eff="2026-09-20"))) == "PLAYER_ON_LOAN"
    for _ in range(2):
        s, h = roster.history(st, lock, "p2", "2027-01-05")
    assert h["current_squad_id"] == "sq-a" and [e["action"] for e in h["history"]] == ["loan", "loan_return"]
    assert h["history"][1]["who"] == "system"

def test_squad_full():
    st, lock = fresh(), threading.Lock()
    roster.setup(st, lock, {"league": {"league_id": "lg-r1", "tenant_id": "t", "sport_id": "football",
        "transfer_window": {"open": "2026-09-01", "close": "2026-09-30"}, "max_squad_size": 1, "required_positions": {}},
        "squads": [{"squad_id": "sq-a", "club_id": "c", "tenant_id": "t"}, {"squad_id": "sq-b", "club_id": "d", "tenant_id": "t"}],
        "players": [{"player_id": "p1", "position": "df", "squad_id": "sq-a"}, {"player_id": "p2", "position": "df", "squad_id": "sq-b"}]})
    assert code(roster.transfer(st, lock, {**tr("x", pid="p1"), "league_id": "lg-r1"})) == "SQUAD_FULL"
    assert roster.transfer(st, lock, tr("y", pid="p1", typ="release", to=None, eff="2027-03-01"))[1]["registration"] is None

def test_offline_sync():
    st, lock = fresh(), threading.Lock()
    setup(st, lock)
    ev(st, lock, "e1", "m1", "match_init", {"sport_id": "football", "home_team_id": "t1", "away_team_id": "t2"})
    ev(st, lock, "e2", "m1", "match_start", {})
    ev(st, lock, "e3", "m1", "score", {"team_id": "t1", "event_type": "goal"})
    req = {"device_id": "dev-1", "actions": [
        {"action_id": "a2", "type": "roster_transfer", "queued_at": "2026-10-17T11:01:00Z", "base_version": 0, "payload": tr("s1")},
        {"action_id": "a1", "type": "score_event", "queued_at": "2026-10-17T11:00:00Z", "base_version": 1,
         "payload": {"event_id": "evt-9", "match_id": "m1", "type": "score", "payload": {"team_id": "t1", "event_type": "goal"}}},
        {"action_id": "a3", "type": "score_event", "queued_at": "2026-10-17T11:02:00Z", "base_version": 99,
         "payload": {"event_id": "evt-10", "match_id": "m1", "type": "score", "payload": {"team_id": "t1", "event_type": "goal"}}},
        {"action_id": "a4", "type": "score_event", "queued_at": "2026-10-17T11:03:00Z", "base_version": 0,
         "payload": {"event_id": "evt-11", "match_id": "nope", "type": "score", "payload": {"team_id": "t1", "event_type": "goal"}}}]}
    s, r = sync.sync(st, lock, req)
    R = {x["action_id"]: x for x in r["results"]}
    assert [x["action_id"] for x in r["results"]] == ["a2", "a1", "a3", "a4"]
    assert R["a1"]["resolution"] == "rebased_append" and R["a1"]["processed_order"] == 1 and R["a1"]["server_version"] == 4 and R["a1"]["signature"]
    assert R["a2"]["resolution"] == "appended" and R["a2"]["server_version"] == 1 and R["a2"]["signature"] is None
    assert R["a3"]["error_code"] == "INVALID_BASE_VERSION"
    assert R["a4"]["error_code"] == "MATCH_NOT_FOUND"
    assert r["summary"] == {"applied": 2, "rejected": 2, "duplicate": 0}
    s, r2 = sync.sync(st, lock, {"device_id": "dev-1", "actions": req["actions"][:2]})
    assert all(x["status"] == "duplicate" for x in r2["results"])
    assert sync.sync(st, lock, {"device_id": "dev-1", "actions": []})[0] == 400
