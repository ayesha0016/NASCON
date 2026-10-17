from collections import Counter
from itertools import combinations
from app.fixtures import generate, seed_order

def T(n): return [f"t{i}" for i in range(1, n + 1)]

def test_round_robin_properties():
    for n in range(2, 41):
        for legs in (1, 2):
            s, r = generate({"format": "round_robin", "teams": T(n), "params": {"legs": legs}})
            assert s == 200
            per = n - 1 if n % 2 == 0 else n
            assert r["round_count"] == per * legs
            pair, ordered = Counter(), Counter()
            byes = Counter()
            for leg in (1, 2)[:legs]:
                ha = {t: [0, 0] for t in T(n)}
                for rd in r["rounds"]:
                    ms = [m for m in rd["matches"] if m["leg"] == leg]
                    if not ms and rd["byes"] and (rd["round"] - 1) // per + 1 != leg: continue
                    if (rd["round"] - 1) // per + 1 != leg: continue
                    assert len(rd["matches"]) == n // 2
                    seen = [m["home"] for m in ms] + [m["away"] for m in ms] + rd["byes"]
                    assert sorted(seen) == sorted(T(n))
                    assert len(rd["byes"]) == n % 2
                    for b in rd["byes"]: byes[(leg, b)] += 1
                    for m in ms:
                        ha[m["home"]][0] += 1; ha[m["away"]][1] += 1
                        pair[frozenset((m["home"], m["away"]))] += 1; ordered[(m["home"], m["away"])] += 1
                assert all(abs(h - a) <= 1 for h, a in ha.values()), (n, legs, ha)
            assert all(v == legs for v in pair.values()) and len(pair) == n * (n - 1) // 2
            if legs == 2: assert all(v == 1 for v in ordered.values())
            if n % 2: assert all(v == 1 for v in byes.values()) and len(byes) == n * legs

def test_rr_withdrawal():
    s, r = generate({"format": "round_robin", "teams": T(5), "params": {"withdrawals": [{"team_id": "t3", "after_round": 2}]}})
    for rd in r["rounds"]:
        for m in rd["matches"]:
            if "t3" in (m["home"], m["away"]) and rd["round"] > 2:
                assert m["status"] == "forfeit" and m["awarded_to"] != "t3"
            else:
                assert m["status"] == "scheduled"
    assert r["summary"]["total"] == 10 and r["summary"]["forfeit"] == 2
    s, r = generate({"format": "round_robin", "teams": T(5), "params": {"withdrawals": [{"team_id": "t3", "after_round": 2}], "withdrawal_policy": "void_all"}})
    assert r["summary"]["voided"] == 4
    assert generate({"format": "round_robin", "teams": T(5), "params": {"withdrawals": [{"team_id": "x", "after_round": 2}]}})[0] == 400

def test_single_elim_example():
    assert seed_order(8) == [1, 8, 4, 5, 2, 7, 3, 6]
    s, r = generate({"format": "single_elim", "teams": T(6), "params": {"results": [{"match_id": "R1M2", "winner": "t4"}]}})
    assert r["bracket_size"] == 8 and len(r["rounds"]) == 3
    r1 = r["rounds"][0]["matches"]
    assert r1[0]["bye"] and r1[0]["home"] == "t1" and r1[0]["away"] is None and r1[0]["winner"] == "t1"
    assert r1[1]["home"] == "t4" and r1[1]["away"] == "t5" and r1[1]["winner_to"] == {"match_id": "R2M1", "slot": "away"}
    r2 = r["rounds"][1]["matches"]
    assert r2[0]["home"] == "t1" and r2[0]["away"] == "t4" and r2[1]["home"] == "t2"
    assert r["rounds"][2]["matches"][0]["winner_to"] is None
    assert generate({"format": "single_elim", "teams": T(6), "params": {"results": [{"match_id": "R1M2", "winner": "t1"}]}})[0] == 400
    s, r = generate({"format": "single_elim", "teams": T(4), "params": {"third_place_match": True}})
    tp = [m for m in r["rounds"][-1]["matches"] if m["match_id"] == "TP"][0]
    assert tp["bracket"] == "third_place"
    assert r["rounds"][0]["matches"][0]["loser_to"] == {"match_id": "TP", "slot": "home"}
    assert r["rounds"][0]["matches"][1]["loser_to"] == {"match_id": "TP", "slot": "away"}

def test_double_elim_structure():
    for n in (3, 5, 8, 13, 16, 33):
        s, r = generate({"format": "double_elim", "teams": T(n), "params": {"bracket_reset": True}})
        assert s == 200
        B = r["bracket_size"]; k = B.bit_length() - 1
        assert [len(x["matches"]) for x in r["winners"]] == [B >> i for i in range(1, k + 1)]
        assert sum(len(x["matches"]) for x in r["losers"]) == B - 2
        allm = [m for x in r["winners"] + r["losers"] for m in x["matches"]] + r["grand_final"]
        feeds = Counter()
        for m in allm:
            for key in ("winner_to", "loser_to"):
                if m[key]: feeds[(m[key]["match_id"], m[key]["slot"])] += 1
        for m in allm:
            if m["match_id"].startswith("WR1") or m["match_id"] == "GF2": continue
            assert feeds[(m["match_id"], "home")] == 1 and feeds[(m["match_id"], "away")] == 1, m["match_id"]
        for x in r["winners"]:
            for m in x["matches"]: assert m["loser_to"] is not None
        assert r["grand_final"][1]["conditional"] is True and r["grand_final"][1]["required"] is None

def test_double_elim_reset_full_run():
    s, r = generate({"format": "double_elim", "teams": T(4), "params": {"bracket_reset": True, "results": [
        {"match_id": "WR1M1", "winner": "t1"}, {"match_id": "WR1M2", "winner": "t2"},
        {"match_id": "WR2M1", "winner": "t1"}, {"match_id": "LR1M1", "winner": "t3"},
        {"match_id": "LR2M1", "winner": "t3"}, {"match_id": "GF1", "winner": "t3"}]}})
    assert s == 200, r
    g1, g2 = r["grand_final"]
    assert g1["home"] == "t1" and g1["away"] == "t3" and g2["required"] is True
    s, r = generate({"format": "double_elim", "teams": T(4), "params": {"results": [
        {"match_id": "WR1M1", "winner": "t1"}, {"match_id": "WR1M2", "winner": "t2"},
        {"match_id": "WR2M1", "winner": "t1"}, {"match_id": "LR1M1", "winner": "t3"},
        {"match_id": "LR2M1", "winner": "t3"}, {"match_id": "GF1", "winner": "t1"}]}})
    assert r["grand_final"][1]["required"] is False

def test_swiss():
    s, r = generate({"format": "swiss", "teams": T(5), "params": {"total_rounds": 3}})
    assert [(p["home"], p["away"]) for p in r["pairings"]] == [("t1", "t3"), ("t2", "t4")] and r["bye"] == "t5"
    s, r = generate({"format": "swiss", "teams": T(5), "params": {"total_rounds": 3, "round": 2, "history": [
        {"round": 1, "bye": "t5", "matches": [{"home": "t1", "away": "t3", "result": "home"}, {"home": "t2", "away": "t4", "result": "draw"}]}]}})
    assert s == 200
    st = {x["team_id"]: (x["score"], x["buchholz"]) for x in r["standings"]}
    assert st["t1"] == (1.0, 0.0) and st["t2"] == (0.5, 0.5)
    assert [x["team_id"] for x in r["standings"]][:2] == ["t1", "t5"]
    assert r["bye"] == "t3"
    used = [p["home"] for p in r["pairings"]] + [p["away"] for p in r["pairings"]]
    assert sorted(used + [r["bye"]]) == T(5)
    assert frozenset(("t1", "t3")) not in {frozenset((p["home"], p["away"])) for p in r["pairings"]}
    # impossible: 2 teams already met
    s, r = generate({"format": "swiss", "teams": T(2), "params": {"total_rounds": 1, "round": 1}})
    assert s == 200
    s, r = generate({"format": "swiss", "teams": T(4), "params": {"total_rounds": 3, "round": 3, "history": [
        {"round": 1, "bye": None, "matches": [{"home": "t1", "away": "t2", "result": "home"}, {"home": "t3", "away": "t4", "result": "home"}]},
        {"round": 2, "bye": None, "matches": [{"home": "t1", "away": "t3", "result": "home"}, {"home": "t2", "away": "t4", "result": "home"}]}]}})
    assert s == 200 and {frozenset((p["home"], p["away"])) for p in r["pairings"]} == {frozenset(("t1", "t4")), frozenset(("t2", "t3"))}
