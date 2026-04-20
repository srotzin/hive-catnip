"""
bounty_routes.py — FastAPI router for /v1/catnip/bounty/* endpoints
"""

import threading
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bounty_scanner import (
    load_findings,
    load_findings_for_address,
    get_scanner_stats,
    scan_single_contract,
    _scanner_state,
    _scanner_lock,
)

router = APIRouter(prefix="/v1/catnip/bounty", tags=["bounty"])


# ── Request models ────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    address: str


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/findings")
async def get_findings():
    """
    Returns last 20 findings from the NDJSON file, newest first.
    """
    findings = load_findings(limit=20)
    return JSONResponse({
        "status": "ok",
        "count": len(findings),
        "findings": findings,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@router.get("/findings/{address}")
async def get_findings_for_address(address: str):
    """
    Returns all findings for a specific contract address.
    """
    findings = load_findings_for_address(address)
    return JSONResponse({
        "status": "ok",
        "address": address,
        "count": len(findings),
        "findings": findings,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@router.get("/stats")
async def get_stats():
    """
    Returns total contracts scanned, findings by severity, uptime, last scan time.
    """
    stats = get_scanner_stats()
    return JSONResponse({
        "status": "ok",
        "scanner": stats,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@router.post("/scan")
async def manual_scan(body: ScanRequest):
    """
    Manually trigger a scan of a specific contract address.
    Runs scan in a background thread and returns immediately with job info.
    """
    address = body.address.strip()
    if not address.startswith("0x") or len(address) != 42:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "message": "Invalid Ethereum address. Must be 0x-prefixed, 42 chars.",
            }
        )

    # Run in a background thread to avoid blocking the event loop
    def _run():
        try:
            scan_single_contract(address)
        except Exception as e:
            import logging
            logging.getLogger("catnip-bounty").error(f"Manual scan error: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return JSONResponse({
        "status": "accepted",
        "message": f"Scan of {address} started in background.",
        "address": address,
        "check_results": f"/v1/catnip/bounty/findings/{address}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@router.get("/status")
async def scanner_status():
    """
    Returns scanner health: running state, last run time, contracts scanned.
    """
    with _scanner_lock:
        state_copy = dict(_scanner_state)

    return JSONResponse({
        "status": "ok",
        "scanner_running": state_copy.get("running", False),
        "last_scan_time": state_copy.get("last_scan_time"),
        "contracts_scanned": state_copy.get("contracts_scanned", 0),
        "findings_total": state_copy.get("findings_total", 0),
        "uptime_since": state_copy.get("start_time"),
        "last_error": state_copy.get("last_error"),
        "service": "HiveCatnip Autonomous Bug Bounty Scanner",
        "chain": "Base Mainnet",
        "poll_interval_seconds": 300,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
