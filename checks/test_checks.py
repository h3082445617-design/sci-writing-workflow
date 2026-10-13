"""Regression tests for the stage-5 gate scripts.

Every case here is a defect the scripts actually produced or actually missed
during the first real project run, not a hypothetical. The point is that a
rule stated only in CONTRIBUTING.md is a rule nobody follows -- this file was
written after two consecutive commits shipped a change to a gate script that
had never been executed against real input, and both times the defect was
found only afterwards.

Run with the standard library, no dependencies:

    python3 -m unittest discover -s checks -v
    python3 checks/test_checks.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import manuscript_audit as audit  # noqa: E402
import reference_identity as ident  # noqa: E402


class TempSourcesMixin:
    """Write source files to a scratch directory and clean them up."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def source(self, name: str, content: str) -> Path:
        path = self.tmp / name
        path.write_text(content, encoding="utf-8")
        return path


class NumberNormalisation(unittest.TestCase):
    def test_percent_forms_unify(self) -> None:
        """A source printing 5% must vouch for a draft printing 5.0%."""
        self.assertEqual(audit.canonical_number("5.0", "%"),
                         audit.canonical_number("5", "%"))
        self.assertEqual(audit.canonical_number("5.0", "%"), "5%")

    def test_grouped_integer_loses_its_separator(self) -> None:
        self.assertEqual(audit.canonical_number("1,234", None), "1234")

    def test_non_numeric_returns_none(self) -> None:
        self.assertIsNone(audit.canonical_number("abc", None))

    def test_unit_magnitude_drops_the_unit(self) -> None:
        """Magnitude comparison is what exposes a rewritten unit."""
        self.assertEqual(audit.unit_magnitude("-1.4%"), audit.unit_magnitude("-1.4pp"))


class UnitShiftDetection(unittest.TestCase):
    """A source saying -1.4% and a draft saying -1.4 percentage points is a
    change of quantity, not of typography, and must not read as traced."""

    def test_percentage_points_canonicalises_to_pp(self) -> None:
        """The unit is recorded, so a draft writing 'pp' where the source
        wrote '%' has a comparable magnitude but a different unit."""
        found = dict(audit.numbers_in("Mean change was -1.4 percentage points."))
        self.assertEqual(found.get("-1.4pp"), "-1.4 percentage points")

    def test_percentage_points_magnitude_matches_percent(self) -> None:
        self.assertEqual(audit.unit_magnitude("-1.4pp"), audit.unit_magnitude("-1.4%"))

    def test_unicode_minus_matches_ascii(self) -> None:
        """Sources and drafts disagree on U+2212; both must canonicalise."""
        found = dict(audit.numbers_in("The difference was −1.4%."))
        self.assertIn("-1.4%", found)

    def test_latex_escaped_percent_is_a_unit(self) -> None:
        found = dict(audit.numbers_in("Response was 5.0\\% on placebo."))
        self.assertIn("5%", found)

    def test_run_of_results_is_split_into_separate_values(self) -> None:
        """'41, 31, 133' is three findings, not the integer 41,311,33."""
        found = {c for c, _ in audit.numbers_in("Rates were 41, 31, 133.")}
        self.assertTrue({"41", "31"} <= found)

    def test_formatted_integer_survives_the_split(self) -> None:
        """...but an all-three-digit run is a formatted number and must not be
        carved into 234 and 567."""
        found = {c for c, _ in audit.numbers_in("Total 1,234,567.")}
        self.assertIn("1234", found)
        self.assertNotIn("567", found)


class MachineIdentifierStripping(TempSourcesMixin, unittest.TestCase):
    """A JATS reference list prints the PMID, volume and year of every cited
    work. Those digits are not results, and leaving them in the pool lets a
    draft value that collides with a PMID read as traced."""

    def test_ref_list_pmid_is_removed(self) -> None:
        xml = ('<article><body><p>Response was 5.0%.</p></body>'
               '<back><ref-list><ref id="R1"><mixed-citation>'
               '<pub-id pub-id-type="pmid">32841496</pub-id>'
               '</mixed-citation></ref></ref-list></back></article>')
        cleaned = audit.clean_source(xml, ".xml")
        self.assertNotIn("32841496", cleaned)
        self.assertIn("5.0", cleaned)

    def test_nested_ref_list_inside_back_is_removed(self) -> None:
        """The first implementation cut <back> by repeated .find and shifted
        its own closer offset, leaving the inner ref-list in place."""
        xml = ('<back><ref-list><ref><mixed-citation>11111111</mixed-citation>'
               '</ref></ref-list></back><body>kept 22222222</body>')
        cleaned = audit.clean_source(xml, ".xml")
        self.assertNotIn("11111111", cleaned)
        self.assertIn("22222222", cleaned)

    def test_html_reference_list_item_is_removed(self) -> None:
        """PMC reference entries carry no <ref> wrapper, only id="R12", so the
        rule can only match while the attribute is still present. Running
        de_markup first destroys it and silently disables this cut."""
        html = ('<body><p>Rate 41%</p></body>'
                '<ol><li id="R4"><cite>Guidance D-12345678</cite>'
                '<a href="https://pubmed.ncbi.nlm.nih.gov/22152166/">PubMed</a>'
                '</li></ol>')
        cleaned = audit.clean_source(html, ".html")
        self.assertNotIn("22152166", cleaned)
        self.assertNotIn("12345678", cleaned)
        self.assertIn("41", cleaned)

    def test_stripping_order_is_what_makes_the_html_rule_work(self) -> None:
        """Guards the ordering itself: the wrong order must lose the entry."""
        html = '<ol><li id="R4"><cite>22152166</cite></li></ol>'
        self.assertNotIn("22152166", audit.clean_source(html, ".html"))
        wrong_order = audit.strip_machine_identifiers(audit.de_markup(html))
        self.assertIn("22152166", wrong_order)

    def test_body_numbers_are_not_stripped(self) -> None:
        xml = '<body><p>73 participants, 5.0% on placebo, P=1.00.</p></body>'
        cleaned = audit.clean_source(xml, ".xml")
        for value in ("73", "5.0", "1.00"):
            self.assertIn(value, cleaned)

    def test_identifiers_do_not_vouch_for_a_draft_value(self) -> None:
        """The end-to-end consequence: a value printed only in a reference
        list must not enter the pool that decides whether a draft number is
        traced."""
        path = self.source("withdrawn.xml",
                           '<article><body><p>Unrelated.</p></body><back>'
                           '<ref-list><ref><mixed-citation>'
                           '<pub-id pub-id-type="pmid">32841496</pub-id>'
                           '</mixed-citation></ref></ref-list></back></article>')
        pool = audit.source_number_pool([path])
        self.assertNotIn("32841496", pool["bare_strong"])


class TermDrift(TempSourcesMixin, unittest.TestCase):
    """A renamed endpoint survives every numeric check: the digits all match
    while the term is attributed to a trial that never used it."""

    def test_drift_is_judged_per_source_not_across_the_corpus(self) -> None:
        """The draft's term exists elsewhere in the corpus, so a corpus-wide
        presence test reports no drift. The source it actually cites uses a
        different name throughout, which is the real finding."""
        naming = self.source(
            "VANCO_fulltext.txt",
            "Serum SAP was measured. SAP changed by 12%. SAP is the endpoint. SAP.",
        )
        other = self.source("PRIMIS_fulltext.txt", "ALP ALP ALP ALP ALP")
        report = audit.term_drift("ALP is the prespecified endpoint.", [naming, other])
        drifted = {d["term"]: d for d in report["drift"]}
        self.assertIn("ALP", drifted)
        self.assertIn("VANCO", " ".join(drifted["ALP"]["examples"]))

    def test_matching_term_produces_no_drift(self) -> None:
        source = self.source("PRIMIS_fulltext.txt", "ALP was measured. ALP changed.")
        report = audit.term_drift("ALP is the endpoint.", [source])
        self.assertEqual([d for d in report["drift"] if d["term"] == "ALP"], [])

    def test_term_absent_from_every_source_is_reported_separately(self) -> None:
        source = self.source("UNREL_fulltext.txt", "Nothing relevant here.")
        report = audit.term_drift("The XQZ scale was used.", [source])
        self.assertIn("XQZ", report["unmatched_terms"])
        self.assertEqual(report["drift"], [])


class SourceIdentity(TempSourcesMixin, unittest.TestCase):
    """One paper arrives as several files. Counting them as several sources
    overstates corroboration: a value printed in a trial's full text and again
    in its own appendix is attested once, not twice."""

    def test_carriers_of_one_paper_collapse_to_one_name(self) -> None:
        names = {audit.source_identity(self.tmp / f"{stem}.txt") for stem in
                 ("PRIMIS_fulltext", "PRIMIS_elsevier", "PRIMIS_appendix1")}
        self.assertEqual(names, {"PRIMIS"})

    def test_a_file_with_no_underscore_is_its_own_identity(self) -> None:
        self.assertEqual(audit.source_identity(self.tmp / "notes.md"), "notes")

    def test_pmid_prefixed_carriers_collapse(self) -> None:
        a = audit.source_identity(self.tmp / "36922785_PMC10015541.xml")
        b = audit.source_identity(self.tmp / "36922785_PMC10015541.txt")
        self.assertEqual(a, b)

    def test_two_papers_do_not_collapse(self) -> None:
        a = audit.source_identity(self.tmp / "PRIMIS_fulltext.txt")
        b = audit.source_identity(self.tmp / "NGM_birmingham.txt")
        self.assertNotEqual(a, b)

    def test_corroboration_is_counted_once_per_paper(self) -> None:
        full = self.source("PRIMIS_fulltext.txt", "Response was 41%.")
        appendix = self.source("PRIMIS_appendix1.txt", "Response was 41%.")
        pool = audit.source_number_pool([full, appendix])
        self.assertEqual(pool["seen_in"]["41%"], {"PRIMIS": 2})

    def test_term_counts_accumulate_across_carriers(self) -> None:
        """Assigning instead of accumulating would drop a carrier's tokens,
        so a term used in the full text but not the appendix would read as
        absent from its own source."""
        full = self.source("PRIMIS_fulltext.txt", "SAP was measured. SAP changed.")
        appendix = self.source("PRIMIS_appendix1.txt", "SAP SAP SAP SAP SAP.")
        other = self.source("NGM_birmingham.txt", "ALP ALP ALP.")
        report = audit.term_drift("ALP is the endpoint.", [full, appendix, other])
        drifted = {d["term"]: d for d in report["drift"]}
        self.assertIn("ALP", drifted)
        self.assertIn("PRIMIS", " ".join(drifted["ALP"]["examples"]))

    def test_drift_examples_name_the_paper_not_the_file(self) -> None:
        source = self.source("PRIMIS_fulltext.txt", "SAP SAP SAP SAP")
        attesting = self.source("NGM_birmingham.txt", "ALP ALP ALP")
        report = audit.term_drift("ALP is the endpoint.", [source, attesting])
        drifted = {d["term"]: d for d in report["drift"]}
        joined = " ".join(drifted["ALP"]["examples"])
        self.assertNotIn("PRIMIS_fulltext", joined)


class SourceNumberPool(TempSourcesMixin, unittest.TestCase):
    def test_source_percent_reaches_a_draft_written_with_decimals(self) -> None:
        """Both sides canonicalise, so a source printing 5% vouches for a
        draft printing 5.0% without either form being stored twice."""
        path = self.source("trial.txt", "Response was 5% on placebo.")
        pool = audit.source_number_pool([path])
        self.assertIn("5%", pool["exact"])
        self.assertIn(("5%", "5.0%"), audit.numbers_in("Response was 5.0%."))

    def test_bare_integer_enters_the_bare_pool_only(self) -> None:
        """A bare integer is reported for review, never as verified: pooling
        bare integers with unit-bearing values would make the check blind to
        derived arithmetic, which is what it exists to catch."""
        path = self.source("trial.txt", "A total of 197 participants.")
        pool = audit.source_number_pool([path])
        self.assertIn("197", pool["bare_strong"])
        self.assertNotIn("197", pool["exact"])

    def test_seen_in_records_which_source_printed_the_value(self) -> None:
        a = self.source("a.txt", "Rate was 41%.")
        b = self.source("b.txt", "Unrelated content.")
        pool = audit.source_number_pool([a, b])
        self.assertEqual(set(pool["seen_in"]["41%"]) - {"a"}, set())

    def test_repeated_values_are_counted_not_collapsed(self) -> None:
        path = self.source("a.txt", "41% then 41% again and 41% once more.")
        pool = audit.source_number_pool([path])
        self.assertEqual(pool["seen_in"]["41%"]["a"], 3)

    def test_missing_file_is_skipped_not_fatal(self) -> None:
        """An unreadable source must not abort the whole pool."""
        pool = audit.source_number_pool([self.tmp / "absent.xml"])
        self.assertEqual(pool["bare_strong"], set())


class CitationAudit(unittest.TestCase):
    BIB = {
        "Known2020": {"title": "A paper"},
        "Unused2019": {"title": "Another paper"},
    }

    def test_resolvable_keys_are_not_reported(self) -> None:
        report = audit.audit_citations("See @Known2020.", self.BIB)
        self.assertEqual(report["missing_from_bib"], [])

    def test_unresolvable_key_is_reported(self) -> None:
        report = audit.audit_citations("See @Ghost2021.", self.BIB)
        self.assertEqual(report["missing_from_bib"], ["Ghost2021"])

    def test_uncited_entries_are_reported(self) -> None:
        report = audit.audit_citations("See @Known2020.", self.BIB)
        self.assertEqual(report["uncited_bib_entries"], ["Unused2019"])


class Placeholders(unittest.TestCase):
    def test_placeholder_markers_are_found(self) -> None:
        for text in ("TODO: add the figure", "值待补", "[insert sample size]",
                     "FIXME", "此处"):
            self.assertTrue(audit.placeholder_hits(text), text)

    def test_clean_prose_has_none(self) -> None:
        self.assertEqual(
            audit.placeholder_hits("Response was 5.0% on placebo."), [])


class YearClassification(unittest.TestCase):
    """Crossref frequently records only the print year, dated to the issue
    rather than the article. A bib citing the online year is correct, and
    reporting it as a hard mismatch failed three of 25 real references."""

    def test_online_and_print_both_year_matched(self) -> None:
        record = {"published-online": {"date-parts": [[2025]]},
                  "published-print": {"date-parts": [[2026]]}}
        status, note = ident.classify(record, 2025)
        self.assertEqual(status, ident.STATUS_DUAL_YEAR)
        self.assertIn("2026", note)

    def test_print_only_with_earlier_online_citation_is_advisory(self) -> None:
        """The regression: Crossref returns print 2026 and no online year,
        while the article was posted in 2025."""
        record = {"published-print": {"date-parts": [[2026]]},
                  "created": {"date-parts": [[2025, 9, 27]]}}
        status, note = ident.classify(record, 2025)
        self.assertEqual(status, ident.STATUS_ONLINE_AHEAD)
        self.assertIn("2026", note)

    def test_year_outside_every_known_date_is_a_mismatch(self) -> None:
        record = {"published-print": {"date-parts": [[2026]]},
                  "created": {"date-parts": [[2025, 9, 27]]}}
        status, _ = ident.classify(record, 2019)
        self.assertEqual(status, ident.STATUS_YEAR_MISMATCH)

    def test_plain_match(self) -> None:
        record = {"published-print": {"date-parts": [[2020]]}}
        self.assertEqual(ident.classify(record, 2020)[0], ident.STATUS_MATCH)

    def test_unparseable_citation_year_is_a_mismatch(self) -> None:
        record = {"published-print": {"date-parts": [[2020]]}}
        self.assertEqual(ident.classify(record, None)[0], ident.STATUS_YEAR_MISMATCH)

    def test_no_dates_at_all_is_a_lookup_failure(self) -> None:
        self.assertEqual(ident.classify({}, 2020)[0], ident.STATUS_LOOKUP_FAILED)

    def test_advisory_statuses_are_distinct_from_blocking_ones(self) -> None:
        """Only an unexplained year or a failed lookup should exit non-zero."""
        blocking = {ident.STATUS_YEAR_MISMATCH, ident.STATUS_LOOKUP_FAILED}
        advisory = {ident.STATUS_DUAL_YEAR, ident.STATUS_ONLINE_AHEAD,
                    ident.STATUS_NO_DOI, ident.STATUS_MATCH}
        self.assertFalse(blocking & advisory)

    def test_published_field_is_accepted_as_the_issue_year(self) -> None:
        record = {"published": {"date-parts": [[2026]]},
                  "created": {"date-parts": [[2025, 9, 27]]}}
        self.assertEqual(ident.classify(record, 2025)[0], ident.STATUS_ONLINE_AHEAD)
        self.assertEqual(ident.classify(record, 2026)[0], ident.STATUS_MATCH)


class BibParsing(TempSourcesMixin, unittest.TestCase):
    def test_fields_are_lowercased_and_values_unwrapped(self) -> None:
        path = self.source("refs.bib", """
@article{Key2020,
  title = {A {Study} of Things},
  Year = {2020},
  doi = {10.1000/xyz},
}
""")
        entries = ident.parse_bib(path)
        self.assertIn("Key2020", entries)
        self.assertEqual(entries["Key2020"]["year"], "2020")
        self.assertEqual(entries["Key2020"]["doi"], "10.1000/xyz")

    def test_year_falls_back_to_a_crossref_entry(self) -> None:
        path = self.source("refs.bib", """
@article{Child2021, crossref = {Parent2021}}
@article{Parent2021, year = {2021}}
""")
        entries = ident.parse_bib(path)
        self.assertEqual(ident.resolve_year(entries["Child2021"], entries), 2021)

    def test_missing_year_resolves_to_none(self) -> None:
        path = self.source("refs.bib", "@misc{NoYear, title = {x}}")
        entries = ident.parse_bib(path)
        self.assertIsNone(ident.resolve_year(entries["NoYear"], entries))


class BodyTextExclusion(unittest.TestCase):
    """Reference lists in the draft itself must not feed the numeric audit,
    or every bib year is checked as if it were a finding."""

    def test_reference_section_is_excluded(self) -> None:
        text = ("# Results\n\nResponse was 5.0%.\n\n"
                "## References\n\n1. Smith 2020. 10.1000/xyz\n")
        body = audit.body_text(text)
        self.assertIn("5.0", body)
        self.assertNotIn("10.1000/xyz", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
