"""
contest_routes.py — FastAPI router for /v1/catnip/contest/*
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from contest_scanner import (
    load_contests,
    load_contest_reports,
    get_contest_stats,
    _scan_contests_once,
    start_contest_scanner,
)

router = APIRouter(prefix="/v1/catnip/contest", tags=["contest"])


class ScanContestRequest(BaseModel):
    force: bool = False


# GET /v1/catnip/contest/active
# ── All active contests we're monitoring
@router.get("/active")
async def get_active_contests():
    contests = load_contests(limit=50)
    # Deduplicate by id
    seen = set()
    unique = []
    for c in contests:
        key = f"{c.get('platform')}:{c.get('id')}"
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return JSONResponse({
        "status": "ok",
        "total": len(unique),
        "contests": unique,
        "note": "Polls Code4rena and Sherlock every 30 minutes",
    })


# GET /v1/catnip/contest/reports
# ── Contest-formatted finding reports ready to submit
@router.get("/reports")
async def get_contest_reports():
    reports = load_contest_reports(limit=50)
    ready = [r for r in reports if r.get("status") == "ready_to_submit"]
    return JSONResponse({
        "status": "ok",
        "total_reports": len(reports),
        "ready_to_submit": len(ready),
        "reports": ready,
        "note": "Review each report — Steve submits manually to contest platform",
    })


# GET /v1/catnip/contest/reports/{contest_id}
# ── Reports for a specific contest
@router.get("/reports/{contest_id}")
async def get_reports_for_contest(contest_id: str):
    all_reports = load_contest_reports(limit=200)
    filtered = [r for r in all_reports if r.get("contest_id") == contest_id]
    return JSONResponse({
        "status": "ok",
        "contest_id": contest_id,
        "count": len(filtered),
        "reports": filtered,
    })


# GET /v1/catnip/contest/stats
# ── Scanner health and stats
@router.get("/stats")
async def get_stats():
    stats = get_contest_stats()
    return JSONResponse({
        "status": "ok",
        "scanner": stats,
        "platforms": ["code4rena", "sherlock"],
        "poll_interval_minutes": 30,
        "note": "H/M findings on C4, High/Medium on Sherlock. Steve reviews and submits.",
    })


# POST /v1/catnip/contest/scan
# ── Manual trigger — force a fresh scan right now
@router.post("/scan")
async def trigger_scan():
    import threading
    def _run():
        try:
            _scan_contests_once()
        except Exception as e:
            pass
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return JSONResponse({
        "status": "ok",
        "message": "Contest scan triggered — check /v1/catnip/contest/reports in ~2 minutes",
    })
