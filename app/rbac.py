"""JWT (HS256, hand-rolled, verified) + 5-role policy matrix + rate limiter."""
import base64, hashlib, hmac, json, os, time, threading
from collections import defaultdict, deque

SECRET = (os.environ.get("JWT_SECRET") or ("jwt|" + os.environ.get("DAY1_SALT", "dev"))).encode()

def _b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def _unb64(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def make_jwt(claims, ttl=7200):
    head = {"alg": "HS256", "typ": "JWT"}
    body = dict(claims, exp=int(time.time()) + ttl, iat=int(time.time()))
    h = _b64(json.dumps(head, separators=(",", ":")).encode())
    p = _b64(json.dumps(body, separators=(",", ":")).encode())
    sig = _b64(hmac.new(SECRET, f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"

def verify_jwt(tok):
    try:
        h, p, s = tok.split(".")
        head = json.loads(_unb64(h))
        if head.get("alg") != "HS256": return None
        exp_sig = _b64(hmac.new(SECRET, f"{h}.{p}".encode(), hashlib.sha256).digest())
        if not s or not hmac.compare_digest(s, exp_sig): return None
        claims = json.loads(_unb64(p))
        if not isinstance(claims.get("exp"), int) or claims["exp"] < time.time(): return None
        return claims
    except Exception:
        return None

# Seeded scenario (Section 5.5)
LEAGUES = {"league-alpha-1": "tenant-alpha", "league-alpha-2": "tenant-alpha", "league-beta-1": "tenant-beta"}
CLUBS = {"club-alpha-1": "tenant-alpha", "club-alpha-2": "tenant-alpha", "club-beta-1": "tenant-beta"}
PLAYERS = {"player-alpha-1": "club-alpha-1", "player-alpha-2": "club-alpha-2", "player-beta-1": "club-beta-1"}

USERS = {
    "admin@nascon.test": {"sub": "user-admin", "role": "super_admin", "tenant": None},
    "organizer@nascon.test": {"sub": "user-organizer", "role": "league_organizer", "tenant": "tenant-alpha", "league": "league-alpha-1"},
    "manager@nascon.test": {"sub": "user-manager", "role": "club_manager", "tenant": "tenant-alpha", "club": "club-alpha-1"},
    "player@nascon.test": {"sub": "user-player", "role": "player", "tenant": "tenant-alpha", "player": "player-alpha-1", "club": "club-alpha-1"},
    "viewer@nascon.test": {"sub": "user-viewer", "role": "viewer", "tenant": "tenant-alpha"},
}
PASSWORD = "Nascon@26"

def matrix():
    tokens = {u["role"]: make_jwt(u) for u in USERS.values()}
    return {"tokens": tokens, "context": {
        "tenant_id": "tenant-alpha", "foreign_tenant_id": "tenant-beta",
        "own_league_id": "league-alpha-1", "other_league_id": "league-alpha-2", "foreign_league_id": "league-beta-1",
        "own_club_id": "club-alpha-1", "other_club_id": "club-alpha-2", "foreign_club_id": "club-beta-1",
        "own_player_id": "player-alpha-1", "other_player_id": "player-alpha-2", "foreign_player_id": "player-beta-1"}}

def allowed(c, probe, target=None):
    role = c.get("role")
    if role == "super_admin": return True
    if probe in ("tenants", "platform"): return False
    if probe == "league_fixtures":
        return role == "league_organizer" and target == c.get("league")
    if probe in ("club_roster", "club_products"):
        return role == "club_manager" and target == c.get("club")
    if probe == "player":
        if role == "player": return target == c.get("player")
        if role == "club_manager": return PLAYERS.get(target) == c.get("club")
        return False
    if probe == "club_read":
        return CLUBS.get(target) == c.get("tenant")
    return False

class RateLimiter:
    def __init__(self, limit=100, window=10.0):
        self.limit, self.window = limit, window
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()
    def check(self, key, now=None):
        """Returns 0 if allowed, else seconds to wait."""
        now = time.monotonic() if now is None else now
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - self.window: q.popleft()
            if len(q) >= self.limit:
                return max(1, int(q[0] + self.window - now) + 1)
            q.append(now)
            return 0
