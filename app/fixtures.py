"""Fixture generation (Section 5.7). Pure computation, nothing stored. No third-party libraries."""
import math
from app.util import E, VE, is_id, is_int

DEAD = object()  # a slot that will never hold a team (missing seed / bye loser)

# ---------------- round robin ----------------
def round_robin(teams, params):
    legs = params.get("legs", 1)
    if legs not in (1, 2) or isinstance(legs, bool): return VE("params.legs")
    if not (2 <= len(teams) <= 40): return VE("teams")
    wd = params.get("withdrawals", []) or []
    pol = params.get("withdrawal_policy", "forfeit_remaining")
    if pol not in ("void_all", "forfeit_remaining"): return VE("params.withdrawal_policy")
    n = len(teams)
    arr = list(teams) + ([None] if n % 2 else [])
    N = len(arr)
    per_leg = N - 1
    round_count = per_leg * legs
    if not isinstance(wd, list): return VE("params.withdrawals")
    out_after = {}
    for w in wd:
        if (not isinstance(w, dict) or w.get("team_id") not in teams or not is_int(w.get("after_round"))
                or not (0 <= w["after_round"] <= round_count)):
            return VE("params.withdrawals")
        out_after[w["team_id"]] = w["after_round"]

    leg1 = []
    rot = arr[:]
    bal = {t: 0 for t in teams}
    for r in range(per_leg):
        pairs = []
        for i in range(N // 2):
            a, b = rot[i], rot[N - 1 - i]
            if a is None or b is None:
                pairs.append((a, b)); continue
            # orient so the team with fewer home games so far plays at home (keeps |home-away| <= 1)
            da, db = bal[a], bal[b]
            if da < db or (da == db and (r + i) % 2 == 0):
                h, w_ = a, b
            else:
                h, w_ = b, a
            bal[h] += 1; bal[w_] -= 1
            pairs.append((h, w_))
        leg1.append(pairs)
        rot = [rot[0]] + [rot[-1]] + rot[1:-1]

    rounds, summ = [], {"total": 0, "scheduled": 0, "forfeit": 0, "voided": 0}
    for leg in range(1, legs + 1):
        for r, pairs in enumerate(leg1, 1):
            g = (leg - 1) * per_leg + r
            matches, byes, idx = [], [], 0
            for h, a in pairs:
                if leg == 2: h, a = a, h
                if h is None or a is None:
                    byes.append(a if h is None else h); continue
                idx += 1
                status, award = "scheduled", None
                hw, aw = h in out_after, a in out_after
                if pol == "void_all":
                    if hw or aw: status = "voided"
                else:
                    ho = hw and g > out_after[h]; ao = aw and g > out_after[a]
                    if ho and ao: status = "voided"
                    elif ho: status, award = "forfeit", a
                    elif ao: status, award = "forfeit", h
                summ["total"] += 1; summ[status] += 1
                matches.append({"match_id": f"RR-L{leg}-R{g}-M{idx}", "leg": leg, "home": h, "away": a,
                                "status": status, "awarded_to": award})
            rounds.append({"round": g, "matches": matches, "byes": byes})
    return 200, {"format": "round_robin", "legs": legs, "team_count": n, "round_count": round_count,
                 "rounds": rounds, "summary": summ}

# ---------------- bracket engine ----------------
def seed_order(B):
    o = [1, 2]
    while len(o) < B:
        k2 = len(o) * 2
        o = [x for s in o for x in (s, k2 + 1 - s)]
    return o

class Bracket:
    def __init__(self, teams):
        self.teams = teams
        self.n = len(teams)
        self.nodes = {}   # mid -> dict(meta, home_src, away_src)
        self.order = []
        self.results = {}

    def add(self, mid, bracket, rnd, idx, hs, as_):
        self.nodes[mid] = {"match_id": mid, "bracket": bracket, "round": rnd, "index": idx,
                           "hs": hs, "as": as_, "winner_to": None, "loser_to": None}
        self.order.append(mid)

    def link(self, src, kind, dst, slot):
        self.nodes[src]["winner_to" if kind == "W" else "loser_to"] = {"match_id": dst, "slot": slot}
        self.nodes[dst]["hs" if slot == "home" else "as"] = (kind, src)

    def val(self, src, memo):
        kind, x = src
        if kind == "S":
            return self.teams[x - 1] if x <= self.n else DEAD
        h, a = self.side(x, memo)
        if kind == "W":
            if h is DEAD and a is DEAD: return DEAD
            if h is DEAD: return a
            if a is DEAD: return h
            if h is None or a is None: return None
            return self.results.get(x)
        # loser
        if h is DEAD or a is DEAD: return DEAD
        if h is None or a is None: return None
        w = self.results.get(x)
        if w is None: return None
        return a if w == h else h

    def side(self, mid, memo):
        if mid not in memo:
            nd = self.nodes[mid]
            memo[mid] = (self.val(nd["hs"], memo), self.val(nd["as"], memo))
        return memo[mid]

    def render(self, mid, memo):
        nd = self.nodes[mid]
        h, a = self.side(mid, memo)
        bye = (h is DEAD) != (a is DEAD)
        hh = None if h is DEAD else h
        aa = None if a is DEAD else a
        winner = loser = None
        if bye:
            winner = hh if hh is not None else aa
        elif hh is not None and aa is not None and mid in self.results:
            winner = self.results[mid]; loser = aa if winner == hh else hh
        seed = lambda t: (self.teams.index(t) + 1) if t is not None else None
        return {"match_id": mid, "bracket": nd["bracket"], "round": nd["round"], "index": nd["index"],
                "home": hh, "away": aa, "home_seed": seed(hh), "away_seed": seed(aa), "bye": bye,
                "winner": winner, "loser": loser, "winner_to": nd["winner_to"], "loser_to": nd["loser_to"]}

    def apply_results(self, results):
        if not isinstance(results, list): return VE("params.results")
        for r in results:
            if not isinstance(r, dict) or r.get("match_id") not in self.nodes or not isinstance(r.get("winner"), str):
                return VE("params.results")
            self.results[r["match_id"]] = r["winner"]
        memo = {}
        for mid, w in self.results.items():
            h, a = self.side(mid, memo)
            if w not in (h, a) or h is DEAD or a is DEAD or h is None or a is None:
                return VE("params.results")
        return None

def _wr1(br, B, prefix, bracket):
    so = seed_order(B)
    for i in range(B // 2):
        br.add(f"{prefix}1M{i+1}", bracket, 1, i + 1, ("S", so[2 * i]), ("S", so[2 * i + 1]))
    k = int(math.log2(B))
    for r in range(2, k + 1):
        for i in range(B >> r):
            br.add(f"{prefix}{r}M{i+1}", bracket, r, i + 1, None, None)
    for r in range(1, k):
        for i in range(B >> r):
            br.link(f"{prefix}{r}M{i+1}", "W", f"{prefix}{r+1}M{i//2+1}", "home" if i % 2 == 0 else "away")
    return k

def single_elim(teams, params):
    if not (2 <= len(teams) <= 128): return VE("teams")
    tp = params.get("third_place_match", False)
    if not isinstance(tp, bool): return VE("params.third_place_match")
    B = 1 << (len(teams) - 1).bit_length()
    br = Bracket(teams)
    k = _wr1(br, B, "R", "main")
    if tp and k >= 2:
        br.add("TP", "third_place", k, 1, None, None)
        br.link(f"R{k-1}M1", "L", "TP", "home"); br.link(f"R{k-1}M2", "L", "TP", "away")
    e = br.apply_results(params.get("results", []) or [])
    if e: return e
    memo = {}
    rounds = []
    for r in range(1, k + 1):
        rounds.append({"round": r, "matches": [br.render(m, memo) for m in br.order
                                                if br.nodes[m]["round"] == r and br.nodes[m]["bracket"] == "main"]})
    if tp and k >= 2:
        rounds[-1]["matches"].append(br.render("TP", memo))
    return 200, {"format": "single_elim", "bracket_size": B, "rounds": rounds}

def double_elim(teams, params):
    if not (3 <= len(teams) <= 128): return VE("teams")
    reset = params.get("bracket_reset", True)
    if not isinstance(reset, bool): return VE("params.bracket_reset")
    B = 1 << (len(teams) - 1).bit_length()
    br = Bracket(teams)
    k = _wr1(br, B, "WR", "winners")
    LR = 2 * (k - 1)
    cnt = lambda r: B >> ((r + 1) // 2 + 1)
    for r in range(1, LR + 1):
        for i in range(cnt(r)):
            br.add(f"LR{r}M{i+1}", "losers", r, i + 1, None, None)
    br.add("GF1", "grand_final", 1, 1, None, None)
    # WR1 losers -> LR1
    for i in range(B // 2):
        br.link(f"WR1M{i+1}", "L", f"LR1M{i//2+1}", "home" if i % 2 == 0 else "away")
    for j in range(1, k):
        odd, even = 2 * j - 1, 2 * j
        for i in range(cnt(odd)):           # LR odd winners -> LR even home
            br.link(f"LR{odd}M{i+1}", "W", f"LR{even}M{i+1}", "home")
        for i in range(cnt(even)):          # WR(j+1) losers -> LR even away
            br.link(f"WR{j+1}M{i+1}", "L", f"LR{even}M{i+1}", "away")
        if even < LR:
            for i in range(cnt(even)):
                br.link(f"LR{even}M{i+1}", "W", f"LR{even+1}M{i//2+1}", "home" if i % 2 == 0 else "away")
    br.link(f"WR{k}M1", "W", "GF1", "home")
    br.link(f"LR{LR}M1", "W", "GF1", "away")
    e = br.apply_results([r for r in (params.get("results", []) or []) if not (isinstance(r, dict) and r.get("match_id") == "GF2")])
    if e: return e
    memo = {}
    W = [{"round": r, "matches": [br.render(f"WR{r}M{i+1}", memo) for i in range(B >> r)]} for r in range(1, k + 1)]
    L = [{"round": r, "matches": [br.render(f"LR{r}M{i+1}", memo) for i in range(cnt(r))]} for r in range(1, LR + 1)]
    gf1 = br.render("GF1", memo)
    gf = [gf1]
    if reset:
        req = None
        if gf1["winner"] is not None:
            req = gf1["winner"] == gf1["away"]
        gres = [r for r in (params.get("results", []) or []) if isinstance(r, dict) and r.get("match_id") == "GF2"]
        w2 = l2 = None
        if gres:
            if not req or gres[-1].get("winner") not in (gf1["home"], gf1["away"]): return VE("params.results")
            w2 = gres[-1]["winner"]; l2 = gf1["away"] if w2 == gf1["home"] else gf1["home"]
        gf.append({"match_id": "GF2", "bracket": "grand_final", "round": 2, "index": 1,
                   "home": gf1["home"], "away": gf1["away"], "home_seed": gf1["home_seed"], "away_seed": gf1["away_seed"],
                   "bye": False, "winner": w2, "loser": l2, "winner_to": None, "loser_to": None,
                   "conditional": True, "required": req})
        gf1["conditional"] = False; gf1["required"] = True
    return 200, {"format": "double_elim", "bracket_size": B, "bracket_reset": reset,
                 "winners": W, "losers": L, "grand_final": gf}

# ---------------- swiss ----------------
def swiss(teams, params):
    n = len(teams)
    if not (2 <= n <= 64): return VE("teams")
    tr = params.get("total_rounds", math.ceil(math.log2(n)))
    if not is_int(tr) or not (1 <= tr <= n - 1): return VE("params.total_rounds")
    rnd = params.get("round", 1)
    if not is_int(rnd) or not (1 <= rnd <= tr): return VE("params.round")
    hist = params.get("history", []) or []
    if not isinstance(hist, list) or len(hist) != rnd - 1: return VE("params.history")
    seed = {t: i for i, t in enumerate(teams)}
    score = {t: 0.0 for t in teams}; opps = {t: [] for t in teams}; had_bye = set(); met = set()
    for i, h in enumerate(sorted(hist, key=lambda x: x.get("round", 0) if isinstance(x, dict) else 0)):
        if not isinstance(h, dict) or h.get("round") != i + 1 or not isinstance(h.get("matches", []), list):
            return VE("params.history")
        b = h.get("bye")
        if b is not None:
            if b not in seed: return VE("params.history")
            score[b] += 1; had_bye.add(b)
        for m in h.get("matches", []):
            if (not isinstance(m, dict) or m.get("home") not in seed or m.get("away") not in seed
                    or m["home"] == m["away"] or m.get("result") not in ("home", "away", "draw")):
                return VE("params.history")
            a, c = m["home"], m["away"]
            if m["result"] == "home": score[a] += 1
            elif m["result"] == "away": score[c] += 1
            else: score[a] += 0.5; score[c] += 0.5
            opps[a].append(c); opps[c].append(a); met.add(frozenset((a, c)))
    buch = {t: float(sum(score[o] for o in opps[t])) for t in teams}
    ranked = sorted(teams, key=lambda t: (-score[t], -buch[t], seed[t]))
    stand = [{"rank": i + 1, "team_id": t, "score": score[t], "buchholz": buch[t]} for i, t in enumerate(ranked)]

    if rnd == 1:
        m = n // 2
        pairs = [(teams[i], teams[i + m]) for i in range(m)]
        bye = teams[-1] if n % 2 else None
    else:
        cands = [None]
        if n % 2:
            pool = [t for t in teams if t not in had_bye] or list(teams)
            cands = sorted(pool, key=lambda t: (score[t], -seed[t]))
        pairs = None
        for bye in cands:
            rest = [t for t in ranked if t != bye]
            pairs = _match(rest, score, met)
            if pairs is not None: break
        if pairs is None:
            return E(409, "NO_VALID_PAIRING", "No rematch-free pairing exists", {"round": rnd})
        pairs = [(a, b) if seed[a] < seed[b] else (b, a) for a, b in pairs]
    out = [{"match_id": f"SW-R{rnd}-M{i+1}", "home": a, "away": b} for i, (a, b) in enumerate(pairs)]
    return 200, {"format": "swiss", "round": rnd, "total_rounds": tr, "pairings": out, "bye": bye, "standings": stand}

def _match(players, score, met, limit=400000):
    """Min-cost (sum |score diff|) rematch-free perfect matching via branch & bound."""
    best = [None, float("inf")]
    calls = [0]
    def lb(rem):
        s = sorted(score[t] for t in rem)
        return sum(s[i + 1] - s[i] for i in range(0, len(s) - 1, 2))
    def go(rem, cost, acc):
        calls[0] += 1
        if calls[0] > limit and best[0] is not None: return
        if not rem:
            if cost < best[1]: best[0], best[1] = list(acc), cost
            return
        if cost + lb(rem) >= best[1]: return
        a = rem[0]
        cand = sorted(rem[1:], key=lambda b: abs(score[a] - score[b]))
        for b in cand:
            if frozenset((a, b)) in met: continue
            nr = [x for x in rem if x not in (a, b)]
            acc.append((a, b)); go(nr, cost + abs(score[a] - score[b]), acc); acc.pop()
            if best[1] == 0: return
    go(players, 0.0, [])
    return best[0]

# ---------------- dispatch ----------------
def generate(b):
    if not isinstance(b, dict): return VE("body")
    fmt, teams, params = b.get("format"), b.get("teams"), b.get("params") or {}
    bad = []
    if fmt not in ("round_robin", "swiss", "single_elim", "double_elim"): bad.append("format")
    if not isinstance(teams, list) or not all(is_id(t) for t in teams) or len(set(teams)) != len(teams): bad.append("teams")
    if not isinstance(params, dict): bad.append("params")
    if bad: return VE(*bad)
    return {"round_robin": round_robin, "swiss": swiss, "single_elim": single_elim, "double_elim": double_elim}[fmt](teams, params)
