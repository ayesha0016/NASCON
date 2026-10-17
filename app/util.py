import re, json
from datetime import datetime, timezone

ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")

def is_id(v):
    return isinstance(v, str) and bool(ID_RE.match(v))

def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def E(status, code, message="", details=None):
    """Error result tuple."""
    return status, {"error": {"code": code, "message": message or code, "details": details or {}}}

def VE(*fields):
    return E(400, "VALIDATION_ERROR", "Invalid fields: " + ", ".join(fields), {"fields": list(fields)})

def same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
