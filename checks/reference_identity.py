#!/usr/bin/env python3
"""Check cited DOI identities against Crossref and report year ambiguities.

This confirms bibliographic identity only. It cannot confirm that a paper says
what the draft claims it says, and passing this check is not evidence review.

The dual-year case matters more than a plain mismatch. Journals that publish
online ahead of an issue carry two years, and a draft that cites one of them
looks correct to a naive comparison while being wrong for a target journal
that wants the other. Those keys are reported explicitly rather than folded
into "match", so the citation style can be settled deliberately.

Usage:
  python3 checks/reference_identity.py draft.md --bib refs.bib --json audit.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

CITE_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_:.-]*)")
ENTRY_RE = re.compile(r"@\w+\s*\{\s*([^,\s]+)\s*,")
FIELD_RE = re.compile(r"(\w+)\s*=\s*[{\"]([^}\"]*)[}\"]")
USER_AGENT = "sci-writing-workflow-reference-audit/1.0"

STATUS_MATCH = "doi_year_match"
STATUS_DUAL_YEAR = "dual_year_ambiguous"
STATUS_YEAR_MISMATCH = "year_mismatch"
STATUS_NO_DOI = "manual_needed_no_doi"
STATUS_LOOKUP_FAILED = "lookup_failed"


def parse_bib(path: Path) -> dict[str, dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    text = path.read_text(errors="ignore")
    for block in re.split(r"(?=@)", text):
        key = ENTRY_RE.match(block)
        if not key:
            continue
        entries[key.group(1)] = {
            k.lower(): v.strip() for k, v in FIELD_RE.findall(block)
        }
    return entries


def deref(value: str) -> str:
    """Strip a BibTeX string-journal or accent wrapper down to its text."""
    value = value.strip()
    if value.startswith("{") and value.endswith("}"):
        value = value[1:-1]
    return value.strip()


def resolve_year(entry: dict[str, str], entries: dict[str, dict[str, str]],
                 depth: int = 0) -> int | None:
    """Read the year, following one level of crossref or a string macro."""
    if depth > 3:
        return None
    raw = entry.get("year") or entry.get("date") or ""
    raw = deref(raw)
    if re.fullmatch(r"\d{4}", raw):
        return int(raw)
    target = entry.get("crossref") or raw
    target = deref(target)
    if target in entries:
        return resolve_year(entries[target], entries, depth + 1)
    found = re.search(r"(\d{4})", raw)
    return int(found.group(1)) if found else None


def fetch(doi: str, timeout: int = 15) -> dict:
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)["message"]


def classify(record: dict, cited_year: int | None) -> tuple[str, str | None]:
    online = (record.get("published-online") or {}).get("date-parts", [[None]])[0][0]
    print_year = (record.get("published-print") or {}).get("date-parts", [[None]])[0][0]
    if cited_year is None:
        return STATUS_YEAR_MISMATCH, "draft cites no parseable year"
    if cited_year not in (online, print_year):
        return STATUS_YEAR_MISMATCH, f"draft {cited_year} not in {online}/{print_year}"
    if online and print_year and online != print_year:
        return STATUS_DUAL_YEAR, f"online {online}, print {print_year}; draft uses {cited_year}"
    return STATUS_MATCH, None


def audit(manuscript: Path, bib_path: Path, sleep: float) -> list[dict]:
    text = manuscript.read_text(errors="ignore")
    entries = parse_bib(bib_path)
    results = []
    for key in sorted(set(CITE_RE.findall(text))):
        entry = entries.get(key, {})
        doi = deref(entry.get("doi", "")) or None
        cited_year = resolve_year(entry, entries)
        item = {"key": key, "doi": doi, "cited_year": cited_year}
        if not doi:
            item["status"] = STATUS_NO_DOI
            item["note"] = "no DOI in bib; verify against the issuing body's own record"
            results.append(item)
            continue
        try:
            record = fetch(doi)
            item["title"] = (record.get("title") or [None])[0]
            item["online_year"] = (record.get("published-online") or {}).get("date-parts", [[None]])[0][0]
            item["print_year"] = (record.get("published-print") or {}).get("date-parts", [[None]])[0][0]
            item["status"], item["note"] = classify(record, cited_year)
        except Exception as exc:  # network, 404, malformed DOI
            item["status"] = STATUS_LOOKUP_FAILED
            item["error"] = str(exc)
        results.append(item)
        time.sleep(sleep)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manuscript", type=Path)
    parser.add_argument("--bib", type=Path, required=True)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--sleep", type=float, default=0.2,
                        help="seconds between Crossref requests")
    args = parser.parse_args()

    results = audit(args.manuscript, args.bib, args.sleep)
    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1

    print(f"cited keys    {len(results)}")
    for status, count in sorted(counts.items()):
        print(f"  {status:24} {count}")
    for item in results:
        if item["status"] != STATUS_MATCH:
            print(f"  {item['key']:28} {item['status']:22} {item.get('note', item.get('error', ''))}")

    if args.json:
        args.json.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")

    blocking = sum(counts.get(s, 0) for s in (STATUS_YEAR_MISMATCH, STATUS_LOOKUP_FAILED))
    review = counts.get(STATUS_DUAL_YEAR, 0) + counts.get(STATUS_NO_DOI, 0)
    if blocking:
        print(f"RESULT        FAIL ({blocking} unresolved)")
        return 1
    if review:
        print(f"RESULT        PASS with {review} item(s) needing a citation-style decision")
        return 0
    print("RESULT        PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
