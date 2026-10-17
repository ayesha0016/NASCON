import threading, copy
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from app.errors import err
from app import store, scoring, standings, rbac, fixtures

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
        "rosters": {}, "orders": {}, "sync": {}, "queue": [], "counter": 0,
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


def out(res):
    status, body = res
    return JSONResponse(status_code=status, content=body)

async def read_json(request):
    try:
        return await request.json()
    except Exception:
        return None

def bad_json():
    return err(400, "VALIDATION_ERROR", "Malformed JSON", {"fields": ["body"]})

@app.post("/audit/v1/checkout")
async def checkout(request: Request):
    b = await read_json(request)
    if b is None: return bad_json()
    return out(store.checkout(STATE, LOCK, b))

@app.get("/audit/v1/inventory/{variant_id}")
def inventory(variant_id: str):
    return out(store.inventory(STATE, LOCK, variant_id))

@app.post("/audit/v1/sports")
async def sports(request: Request):
    b = await read_json(request)
    if b is None: return bad_json()
    return out(scoring.create_sport(STATE, LOCK, b))

@app.post("/audit/v1/events")
async def events(request: Request):
    b = await read_json(request)
    if b is None: return bad_json()
    return out(scoring.post_event(STATE, LOCK, b))

@app.get("/audit/v1/score/{match_id}")
def score(match_id: str):
    return out(scoring.score_view(STATE, LOCK, match_id))

@app.post("/audit/v1/leagues")
async def leagues(request: Request):
    b = await read_json(request)
    if b is None: return bad_json()
    return out(scoring.create_league(STATE, LOCK, b))

@app.get("/audit/v1/standings/{league_id}")
def standings_view(league_id: str):
    return out(standings.compute(STATE, LOCK, league_id))

# ---------------- RBAC ----------------
@app.post("/audit/v1/rbac-matrix")
def rbac_matrix():
    return rbac.matrix()

def _claims(request):
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "): return None
    return rbac.verify_jwt(h[7:].strip())

def _probe(request, probe, target=None):
    c = _claims(request)
    if c is None: return err(401, "UNAUTHENTICATED", "Missing or invalid token")
    if not rbac.allowed(c, probe, target): return err(403, "FORBIDDEN", "Not allowed", {"probe": probe})
    return {"authorized": True}

@app.post("/audit/v1/rbac/tenants")
def p1(request: Request): return _probe(request, "tenants")

@app.post("/audit/v1/rbac/leagues/{league_id}/fixtures")
def p2(league_id: str, request: Request): return _probe(request, "league_fixtures", league_id)

@app.post("/audit/v1/rbac/clubs/{club_id}/roster")
def p3(club_id: str, request: Request): return _probe(request, "club_roster", club_id)

@app.post("/audit/v1/rbac/clubs/{club_id}/products")
def p4(club_id: str, request: Request): return _probe(request, "club_products", club_id)

@app.patch("/audit/v1/rbac/players/{player_id}")
def p5(player_id: str, request: Request): return _probe(request, "player", player_id)

@app.get("/audit/v1/rbac/clubs/{club_id}")
def p6(club_id: str, request: Request): return _probe(request, "club_read", club_id)

@app.get("/audit/v1/rbac/platform/config")
def p7(request: Request): return _probe(request, "platform")

LIMITER = rbac.RateLimiter(100, 10.0)

@app.get("/api/v1/ping")
def ping(request: Request):
    ip = request.client.host if request.client else "unknown"
    wait = LIMITER.check(ip)
    if wait:
        r = err(429, "RATE_LIMITED", "Too many requests", {"retry_after": wait})
        r.headers["Retry-After"] = str(wait)
        return r
    return {"pong": True}

@app.post("/api/v1/auth/login")
async def login(request: Request):
    b = await read_json(request)
    if not isinstance(b, dict): return bad_json()
    u = rbac.USERS.get(b.get("email"))
    if not u or b.get("password") != rbac.PASSWORD:
        return err(401, "UNAUTHENTICATED", "Wrong email or password")
    return {"token": rbac.make_jwt(u), "role": u["role"], "user": u}

@app.post("/audit/v1/fixtures")
async def fixtures_view(request: Request):
    b = await read_json(request)
    if b is None: return bad_json()
    return out(fixtures.generate(b))
