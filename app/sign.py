import hmac, hashlib, json, os

SALT = os.environ.get("DAY1_SALT", "")

def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def sign(id_, payload, salt=None):
    key = SALT if salt is None else salt
    msg = (id_ + "|" + canonical(payload)).encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()

def lots_key(seed, league_id, team_id):
    return hashlib.sha256(f"{seed}|{league_id}|{team_id}".encode("utf-8")).hexdigest()
