"""Export accounting and progress reporting.

The bug these pin down: run 33987734987 exported 6,474 distinct titles, said
`written=6216, skipped=2, errors=0`, printed `0 versions in he` 252 times — and
4 titles that *did* have a Hebrew version were neither written, skipped nor
counted as errors.  Upstream's `prepare_text_for_export` returns None without a
word when an index has virtual leaf nodes, and our loop had no branch for that.

The same run spent 12,759 log lines (42% of the whole job) echoing every title
twice, from inside that upstream helper.  So: capture upstream's per-title
chatter, turn it into counters, replay anything unrecognised, and make every
summary an identity that has to add up.

Like test_authors_export.py these run without a Sefaria checkout or a Mongo
instance — the exporter imports `sefaria.*` lazily, so stubs in `sys.modules`
are enough.
"""
import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from run_exports import (
    EXPORT_REPORT_FILENAME,
    MERGED_CLASSES,
    VERSION_CLASSES,
    ProgressTicker,
    build_export_report,
    classify_title_output,
    format_category_timings,
    format_duration,
    format_named,
    format_progress,
    run_merged_export_he_only,
    run_versions_export_he_only,
    write_export_report,
)


class FakeClock:
    """A monotonic clock the test drives by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# --------------------------------------------------------------------------
# Pure formatting helpers
# --------------------------------------------------------------------------


class DurationTest(unittest.TestCase):
    def test_seconds_minutes_hours(self):
        self.assertEqual("0s", format_duration(0))
        self.assertEqual("45s", format_duration(45.4))
        self.assertEqual("1m00s", format_duration(60))
        self.assertEqual("9m17s", format_duration(557))
        self.assertEqual("20m13s", format_duration(1213))
        self.assertEqual("1h02m", format_duration(3720))

    def test_negative_elapsed_never_leaks_a_minus_sign(self):
        self.assertEqual("0s", format_duration(-3))


class ProgressLineTest(unittest.TestCase):
    def test_line_carries_n_of_m_percent_counters_elapsed_and_eta(self):
        line = format_progress(6400, 6474, 45.0,
                               {"written": 6161, "skipped": 239, "errors": 0})
        self.assertEqual(
            "  …6400/6474 (98.9%) written=6161 skipped=239 errors=0 elapsed 45s eta ~1s",
            line,
        )

    def test_the_final_line_has_no_eta(self):
        line = format_progress(10, 10, 20.0, {"written": 10})
        self.assertIn("100.0%", line)
        self.assertNotIn("eta", line)

    def test_an_uncountable_stage_claims_neither_a_percentage_nor_an_eta(self):
        line = format_progress(500, 0, 30.0)
        self.assertEqual("  …500 elapsed 30s", line)


class ProgressTickerTest(unittest.TestCase):
    def test_the_first_item_always_ticks_so_the_stage_looks_alive(self):
        ticker = ProgressTicker(1000, every=100, seconds=60.0, clock=FakeClock())
        self.assertIsNotNone(ticker.tick(1))

    def test_it_then_ticks_every_n_items(self):
        clock = FakeClock()
        ticker = ProgressTicker(1000, every=100, seconds=60.0, clock=clock)
        ticker.tick(1)
        self.assertIsNone(ticker.tick(50))
        self.assertIsNone(ticker.tick(100))
        self.assertIsNotNone(ticker.tick(101))

    def test_a_slow_stage_still_ticks_on_the_clock(self):
        clock = FakeClock()
        ticker = ProgressTicker(1000, every=100, seconds=60.0, clock=clock)
        ticker.tick(1)
        clock.advance(59)
        self.assertIsNone(ticker.tick(2))
        clock.advance(2)
        self.assertIsNotNone(ticker.tick(3))

    def test_elapsed_comes_off_the_injected_clock(self):
        clock = FakeClock()
        ticker = ProgressTicker(10, clock=clock)
        clock.advance(557)
        self.assertEqual(557.0, ticker.elapsed)


class NamedListTest(unittest.TestCase):
    def test_short_lists_are_printed_whole(self):
        self.assertEqual("   2 skipped: a, b", format_named("skipped", ["a", "b"], limit=30))

    def test_long_lists_are_bounded_and_the_tail_is_counted(self):
        line = format_named("no_he_version", [f"t{i}" for i in range(252)], limit=30)
        self.assertIn("252 no_he_version:", line)
        self.assertIn("t29 ", line)
        self.assertNotIn("t30", line)
        self.assertIn(f"… and 222 more (see {EXPORT_REPORT_FILENAME})", line)

    def test_nothing_to_name_prints_nothing(self):
        self.assertEqual("", format_named("skipped", []))


class CategoryTimingTest(unittest.TestCase):
    def test_slowest_categories_come_first_and_the_list_is_capped(self):
        seconds = {"Talmud": 231.0, "Tanakh": 98.0, "Mishnah": 12.0,
                   "Halakhah": 9.0, "Midrash": 5.0, "Other": 1.0}
        rows = {k: 10 for k in seconds}
        out = format_category_timings(seconds, rows, top=5)
        self.assertTrue(out.startswith("Talmud 3m51s/10 rows · Tanakh 1m38s/10 rows"))
        self.assertNotIn("Other", out)

    def test_no_rows_means_no_line(self):
        self.assertEqual("", format_category_timings({}, {}))


# --------------------------------------------------------------------------
# The accounting hole: telling upstream's silent returns apart
# --------------------------------------------------------------------------


class ClassifyTitleOutputTest(unittest.TestCase):
    def test_the_version_count_and_the_echoed_title_are_recognised(self):
        chatter = classify_title_output("Abarbanel on Amos",
                                        "1 versions in he\nAbarbanel on Amos\n")
        self.assertEqual(1, chatter.versions)
        self.assertIsNone(chatter.outcome)
        self.assertEqual([], chatter.residual)

    def test_a_title_with_no_hebrew_version_is_recognised(self):
        chatter = classify_title_output("Ein Yaakov", "0 versions in he\n")
        self.assertEqual(0, chatter.versions)
        self.assertIsNone(chatter.outcome)

    def test_an_index_lookup_failure_is_recognised_with_its_reason(self):
        chatter = classify_title_output(
            "Jastrow", "2 versions in he\nJastrow\nSkipping Jastrow - no such index\n")
        self.assertEqual("index_error", chatter.outcome)
        self.assertEqual("no such index", chatter.reason)

    def test_a_write_that_produced_nothing_is_recognised(self):
        chatter = classify_title_output(
            "Klein", "1 versions in he\nKlein\nSkipping Klein - no content\n")
        self.assertEqual("no_content", chatter.outcome)

    def test_anything_unrecognised_survives_for_the_caller_to_replay(self):
        chatter = classify_title_output(
            "Genesis", "1 versions in he\nGenesis\nsomething new upstream says\n")
        self.assertEqual(["something new upstream says"], chatter.residual)


# --------------------------------------------------------------------------
# Fakes for the two per-title loops
# --------------------------------------------------------------------------


class FakeRefError(Exception):
    pass


def fake_ref(title):
    if title.startswith("!bad-ref"):
        raise FakeRefError(title)
    return object()


class FakeExport:
    """Reproduces every print/return path of sefaria.export we depend on.

    Behaviour is driven by the title prefix so a test reads as a list of cases:
      * `!empty`     -> 0 Hebrew versions, upstream returns None
      * `!virtual`   -> versions exist, upstream returns None *silently*
      * `!noindex`   -> upstream prints `Skipping <t> - ...` and returns None
      * `!nocontent` -> the write prints `Skipping <t> - no content`
      * `!boom`      -> upstream raises
      * `!chatty`    -> upstream prints something we have never seen before
    """

    def __init__(self):
        self.written = []

    def prepare_merged_text_for_export(self, title, lang="he"):
        if title.startswith("!boom"):
            print("1 versions in he")
            raise RuntimeError("upstream exploded")
        versions = 0 if title.startswith("!empty") else 2
        print(f"{versions} versions in {lang}")
        if versions == 0:
            return None
        print(title)
        if title.startswith("!chatty"):
            print("brand new upstream diagnostic")
        if title.startswith("!noindex"):
            print(f"Skipping {title} - no such index")
            return None
        if title.startswith("!virtual"):
            return None
        return {"title": title, "text": "x"}

    def prepare_text_for_export(self, doc):
        title = doc["title"]
        if title.startswith("!boom"):
            raise RuntimeError("upstream exploded")
        print(title)
        if title.startswith("!noindex"):
            print(f"Skipping {title} - no such index")
            return None
        if title.startswith("!virtual"):
            return None
        return dict(doc)

    def write_text_doc_to_disk(self, doc):
        if doc["title"].startswith("!nocontent"):
            print(f"Skipping {doc['title']} - no content")
            return
        self.written.append(doc["title"])

    @staticmethod
    def text_is_copyright(doc):
        return str(doc.get("license", "")).startswith("Copyright")

    @staticmethod
    def remove_illegal_file_chars(name):
        return name.replace("/", "")


class FakeTexts:
    def __init__(self, titles=(), docs=()):
        self._titles = list(titles)
        self._docs = list(docs)

    def find(self, query=None, projection=None):
        query = query or {}
        parent = self

        class Cursor:
            def distinct(self, field):
                return list(parent._titles)

            def __iter__(self):
                matched = []
                for d in parent._docs:
                    titles = query.get("title")
                    if isinstance(titles, dict) and d["title"] not in titles["$in"]:
                        continue
                    if "language" in query and d.get("language") != query["language"]:
                        continue
                    matched.append(d)
                return iter(matched)

        return Cursor()


class SefariaStubMixin:
    def install(self, titles=(), docs=()):
        texts = FakeTexts(titles, docs)
        database = types.ModuleType("sefaria.system.database")
        database.db = types.SimpleNamespace(texts=texts)
        text_mod = types.ModuleType("sefaria.model.text")
        text_mod.Ref = fake_ref
        modules = {
            "sefaria": types.ModuleType("sefaria"),
            "sefaria.system": types.ModuleType("sefaria.system"),
            "sefaria.system.database": database,
            "sefaria.model": types.ModuleType("sefaria.model"),
            "sefaria.model.text": text_mod,
        }
        for name, mod in modules.items():
            self.addCleanup(sys.modules.pop, name, None)
            sys.modules[name] = mod
        return texts

    @staticmethod
    def run_captured(fn, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stats = fn(*args)
        return stats, buf.getvalue()


# --------------------------------------------------------------------------
# The merged loop
# --------------------------------------------------------------------------


class MergedAccountingTest(SefariaStubMixin, unittest.TestCase):
    TITLES = [
        "Genesis", "Exodus",            # written
        "!empty-a", "!empty-b",         # 0 Hebrew versions
        "!virtual-a",                   # the 4-title hole from run 33987734987
        "!noindex-a",
        "!nocontent-a",
        "!bad-ref-a",
        "",                             # distinct() does hand back blanks
        "!boom-a",
    ]

    def _run(self):
        self.install(titles=self.TITLES)
        return self.run_captured(run_merged_export_he_only, FakeExport())

    def test_every_title_lands_in_exactly_one_class(self):
        stats, _ = self._run()
        self.assertEqual(len(self.TITLES), stats["titles"])
        self.assertEqual(stats["titles"], sum(stats["counts"].values()))
        self.assertEqual(set(MERGED_CLASSES), set(stats["counts"]))

    def test_a_silently_dropped_virtual_node_title_is_its_own_class(self):
        """The whole point: `written + skipped + errors` used to lose these."""
        stats, out = self._run()
        self.assertEqual(1, stats["counts"]["virtual_nodes"])
        self.assertEqual(["!virtual-a"], stats["names"]["virtual_nodes"])
        self.assertIn("virtual_nodes=1", out)

    def test_each_upstream_return_path_is_counted_separately(self):
        stats, _ = self._run()
        counts = stats["counts"]
        self.assertEqual(2, counts["written"])
        self.assertEqual(2, counts["no_he_version"])
        self.assertEqual(1, counts["index_error"])
        self.assertEqual(1, counts["no_content"])
        self.assertEqual(1, counts["bad_ref"])
        self.assertEqual(1, counts["blank_title"])
        self.assertEqual(1, counts["errors"])

    def test_a_write_that_produced_no_content_is_not_reported_as_written(self):
        ex = FakeExport()
        self.install(titles=["!nocontent-a"])
        stats, _ = self.run_captured(run_merged_export_he_only, ex)
        self.assertEqual(0, stats["counts"]["written"])
        self.assertEqual([], ex.written)

    def test_the_summary_is_an_identity(self):
        stats, out = self._run()
        counts = stats["counts"]
        skipped = stats["titles"] - counts["written"] - counts["errors"]
        self.assertIn(
            f"titles={stats['titles']} = written={counts['written']} "
            f"+ skipped={skipped} + errors={counts['errors']}",
            out,
        )

    def test_dropped_titles_are_named_in_the_log(self):
        _, out = self._run()
        self.assertIn("2 no_he_version: !empty-a, !empty-b", out)
        self.assertIn("!noindex-a (no such index)", out)

    def test_the_per_title_chatter_is_gone(self):
        """42% of run 33987734987's log was `N versions in he` plus the title."""
        self.install(titles=["Genesis", "Exodus", "!empty-a", "!virtual-a"])
        _, out = self.run_captured(run_merged_export_he_only, FakeExport())
        self.assertNotIn("versions in he", out)
        self.assertNotIn("\nGenesis\n", out)
        # …but a progress line still says how far the stage got.
        self.assertIn("…1/4 (25.0%)", out)
        self.assertIn("…4/4 (100.0%)", out)

    def test_a_failing_title_still_gets_its_full_upstream_output_replayed(self):
        self.install(titles=["!boom-a"])
        _, out = self.run_captured(run_merged_export_he_only, FakeExport())
        self.assertIn("[!boom-a] 1 versions in he", out)

    def test_an_unrecognised_upstream_line_is_still_printed(self):
        self.install(titles=["!chatty-a"])
        _, out = self.run_captured(run_merged_export_he_only, FakeExport())
        self.assertIn("[!chatty-a] brand new upstream diagnostic", out)

    def test_a_raising_title_is_counted_and_named_with_its_error(self):
        stats, out = self._run()
        self.assertEqual(1, stats["counts"]["errors"])
        self.assertIn("RuntimeError: upstream exploded", stats["names"]["errors"][0])
        self.assertIn("⚠️  !boom-a: upstream exploded", out)

    def test_the_last_progress_line_agrees_with_the_summary(self):
        """`written=6215` on the last tick vs `written=6216` in the summary."""
        self.install(titles=[f"t{i}" for i in range(100)])
        stats, out = self.run_captured(run_merged_export_he_only, FakeExport())
        ticks = [ln for ln in out.splitlines() if ln.startswith("  …")]
        self.assertIn("100/100", ticks[-1])
        self.assertIn(f"written={stats['counts']['written']}", ticks[-1])


# --------------------------------------------------------------------------
# The per-version loop
# --------------------------------------------------------------------------


class VersionsAccountingTest(SefariaStubMixin, unittest.TestCase):
    def _docs(self):
        def doc(title, version, chapter="text", license=None):
            d = {"title": title, "language": "he", "versionTitle": version,
                 "chapter": chapter}
            if license:
                d["license"] = license
            return d

        return [
            doc("Genesis", "A"), doc("Genesis", "B"),
            doc("Exodus", "A"), doc("Exodus", "B"),
            doc("Exodus", "C", chapter=["", "  "]),          # empty
            doc("Exodus", "D", license="Copyright 2026"),    # never in scope
            doc("!virtual-a", "A"), doc("!virtual-a", "B"),
            doc("Leviticus", "A"), doc("Leviticus", ""),     # no versionTitle
        ]

    def _run(self):
        self.install(docs=self._docs())
        return self.run_captured(run_versions_export_he_only, FakeExport())

    def test_the_summary_is_an_identity_over_non_copyright_docs(self):
        stats, out = self._run()
        counts = stats["counts"]
        self.assertEqual(stats["docs"], sum(counts.values()))
        self.assertEqual(set(VERSION_CLASSES), set(counts))
        self.assertIn(f"docs={stats['docs']} = written={counts['written']}", out)

    def test_copyrighted_versions_are_counted_but_kept_out_of_the_identity(self):
        stats, out = self._run()
        self.assertEqual(1, stats["copyright_excluded"])
        self.assertIn("copyrighted, never in scope", out)

    def test_empty_and_skipped_versions_are_named(self):
        stats, out = self._run()
        self.assertEqual(["Exodus / C"], stats["names"]["empty"])
        self.assertEqual(["Leviticus"], stats["names"]["no_version_title"])
        self.assertIn("1 empty: Exodus / C", out)

    def test_a_silently_dropped_version_is_its_own_class(self):
        stats, _ = self._run()
        self.assertEqual(2, stats["counts"]["virtual_nodes"])

    def test_the_per_version_title_echo_is_gone(self):
        _, out = self._run()
        self.assertNotIn("\nGenesis\n", out)
        self.assertIn("  …", out)

    def test_the_expected_and_the_seen_document_count_are_reconciled(self):
        stats, out = self._run()
        self.assertEqual(stats["expected_docs"], stats["docs"])
        self.assertNotIn("but expected", out)


# --------------------------------------------------------------------------
# The report that ships inside the archive
# --------------------------------------------------------------------------


class ExportReportTest(unittest.TestCase):
    def test_the_report_carries_every_section_it_is_given(self):
        report = build_export_report({"titles": 3}, {"docs": 2}, {"links": 1})
        self.assertEqual(1, report["schema_version"])
        self.assertEqual({"titles": 3}, report["merged"])
        self.assertEqual({"docs": 2}, report["versions"])
        self.assertEqual({"links": 1}, report["links"])

    def test_a_missing_section_is_omitted_rather_than_faked(self):
        self.assertNotIn("links", build_export_report({"titles": 3}, {"docs": 2}))

    def test_it_lands_in_metadata_beside_the_link_visibility_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_export_report(tmp, build_export_report({"titles": 3}))
            self.assertEqual(("metadata", EXPORT_REPORT_FILENAME),
                             (Path(path).parent.name, Path(path).name))
            self.assertEqual(3, json.loads(Path(path).read_text(encoding="utf-8"))
                             ["merged"]["titles"])

    def test_hebrew_names_survive_the_round_trip_unescaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = build_export_report({"names": {"empty": ["בראשית"]}})
            path = write_export_report(tmp, report)
            raw = Path(path).read_text(encoding="utf-8")
            self.assertIn("בראשית", raw)

    def test_two_identical_runs_produce_identical_bytes(self):
        """It is hashed into manifest.txt, so key order must not drift."""
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(write_export_report(tmp, build_export_report({"b": 1, "a": 2})))
            first = a.read_bytes()
            b = Path(write_export_report(tmp, build_export_report({"a": 2, "b": 1})))
            self.assertEqual(first, b.read_bytes())


if __name__ == "__main__":
    unittest.main()
