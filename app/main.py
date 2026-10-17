import threading, copy
from fastapi import FastAPI, Request
from app.errors import err

app = FastAPI()
LOCK = threading.Lock()
STATE = {}

TB = ["h2h_points","h2h_diff","overall_diff","scored","fair_play","draw_lots"]

SPORTS_SEED = {
  "football": {"sport_id":"football","name":"Football","scoring_model":"event",
    "periods":{"count":2,"duration_minutes":45},
    "scoring_events":{"goal":{"points":1}},
    "draw_allowed":True,"overtime":{"policy":"none"},"tie_break_order":TB,
    "roster":{"min_squad_size":11,"max_squad_size":25,"required_positions":{"gk":1}}},
  "basketball": {"sport_id":"basketball","name":"Basketball","scoring_model":"event",
    "periods":{"count":4,"duration_minutes":12},
    "scoring_events":{"free_throw":{"points":1},"two_pointer":{"points":2},"three_pointer":{"points":3}},
    "draw_allowed":False,"overtime":{"policy":"extra_time","duration_minutes":5},"tie_break_order":TB,
    "roster":{"min_squad_size":5,"max_squad_size":15,"required_positions":{}}},
  "cricket": {"sport_id":"cricket","name":"Cricket","scoring_model":"innings",
    "periods":{"innings_per_team":1,"overs_per_innings":20,"balls_per_over":6,"wickets_per_innings":10},
    "scoring_events":{
      "dot_ball":{"runs":0,"legal_ball":True,"wicket":False},
      "single":{"runs":1,"legal_ball":True,"wicket":False},
      "two":{"runs":2,"legal_ball":True,"wicket":False},
      "four":{"runs":4,"legal_ball":True,"wicket":False},
      "six":{"runs":6,"legal_ball":True,"wicket":False},
      "wide":{"runs":1,"legal_ball":False,"wicket":False},
      "no_ball":{"runs":1,"legal_ball":False,"wicket":False},
      "wicket":{"runs":0,"legal_ball":True,"wicket":True}},
    "draw_allowed":True,"overtime":{"policy":"super_over"},"tie_break_order":TB,
    "roster":{"min_squad_size":11,"max_squad_size":18,"required_positions":{"wicketkeeper":1}}},
  "esports": {"sport_id":"esports","name":"Esports","scoring_model":"round",
    "periods":{"best_of":3},"scoring_events":{"round_win":{"rounds":1}},
    "draw_allowed":False,"overtime":{"policy":"none"},"tie_break_order":TB,
    "roster":{"min_squad_size":5,"max_squad_size":8,"required_positions":{}}},
}

def do_reset(seed=42, stock=None):
    stock = stock or {"var-jersey-001-M": 1, "var-jersey-001-L": 5}
    STATE.clear()
    STATE.update({
        "seed": seed,
        "sports": copy.deepcopy(SPORTS_SEED),
        "matches": {}, "events": {}, "leagues": {},
        "rosters": {}, "orders": {}, "sync": {},
        "stock": dict(stock),
        "reserved": {k: 0 for k in stock},
    })

do_reset()

@app.middleware("http")
async def size_limit(request: Request, call_next):
    body = await request.body()
    if len(body) > 1_000_000:
        return err(413, "PAYLOAD_TOO_LARGE")
    return await call_next(request)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/audit/v1/reset")
async def reset(request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    seed = body.get("seed", 42)
    stock = body.get("stock")
    with LOCK:
        do_reset(seed, stock)
    return {"status": "reset_complete", "seed": seed,
            "sports": ["football","basketball","cricket","esports"],
            "tenants": ["tenant-alpha","tenant-beta"],
            "stock": STATE["stock"]}
