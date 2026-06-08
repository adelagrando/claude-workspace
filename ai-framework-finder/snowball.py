"""
AI Framework Snowballing Tool
==============================
Systematically discovers all ethical AI frameworks/guidelines/best practices
for health technology using forward and backward snowballing.

Usage:
    python snowball.py                    # Run full snowball search
    python snowball.py --resume           # Resume from saved state
    python snowball.py --report-only      # Generate report from saved state

Requirements:
    ANTHROPIC_API_KEY environment variable must be set.
"""

import os
import sys
import json
import csv
import time
import hashlib
import argparse
import textwrap
from datetime import datetime
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("ERROR: anthropic package not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
    USE_COLOR = True
except ImportError:
    USE_COLOR = False
    class Fore:
        GREEN = YELLOW = RED = CYAN = MAGENTA = WHITE = BLUE = ""
    class Style:
        BRIGHT = RESET_ALL = DIM = ""

# ── PATHS ─────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
STATE_FILE  = BASE_DIR / "state.json"
OUTPUT_CSV  = BASE_DIR / "frameworks_found.csv"
OUTPUT_JSON = BASE_DIR / "frameworks_found.json"
LOG_FILE    = BASE_DIR / "snowball_log.txt"

# ── INCLUSION CRITERIA (used in the prompt) ───────────────────────────────────
INCLUSION_CRITERIA = """
INCLUSION CRITERIA — a framework/guideline/best practice MUST meet ALL of:
1. Created in the USA or internationally (including USA participation)
2. Published in 2018 or later
3. Is a framework, policy, guideline, or best practice about AI that could affect
   healthcare (broadly) OR specifically about AI in healthcare
4. Created through consensus or by a group of people (not a single individual's opinion)
5. Could come from industry, non-profit, or for-profit organizations
6. Is publicly available

EXCLUSION CRITERIA — exclude if ANY of:
- Published before 2018
- Only about a single country outside the USA (e.g., purely EU without USA involvement)
- Not about AI (e.g., general digital health, non-AI software)
- Individual opinion piece / single-author blog without institutional backing
- Not publicly accessible (behind hard paywall with no free version)
- A research paper evaluating/reviewing frameworks (not a framework itself)
- A journal article or systematic review (not a framework, guideline, or policy document)
"""

# ── PROMPTS ───────────────────────────────────────────────────────────────────

BACKWARD_SNOWBALL_PROMPT = """
You are a research assistant performing BACKWARD SNOWBALLING for a systematic review
of ethical AI frameworks in health technology.

Your task: Given a source document, find all the frameworks, guidelines, policies,
or best practices on AI (especially in healthcare) that IT REFERENCES OR CITES.

SOURCE DOCUMENT:
Name: {name}
Organization: {organization}
URL: {url}
Year: {year}

Step 1: Search the web for the actual document at the URL above. If not directly
accessible, search for "{name} {organization} full text references" to find its
reference list or bibliography.

Step 2: From the document's references/citations, identify every item that could be
an AI framework, guideline, policy, or best practice (not journal articles or
research papers — only documents that ARE frameworks/guidelines/policies themselves).

Step 3: For each candidate found, search the web to find:
- Full official name
- Publishing organization
- Year published
- Official URL
- A 2-3 sentence description

{criteria}

Return your findings as a JSON array (and ONLY the JSON array, no other text):
[
  {{
    "name": "...",
    "organization": "...",
    "year": <integer or null>,
    "url": "...",
    "summary": "2-3 sentence description of what this framework covers",
    "include": true or false,
    "inclusion_reason": "Why it meets or does not meet the inclusion criteria",
    "source_direction": "backward",
    "found_in": "{short}"
  }},
  ...
]

If no relevant frameworks are found in the references, return an empty array: []
"""

FORWARD_SNOWBALL_PROMPT = """
You are a research assistant performing FORWARD SNOWBALLING for a systematic review
of ethical AI frameworks in health technology.

Your task: Find documents that CITE OR BUILD UPON the source document below, and
identify which of those are themselves AI frameworks, guidelines, or best practices.

SOURCE DOCUMENT:
Name: {name}
Organization: {organization}
URL: {url}
Year: {year}

Step 1: Search the web for:
- "{name} {organization}" cited by
- Documents that reference or build on this framework
- Subsequent versions or updates to this framework
- Other frameworks that explicitly acknowledge this document

Step 2: Also search for AI ethics frameworks in healthcare published after {year}
that are related to this domain, using searches like:
- "AI ethics framework healthcare {year} to 2024"
- "responsible AI health guidelines published after {year}"
- "AI policy healthcare consensus group"

Step 3: For each candidate framework found, search to verify:
- Full official name and organization
- Year published
- Official URL
- Brief description

{criteria}

Return ONLY a JSON array:
[
  {{
    "name": "...",
    "organization": "...",
    "year": <integer or null>,
    "url": "...",
    "summary": "2-3 sentence description",
    "include": true or false,
    "inclusion_reason": "Why it meets or does not meet the inclusion criteria",
    "source_direction": "forward",
    "found_in": "{short}"
  }},
  ...
]

If nothing new is found, return: []
"""

VERIFY_FRAMEWORK_PROMPT = """
You are verifying a candidate AI framework/guideline for inclusion in a systematic review
of ethical AI frameworks in health technology.

CANDIDATE:
Name: {name}
Organization: {organization}
Year: {year}
URL: {url}

Please:
1. Search the web to confirm this document exists and find the most accurate URL.
2. Verify the year of publication.
3. Write a clear 3-4 sentence summary of what this framework covers.
4. Apply the inclusion criteria below.

{criteria}

Return ONLY a JSON object:
{{
  "name": "confirmed or corrected official name",
  "organization": "confirmed or corrected organization name",
  "year": <confirmed integer year or null if unknown>,
  "url": "best available URL",
  "summary": "3-4 sentence summary of the framework's scope and content",
  "include": true or false,
  "inclusion_reason": "Detailed explanation of why it is included or excluded, referencing specific criteria",
  "verified": true
}}
"""


# ── LOGGING ───────────────────────────────────────────────────────────────────
def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_line = f"[{timestamp}] [{level}] {msg}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_line + "\n")

    color = {
        "INFO":    Fore.WHITE,
        "SUCCESS": Fore.GREEN,
        "FOUND":   Fore.CYAN,
        "SKIP":    Fore.YELLOW,
        "EXCLUDE": Fore.RED,
        "SECTION": Fore.MAGENTA + Style.BRIGHT,
        "ERROR":   Fore.RED + Style.BRIGHT,
    }.get(level, "")
    print(f"{color}{log_line}{Style.RESET_ALL}")


def section(title):
    bar = "─" * 70
    log(f"\n{bar}\n  {title}\n{bar}", "SECTION")


# ── FINGERPRINTING (deduplication) ───────────────────────────────────────────
def fingerprint(name: str, org: str) -> str:
    """Create a canonical key for deduplication."""
    key = (name + org).lower()
    key = "".join(c for c in key if c.isalnum())
    return hashlib.md5(key.encode()).hexdigest()[:12]


# ── STATE MANAGEMENT ──────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "queue":    [],   # items to process: [{name, org, url, year, short, source}]
        "visited":  [],   # fingerprints already processed
        "found":    [],   # all frameworks discovered (included + excluded)
        "iteration": 0,
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ── CLAUDE WEB SEARCH ─────────────────────────────────────────────────────────
def call_claude_with_search(client: anthropic.Anthropic, prompt: str, max_retries=3) -> str:
    """
    Call Claude with web_search tool enabled. Returns the text of the final response.
    Retries on transient errors.
    """
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=4096,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract the text blocks from the response
            text_parts = []
            for block in response.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)

            return "\n".join(text_parts).strip()

        except anthropic.RateLimitError:
            wait = 60 * (attempt + 1)
            log(f"Rate limit hit. Waiting {wait}s...", "ERROR")
            time.sleep(wait)
        except anthropic.APIError as e:
            log(f"API error (attempt {attempt+1}): {e}", "ERROR")
            time.sleep(10)

    return ""


def extract_json_array(text: str) -> list:
    """Extract a JSON array from text that may have extra content around it."""
    text = text.strip()
    # Find first [ and last ]
    start = text.find("[")
    end   = text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        return json.loads(text[start:end+1])
    except json.JSONDecodeError:
        return []


def extract_json_object(text: str) -> dict:
    """Extract a JSON object from text."""
    text = text.strip()
    start = text.find("{")
    end   = text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(text[start:end+1])
    except json.JSONDecodeError:
        return {}


# ── SNOWBALLING LOGIC ─────────────────────────────────────────────────────────
def snowball_backward(client, item: dict) -> list:
    """Find frameworks that this document cites."""
    log(f"  ← Backward snowball: {item['short']}", "INFO")
    prompt = BACKWARD_SNOWBALL_PROMPT.format(
        name=item["name"],
        organization=item["organization"],
        url=item.get("url", "unknown"),
        year=item.get("year", "unknown"),
        short=item["short"],
        criteria=INCLUSION_CRITERIA,
    )
    response = call_claude_with_search(client, prompt)
    results = extract_json_array(response)
    log(f"    Found {len(results)} backward candidates", "INFO")
    return results


def snowball_forward(client, item: dict) -> list:
    """Find frameworks that cite this document."""
    log(f"  → Forward snowball: {item['short']}", "INFO")
    prompt = FORWARD_SNOWBALL_PROMPT.format(
        name=item["name"],
        organization=item["organization"],
        url=item.get("url", "unknown"),
        year=item.get("year", "unknown"),
        short=item["short"],
        criteria=INCLUSION_CRITERIA,
    )
    response = call_claude_with_search(client, prompt)
    results = extract_json_array(response)
    log(f"    Found {len(results)} forward candidates", "INFO")
    return results


def verify_framework(client, candidate: dict) -> dict:
    """Verify and enrich a candidate framework."""
    prompt = VERIFY_FRAMEWORK_PROMPT.format(
        name=candidate.get("name", ""),
        organization=candidate.get("organization", ""),
        year=candidate.get("year", "unknown"),
        url=candidate.get("url", ""),
        criteria=INCLUSION_CRITERIA,
    )
    response = call_claude_with_search(client, prompt)
    verified = extract_json_object(response)
    if verified:
        # Merge source metadata
        verified["source_direction"] = candidate.get("source_direction", "")
        verified["found_in"]         = candidate.get("found_in", "")
        verified["fingerprint"]      = fingerprint(
            verified.get("name", candidate.get("name", "")),
            verified.get("organization", candidate.get("organization", ""))
        )
    return verified


# ── OUTPUT ────────────────────────────────────────────────────────────────────
CSV_FIELDS = [
    "name", "organization", "year", "url",
    "summary", "include", "inclusion_reason",
    "source_direction", "found_in",
]

def write_outputs(found: list):
    """Write CSV and JSON output files."""
    # Sort: included first, then by year
    sorted_found = sorted(
        found,
        key=lambda x: (0 if x.get("include") else 1, x.get("year") or 9999)
    )

    # CSV
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted_found)

    # JSON
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(sorted_found, f, indent=2, ensure_ascii=False)

    log(f"Output written: {OUTPUT_CSV.name} and {OUTPUT_JSON.name}", "SUCCESS")


def print_summary(found: list):
    included = [f for f in found if f.get("include")]
    excluded = [f for f in found if not f.get("include")]

    section("FINAL RESULTS SUMMARY")
    print(f"\n  Total frameworks found:    {len(found)}")
    print(f"  Included:                  {len(included)}")
    print(f"  Excluded:                  {len(excluded)}")

    print(f"\n{Fore.GREEN}{Style.BRIGHT}  ✅ INCLUDED FRAMEWORKS ({len(included)}){Style.RESET_ALL}")
    print("  " + "─"*65)
    for fw in sorted(included, key=lambda x: x.get("year") or 9999):
        year = fw.get("year") or "n/a"
        print(f"\n  {Fore.CYAN}{Style.BRIGHT}{fw.get('name', 'Unknown')}{Style.RESET_ALL}")
        print(f"  {Fore.WHITE}Org:  {fw.get('organization', '')}{Style.RESET_ALL}")
        print(f"  {Fore.WHITE}Year: {year}{Style.RESET_ALL}")
        print(f"  {Fore.WHITE}URL:  {fw.get('url', '')}{Style.RESET_ALL}")
        print(f"  {Fore.WHITE}Why:  {fw.get('inclusion_reason', '')}{Style.RESET_ALL}")
        summary = fw.get("summary", "")
        wrapped = textwrap.fill(summary, width=65, initial_indent="  ", subsequent_indent="  ")
        print(f"{Fore.WHITE}{wrapped}{Style.RESET_ALL}")

    print(f"\n{Fore.RED}{Style.BRIGHT}  ❌ EXCLUDED ({len(excluded)}){Style.RESET_ALL}")
    print("  " + "─"*65)
    for fw in excluded:
        print(f"  • {fw.get('name','?')} ({fw.get('organization','?')}, {fw.get('year','?')})")
        print(f"    {Style.DIM}{fw.get('inclusion_reason','')}{Style.RESET_ALL}")


# ── MAIN SNOWBALLING LOOP ─────────────────────────────────────────────────────
def run_snowball(resume=False):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.")
        print("  Set it with: export ANTHROPIC_API_KEY='sk-ant-...'")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Load or initialise state
    if resume and STATE_FILE.exists():
        state = load_state()
        log(f"Resuming from saved state. Queue: {len(state['queue'])} items, "
            f"Visited: {len(state['visited'])}, Found: {len(state['found'])}", "INFO")
    else:
        state = load_state()
        # Load seed frameworks into queue
        seeds_path = BASE_DIR / "seeds.json"
        with open(seeds_path, "r", encoding="utf-8") as f:
            seeds = json.load(f)

        for seed in seeds:
            fp = fingerprint(seed["name"], seed["organization"])
            queue_item = {
                "name":         seed["name"],
                "organization": seed["organization"],
                "short":        seed["short"],
                "url":          seed.get("url", ""),
                "year":         seed.get("known_year"),
                "source_direction": "seed",
                "found_in":     "seed",
                "fingerprint":  fp,
            }
            state["queue"].append(queue_item)
            # Add seeds to found as included (they are the starting point)
            seed_entry = {
                "name":             seed["name"],
                "organization":     seed["organization"],
                "year":             seed.get("known_year"),
                "url":              seed.get("url", ""),
                "summary":          f"Seed framework: {seed['short']}. One of the six starting documents for this snowballing review.",
                "include":          True,
                "inclusion_reason": "Seed framework — pre-selected as starting point for snowballing.",
                "source_direction": "seed",
                "found_in":         "seed",
                "fingerprint":      fp,
            }
            state["found"].append(seed_entry)
            state["visited"].append(fp)

        save_state(state)
        log(f"Initialised with {len(seeds)} seed frameworks.", "SUCCESS")

    section("STARTING SNOWBALLING SEARCH")

    iteration = 0
    while state["queue"]:
        iteration += 1
        item = state["queue"].pop(0)
        fp   = item.get("fingerprint") or fingerprint(item["name"], item["organization"])

        section(f"Iteration {iteration} — {item['short']}")
        log(f"Processing: {item['name']} ({item['organization']})", "INFO")

        # Collect candidates from both directions
        candidates = []
        try:
            candidates += snowball_backward(client, item)
            time.sleep(2)  # be polite to the API
            candidates += snowball_forward(client, item)
            time.sleep(2)
        except Exception as e:
            log(f"Error during snowballing for {item['short']}: {e}", "ERROR")

        log(f"  Total candidates to evaluate: {len(candidates)}", "INFO")

        new_count = 0
        for candidate in candidates:
            if not candidate.get("name") or not candidate.get("organization"):
                continue

            cand_fp = fingerprint(
                candidate.get("name", ""),
                candidate.get("organization", "")
            )

            # Skip if already seen
            if cand_fp in state["visited"]:
                log(f"  SKIP (already seen): {candidate['name']}", "SKIP")
                continue

            state["visited"].append(cand_fp)

            # Verify the candidate
            log(f"  Verifying: {candidate['name'][:60]}...", "INFO")
            try:
                verified = verify_framework(client, candidate)
                time.sleep(1)
            except Exception as e:
                log(f"  Error verifying {candidate['name']}: {e}", "ERROR")
                verified = candidate
                verified["fingerprint"] = cand_fp

            if not verified:
                continue

            verified.setdefault("fingerprint", cand_fp)
            state["found"].append(verified)

            if verified.get("include"):
                new_count += 1
                log(f"  ✅ INCLUDED: {verified.get('name')} ({verified.get('year')})", "FOUND")
                # Add to queue for further snowballing
                state["queue"].append({
                    "name":         verified.get("name", ""),
                    "organization": verified.get("organization", ""),
                    "short":        verified.get("name", "")[:40],
                    "url":          verified.get("url", ""),
                    "year":         verified.get("year"),
                    "source_direction": verified.get("source_direction", ""),
                    "found_in":     verified.get("found_in", ""),
                    "fingerprint":  cand_fp,
                })
            else:
                log(f"  ❌ EXCLUDED: {verified.get('name')} — {verified.get('inclusion_reason','')[:60]}", "EXCLUDE")

        log(f"  New frameworks added to queue this iteration: {new_count}", "INFO")

        # Save state after every item
        state["iteration"] = iteration
        save_state(state)

        # Write outputs incrementally
        write_outputs(state["found"])

    section("SNOWBALLING COMPLETE")
    log(f"Queue exhausted after {iteration} iterations.", "SUCCESS")
    log(f"Total unique items evaluated: {len(state['visited'])}", "INFO")
    log(f"Total in found list: {len(state['found'])}", "INFO")

    write_outputs(state["found"])
    print_summary(state["found"])


# ── REPORT ONLY ───────────────────────────────────────────────────────────────
def run_report():
    if not STATE_FILE.exists():
        print("No saved state found. Run the snowball search first.")
        sys.exit(1)
    state = load_state()
    write_outputs(state["found"])
    print_summary(state["found"])


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="AI Framework Snowballing Tool — finds all ethical AI frameworks in health tech"
    )
    parser.add_argument("--resume",      action="store_true", help="Resume from saved state")
    parser.add_argument("--report-only", action="store_true", help="Generate report from saved state without running search")
    args = parser.parse_args()

    if args.report_only:
        run_report()
    else:
        run_snowball(resume=args.resume)
