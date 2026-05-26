"""Per-feature FastAPI routers.

The main app module `web/api.py` got big enough (~1700 lines, 21
endpoints) that small changes started conflicting in PRs. This
package splits the API into focused routers:

  google.py    -- Gmail + Drive OAuth (4 endpoints)

Each router is imported + included by `web/api.py`. Routers depend
on `web/deps.py` for shared helpers (require_auth, _engine_and_ws,
etc.) so they don't import back into the main app module.

Migration is incremental. Endpoints not yet extracted remain in
`web/api.py` and behave identically. New endpoints should be
added to the most-relevant router rather than the main module.
"""
