#!/usr/bin/env python3
"""Audit a manuscript draft against its bibliography and its source evidence.

This is the executable form of the stage-5 acceptance gate. It answers five
questions that a reader cannot answer by reading the draft alone:

  1. structure      - what headings, tables, figures and how many words exist
  2. placeholders   - did drafting notes leak into the submission text
  3. citations      - does every @key resolve, and which entries are unused
  4. numeric trace  - is every substantive number verbatim in a source file,
                      or declared as derived arithmetic
  5. phrasing       - repeated sentence forms and hedge density

Nothing here judges scientific merit. It only reports whether the draft is
internally consistent and traceable, so that a human can spend attention on
the argument instead of on typography and bookkeeping.

Usage:
  python3 checks/manuscript_audit.py draft.md --bib refs.bib \\
      --evidence-dir evidence/fulltext --allow-derived derived.json \\
      --json audit.json

Exit status is 0 when no hard failure is found, 1 otherwise. Hard failures are
missing citation keys, leaked placeholders, and untraced numbers. Use
--report-only to always exit 0.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

CITE_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_:.-]*)")
BIB_KEY_RE = re.compile(r"@\w+\s*\{\s*([^,\s]+)\s*,")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
FIGURE_RE = re.compile(r"!\[(?P<cap>[^\]]*)\]\((?P<src>[^)]+)\)")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", re.MULTILINE)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")

PLACEHOLDER_PATTERNS = [
    r"\bTODO\b", r"\bTBD\b", r"\bFIXME\b", r"\bXXX\b", r"\bPLACEHOLDER\b",
    r"\[insert[^\]]*\]", r"<[a-z_]+>", r"待补", r"待定", r"待核实",
    r"【待", r"此处", r"占位", r"填表", r"lorem ipsum",
]

# A comma is only a thousands separator when it groups digits in threes. An
# opaque \d[\d,]* swallows the leading "1," of "1 January 2024" and turns the
# following 419 into 1,419, which silently corrupts the trace of every number
# that happens to follow a date, a list or a citation. The grouped alternative
# is tried first and a plain run of digits is the fallback.
NUM_RE = re.compile(
    r"(?<![\w.])"
    r"(?P<grouped>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?![\d.])"
    r"\s*(?P<unit>%|percent\b)?"
    r"(?:\s*percentage\s+points)?"
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;:!?])\s+")

# Years appear as dates, as "2024;" after a citation, and inside study names.
# Treating them as findings buries the real ones.
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
# "41, 21, 133, 64" is four results; "1,234,567" is one. Both are comma-joined
# digit groups, so the deciding property is the widths: a thousands separator
# always groups in exact threes after the leading group, while a list of
# study counts does not. "1,234,567" is therefore left intact and "41, 21,
# 133, 64" is split. A run whose groups happen to be all three digits wide is
# indistinguishable from a formatted integer and is left alone, which errs
# toward under-splitting: the value is still checked, only as one token.
RUN_SPAN_RE = re.compile(r"\b\d{1,4}(?:\s*,\s*\d{1,3})+")
# A unit written after the number, possibly behind LaTeX markup: "6.7%",
# "6.7$\%$" and "6.7 per cent" are the same quantity. The unit matters
# because a number carrying one is a result the reader is meant to check,
# while a bare small integer in prose usually is not. The spelled-out
# "percentage points" is included because sources and drafts disagree on
# whether to abbreviate it, and a mismatch there makes a correctly-copied
# value read as fabricated.
UNIT_AFTER_RE = re.compile(
    r"^\s*\$*\\?%"
    r"|^\s*(?:percent|per\s+cent|\bpp\b|percentage\s+points?\b)"
)

HEDGE_RE = re.compile(
    r"\b(does not|do not|did not|cannot|can not|not established|"
    r"no evidence|remains? (?:unclear|unknown|unresolved)|not demonstrate[sd]?)\b",
    re.IGNORECASE,
)


def canonical_number(raw: str, unit: str | None) -> str | None:
    """Return a comparison form for a numeric token, or None if not substantive.

    Any value carrying a decimal point is kept: 0.42, 1.00 and 4.8 are all
    quantities a reader will check. Bare integers need at least two digits,
    because single digits appear everywhere as list markers, version numbers
    and doses, and keeping them buries the real findings in noise.
    """
    cleaned = (raw or "").replace(",", "").strip()
    if not cleaned:
        return None
    has_decimal = "." in cleaned
    if not has_decimal and not unit and len(cleaned) < 2:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    if text == "":
        return None
    if unit and unit.lower() in ("%", "percent"):
        text += "%"
    elif unit and "point" in unit.lower():
        text += "pp"
    return text


RUN_GROUP_RE = re.compile(r"\d+")

# Source material arrives as plain text, as PMC JATS XML, as publisher HTML
# and as CSV exports of supplementary tables. Skipping a format does not make
# the draft safe: it makes every number that only appears in that format look
# unsupported. A run that reads only .txt reported ten participant counts as
# unverifiable when 24 XML files in the same folder print them, so the
# default set is everything that can be read as text.
TEXT_SUFFIXES = (".txt", ".md", ".xml", ".html", ".htm", ".csv", ".tsv", ".tex")
MARKUP_RE = re.compile(r"<[^>]{0,400}>|&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")


def de_markup(text: str) -> str:
    """Strip tags and entities so source numbers sit next to their units.

    JATS XML wraps every value in its own element, so 41, 31, 133 and the
    percent sign arrive separated by markup rather than adjacent as prose.
    Removing the tags restores the adjacency the numeric parser relies on.
    """
    return MARKUP_RE.sub(" ", text)


# Machine identifiers and reference lists are numbers a source prints but not
# numbers a source reports. A JATS <ref-list> or a PMC reference <li> carries
# the PMID, PMCID, DOI, volume and page of every cited work, and <article-id>
# repeats them for the article itself. Left in the pool, they vouch for any
# draft value that happens to share those digits -- and a PMID suffix and a
# study size are both short integers, so collisions are routine rather than
# rare.
#
# Reference lists appear in two shapes in practice: JATS <ref-list> (nested
# inside <back>, so a whole-<back> cut must run first) and PMC HTML
# <li id="R12"> siblings. Both are cut here rather than by tag alone, because
# the reference entries in the HTML carry no <ref> wrapper to match on.
REFERENCE_BLOCK_RES = (
    re.compile(r"<ref-list\b.*?</ref-list\s*>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<ref\b[^>]*>.*?</ref\s*>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<li\b[^>]*\bid=[\"']?R\d+[\"']?[^>]*>.*?</li\s*>",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"<([a-z]+)\b[^>]*\bclass=[\"'][^\"']*\breferences?\b[^\"']*[\"'][^>]*>"
               r".*?</\1\s*>", re.IGNORECASE | re.DOTALL),
)
IDENTIFIER_RES = (
    re.compile(r"<article-id[^>]*>.*?</article-id\s*>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<pub-id[^>]*>.*?</pub-id\s*>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<journal-id[^>]*>.*?</journal-id\s*>", re.IGNORECASE | re.DOTALL),
    re.compile(r"\b(?:doi|pmid|pmcid)\s*[:=]?\s*[\w./()-]+", re.IGNORECASE),
    re.compile(r"https?://\S*?(?:pubmed|doi\.org|ncbi\.nlm\.nih\.gov)\S*",
               re.IGNORECASE),
)
# A <back> holds the reference list and nothing a numeric check needs, so it
# is removed wholesale. The cut is done in one pass over a finditer rather
# than by repeated .find, which shifted the closer offset on each iteration
# and silently left the inner reference lists intact.
BACK_BLOCK_RE = re.compile(r"<back\b.*?</back\s*>", re.IGNORECASE | re.DOTALL)


def strip_machine_identifiers(text: str) -> str:
    """Remove identifier and reference-list material from a source file.

    Only the numbers a source *reports* should be able to vouch for a draft.
    Verbatim presence of "32841496" in a reference list says nothing about the
    study's results, so leaving it in the pool trades a real check for a
    spurious pass: a draft value that collides with a PMID is reported as
    traced, which is the one direction this gate must never fail in.
    """
    text = BACK_BLOCK_RE.sub(" ", text)
    for pattern in REFERENCE_BLOCK_RES + IDENTIFIER_RES:
        text = pattern.sub(" ", text)
    return text


ABBREV_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)*\b")

# Synonyms that name the same quantity under different abbreviations. Drift
# is only detectable when the two names are known to be equivalent; without
# this table the check would flag every abbreviation the sources do not
# happen to print, which is noise rather than a finding. Keys are the
# abbreviation the draft might use, values are the alternatives a source
# might use instead.
EXPANSIONS: dict[str, tuple[str, ...]] = {
    "ALP": ("SAP", "ALKP", "alkaline phosphatase"),
    "SAP": ("ALP", "ALKP", "alkaline phosphatase"),
    "ELF": ("enhanced liver fibrosis",),
    "PRO": ("patient-reported outcome", "patient reported outcome"),
    "LSM": ("liver stiffness",),
    "qMRCP": ("magnetic resonance cholangiopancreatography",),
    "IBD": ("inflammatory bowel disease",),
    "OLE": ("open-label extension", "open label extension"),
    "UDCA": ("ursodeoxycholic acid",),
    "TEAE": ("treatment-emergent adverse event", "treatment emergent adverse event"),
    "NRS": ("numeric rating scale", "numerical rating scale"),
    "AE": ("adverse event",),
    "SAE": ("serious adverse event",),
    "ITT": ("intention-to-treat", "intention to treat"),
    "mITT": ("modified intention-to-treat", "modified intention to treat"),
}


def term_drift(text: str, paths: list[Path], top: int = 12) -> dict:
    """Report abbreviations the draft uses that its sources name differently.

    A number trace cannot catch a renamed endpoint. When a draft writes "ALP"
    throughout and the source it cites writes "SAP" 48 times and "ALP" never,
    every digit still matches and the draft still misattributes a term to a
    trial that did not use it. The review's own thesis is that endpoint
    naming must be exact, so this class of drift is precisely what it must
    not contain.

    Only short all-caps tokens are compared, because those are the readable
    aliases (ALP, SAP, ELF, PRO, qMRCP). A term used by the draft and absent
    from every source is reported; one used by both is not.
    """
    corpus: Counter = Counter()
    per_source: dict[str, Counter] = {}
    for path in paths:
        try:
            raw = path.read_text(errors="ignore")
        except OSError:
            continue
        if path.suffix.lower() in (".xml", ".html", ".htm"):
            raw = de_markup(raw)
        # Reference lists are dense with the abbreviations of other papers
        # (PMID, DOI, RCT) and would otherwise inflate the corpus that decides
        # whether a draft's term is attested anywhere.
        raw = strip_machine_identifiers(raw)
        counts: Counter = Counter()
        # Both the abbreviation and its spelled-out form are counted, so
        # "alkaline phosphatase" matches a source that never abbreviates and
        # "SAP" matches one that does. Without the spelled form the synonym
        # table would compare an abbreviation against a phrase and miss.
        for token in ABBREV_RE.findall(raw):
            counts[token] += 1
        lowered = raw.lower()
        for expansion in {e for values in EXPANSIONS.values() for e in values}:
            if len(expansion) > 4:
                counts[expansion] += lowered.count(expansion)
        per_source[path.stem] = counts
        corpus.update({t for t in counts if t.isupper() and 2 <= len(t) <= 6})

    used = {t for t in ABBREV_RE.findall(body_text(text)) if t.isupper() and 2 <= len(t) <= 6}
    # Corpus-wide presence is the wrong test and let the real case through.
    # "ALP" appears in the corpus because PRIMIS and SPRING use it, while the
    # vancomycin source writes "SAP" 49 times and "ALP" never. A draft that
    # writes ALP while describing the vancomycin trial is therefore correct
    # against the corpus and wrong against its own source. What matters is
    # whether any source uses one synonym to the exclusion of the other, so
    # exclusivity within a source is the test.
    drift = []
    for term in sorted(used):
        if corpus.get(term, 0) == 0:
            continue          # the draft's term is absent everywhere; see unmatched_terms
        expansions = EXPANSIONS.get(term)
        if not expansions:
            continue
        exclusive = {
            name: sum(counts.get(a, 0) for a in expansions)
            for name, counts in per_source.items()
            if counts.get(term, 0) == 0
            and sum(counts.get(a, 0) for a in expansions) > 0
        }
        if exclusive:
            # Ordered by how heavily the source leans on the synonym, because
            # the heavily-leaning source is the one whose terminology the
            # draft is most visibly overriding.
            ranked = sorted(exclusive.items(), key=lambda kv: -kv[1])
            drift.append({
                "term": term,
                "sources_use": expansions,
                "examples": [f"{name} ({n}x)" for name, n in ranked[:4]],
                "exclusive_in": len(exclusive),
            })
    return {
        "unmatched_terms": sorted(used - set(corpus))[:top],
        "drift": sorted(drift, key=lambda d: -d["exclusive_in"])[:top],
    }


def split_run(match: re.Match) -> str:
    """Replace a comma-joined result list with space-separated tokens.

    A run is treated as a list unless every group after the first is exactly
    three digits wide, which is the shape of a formatted integer. Counts in
    this domain rarely exceed four digits, so an all-threes run is far more
    likely to be a coincidence of three-digit study sizes than a formatted
    number, and leaving it intact only costs a slightly coarser check.
    """
    groups = RUN_GROUP_RE.findall(match.group(0))
    if len(groups) >= 2 and all(len(g) == 3 for g in groups[1:]):
        return match.group(0)
    return " ".join(groups)


def numbers_in(text: str) -> list[tuple[str, str]]:
    """Return (canonical, raw) for each substantive number in text.

    A leading minus sign is kept. Reporting a change as "1.2%" when the draft
    says "-1.2%" would invert the direction of the result, which is the one
    error a numeric trace exists to prevent.
    """
    found = []
    # Split result lists before the main pattern runs. "41, 21, 133, 64" is
    # four results; the numeric regex would otherwise read it as one
    # comma-separated integer and lose every component. A genuine thousands
    # separator ("1,234,567") keeps its commas.
    text = RUN_SPAN_RE.sub(split_run, text)
    for match in NUM_RE.finditer(text):
        unit = match.group("unit")
        if not unit and "percentage points" in match.group(0).lower():
            unit = "percentage points"
        # A unit the main pattern did not capture sits just after the match:
        # a LaTeX-escaped percent, or the spelled-out "percentage points".
        if not unit:
            tail = text[match.end():match.end() + 20]
            found_unit = UNIT_AFTER_RE.match(tail)
            if found_unit:
                unit = ("percentage points"
                        if found_unit.group(0).strip().lstrip("$\\").startswith(("pp", "percentage"))
                        else "%")
        canonical = canonical_number(match.group("grouped"), unit)
        if canonical is None:
            continue
        raw = match.group(0).strip()
        start = match.start()
        # A minus sign may be ASCII or the typographic U+2212 that Word and
        # LaTeX output. Sources and drafts disagree on which they use, so both
        # are canonicalised to ASCII or every negative result reads as untraced.
        if start > 0 and text[start - 1] in "-−–—":
            canonical = "-" + canonical
            raw = text[start - 1] + raw
        found.append((canonical, raw))
    return found


def source_number_pool(paths: list[Path]) -> dict[str, set[str]]:
    """Collect the numbers a source file actually prints, in two pools.

    The pools are kept apart on purpose. A source reporting "5%" supports a
    draft that writes "5.0%", and the reverse also occurs, so the percent
    forms are unioned. But a source that prints the bare integer 197 for a
    baseline laboratory value must not be able to vouch for a draft that
    presents 197 as a participant count. Pooling all bare integers together
    makes the check blind to exactly the derived arithmetic it exists to
    catch, so a bare integer is reported as needing review rather than as
    verified.

    Size is not a useful discriminator and was a mistake to use. A participant
    count of 73 is just as claim-bearing as one of 419, and thresholding at
    three digits dropped every small count out of the pool entirely, where it
    then surfaced as "not found in any source" — the loudest possible
    finding — when the source prints it plainly. Every bare integer is
    therefore retained and classified by the caller.
    """
    exact: set[str] = set()
    bare: set[str] = set()
    # canonical -> stem -> occurrences. Counts, not a set: a value printed
    # once in a passing sentence is weaker evidence than one printed forty
    # times in results tables, and the caller shows the ranking.
    seen_in: dict[str, dict[str, int]] = {}
    for path in paths:
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if path.suffix.lower() in (".xml", ".html", ".htm"):
            text = de_markup(text)
        text = strip_machine_identifiers(text)
        counts: Counter = Counter()
        for canonical, _ in numbers_in(text):
            counts[canonical] += 1
            if canonical.endswith("%") or canonical.endswith("pp"):
                exact.add(canonical)
            else:
                bare.add(canonical)
        # Keyed on the stem so a JATS XML and its extracted .txt count as
        # one source rather than two, which would overstate how widely a
        # value is corroborated. Counts are summed across the two formats.
        for canonical, n in counts.items():
            per_source = seen_in.setdefault(canonical, {})
            per_source[path.stem] = per_source.get(path.stem, 0) + n
    percent_forms = {v.rstrip("%").rstrip("p") for v in exact}
    # Bare magnitudes are kept beside the unit-bearing forms. A draft that
    # writes "−1.4 percentage points" where the source writes "−1·4%" has the
    # right number and the wrong unit, and collapsing the two would report it
    # as verified. Keeping the bare form lets audit_numbers tell a unit change
    # apart from a fabricated value.
    magnitudes = {v.rstrip("%").rstrip("p") for v in (exact | bare)}
    return {
        "exact": exact | {f"{v}%" for v in percent_forms} | {f"{v}pp" for v in percent_forms},
        "bare_strong": bare,
        "magnitudes": exact | bare | magnitudes,
        "seen_in": seen_in,
    }


def read_bib(path: Path) -> dict[str, dict[str, str]]:
    """Return {key: {field: value}} parsed without a BibTeX dependency."""
    entries: dict[str, dict[str, str]] = {}
    if not path.exists():
        return entries
    text = path.read_text(errors="ignore")
    for block in re.split(r"(?=@)", text):
        key_match = BIB_KEY_RE.match(block)
        if not key_match:
            continue
        fields = {}
        for field, value in re.findall(r"(\w+)\s*=\s*[{\"]([^}\"]*)[}\"]", block):
            fields[field.lower()] = value.strip()
        entries[key_match.group(1)] = fields
    return entries


def structure_report(text: str) -> dict:
    headings = [(len(h), t.strip()) for h, t in HEADING_RE.findall(text)]
    figures = FIGURE_RE.findall(text)
    tables = len(TABLE_SEP_RE.findall(text))
    without_frontmatter = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
    return {
        "word_count": len(WORD_RE.findall(without_frontmatter)),
        "headings": headings,
        "heading_depth": max((d for d, _ in headings), default=0),
        "figures": [{"caption": c, "src": s} for c, s in figures],
        "table_count": tables,
    }


def placeholder_hits(text: str) -> list[dict]:
    stripped = body_text(text)
    hits = []
    for pattern in PLACEHOLDER_PATTERNS:
        for match in re.finditer(pattern, stripped, re.IGNORECASE):
            start = max(0, match.start() - 60)
            hits.append({
                "pattern": pattern,
                "match": match.group(0),
                "context": re.sub(r"\s+", " ", stripped[start:match.end() + 60]),
            })
    return hits


def phrasing_report(text: str, top: int = 8) -> dict:
    body = body_text(text)
    sentences = [s for s in SENTENCE_SPLIT_RE.split(body) if len(WORD_RE.findall(s)) > 3]
    openers = Counter(
        " ".join(WORD_RE.findall(s)[:3]).lower() for s in sentences if len(WORD_RE.findall(s)) >= 3
    )
    words = max(len(WORD_RE.findall(body)), 1)
    hedge_count = len(HEDGE_RE.findall(body))
    return {
        "sentence_count": len(sentences),
        "hedge_count": hedge_count,
        "hedges_per_1000_words": round(hedge_count * 1000 / words, 1),
        "repeated_openers": [
            {"opener": o, "count": c} for o, c in openers.most_common(top) if c > 1
        ],
    }


def audit_citations(text: str, bib: dict) -> dict:
    cited = sorted(set(CITE_RE.findall(text)))
    missing = [k for k in cited if k not in bib]
    return {
        "cited_count": len(cited),
        "bib_entry_count": len(bib),
        "missing_from_bib": missing,
        "uncited_bib_entries": sorted(set(bib) - set(cited)),
    }


def body_text(text: str) -> str:
    """Strip everything whose numbers are not claim-bearing.

    Three removals matter. The YAML front matter holds a bibliography path.
    The reference list holds publication years. Citation keys such as
    @Bowlus2023 carry a year inside the key itself, which would otherwise be
    traced as though the draft had asserted it. Leaving any of these in
    produces a stream of false findings that hides the real ones.
    """
    body = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
    body = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    body = re.sub(r"^#{1,6}\s*References?\s*$.*\Z", "", body,
                  flags=re.DOTALL | re.MULTILINE | re.IGNORECASE)
    body = re.sub(r"\[((?:[^\]]*@[^\]]*)+)\]", " ", body)   # bracketed citation groups
    body = re.sub(r"@[A-Za-z][A-Za-z0-9_:.-]*", " ", body)  # bare citation keys
    return body


def unit_magnitude(canonical: str) -> str:
    """Return the number without its unit, so units can be compared apart."""
    return canonical.rstrip("%").rstrip("p").rstrip("p") if canonical.endswith("pp") \
        else canonical.rstrip("%")


def audit_numbers(text: str, pool: dict[str, set[str]], allowed: dict[str, str]) -> dict:
    body = body_text(text)
    exact, bare_strong = pool["exact"], pool["bare_strong"]
    magnitudes = pool.get("magnitudes", exact | bare_strong)
    seen_in = pool.get("seen_in", {})
    traced, derived, weak, untraced, unit_shift = [], [], [], [], []
    seen: set[str] = set()
    years: set[str] = set()
    for canonical, raw in numbers_in(body):
        if canonical in seen:
            continue
        seen.add(canonical)
        # A year is context, not a claim. It reaches this function through
        # search dates and study descriptors, and reporting it as untraced
        # would put four noise lines above every real finding.
        if YEAR_RE.fullmatch(canonical):
            years.add(canonical)
            continue
        if canonical in allowed:
            derived.append({"value": raw, "reason": allowed[canonical]})
        elif canonical in exact:
            traced.append(raw)
        elif unit_magnitude(canonical) in magnitudes and unit_magnitude(canonical) != canonical:
            # The magnitude is printed in a source but never with this unit.
            # Either the unit is an error, or the draft converted a
            # percentage into percentage points, or the source uses a
            # different unit for the same quantity. All three need an author
            # decision, and none can be settled by the checker.
            start = body.find(raw)
            unit_shift.append({
                "value": raw,
                "source_forms": sorted(
                    m for m in magnitudes
                    if unit_magnitude(m) == unit_magnitude(canonical) and m != canonical
                )[:6],
                "context": re.sub(r"\s+", " ", body[max(0, start - 90):start + 90]),
            })
        elif canonical in bare_strong:
            # Printed somewhere in a source, but as a bare integer, so the
            # match may be the same quantity or an unrelated coincidence.
            # This is what correctly-computed derived arithmetic looks like
            # (133 + 64 = 197) and also what a typo looks like. It cannot be
            # resolved mechanically, so it is reported as needing review
            # rather than as verified or as fabricated.
            start = body.find(raw)
            weak.append({
                "value": raw,
                "printed_in": sorted(seen_in.get(canonical, ()))[:4],
                "context": re.sub(r"\s+", " ", body[max(0, start - 90):start + 90]),
            })
        else:
            start = body.find(raw)
            untraced.append({
                "value": raw,
                "context": re.sub(r"\s+", " ", body[max(0, start - 90):start + 90]),
            })
    return {
        "traced": len(traced),
        "declared_derived": derived,
        "bare_integer_only": weak,
        "unit_shift": sorted(unit_shift, key=lambda d: d["value"]),
        "untraced": untraced,
        "years": sorted(years),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manuscript", type=Path)
    parser.add_argument("--bib", type=Path, default=None)
    parser.add_argument("--evidence-dir", type=Path, default=None,
                        help="directory of source full texts; txt, md, xml, html and csv are read")
    parser.add_argument("--allow-derived", type=Path, default=None,
                        help="JSON object mapping a number to the reason it is derived")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()

    if not args.manuscript.exists():
        print(f"error: manuscript not found: {args.manuscript}", file=sys.stderr)
        return 1
    text = args.manuscript.read_text(errors="ignore")

    bib = read_bib(args.bib) if args.bib else {}
    citations = audit_citations(text, bib)

    evidence_paths: list[Path] = []
    if args.evidence_dir:
        if not args.evidence_dir.is_dir():
            print(f"error: evidence dir not found: {args.evidence_dir}", file=sys.stderr)
            return 1
        evidence_paths = sorted(
            p for p in args.evidence_dir.rglob("*")
            if p.suffix.lower() in TEXT_SUFFIXES and p.is_file()
        )
        if not evidence_paths:
            print(f"error: no readable sources under {args.evidence_dir}",
                  file=sys.stderr)
            return 1
    if args.bib and not args.bib.exists():
        print(f"error: bib not found: {args.bib}", file=sys.stderr)
        return 1
    pool = (source_number_pool(evidence_paths) if evidence_paths
            else {"exact": set(), "bare_strong": set()})

    allowed: dict[str, str] = {}
    if args.allow_derived and args.allow_derived.exists():
        allowed = json.loads(args.allow_derived.read_text())
        allowed = {canonical_number(k, "%" if k.endswith("%") else None) or k: v
                   for k, v in allowed.items()}

    report = {
        "manuscript": str(args.manuscript),
        "structure": structure_report(text),
        "placeholders": placeholder_hits(text),
        "citations": citations,
        "numbers": audit_numbers(text, pool, allowed) if pool else {"skipped": "no evidence supplied"},
        "terms": term_drift(text, evidence_paths) if evidence_paths else {"skipped": "no evidence supplied"},
        "phrasing": phrasing_report(text),
        "evidence_files_read": len(evidence_paths),
    }

    structure = report["structure"]
    print(f"manuscript    {args.manuscript}")
    print(f"words         {structure['word_count']}")
    print(f"headings      {len(structure['headings'])} (max depth {structure['heading_depth']})")
    print(f"figures       {len(structure['figures'])}   tables {structure['table_count']}")
    print(f"citations     {citations['cited_count']} cited / {citations['bib_entry_count']} in bib")
    if citations["missing_from_bib"]:
        print(f"  MISSING     {', '.join(citations['missing_from_bib'])}")
    if citations["uncited_bib_entries"]:
        print(f"  unused      {len(citations['uncited_bib_entries'])} bib entries not cited")
    if report["placeholders"]:
        print(f"placeholders  {len(report['placeholders'])} HARD FAIL")
        for hit in report["placeholders"][:5]:
            print(f"  {hit['match']!r} in: {hit['context'][:90]}")
    else:
        print("placeholders  none")
    numbers = report["numbers"]
    if "skipped" not in numbers:
        print(f"numbers       {numbers['traced']} traced, "
              f"{len(numbers['declared_derived'])} declared derived, "
              f"{len(numbers['bare_integer_only'])} bare-integer only, "
              f"{len(numbers['unit_shift'])} unit-shifted, "
              f"{len(numbers['untraced'])} untraced")
        for item in numbers["unit_shift"][:10]:
            print(f"  UNIT?       {item['value']}  source has "
                  f"{', '.join(item['source_forms']) or 'only the bare number'}"
                  f"  ...{item['context'][:60]}...")
        for item in numbers["bare_integer_only"][:10]:
            src = ",".join(s.split("_")[0] for s in item.get("printed_in", ())) or "-"
            print(f"  DERIVED?    {item['value']:<9} [{src[:34]}]  ...{item['context'][:60]}...")
        for item in numbers["untraced"][:10]:
            print(f"  UNTRACED    {item['value']}  ...{item['context'][:80]}...")
    terms = report["terms"]
    if "skipped" not in terms and terms["drift"]:
        print(f"term drift    {len(terms['drift'])} abbreviation(s) the sources name otherwise")
        for item in terms["drift"][:8]:
            print(f"  DRIFT       {item['term']} -> sources say "
                  f"{'/'.join(item['sources_use'])}  [{', '.join(item['examples'][:2])}]")
    phrasing = report["phrasing"]
    print(f"hedges        {phrasing['hedge_count']} "
          f"({phrasing['hedges_per_1000_words']} per 1000 words)")
    for opener in phrasing["repeated_openers"][:5]:
        print(f"  repeated    {opener['count']}x  \"{opener['opener']}\"")

    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    hard_fail = bool(citations["missing_from_bib"]) or bool(report["placeholders"])
    if "skipped" not in numbers and numbers["untraced"]:
        hard_fail = True
    if args.report_only:
        return 0
    print("RESULT        " + ("FAIL" if hard_fail else "PASS"))
    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())
