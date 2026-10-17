from app.sign import sign, lots_key

def test_vector_1():
    assert sign("evt-1", {"team_id": "t1", "event_type": "goal"}, salt="demo-salt") == \
        "972a8b558690b19121cc5bea84978f85e8eb15a3735018db36616bab1d7ec501"

def test_lots():
    assert lots_key(42, "lg-1", "t1").startswith("0613316991")
