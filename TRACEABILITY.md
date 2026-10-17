# Traceability

| ID | Source | Tests |
|---|---|---|
| FR-1.1 Multi-sport + event sourcing | app/scoring.py, app/sign.py | tests/test_store_scoring.py, tests/test_sign.py |
| FR-1.2 Leagues | app/scoring.py (create_league) | tests/test_rbac_standings.py |
| FR-1.3 Fixtures (RR, Swiss, SE, DE) | app/fixtures.py | tests/test_fixtures.py |
| FR-1.4 Standings | app/standings.py | tests/test_rbac_standings.py |
| FR-2.1 Clubs / tenant isolation | app/rbac.py, app/roster.py | tests/test_rbac_standings.py, tests/test_roster_sync.py |
| FR-2.2 Roster / transfers / loans | app/roster.py | tests/test_roster_sync.py |
| FR-2.3 Store / checkout / PDF | app/store.py, app/pdf.py | tests/test_store_scoring.py |
| FR-5.1 5-tier RBAC | app/rbac.py, app/main.py | tests/test_rbac_standings.py |
| NFR-3 Concurrency | app/store.py | tests/test_store_scoring.py::test_concurrent_last_unit |
| NFR-4 Offline sync | app/sync.py | tests/test_roster_sync.py::test_offline_sync |
| NFR-5 Security (JWT, rate limit, body size) | app/rbac.py, app/main.py | tests/test_rbac_standings.py |
| NFR-7 Reproducibility | docker-compose.yml, Dockerfile | — |
