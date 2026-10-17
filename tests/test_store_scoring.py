import threading, base64, copy
from app import store, scoring
from app.main import SPORTS_SEED

def fresh(stock=None):
    stock = stock or {"var-jersey-001-M": 1, "var-jersey-001-L": 5}
    return {"seed": 42, "sports": copy.deepcopy(SPORTS_SEED), "matches": {}, "events": {}, "leagues": {},
            "orders": {}, "queue": [], "counter": 0, "rosters": {}, "player_league": {}, "audit_seq": 0, "sync": {}, "stock": dict(stock), "reserved": {k: 0 for k in stock}}

def order(i, tok="tok_success", dc=None, var="var-jersey-001-M"):
    return {"order_id": f"ord-{i}", "customer_id": "c1", "items": [{"variant_id": var, "quantity": 1}],
            "discount_code": dc, "payment": {"token": tok}}

def test_concurrent_last_unit():
    st, lock = fresh(), threading.Lock()
    res, bar = [], threading.Barrier(20)
    def go(i):
        bar.wait(); res.append(store.checkout(st, lock, order(i))[0])
    ts = [threading.Thread(target=go, args=(i,)) for i in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(res) == [201] + [409] * 19
    assert store.inventory(st, lock, "var-jersey-001-M")[1] == {"variant_id": "var-jersey-001-M", "stock": 0, "reserved": 0}

def test_receipt_and_idempotency():
    st, lock = fresh(), threading.Lock()
    s, r = store.checkout(st, lock, order(1, dc="WELCOME10", var="var-jersey-001-L"))
    assert s == 201 and r["discount_minor"] == 45000 and r["total_minor"] == 405000
    pdf = base64.b64decode(r["receipt"]["pdf_base64"])
    assert pdf.startswith(b"%PDF") and b"PKR 4050.00" in pdf and b"ord-1" in pdf
    assert store.checkout(st, lock, order(1, dc="WELCOME10", var="var-jersey-001-L")) == (200, r)
    assert store.checkout(st, lock, order(1, var="var-jersey-001-L"))[1]["error"]["code"] == "DUPLICATE_ORDER_ID"

def test_payment_failures_release():
    st, lock = fresh(), threading.Lock()
    assert store.checkout(st, lock, order(1, "tok_declined"))[0] == 402
    assert store.checkout(st, lock, order(2, "tok_capture_fail"))[1]["error"]["code"] == "PAYMENT_CAPTURE_FAILED"
    assert store.checkout(st, lock, order(3))[0] == 201
    assert store.checkout(st, lock, order(4, dc="BAD"))[1]["error"]["code"] == "INVALID_DISCOUNT_CODE"
    assert store.checkout(st, lock, order(5, var="nope"))[1]["error"]["code"] == "VARIANT_NOT_FOUND"

def ev(st, lock, eid, mid, typ, payload):
    return scoring.post_event(st, lock, {"event_id": eid, "match_id": mid, "type": typ, "payload": payload})

def test_mystery_sport_and_compensation():
    st, lock = fresh(), threading.Lock()
    sp = {"sport_id": "kabaddi", "name": "Kabaddi", "scoring_model": "event", "periods": {"count": 2, "duration_minutes": 20},
          "scoring_events": {"raid_point": {"points": 1}, "all_out": {"points": 2}}, "draw_allowed": False,
          "overtime": {"policy": "golden_score", "duration_minutes": 5},
          "roster": {"min_squad_size": 7, "max_squad_size": 12, "required_positions": {"raider": 2}}}
    assert scoring.create_sport(st, lock, sp)[0] == 201
    assert scoring.create_sport(st, lock, sp)[1]["error"]["code"] == "SPORT_EXISTS"
    assert ev(st, lock, "e1", "m1", "match_init", {"sport_id": "kabaddi", "home_team_id": "a", "away_team_id": "b"})[0] == 201
    assert ev(st, lock, "e2", "m1", "score", {"team_id": "a", "event_type": "all_out"})[1]["error"]["code"] == "MATCH_NOT_IN_PROGRESS"
    ev(st, lock, "e3", "m1", "match_start", {})
    assert ev(st, lock, "e4", "m1", "score", {"team_id": "a", "event_type": "goal"})[1]["error"]["code"] == "UNKNOWN_EVENT_TYPE"
    s, r = ev(st, lock, "e5", "m1", "score", {"team_id": "a", "event_type": "all_out"})
    assert s == 201 and r["sequence"] == 3
    ev(st, lock, "e6", "m1", "compensation", {"compensates": "e5", "reason": "disallowed"})
    assert ev(st, lock, "e7", "m1", "compensation", {"compensates": "e5", "reason": "x"})[1]["error"]["code"] == "INVALID_COMPENSATION"
    assert ev(st, lock, "e8", "m1", "match_end", {})[1]["error"]["code"] == "TIE_NOT_ALLOWED"
    ev(st, lock, "e9", "m1", "tiebreak_winner", {"team_id": "b"})
    assert ev(st, lock, "e10", "m1", "match_end", {})[0] == 201
    s, v = scoring.score_view(st, lock, "m1")
    assert v["outcome"] == "away_win" and v["home"]["score"] == 0 and v["events"][2]["compensated_by"] == "e6"
    assert ev(st, lock, "e5", "m1", "score", {"team_id": "a", "event_type": "all_out"})[0] == 200

def test_cricket_overs():
    st, lock = fresh(), threading.Lock()
    ev(st, lock, "c0", "cm", "match_init", {"sport_id": "cricket", "home_team_id": "x", "away_team_id": "y"})
    ev(st, lock, "c1", "cm", "match_start", {})
    for i, t in enumerate(["four", "wide", "six", "single"]):
        ev(st, lock, f"b{i}", "cm", "score", {"team_id": "x", "event_type": t})
    v = scoring.score_view(st, lock, "cm")[1]["home"]
    assert v["score"] == 12 and v["balls"] == 3 and v["overs"] == "0.3"
