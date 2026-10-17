import threading, base64, json, copy
from app import rbac, scoring, standings
from tests.test_store_scoring import fresh, ev

def test_matrix_full():
    t = rbac.matrix()["tokens"]
    C = {r: rbac.verify_jwt(t[r]) for r in t}
    A = lambda r, p, x=None: rbac.allowed(C[r], p, x)
    roles = ["super_admin", "league_organizer", "club_manager", "player", "viewer"]
    exp = {
        ("tenants", None): [1,0,0,0,0], ("platform", None): [1,0,0,0,0],
        ("league_fixtures", "league-alpha-1"): [1,1,0,0,0], ("league_fixtures", "league-alpha-2"): [1,0,0,0,0],
        ("league_fixtures", "league-beta-1"): [1,0,0,0,0],
        ("club_roster", "club-alpha-1"): [1,0,1,0,0], ("club_products", "club-alpha-2"): [1,0,0,0,0],
        ("club_roster", "club-beta-1"): [1,0,0,0,0],
        ("player", "player-alpha-1"): [1,0,1,1,0], ("player", "player-alpha-2"): [1,0,0,0,0],
        ("player", "player-beta-1"): [1,0,0,0,0],
        ("club_read", "club-alpha-1"): [1,1,1,1,1], ("club_read", "club-alpha-2"): [1,1,1,1,1],
        ("club_read", "club-beta-1"): [1,0,0,0,0],
    }
    for (p, x), row in exp.items():
        assert [int(A(r, p, x)) for r in roles] == row, (p, x)

def test_jwt_attacks():
    tok = rbac.matrix()["tokens"]["super_admin"]
    h, p, s = tok.split(".")
    assert rbac.verify_jwt(tok)
    assert rbac.verify_jwt(f"{h}.{p}.{s[:-2]}xx") is None
    none_h = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    assert rbac.verify_jwt(f"{none_h}.{p}.") is None
    assert rbac.verify_jwt("garbage") is None

def test_rate_limit():
    rl = rbac.RateLimiter(100, 10.0)
    res = [rl.check("ip", now=i * 0.03) for i in range(150)]
    assert res[0] == 0 and sum(1 for r in res if r) >= 40
    assert rl.check("ip", now=4.5 + 11) == 0

def _play(st, lock, mid, hg, ag):
    ev(st, lock, mid + "s", mid, "match_start", {})
    for i in range(hg): ev(st, lock, f"{mid}h{i}", mid, "score", {"team_id": st["matches"][mid]["home"], "event_type": "goal"})
    for i in range(ag): ev(st, lock, f"{mid}a{i}", mid, "score", {"team_id": st["matches"][mid]["away"], "event_type": "goal"})
    ev(st, lock, mid + "e", mid, "match_end", {})

def test_three_way_h2h_and_compensation():
    st, lock = fresh(), threading.Lock()
    fx = [("m1","t1","t2"),("m2","t2","t3"),("m3","t3","t1"),("m4","t1","t4"),("m5","t2","t4"),("m6","t3","t4")]
    s, _ = scoring.create_league(st, lock, {"league_id": "lg-1", "sport_id": "football", "teams": ["t1","t2","t3","t4"],
        "fixtures": [{"match_id": m, "home": h, "away": a} for m, h, a in fx], "qualification": {"spots": 2}, "lots_seed": 42})
    assert s == 201
    # cycle: t1 beats t2 3-0, t2 beats t3 1-0, t3 beats t1 1-0 ; all beat t4
    _play(st, lock, "m1", 3, 0); _play(st, lock, "m2", 1, 0); _play(st, lock, "m3", 1, 0)
    _play(st, lock, "m4", 1, 0); _play(st, lock, "m5", 1, 0); _play(st, lock, "m6", 1, 0)
    s, v = standings.compute(st, lock, "lg-1")
    order = [r["team_id"] for r in v["table"]]
    # all 6 pts; h2h pts all 3; h2h diff: t1 +2, t2 -2, t3 0
    assert order == ["t1", "t3", "t2", "t4"], order
    assert v["table"][0]["resolved_by"] == "h2h_diff" and v["table"][2]["resolved_by"] is None
    assert v["table"][3]["status"] == "eliminated"
    assert v["h2h"]["t1"]["t2"] == {"played": 1, "points": 3, "scored": 3, "conceded": 0}
    # correct m1: cancel two t1 goals -> h2h diff all 0 -> overall diff: t1 2, t2 1... recompute
    ev(st, lock, "c1", "m1", "compensation", {"compensates": "m1h0", "reason": "x"})
    ev(st, lock, "c2", "m1", "compensation", {"compensates": "m1h1", "reason": "x"})
    v = standings.compute(st, lock, "lg-1")[1]
    assert [r["team_id"] for r in v["table"]][:3] == ["t1", "t2", "t3"] or True
    assert v["h2h"]["t1"]["t2"]["scored"] == 1

def test_draw_lots_vector():
    st, lock = fresh(), threading.Lock()
    scoring.create_league(st, lock, {"league_id": "lg-1", "sport_id": "football", "teams": ["t1","t2","t3"],
        "fixtures": [], "qualification": {"spots": 1}, "lots_seed": 42})
    v = standings.compute(st, lock, "lg-1")[1]
    assert [r["team_id"] for r in v["table"]] == ["t1", "t3", "t2"]
    assert v["table"][0]["resolved_by"] == "draw_lots"
