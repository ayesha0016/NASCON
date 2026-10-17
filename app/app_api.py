"""Mobile app support: demo data (created lazily with demo-* ids, never touching grader ids) + bootstrap payload."""
from app import scoring, roster, store

TEAM_NAMES = {"club-alpha-1": "Alpha Lions", "club-alpha-2": "Alpha Eagles", "demo-club-3": "Falcons FC", "demo-club-4": "Rangers SC"}
LEAGUE = "demo-league"
FIXTURES = [("demo-m1", "club-alpha-1", "club-alpha-2"), ("demo-m2", "demo-club-3", "demo-club-4"),
            ("demo-m3", "club-alpha-1", "demo-club-3"), ("demo-m4", "club-alpha-2", "demo-club-4"),
            ("demo-m5", "demo-club-4", "club-alpha-1"), ("demo-m6", "demo-club-3", "club-alpha-2")]

def ensure_demo(state, lock):
    if LEAGUE in state["leagues"]:
        return
    scoring.create_league(state, lock, {"league_id": LEAGUE, "sport_id": "football", "teams": list(TEAM_NAMES),
        "fixtures": [{"match_id": m, "home": h, "away": a} for m, h, a in FIXTURES], "qualification": {"spots": 2}})
    first_evt = next(iter(state["sports"]["football"]["scoring_events"]))
    def ev(eid, mid, typ, payload):
        scoring.post_event(state, lock, {"event_id": eid, "match_id": mid, "type": typ, "payload": payload})
    for mid, goals in (("demo-m1", ["club-alpha-1", "club-alpha-1", "club-alpha-2"]), ("demo-m2", ["demo-club-3"])):
        ev(f"{mid}-start", mid, "match_start", {})
        for i, t in enumerate(goals):
            ev(f"{mid}-g{i}", mid, "score", {"team_id": t, "event_type": first_evt})
        ev(f"{mid}-end", mid, "match_end", {})
    ev("demo-m3-start", "demo-m3", "match_start", {})
    ev("demo-m3-g0", "demo-m3", "score", {"team_id": "demo-club-3", "event_type": first_evt})
    roster.setup(state, lock, {
        "league": {"league_id": "demo-roster", "tenant_id": "tenant-alpha", "sport_id": "football",
                   "transfer_window": {"open": "2026-01-01", "close": "2026-12-31"}, "max_squad_size": 25, "required_positions": {"gk": 1}},
        "squads": [{"squad_id": "club-alpha-1", "club_id": "club-alpha-1", "tenant_id": "tenant-alpha", "name": "Alpha Lions"},
                   {"squad_id": "club-alpha-2", "club_id": "club-alpha-2", "tenant_id": "tenant-alpha", "name": "Alpha Eagles"}],
        "players": [
            {"player_id": "demo-p1", "name": "Ali Raza", "position": "gk", "squad_id": "club-alpha-1", "role": "captain"},
            {"player_id": "demo-p2", "name": "Bilal Khan", "position": "df", "squad_id": "club-alpha-1", "role": "vice_captain"},
            {"player_id": "demo-p3", "name": "Hamza Iqbal", "position": "mf", "squad_id": "club-alpha-1", "role": "starter"},
            {"player_id": "demo-p4", "name": "Usman Tariq", "position": "fw", "squad_id": "club-alpha-1", "role": "substitute"},
            {"player_id": "demo-p5", "name": "Saad Ahmed", "position": "gk", "squad_id": "club-alpha-2", "role": "captain"},
            {"player_id": "demo-p6", "name": "Zain Ali", "position": "fw", "squad_id": "club-alpha-2", "role": "starter"}]})

def bootstrap(state, lock, claims):
    ensure_demo(state, lock)
    matches = []
    for i, (mid, h, a) in enumerate(FIXTURES):
        s, v = scoring.score_view(state, lock, mid)
        matches.append({"match_id": mid, "home": h, "away": a, "home_name": TEAM_NAMES[h], "away_name": TEAM_NAMES[a],
                        "status": v["status"], "home_score": v["home"]["score"], "away_score": v["away"]["score"],
                        "version": v["version"], "kickoff": f"2026-10-{17 + i // 2:02d}T{15 + (i % 2) * 3}:00:00Z"})
    with lock:
        R = state["rosters"]["demo-roster"]
        players = [{"player_id": p["player_id"], "name": p["name"], "position": p["position"], "squad_id": p["squad_id"],
                    "role": p["role"], "version": len(p["audit"])} for p in R["players"].values()]
        variants = [{"variant_id": v, "size": v.rsplit("-", 1)[-1], "price_minor": store.PRICE, "stock": state["stock"].get(v, 0)}
                    for v in sorted(store.variants(state))]
        sport = state["sports"]["football"]
    return 200, {
        "user": {k: claims.get(k) for k in ("role", "tenant", "league", "club", "player")},
        "tenant": {"id": "tenant-alpha", "name": "Alpha Sports", "color": "#0B5FFF"},
        "league": {"league_id": LEAGUE, "name": "Alpha Premier League", "sport_id": "football",
                   "scoring_events": list(sport["scoring_events"].keys())},
        "teams": TEAM_NAMES, "matches": matches,
        "club": {"club_id": "club-alpha-1", "name": "Alpha Lions", "venue": "Jinnah Stadium, Islamabad (33.70, 73.05)",
                 "social": "@alphalions", "staff": [{"role": "Coach", "name": "Imran Shah"}, {"role": "Manager", "name": "Sana Malik"},
                                                    {"role": "Physio", "name": "Dr. Faisal"}],
                 "history": "2025: 2nd place, 2024: Champions", "roster_league_id": "demo-roster", "players": players,
                 "squads": [{"squad_id": "club-alpha-1", "name": "Alpha Lions"}, {"squad_id": "club-alpha-2", "name": "Alpha Eagles"}]},
        "store": {"product_id": "prod-jersey-001", "name": "Alpha Home Jersey 2026", "variants": variants, "discount_hint": "WELCOME10",
                  "pinned_on_match": "demo-m3"},
    }
