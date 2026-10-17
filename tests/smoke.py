"""End-to-end smoke test against the running server. Run inside the api container:
   docker cp tests/smoke.py nascon-api-1:/code/smoke.py
   docker compose exec api python smoke.py
"""
import base64, json, threading
import httpx

B = "http://localhost:8080"
c = httpx.Client(base_url=B, timeout=10)
ok = fail = 0

def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("PASS", name)
    else:
        fail += 1; print("FAIL", name, extra)

r = c.get("/health"); check("health", r.status_code == 200 and r.json() == {"status": "ok"})
r = c.post("/audit/v1/reset", json={"seed": 42, "stock": {"var-jersey-001-M": 1, "var-jersey-001-L": 5}})
check("reset", r.status_code == 200 and r.json()["status"] == "reset_complete")
r = c.post("/audit/v1/reset"); check("reset no body", r.status_code == 200)

# checkout burst
res = []
bar = threading.Barrier(20)
def buy(i):
    cl = httpx.Client(base_url=B, timeout=10)
    bar.wait()
    res.append(cl.post("/audit/v1/checkout", json={"order_id": f"ord-{i}", "customer_id": "c", "items": [{"variant_id": "var-jersey-001-M", "quantity": 1}], "discount_code": None, "payment": {"token": "tok_success"}}).status_code)
ts = [threading.Thread(target=buy, args=(i,)) for i in range(20)]
[t.start() for t in ts]; [t.join() for t in ts]
check("20 buyers -> 1 success", sorted(res) == [201] + [409] * 19, res)
inv = c.get("/audit/v1/inventory/var-jersey-001-M").json()
check("inventory 0/0", inv == {"variant_id": "var-jersey-001-M", "stock": 0, "reserved": 0}, inv)
win = [i for i in range(20)]
r = c.post("/audit/v1/checkout", json={"order_id": "ord-L", "customer_id": "c", "items": [{"variant_id": "var-jersey-001-L", "quantity": 1}], "discount_code": None, "payment": {"token": "tok_success"}})
pdf = base64.b64decode(r.json()["receipt"]["pdf_base64"])
check("receipt pdf", r.status_code == 201 and pdf.startswith(b"%PDF") and b"PKR 4500.00" in pdf)
r = c.post("/audit/v1/checkout", json={"order_id": "ord-D", "customer_id": "c", "items": [{"variant_id": "var-jersey-001-L", "quantity": 1}], "discount_code": None, "payment": {"token": "tok_declined"}})
check("declined 402", r.status_code == 402 and r.json()["error"]["code"] == "PAYMENT_DECLINED")

# events
c.post("/audit/v1/events", json={"event_id": "e1", "match_id": "m1", "type": "match_init", "payload": {"sport_id": "football", "home_team_id": "t1", "away_team_id": "t2"}})
c.post("/audit/v1/events", json={"event_id": "e2", "match_id": "m1", "type": "match_start", "payload": {}})
r = c.post("/audit/v1/events", json={"event_id": "e3", "match_id": "m1", "type": "score", "payload": {"team_id": "t1", "event_type": "goal"}})
check("score event 201 + signature", r.status_code == 201 and len(r.json()["signature"]) == 64)
r = c.get("/audit/v1/score/m1"); check("score view", r.json()["home"]["score"] == 1)
r = c.post("/audit/v1/events", content=b"{not json", headers={"content-type": "application/json"})
check("bad json 400", r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR")

# fixtures
r = c.post("/audit/v1/fixtures", json={"format": "single_elim", "teams": ["t1", "t2", "t3", "t4", "t5", "t6"], "params": {}})
check("fixtures single elim", r.status_code == 200 and r.json()["bracket_size"] == 8)

# rbac
m = c.post("/audit/v1/rbac-matrix").json()
h = lambda role: {"Authorization": "Bearer " + m["tokens"][role]}
check("P7 super 200", c.get("/audit/v1/rbac/platform/config", headers=h("super_admin")).status_code == 200)
check("P7 viewer 403", c.get("/audit/v1/rbac/platform/config", headers=h("viewer")).status_code == 403)
check("P7 no token 401", c.get("/audit/v1/rbac/platform/config").status_code == 401)
check("P5 PATCH player self", c.patch("/audit/v1/rbac/players/player-alpha-1", headers=h("player"), json={}).status_code == 200)

# body size
r = c.post("/audit/v1/sports", content=b"x" * 1_100_000, headers={"content-type": "application/json"})
check("413 big body", r.status_code == 413)
r = c.post("/audit/v1/sports", json={"sport_id": "x'; DROP TABLE sports;--", "name": "x"})
check("sql injection 400", r.status_code == 400)

# rate limit
codes = [c.get("/api/v1/ping").status_code for _ in range(150)]
check("rate limit", codes[0] == 200 and codes.count(429) >= 40, codes.count(429))

print(f"\n{ok} passed, {fail} failed")
