"""
bounty_scanner.py — HiveCatnip Autonomous Bug Bounty Scanner
Polls Base mainnet for new verified contracts, runs static analysis,
generates Immunefi-compatible vulnerability reports.
"""

import re
import json
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

# ── Logging ──────────────────────────────────────────────────────────────────
logger = logging.getLogger("catnip-bounty")
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[catnip-bounty] %(levelname)s %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ── Persistence paths ─────────────────────────────────────────────────────────
FINDINGS_FILE = "/tmp/catnip-findings.ndjson"
STATE_FILE = "/tmp/catnip-state.json"

# ── Scanner state (in-memory) ─────────────────────────────────────────────────
_scanner_state = {
    "running": False,
    "last_scan_time": None,
    "contracts_scanned": 0,
    "findings_total": 0,
    "start_time": datetime.now(timezone.utc).isoformat(),
    "last_error": None,
}
_scanner_lock = threading.Lock()


# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"seen_addresses": [], "last_scan_timestamp": None}


def save_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.warning(f"Could not save state: {e}")


# ── Findings persistence ──────────────────────────────────────────────────────

def append_finding(finding: dict):
    try:
        with open(FINDINGS_FILE, "a") as f:
            f.write(json.dumps(finding) + "\n")
        with _scanner_lock:
            _scanner_state["findings_total"] += 1
    except Exception as e:
        logger.warning(f"Could not append finding: {e}")


def load_findings(limit: int = 20) -> list:
    findings = []
    try:
        with open(FINDINGS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        findings.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"Could not load findings: {e}")
    # Newest first
    return list(reversed(findings))[:limit]


def load_findings_for_address(address: str) -> list:
    address_lower = address.lower()
    results = []
    try:
        with open(FINDINGS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        finding = json.loads(line)
                        if finding.get("contract_address", "").lower() == address_lower:
                            results.append(finding)
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"Could not load findings for address: {e}")
    return list(reversed(results))


def count_findings_by_severity() -> dict:
    counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Info": 0}
    total = 0
    try:
        with open(FINDINGS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        finding = json.loads(line)
                        sev = finding.get("severity", "Info")
                        counts[sev] = counts.get(sev, 0) + 1
                        total += 1
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return counts, total


# ── Contract discovery ────────────────────────────────────────────────────────

def fetch_blockscout_contracts(seen: set) -> list:
    """
    Fetch recently verified contracts from Blockscout Base.
    Returns list of new address strings.
    """
    url = (
        "https://base.blockscout.com/api/v2/smart-contracts"
        "?filter=verified&sort=inserted_at&order=desc"
    )
    new_addresses = []
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(url)
            if r.status_code == 200:
                data = r.json()
                items = data.get("items", [])
                for item in items:
                    addr = item.get("address", {})
                    if isinstance(addr, dict):
                        addr = addr.get("hash", "")
                    if isinstance(addr, str) and addr.startswith("0x"):
                        if addr.lower() not in seen:
                            new_addresses.append(addr)
            else:
                logger.warning(f"Blockscout contract list returned {r.status_code}")
    except Exception as e:
        logger.warning(f"Blockscout contract list error: {e}")
    return new_addresses


def fetch_basescan_txlist(seen: set) -> list:
    """
    Fallback: fetch recent txns from zero address, pick contract creations
    (to= empty/null means contract creation).
    """
    url = (
        "https://api.basescan.org/api"
        "?module=account&action=txlist"
        "&address=0x0000000000000000000000000000000000000000"
        "&sort=desc&page=1&offset=20"
        "&apikey=freekey"
    )
    new_addresses = []
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(url)
            if r.status_code == 200:
                data = r.json()
                txs = data.get("result", [])
                if isinstance(txs, list):
                    for tx in txs:
                        # Contract creation: 'to' is empty/null, contractAddress filled
                        to_addr = tx.get("to", "")
                        contract_addr = tx.get("contractAddress", "")
                        if (not to_addr) and contract_addr and contract_addr.startswith("0x"):
                            if contract_addr.lower() not in seen:
                                new_addresses.append(contract_addr)
    except Exception as e:
        logger.warning(f"Basescan txlist error: {e}")
    return new_addresses


def discover_new_contracts(seen: set) -> list:
    """
    Try Blockscout first, fall back to Basescan txlist.
    Returns deduplicated list of new contract addresses.
    """
    addresses = fetch_blockscout_contracts(seen)
    if not addresses:
        logger.info("Blockscout returned no new contracts, trying Basescan fallback")
        addresses = fetch_basescan_txlist(seen)

    # Additional attempt via Basescan verified contracts endpoint
    try:
        url = (
            "https://api.basescan.org/api"
            "?module=contract&action=getverifiedcontractaddresses"
            "&apikey=freekey"
        )
        with httpx.Client(timeout=10.0) as client:
            r = client.get(url)
            if r.status_code == 200:
                data = r.json()
                result = data.get("result", [])
                if isinstance(result, list):
                    for item in result:
                        addr = item.get("ContractAddress", "")
                        if addr and addr.startswith("0x") and addr.lower() not in seen:
                            if addr not in addresses:
                                addresses.append(addr)
    except Exception as e:
        logger.warning(f"Basescan verified contracts error: {e}")

    return addresses


# ── Contract source fetch ─────────────────────────────────────────────────────

def fetch_contract_source(address: str) -> Optional[dict]:
    """
    Fetch contract source code from Blockscout.
    Returns dict with source_code, abi, contract_name, compiler_version.
    """
    url = f"https://base.blockscout.com/api/v2/smart-contracts/{address}"
    try:
        with httpx.Client(timeout=12.0) as client:
            r = client.get(url)
            if r.status_code == 200:
                data = r.json()
                source = (
                    data.get("source_code")
                    or data.get("sourcify_repo_url", "")
                )
                # Try additional_sources for multi-file contracts
                if not source:
                    add_sources = data.get("additional_sources", [])
                    if add_sources:
                        source = "\n\n".join(
                            s.get("source_code", "") for s in add_sources
                        )
                return {
                    "address": address,
                    "source_code": source or "",
                    "abi": data.get("abi", []),
                    "contract_name": data.get("name", "UnknownContract"),
                    "compiler_version": data.get("compiler_version", "unknown"),
                    "raw": data,
                }
            elif r.status_code == 404:
                logger.debug(f"Contract {address} not found on Blockscout")
            else:
                logger.warning(f"Blockscout source fetch {address} returned {r.status_code}")
    except Exception as e:
        logger.warning(f"Error fetching source for {address}: {e}")
    return None


# ── Static analysis ───────────────────────────────────────────────────────────

# Severity labels
P0 = "Critical"
P1 = "High"
P2 = "Medium"


def _find_functions(source: str) -> list:
    """
    Crude split of source into function blocks.
    Returns list of (function_name, function_body) tuples.
    """
    functions = []
    # Match 'function NAME(...) ... { ... }' — simplified
    fn_pattern = re.compile(
        r'function\s+(\w+)\s*\([^)]*\)[^{]*\{',
        re.DOTALL
    )
    for m in fn_pattern.finditer(source):
        fn_name = m.group(1)
        start = m.start()
        # Find matching closing brace
        depth = 0
        i = m.end() - 1  # starts at the opening {
        while i < len(source):
            if source[i] == '{':
                depth += 1
            elif source[i] == '}':
                depth -= 1
                if depth == 0:
                    functions.append((fn_name, source[start:i+1]))
                    break
            i += 1
    return functions


def check_reentrancy(source: str) -> list:
    """
    Detect potential reentrancy: external call before state variable write
    in the same function.
    """
    findings = []
    # Patterns for external calls
    call_pattern = re.compile(
        r'\.call\{value:|\.transfer\(|\.send\(',
        re.IGNORECASE
    )
    # State write patterns (simplified): assignment to storage-like vars after the call
    state_write_pattern = re.compile(
        r'\b(\w+)\s*[\[\(]?[^\n]*[\]\)]?\s*=\s*(?!>)',
        re.MULTILINE
    )

    functions = _find_functions(source)
    for fn_name, fn_body in functions:
        call_match = call_pattern.search(fn_body)
        if not call_match:
            continue
        # Find state writes after the call
        after_call = fn_body[call_match.end():]
        write_match = state_write_pattern.search(after_call)
        if write_match:
            findings.append({
                "check": "reentrancy",
                "severity": P0,
                "title": f"Potential reentrancy in {fn_name}()",
                "description": (
                    f"External call (.call{{value:}}, .transfer(), or .send()) "
                    f"found before a state variable write in function '{fn_name}'. "
                    "This pattern is vulnerable to reentrancy attacks where a malicious "
                    "contract can re-enter before state is updated."
                ),
                "proof_of_concept": (
                    f"1. Deploy attacker contract with fallback that calls back into {fn_name}.\n"
                    "2. Call the vulnerable function.\n"
                    "3. During the external call, the fallback re-enters before balances are updated.\n"
                    "4. Repeat to drain funds."
                ),
                "recommended_fix": (
                    "Apply checks-effects-interactions pattern: update all state variables "
                    "BEFORE making external calls. Consider using OpenZeppelin ReentrancyGuard."
                ),
                "location": f"function {fn_name}",
            })
    return findings


def check_unchecked_return(source: str) -> list:
    """
    Detect .call( without checking the return bool.
    """
    findings = []
    # Look for .call( not preceded by (bool ... =) or similar
    # Pattern: .call( not followed immediately by a bool-capture
    unchecked_call = re.compile(
        r'(?<!\(bool\s)(?<!\w,\s)\.call\s*\(',
        re.MULTILINE
    )
    # Better heuristic: lines containing .call( where the line doesn't have bool capture
    for line_num, line in enumerate(source.split('\n'), 1):
        # Skip comments
        stripped = line.strip()
        if stripped.startswith('//') or stripped.startswith('*'):
            continue
        if '.call(' in line or '.call{' in line:
            # Check if return value is captured
            if not re.search(r'\(bool\s+\w+', line) and \
               not re.search(r'bool\s+\w+\s*=', line) and \
               not re.search(r'\(\s*bool\s*,', line):
                findings.append({
                    "check": "unchecked_return_value",
                    "severity": P0,
                    "title": "Unchecked return value from low-level call",
                    "description": (
                        f"Line {line_num}: A low-level `.call()` is made without checking "
                        "its boolean return value. If the call fails silently, the contract "
                        "may continue execution with invalid assumptions."
                    ),
                    "proof_of_concept": (
                        "Call the function. If the .call() target reverts, the calling "
                        "function continues without detecting the failure, potentially "
                        "leading to state inconsistency or fund loss."
                    ),
                    "recommended_fix": (
                        "Always check the return value: `(bool success, ) = addr.call{...}(data); "
                        "require(success, 'Call failed');`"
                    ),
                    "location": f"line {line_num}: {stripped[:100]}",
                })
            break  # One finding per contract for this check
    return findings


def check_integer_overflow(source: str) -> list:
    """
    Detect integer overflow risk in pre-0.8 contracts without SafeMath.
    """
    findings = []
    old_pragma = re.search(
        r'pragma\s+solidity\s+[\^~]?(0\.[4-7])\.',
        source
    )
    if old_pragma:
        version = old_pragma.group(1)
        has_safemath = bool(re.search(
            r'SafeMath|using\s+SafeMath\s+for',
            source,
            re.IGNORECASE
        ))
        if not has_safemath:
            findings.append({
                "check": "integer_overflow",
                "severity": P0,
                "title": f"Integer overflow risk — Solidity {version}.x without SafeMath",
                "description": (
                    f"Contract uses Solidity {version}.x which does not have built-in "
                    "overflow/underflow protection. No SafeMath library import detected. "
                    "Arithmetic operations may silently overflow or underflow."
                ),
                "proof_of_concept": (
                    "Call any function performing arithmetic on uint/int values with "
                    "boundary inputs (e.g., uint max value + 1 wraps to 0). "
                    "This can be exploited to bypass balance checks or inflate token balances."
                ),
                "recommended_fix": (
                    "Upgrade to Solidity ^0.8.0 for built-in overflow protection, "
                    "or use OpenZeppelin SafeMath: `using SafeMath for uint256;`"
                ),
                "location": f"pragma solidity {version}.x",
            })
    return findings


def check_selfdestruct(source: str) -> list:
    """
    Flag any use of selfdestruct — always high severity.
    """
    findings = []
    pattern = re.compile(r'\bselfdestruct\s*\(', re.IGNORECASE)
    functions = _find_functions(source)
    flagged_fns = set()
    for fn_name, fn_body in functions:
        if pattern.search(fn_body) and fn_name not in flagged_fns:
            flagged_fns.add(fn_name)
            findings.append({
                "check": "selfdestruct",
                "severity": P0,
                "title": f"selfdestruct() present in {fn_name}()",
                "description": (
                    f"Function '{fn_name}' contains a selfdestruct() call. "
                    "This permanently destroys the contract and sends all ETH to a specified address. "
                    "If the function is accessible to an attacker, all funds can be drained "
                    "and the contract permanently disabled."
                ),
                "proof_of_concept": (
                    "If selfdestruct is accessible without sufficient access control, "
                    "call the function to destroy the contract and redirect all ETH."
                ),
                "recommended_fix": (
                    "Remove selfdestruct if not essential. If needed, protect with "
                    "multi-sig or timelock. Consider OpenZeppelin's Pausable pattern instead."
                ),
                "location": f"function {fn_name}",
            })
    # Also check outside functions
    if pattern.search(source) and not flagged_fns:
        findings.append({
            "check": "selfdestruct",
            "severity": P0,
            "title": "selfdestruct() present in contract",
            "description": (
                "selfdestruct() found in contract source. If accessible without "
                "adequate access control, this can permanently destroy the contract."
            ),
            "proof_of_concept": "Trigger the selfdestruct path to destroy contract and drain ETH.",
            "recommended_fix": (
                "Remove selfdestruct or protect with strict multi-sig and timelock."
            ),
            "location": "contract body",
        })
    return findings


def check_tx_origin(source: str) -> list:
    """
    Detect tx.origin used for authentication.
    """
    findings = []
    pattern = re.compile(r'require\s*\(\s*tx\.origin', re.IGNORECASE)
    for line_num, line in enumerate(source.split('\n'), 1):
        stripped = line.strip()
        if stripped.startswith('//') or stripped.startswith('*'):
            continue
        if pattern.search(line):
            findings.append({
                "check": "tx_origin_auth",
                "severity": P0,
                "title": "tx.origin used for authentication",
                "description": (
                    f"Line {line_num}: `require(tx.origin == ...)` is used for authentication. "
                    "tx.origin returns the original external account that initiated the transaction, "
                    "making it vulnerable to phishing attacks via malicious contracts."
                ),
                "proof_of_concept": (
                    "1. Deploy attacker contract.\n"
                    "2. Trick the owner into calling attacker contract.\n"
                    "3. Attacker contract calls vulnerable contract — tx.origin is still owner.\n"
                    "4. Authentication bypassed."
                ),
                "recommended_fix": (
                    "Replace `tx.origin` with `msg.sender` for authentication checks."
                ),
                "location": f"line {line_num}: {stripped[:100]}",
            })
            break
    return findings


def check_unprotected_initialize(source: str) -> list:
    """
    Detect initialize() function without onlyOwner or initializer modifier.
    """
    findings = []
    functions = _find_functions(source)
    for fn_name, fn_body in functions:
        if fn_name.lower() == "initialize":
            fn_sig_end = fn_body.find('{')
            fn_sig = fn_body[:fn_sig_end]
            has_protection = bool(re.search(
                r'\b(onlyOwner|initializer|onlyAdmin|onlyProxy)\b',
                fn_sig,
                re.IGNORECASE
            ))
            if not has_protection:
                findings.append({
                    "check": "unprotected_initialize",
                    "severity": P1,
                    "title": "Unprotected initialize() function",
                    "description": (
                        "Function 'initialize()' exists without 'onlyOwner' or 'initializer' "
                        "modifier. In upgradeable proxy patterns, this can allow anyone to "
                        "call initialize() and take ownership of the implementation contract."
                    ),
                    "proof_of_concept": (
                        "1. Find the implementation contract address behind the proxy.\n"
                        "2. Call initialize() directly on the implementation.\n"
                        "3. Become owner of the implementation.\n"
                        "4. Use delegatecall gadgets to drain or destroy the proxy."
                    ),
                    "recommended_fix": (
                        "Add OpenZeppelin's `initializer` modifier from Initializable.sol, "
                        "or add `onlyOwner` if ownership is established before initialization. "
                        "Use `_disableInitializers()` in the implementation constructor."
                    ),
                    "location": "function initialize",
                })
    return findings


def check_flash_loan_callback(source: str) -> list:
    """
    Detect flash loan callback functions without proper sender validation.
    """
    findings = []
    callback_names = ['uniswapV2Call', 'flashLoan', 'onFlashLoan', 'executeOperation']
    functions = _find_functions(source)
    for fn_name, fn_body in functions:
        if fn_name in callback_names:
            has_sender_check = bool(re.search(
                r'require\s*\(\s*msg\.sender\s*==',
                fn_body,
                re.IGNORECASE
            ))
            if not has_sender_check:
                findings.append({
                    "check": "flash_loan_callback",
                    "severity": P1,
                    "title": f"Flash loan callback {fn_name}() lacks sender validation",
                    "description": (
                        f"Function '{fn_name}' is a flash loan callback but does not validate "
                        "msg.sender against the expected pool address. "
                        "An attacker can call this function directly to trigger arbitrary logic."
                    ),
                    "proof_of_concept": (
                        f"Call {fn_name}() directly without going through the flash loan pool. "
                        "The function executes privileged logic without verifying it was triggered "
                        "by the legitimate pool contract."
                    ),
                    "recommended_fix": (
                        f"Add: `require(msg.sender == POOL_ADDRESS, 'Unauthorized callback');` "
                        f"at the start of {fn_name}()."
                    ),
                    "location": f"function {fn_name}",
                })
    return findings


def check_dangerous_delegatecall(source: str) -> list:
    """
    Detect delegatecall to a potentially user-controlled address.
    """
    findings = []
    # Look for delegatecall where the target appears to be a variable (not a hardcoded address)
    delegatecall_pattern = re.compile(
        r'(\w+)\.delegatecall\s*\(',
        re.IGNORECASE
    )
    for line_num, line in enumerate(source.split('\n'), 1):
        stripped = line.strip()
        if stripped.startswith('//') or stripped.startswith('*'):
            continue
        m = delegatecall_pattern.search(line)
        if m:
            target = m.group(1)
            # If target is not a known safe pattern (not 'implementation', not 'this')
            if target.lower() not in ('implementation', 'logic', 'this'):
                findings.append({
                    "check": "dangerous_delegatecall",
                    "severity": P1,
                    "title": f"Potentially dangerous delegatecall to variable '{target}'",
                    "description": (
                        f"Line {line_num}: delegatecall is used with target '{target}'. "
                        "If this address is user-controlled or can be manipulated, "
                        "an attacker can execute arbitrary code in the context of this contract, "
                        "leading to complete compromise."
                    ),
                    "proof_of_concept": (
                        "If you can influence the target address (via storage manipulation or "
                        "initialization), deploy a malicious contract and have delegatecall "
                        "execute its code in the vulnerable contract's storage context."
                    ),
                    "recommended_fix": (
                        "Ensure the delegatecall target is a trusted, immutable address. "
                        "Use a whitelist or restrict target to a hardcoded/owner-set address "
                        "with proper access control on the setter."
                    ),
                    "location": f"line {line_num}: {stripped[:100]}",
                })
            break
    return findings


def check_arbitrary_external_call(source: str) -> list:
    """
    Detect .call(abi.encodeWithSelector(...)) with user-supplied target.
    """
    findings = []
    pattern = re.compile(
        r'\.call\s*\(\s*abi\.encodeWith(?:Selector|Signature)\s*\(',
        re.IGNORECASE
    )
    for line_num, line in enumerate(source.split('\n'), 1):
        stripped = line.strip()
        if stripped.startswith('//') or stripped.startswith('*'):
            continue
        if pattern.search(line):
            findings.append({
                "check": "arbitrary_external_call",
                "severity": P1,
                "title": "Arbitrary external call with encoded selector",
                "description": (
                    f"Line {line_num}: `.call(abi.encodeWithSelector(...))` detected. "
                    "If the call target address is user-supplied or derived from user input, "
                    "this allows attackers to make the contract call arbitrary functions "
                    "on arbitrary addresses."
                ),
                "proof_of_concept": (
                    "Supply a malicious contract address as the call target. "
                    "The contract will execute arbitrary code at attacker's address, "
                    "potentially draining funds or manipulating state."
                ),
                "recommended_fix": (
                    "Whitelist allowed target addresses. Never allow user-supplied "
                    "addresses as call targets. Validate that target is a trusted contract."
                ),
                "location": f"line {line_num}: {stripped[:100]}",
            })
            break
    return findings


def check_centralization_risk(source: str) -> list:
    """
    Flag single EOA owner patterns combined with selfdestruct or withdrawAll.
    """
    findings = []
    has_owner = bool(re.search(r'\bowner\b', source, re.IGNORECASE))
    has_selfdestruct = bool(re.search(r'\bselfdestruct\s*\(', source, re.IGNORECASE))
    has_withdraw_all = bool(re.search(
        r'function\s+withdraw(?:All|Funds|Everything)?\s*\(',
        source,
        re.IGNORECASE
    ))
    onlyowner_pattern = re.search(r'\bonlyOwner\b', source)

    if has_owner and (has_selfdestruct or has_withdraw_all) and onlyowner_pattern:
        findings.append({
            "check": "centralization_risk",
            "severity": P2,
            "title": "Centralization risk: single owner can drain/destroy contract",
            "description": (
                "Contract has a single owner with ability to call selfdestruct() or "
                "withdrawAll(). If the owner key is compromised, all funds can be "
                "drained or the contract permanently destroyed. "
                "Users must fully trust the owner."
            ),
            "proof_of_concept": (
                "If owner key is compromised or owner is malicious, call the "
                "privileged function to drain all funds or destroy the contract."
            ),
            "recommended_fix": (
                "Replace single owner with multi-sig (e.g., Gnosis Safe). "
                "Add timelock for critical operations. "
                "Consider governance mechanisms for large fund movements."
            ),
            "location": "contract-wide",
        })
    return findings


def check_unprotected_mint(source: str) -> list:
    """
    Detect mint() without access control.
    """
    findings = []
    functions = _find_functions(source)
    for fn_name, fn_body in functions:
        if 'mint' in fn_name.lower():
            fn_sig_end = fn_body.find('{')
            fn_sig = fn_body[:fn_sig_end]
            has_protection = bool(re.search(
                r'\b(onlyOwner|onlyMinter|onlyRole|onlyAdmin|whenNotPaused\s+onlyOwner)\b',
                fn_sig,
                re.IGNORECASE
            ))
            if not has_protection:
                findings.append({
                    "check": "unprotected_mint",
                    "severity": P2,
                    "title": f"Potentially unprotected mint function: {fn_name}()",
                    "description": (
                        f"Function '{fn_name}' appears to mint tokens without "
                        "onlyOwner or onlyMinter access control modifier. "
                        "Anyone could potentially mint unlimited tokens."
                    ),
                    "proof_of_concept": (
                        f"Call {fn_name}() from any address. "
                        "If unprotected, mint unlimited tokens to attacker address, "
                        "causing token inflation and value destruction."
                    ),
                    "recommended_fix": (
                        f"Add `onlyOwner` or role-based access control to {fn_name}(). "
                        "Use OpenZeppelin AccessControl or Ownable."
                    ),
                    "location": f"function {fn_name}",
                })
    return findings


def analyze_contract(address: str, source_code: str, contract_name: str = "Unknown") -> list:
    """
    Public wrapper used by contest_scanner. Runs all static analysis checks
    and tags each finding with address and contract_name.
    """
    findings = run_static_analysis(source_code)
    for f in findings:
        f.setdefault("contract_address", address)
        f.setdefault("contract_name", contract_name)
    return findings


def run_static_analysis(source: str) -> list:
    """
    Run all static analysis checks and return list of findings.
    """
    if not source or len(source.strip()) < 10:
        return []

    all_findings = []
    checks = [
        check_reentrancy,
        check_unchecked_return,
        check_integer_overflow,
        check_selfdestruct,
        check_tx_origin,
        check_unprotected_initialize,
        check_flash_loan_callback,
        check_dangerous_delegatecall,
        check_arbitrary_external_call,
        check_centralization_risk,
        check_unprotected_mint,
    ]

    for check_fn in checks:
        try:
            results = check_fn(source)
            all_findings.extend(results)
        except Exception as e:
            logger.warning(f"Check {check_fn.__name__} failed: {e}")

    return all_findings


# ── LLM triage ────────────────────────────────────────────────────────────────

def generate_llm_report(contract_info: dict, findings: list) -> str:
    """
    Try to get a human-readable report from Hive internal LLM endpoint.
    Falls back to templated report.
    """
    contract_name = contract_info.get("contract_name", "Unknown")
    address = contract_info.get("address", "")
    summary_lines = []
    for f in findings:
        summary_lines.append(
            f"- [{f['severity']}] {f['title']}: {f['description'][:200]}"
        )
    findings_text = "\n".join(summary_lines)

    prompt = (
        f"You are a smart contract security researcher. "
        f"Analyze the following vulnerability findings for contract '{contract_name}' "
        f"at address {address} on Base mainnet:\n\n"
        f"{findings_text}\n\n"
        "Write a concise security report with: "
        "1) Executive summary, "
        "2) Key risks, "
        "3) Recommended immediate actions. "
        "Keep it under 300 words."
    )

    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(
                "https://hive-catnip.onrender.com/v1/hive/alpha/free",
                json={"message": prompt},
                headers={"Content-Type": "application/json"},
            )
            if r.status_code == 200:
                data = r.json()
                # Try to extract text from the response
                if isinstance(data, dict):
                    text = (
                        data.get("text")
                        or data.get("content")
                        or data.get("message")
                        or data.get("response")
                    )
                    if text and isinstance(text, str) and len(text) > 50:
                        return text
    except Exception as e:
        logger.debug(f"LLM triage request failed: {e}")

    # Fallback: generate templated report
    severity_counts = {}
    for f in findings:
        sev = f.get("severity", "Unknown")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    lines = [
        f"## Security Report: {contract_name}",
        f"**Contract:** {address}",
        f"**Chain:** Base Mainnet",
        f"**Total Findings:** {len(findings)}",
        "",
        "### Finding Summary",
    ]
    for sev, count in sorted(severity_counts.items()):
        lines.append(f"- **{sev}**: {count} finding(s)")
    lines.append("")
    lines.append("### Findings Detail")
    for i, f in enumerate(findings, 1):
        lines.append(f"\n#### {i}. [{f['severity']}] {f['title']}")
        lines.append(f"**Description:** {f['description']}")
        lines.append(f"**Recommended Fix:** {f['recommended_fix']}")

    lines.append("\n### Recommendation")
    if severity_counts.get("Critical", 0) > 0:
        lines.append(
            "IMMEDIATE ACTION REQUIRED: Critical vulnerabilities detected. "
            "Pause the contract and remediate before any further user interaction."
        )
    elif severity_counts.get("High", 0) > 0:
        lines.append(
            "HIGH PRIORITY: High severity findings require prompt remediation. "
            "Consider pausing deposits until fixes are deployed."
        )
    else:
        lines.append(
            "Medium/Low findings should be addressed in the next contract upgrade."
        )

    return "\n".join(lines)


# ── Report generation ─────────────────────────────────────────────────────────

SEVERITY_MAP = {
    P0: "Critical",
    P1: "High",
    P2: "Medium",
}


def build_immunefi_report(contract_info: dict, finding: dict, llm_report: str) -> dict:
    """
    Build an Immunefi-compatible vulnerability report.
    """
    contract_name = contract_info.get("contract_name", "Unknown")
    address = contract_info.get("address", "")
    severity = finding.get("severity", "Medium")
    timestamp = datetime.now(timezone.utc).isoformat()

    return {
        "title": f"{finding['title']} — {contract_name}",
        "severity": severity,
        "contract_address": address,
        "chain": "Base",
        "description": finding.get("description", ""),
        "proof_of_concept": finding.get("proof_of_concept", ""),
        "recommended_fix": finding.get("recommended_fix", ""),
        "check_id": finding.get("check", ""),
        "location": finding.get("location", ""),
        "llm_report": llm_report,
        "immunefi_url": "https://immunefi.com/explore/",
        "found_by": "HiveCatnip Autonomous Scanner",
        "hive_did": "did:hive:hive-catnip",
        "contract_name": contract_name,
        "compiler_version": contract_info.get("compiler_version", "unknown"),
        "timestamp": timestamp,
    }


# ── Single contract scan ──────────────────────────────────────────────────────

def scan_single_contract(address: str) -> list:
    """
    Fetch and scan a single contract address.
    Returns list of Immunefi-compatible report dicts.
    """
    logger.info(f"Scanning contract {address}")
    contract_info = fetch_contract_source(address)
    if not contract_info:
        logger.info(f"No source available for {address}, skipping")
        return []

    source = contract_info.get("source_code", "")
    if not source:
        logger.info(f"Empty source for {address}, skipping")
        return []

    findings = run_static_analysis(source)
    if not findings:
        logger.info(f"No findings for {address}")
        with _scanner_lock:
            _scanner_state["contracts_scanned"] += 1
        return []

    logger.info(f"Found {len(findings)} issues in {address}")

    # Generate LLM report for P0/P1 findings
    critical_findings = [
        f for f in findings
        if f.get("severity") in (P0, P1)
    ]
    llm_report = ""
    if critical_findings:
        llm_report = generate_llm_report(contract_info, critical_findings)

    reports = []
    for finding in findings:
        report = build_immunefi_report(contract_info, finding, llm_report)
        append_finding(report)
        reports.append(report)
        logger.info(
            f"[catnip-bounty] [{report['severity']}] {report['title']} "
            f"@ {address}"
        )

    with _scanner_lock:
        _scanner_state["contracts_scanned"] += 1

    return reports


# ── Background scanner loop ───────────────────────────────────────────────────

POLL_INTERVAL = 300  # 5 minutes


def scanner_loop():
    """
    Background thread: continuously discovers and scans new contracts.
    """
    logger.info("Background scanner started")
    with _scanner_lock:
        _scanner_state["running"] = True

    state = load_state()
    seen_addresses = set(a.lower() for a in state.get("seen_addresses", []))

    while True:
        try:
            logger.info("Starting discovery scan cycle")
            with _scanner_lock:
                _scanner_state["last_scan_time"] = datetime.now(timezone.utc).isoformat()

            new_addresses = discover_new_contracts(seen_addresses)
            logger.info(f"Discovered {len(new_addresses)} new contract(s)")

            for address in new_addresses:
                try:
                    scan_single_contract(address)
                except Exception as e:
                    logger.warning(f"Error scanning {address}: {e}")
                seen_addresses.add(address.lower())

            # Persist state (keep last 5000 seen addresses to avoid unbounded growth)
            state["seen_addresses"] = list(seen_addresses)[-5000:]
            state["last_scan_timestamp"] = datetime.now(timezone.utc).isoformat()
            save_state(state)

        except Exception as e:
            logger.error(f"Scanner loop error: {e}")
            with _scanner_lock:
                _scanner_state["last_error"] = str(e)

        time.sleep(POLL_INTERVAL)


def start_scanner():
    """
    Start the background scanner thread (daemon so it exits with main process).
    """
    t = threading.Thread(target=scanner_loop, name="catnip-bounty-scanner", daemon=True)
    t.start()
    logger.info("Scanner thread launched")
    return t


# ── Stats helper ──────────────────────────────────────────────────────────────

def get_scanner_stats() -> dict:
    with _scanner_lock:
        state_copy = dict(_scanner_state)
    severity_counts, total = count_findings_by_severity()
    return {
        **state_copy,
        "findings_by_severity": severity_counts,
        "findings_total_on_disk": total,
    }
