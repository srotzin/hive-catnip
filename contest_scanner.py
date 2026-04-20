"""
contest_scanner.py — HiveCatnip Audit Contest Monitor
======================================================
Monitors Code4rena and Sherlock for open audit contests.
For each active contest:
  1. Fetches the contest's GitHub repo / scoped contracts
  2. Runs the same static analysis engine as bounty_scanner.py
  3. Generates contest-formatted finding reports
  4. Tracks which contests have been entered

This is the fastest path to first revenue — contest pools are
pre-committed, payouts in 2-3 weeks, no Immunefi review queue.

Payout model:
  Code4rena:  H/M findings share the prize pool proportionally
  Sherlock:   Fixed payouts per severity (H: up to $5K-$50K, M: $500-$5K)

Steve reviews findings → submits manually to contest platform.
"""

import re
import json
import time
import threading
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from bounty_scanner import (
    analyze_contract,
    append_finding,
    _scanner_lock,
    _scanner_state,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger("catnip-contest")
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[catnip-contest] %(levelname)s %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ── Persistence ───────────────────────────────────────────────────────────────
CONTESTS_FILE   = "/tmp/catnip-contests.ndjson"   # active + completed contests
CONTEST_STATE   = "/tmp/catnip-contest-state.json" # which contest IDs we've seen
CONTEST_REPORTS = "/tmp/catnip-contest-reports.ndjson"  # formatted contest reports

# ── Contest state ─────────────────────────────────────────────────────────────
_contest_state = {
    "running": False,
    "last_scan_time": None,
    "contests_found": 0,
    "contests_scanned": 0,
    "findings_total": 0,
    "start_time": datetime.now(timezone.utc).isoformat(),
}
_contest_lock = threading.Lock()


# ── Persistence helpers ───────────────────────────────────────────────────────

def _load_contest_state() -> dict:
    try:
        return json.loads(Path(CONTEST_STATE).read_text())
    except Exception:
        return {"seen_contest_ids": [], "last_checked": None}

def _save_contest_state(state: dict):
    try:
        Path(CONTEST_STATE).write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.warning(f"Could not save contest state: {e}")

def _append_contest(contest: dict):
    try:
        with open(CONTESTS_FILE, "a") as f:
            f.write(json.dumps(contest) + "\n")
    except Exception as e:
        logger.warning(f"Could not append contest: {e}")

def _append_report(report: dict):
    try:
        with open(CONTEST_REPORTS, "a") as f:
            f.write(json.dumps(report) + "\n")
    except Exception as e:
        logger.warning(f"Could not append report: {e}")

def load_contests(limit: int = 20) -> list:
    contests = []
    try:
        with open(CONTESTS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        contests.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return list(reversed(contests))[:limit]

def load_contest_reports(limit: int = 20) -> list:
    reports = []
    try:
        with open(CONTEST_REPORTS, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        reports.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return list(reversed(reports))[:limit]


# ── Code4rena API ─────────────────────────────────────────────────────────────

def fetch_code4rena_contests(client: httpx.Client) -> list[dict]:
    """
    Fetch active/upcoming contests from Code4rena.
    Primary: https://code4rena.com/contests (HTML scrape for contest list)
    Secondary: GitHub API for c4-findings repos
    """
    contests = []

    # Try Code4rena public API / graph endpoint
    urls_to_try = [
        "https://code4rena.com/api/contests",
        "https://raw.githubusercontent.com/code-423n4/code423n4.com/main/_data/contests/contests.json",
    ]

    for url in urls_to_try:
        try:
            r = client.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                # Handle both list and {contests: [...]} formats
                if isinstance(data, list):
                    raw = data
                elif isinstance(data, dict):
                    raw = data.get("contests", data.get("data", []))
                else:
                    continue

                now = datetime.now(timezone.utc).timestamp()
                for c in raw:
                    start = c.get("start_time") or c.get("start") or c.get("startDate", 0)
                    end   = c.get("end_time")   or c.get("end")   or c.get("endDate", 0)
                    # Normalize timestamps
                    if isinstance(start, str):
                        try:
                            from datetime import datetime as dt
                            start = dt.fromisoformat(start.replace("Z","+00:00")).timestamp()
                        except Exception:
                            start = 0
                    if isinstance(end, str):
                        try:
                            from datetime import datetime as dt
                            end = dt.fromisoformat(end.replace("Z","+00:00")).timestamp()
                        except Exception:
                            end = 0

                    # Only active (started, not ended)
                    if start and end and start <= now <= end:
                        repo = c.get("repo") or c.get("findingsRepo") or c.get("contestUrl", "")
                        prize = c.get("total_prize") or c.get("prize") or c.get("prizePool", "unknown")
                        contests.append({
                            "id":       c.get("id") or c.get("contestid") or c.get("title",""),
                            "platform": "code4rena",
                            "name":     c.get("title") or c.get("name", ""),
                            "repo":     repo,
                            "prize":    prize,
                            "end_time": end,
                            "url":      f"https://code4rena.com/contests/{c.get('id','')}",
                            "scope":    c.get("scope", []),
                        })
                logger.info(f"[c4] Found {len(contests)} active contests from {url}")
                if contests:
                    return contests
        except Exception as e:
            logger.debug(f"[c4] {url} failed: {e}")

    # Fallback: search GitHub for recently created c4-* repos with open issues
    try:
        r = client.get(
            "https://api.github.com/search/repositories"
            "?q=org:code-423n4+created:>2026-04-01&sort=updated&order=desc&per_page=10",
            headers={"Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if r.status_code == 200:
            for repo in r.json().get("items", []):
                name = repo.get("name", "")
                if re.match(r"^\d{4}-\d{2}", name):  # contest repos are YYYY-MM-name
                    contests.append({
                        "id":       name,
                        "platform": "code4rena",
                        "name":     name,
                        "repo":     repo.get("html_url", ""),
                        "prize":    "unknown",
                        "end_time": 0,
                        "url":      f"https://code4rena.com/contests/{name}",
                        "scope":    [],
                        "github_repo": repo.get("full_name", ""),
                    })
            logger.info(f"[c4] GitHub fallback: {len(contests)} recent c4 repos")
    except Exception as e:
        logger.debug(f"[c4] GitHub fallback failed: {e}")

    return contests


def fetch_sherlock_contests(client: httpx.Client) -> list[dict]:
    """
    Fetch active contests from Sherlock.
    Primary: https://app.sherlock.xyz/audits/contests (JSON API)
    """
    contests = []

    urls = [
        "https://mainnet-contest.sherlock.xyz/contests",
        "https://app.sherlock.xyz/api/contests",
    ]

    for url in urls:
        try:
            r = client.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                raw = data if isinstance(data, list) else data.get("contests", [])
                now = datetime.now(timezone.utc).timestamp()
                for c in raw:
                    status = c.get("status", "").lower()
                    if status in ("active", "running", "judging", "open"):
                        end = c.get("ends_at") or c.get("end_time", 0)
                        if isinstance(end, str):
                            try:
                                from datetime import datetime as dt
                                end = dt.fromisoformat(end.replace("Z","+00:00")).timestamp()
                            except Exception:
                                end = 0
                        contests.append({
                            "id":       str(c.get("id", c.get("title",""))),
                            "platform": "sherlock",
                            "name":     c.get("title") or c.get("name", ""),
                            "repo":     c.get("template_repo_name") or c.get("repo", ""),
                            "prize":    c.get("prize_pool") or c.get("rewards_usdc", "unknown"),
                            "end_time": end,
                            "url":      f"https://app.sherlock.xyz/audits/contests/{c.get('id','')}",
                            "scope":    c.get("scope", []),
                        })
                logger.info(f"[sherlock] Found {len(contests)} active contests")
                if contests:
                    return contests
        except Exception as e:
            logger.debug(f"[sherlock] {url} failed: {e}")

    # Fallback: GitHub org search
    try:
        r = client.get(
            "https://api.github.com/search/repositories"
            "?q=org:sherlock-audit+created:>2026-04-01&sort=updated&order=desc&per_page=10",
            headers={"Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if r.status_code == 200:
            for repo in r.json().get("items", []):
                contests.append({
                    "id":          repo.get("name",""),
                    "platform":    "sherlock",
                    "name":        repo.get("name",""),
                    "repo":        repo.get("html_url",""),
                    "prize":       "unknown",
                    "end_time":    0,
                    "url":         f"https://app.sherlock.xyz/audits/contests",
                    "scope":       [],
                    "github_repo": repo.get("full_name",""),
                })
            logger.info(f"[sherlock] GitHub fallback: {len(contests)} repos")
    except Exception as e:
        logger.debug(f"[sherlock] GitHub fallback failed: {e}")

    return contests


# ── Contract source fetcher ───────────────────────────────────────────────────

def fetch_contracts_for_contest(contest: dict, client: httpx.Client) -> list[dict]:
    """
    Given a contest dict, find Solidity contracts to scan.
    Returns list of {address_or_path, source_code, contract_name}
    """
    contracts = []
    github_repo = contest.get("github_repo") or contest.get("repo", "")

    # Extract org/repo from GitHub URL
    m = re.search(r"github\.com/([^/]+/[^/]+)", github_repo)
    if not m:
        return contracts
    repo_path = m.group(1).rstrip(".git")

    # Fetch file tree via GitHub API
    try:
        r = client.get(
            f"https://api.github.com/repos/{repo_path}/git/trees/HEAD?recursive=1",
            headers={"Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if r.status_code != 200:
            return contracts

        tree = r.json().get("tree", [])
        sol_files = [
            f for f in tree
            if f.get("path","").endswith(".sol")
            and "/test/" not in f.get("path","")
            and "/mock" not in f.get("path","").lower()
            and "/lib/" not in f.get("path","")
            and f.get("type") == "blob"
        ]

        # Limit to 20 most interesting files (src/ or contracts/)
        priority = [f for f in sol_files if "/src/" in f.get("path","") or "/contracts/" in f.get("path","")]
        to_fetch = (priority or sol_files)[:20]

        for file_info in to_fetch:
            path = file_info.get("path","")
            try:
                raw_url = f"https://raw.githubusercontent.com/{repo_path}/HEAD/{path}"
                rc = client.get(raw_url, timeout=10)
                if rc.status_code == 200:
                    contracts.append({
                        "address_or_path": path,
                        "source_code":     rc.text,
                        "contract_name":   Path(path).stem,
                        "repo":            repo_path,
                        "file_path":       path,
                    })
            except Exception as e:
                logger.debug(f"Could not fetch {path}: {e}")

        logger.info(f"[contest] Fetched {len(contracts)} contracts from {repo_path}")
    except Exception as e:
        logger.warning(f"[contest] Could not fetch contracts for {repo_path}: {e}")

    return contracts


# ── Contest report formatter ──────────────────────────────────────────────────

def format_contest_report(finding: dict, contest: dict) -> dict:
    """
    Format a finding as a contest submission report.
    Code4rena format: https://docs.code4rena.com/roles/wardens/submission-policy
    Sherlock format:  https://docs.sherlock.xyz/audits/watsons/how-to-submit
    """
    severity_map = {
        "Critical": {"c4": "H", "sherlock": "High"},
        "High":     {"c4": "H", "sherlock": "High"},
        "Medium":   {"c4": "M", "sherlock": "Medium"},
        "Low":      {"c4": "L", "sherlock": "Low"},
    }
    sev = finding.get("severity", "Medium")
    platform = contest.get("platform", "code4rena")
    contest_sev = severity_map.get(sev, {"c4":"M","sherlock":"Medium"})

    title = finding.get("title", "Vulnerability found")
    description = finding.get("description", "")
    poc = finding.get("proof_of_concept", "")
    fix = finding.get("recommended_fix", "")
    contract = finding.get("contract_address") or finding.get("address_or_path", "")

    if platform == "code4rena":
        # C4 markdown format
        body = f"""## {title}

**Severity:** {contest_sev['c4']}
**Contract:** `{contract}`

### Description

{description}

### Proof of Concept

{poc}

### Recommended Mitigation

{fix}

---
*Found by HiveCatnip Autonomous Scanner | did:hive:hive-catnip*
"""
    else:
        # Sherlock format
        body = f"""**Severity:** {contest_sev['sherlock']}

**Summary:** {title}

**Vulnerability Detail:**
{description}

**Impact:**
{finding.get('impact', 'Potential loss of funds or unauthorized access.')}

**Code Snippet:**
`{contract}`

**Tool used:** HiveCatnip Static Analyzer

**Recommendation:**
{fix}
"""

    return {
        "contest_id":      contest.get("id"),
        "contest_name":    contest.get("name"),
        "platform":        platform,
        "contest_url":     contest.get("url"),
        "prize_pool":      contest.get("prize"),
        "severity":        sev,
        "platform_severity": contest_sev.get(platform[:2] if platform == "code4rena" else "sherlock"),
        "title":           title,
        "contract":        contract,
        "submission_body": body,
        "raw_finding":     finding,
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "status":          "ready_to_submit",
        "submit_url":      contest.get("url"),
        "hive_did":        "did:hive:hive-catnip",
    }


# ── Main contest scan loop ────────────────────────────────────────────────────

def run_contest_scanner():
    """Background thread — runs every 30 minutes."""
    logger.info("[contest] Scanner started")
    with _contest_lock:
        _contest_state["running"] = True

    while True:
        try:
            _scan_contests_once()
        except Exception as e:
            logger.error(f"[contest] Unhandled error in scan loop: {e}")
        time.sleep(1800)  # 30 minutes


def _scan_contests_once():
    state = _load_contest_state()
    seen_ids = set(state.get("seen_contest_ids", []))
    new_reports = 0

    with httpx.Client(
        headers={"User-Agent": "HiveCatnip/1.0 (security-research; did:hive:hive-catnip)"},
        follow_redirects=True,
    ) as client:

        # Fetch active contests from both platforms
        c4_contests = fetch_code4rena_contests(client)
        sherlock_contests = fetch_sherlock_contests(client)
        all_contests = c4_contests + sherlock_contests

        with _contest_lock:
            _contest_state["contests_found"] = len(all_contests)
        logger.info(f"[contest] {len(c4_contests)} C4 + {len(sherlock_contests)} Sherlock = {len(all_contests)} active")

        for contest in all_contests:
            cid = contest.get("id","")
            contest_key = f"{contest['platform']}:{cid}"

            # Log contest regardless of whether we've scanned it
            _append_contest({**contest, "logged_at": datetime.now(timezone.utc).isoformat()})

            if contest_key in seen_ids:
                logger.debug(f"[contest] Already scanned {contest_key}, skipping")
                continue

            logger.info(f"[contest] Scanning {contest['platform']} — {contest['name']} (prize: {contest['prize']})")

            # Fetch contracts
            contracts = fetch_contracts_for_contest(contest, client)
            if not contracts:
                logger.info(f"[contest] No contracts found for {contest_key}")
                seen_ids.add(contest_key)
                continue

            # Analyze each contract
            contest_findings = 0
            for contract_info in contracts:
                source = contract_info.get("source_code", "")
                if len(source) < 50:
                    continue

                findings = analyze_contract(
                    address=contract_info.get("address_or_path", "unknown"),
                    source_code=source,
                    contract_name=contract_info.get("contract_name", "Unknown"),
                )

                # Only P0/P1 for contests — Medium+ only
                real_findings = [f for f in findings if f.get("severity") in ("Critical","High","Medium")]

                for finding in real_findings:
                    # Enrich finding with contest context
                    finding["contest_id"]   = cid
                    finding["contest_name"] = contest.get("name","")
                    finding["platform"]     = contest.get("platform","")
                    finding["file_path"]    = contract_info.get("file_path","")

                    # Append to main findings log
                    append_finding(finding)

                    # Generate contest-formatted report
                    report = format_contest_report(finding, contest)
                    _append_report(report)

                    contest_findings += 1
                    with _contest_lock:
                        _contest_state["findings_total"] += 1

                    logger.info(
                        f"[contest] {finding['severity']} in {contract_info['contract_name']} "
                        f"— {contest['name']} ({contest['platform']})"
                    )

            logger.info(f"[contest] {contest_key}: {len(contracts)} contracts, {contest_findings} findings")
            seen_ids.add(contest_key)
            with _contest_lock:
                _contest_state["contests_scanned"] += 1

    # Save updated state
    state["seen_contest_ids"] = list(seen_ids)
    state["last_checked"] = datetime.now(timezone.utc).isoformat()
    _save_contest_state(state)

    with _contest_lock:
        _contest_state["last_scan_time"] = datetime.now(timezone.utc).isoformat()


def get_contest_stats() -> dict:
    with _contest_lock:
        return {**_contest_state}


def start_contest_scanner():
    t = threading.Thread(target=run_contest_scanner, daemon=True, name="contest-scanner")
    t.start()
    logger.info("[contest] Background thread started")
    return t
