"""Acknowledged terminal dead letters are reported, not re-warned.

Root 30562807099 exhausted its three attempts on 2026-07-30 and can never
clear, so the scheduled reconciler raised the same `::warning::` on the order
of 650 times (653 scheduled runs of reconcile-downstream.yml between that root
and 2026-09-07; GitHub throttles the `7,37 * * * *` cron to roughly 17 ticks a
day, not the nominal 48).  Permanent noise masks a *new* dead letter beside it,
which is the only thing the annotation exists to surface.  These tests pin the
acknowledgement store's exact-match semantics, its fail-closed parsing, and the
one summary line that distinguishes acknowledged from new.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import reconcile_downstream as reconciler


ACKNOWLEDGED_ROOT = 30562807099
ACKNOWLEDGED_TAG = "2026-07-30_18-55-30558924132-1"
SOURCE_REPO = "Otzaria/SefariaExport"


def make_release(run_id: int, attempt: int = 1) -> reconciler.ReleaseIntent:
    sha = f"{run_id:064x}"
    tag = f"2026-07-30_18-55-{run_id}-{attempt}"
    intent_asset = {
        "name": f"downstream-intent-{sha}.json",
        "size": 2,
        "digest": "sha256:" + sha,
    }
    metadata_asset = {"name": "release_metadata.json", "size": 2, "digest": "sha256:" + sha}
    return reconciler.ReleaseIntent(
        tag,
        "2026-07-30T16:44:06Z",
        sha,
        intent_asset,
        metadata_asset,
        (intent_asset, metadata_asset),
    )


def make_root(release: reconciler.ReleaseIntent, run_id: int, **changes) -> dict:
    root = {
        "id": run_id,
        "title": release.root_title,
        "status": "completed",
        "conclusion": "success",
        "attempt": 1,
        "event": "workflow_dispatch",
        "created_at": "2026-07-30T16:44:12Z",
    }
    root.update(changes)
    return root


def exhausted(release: reconciler.ReleaseIntent, run_id: int) -> dict:
    return make_root(release, run_id, conclusion="failure", attempt=reconciler.MAX_RERUN_ATTEMPTS)


def acknowledgement(
    tag: str = ACKNOWLEDGED_TAG,
    root_run_id: int = ACKNOWLEDGED_ROOT,
    acknowledged_on: str = "2026-09-07",
    reason: str = "superseded by later successful cycles",
) -> reconciler.DeadLetterAcknowledgement:
    return reconciler.DeadLetterAcknowledgement(tag, root_run_id, acknowledged_on, reason)


class DeadLetterReportingTest(unittest.TestCase):
    def scan(self, releases, roots, acknowledgements, argv=()):
        by_title = {}
        for root in roots:
            by_title.setdefault(root["title"], []).append(root)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(reconciler, "published_intents", return_value=list(releases)), \
                mock.patch.object(reconciler, "target_runs", return_value=by_title), \
                mock.patch.object(
                    reconciler, "load_acknowledgements", return_value=acknowledgements
                ), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = reconciler.main(["--source-repo", SOURCE_REPO, *argv])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_unacknowledged_terminal_dead_letter_still_warns(self):
        release = make_release(30558924132)
        code, out, err = self.scan([release], [exhausted(release, ACKNOWLEDGED_ROOT)], {})
        self.assertEqual(0, code)
        self.assertIn(
            f"::warning::{release.tag}: terminal dead letter: "
            f"root {ACKNOWLEDGED_ROOT} exhausted 3 attempts (failure)",
            out,
        )
        self.assertNotIn("::notice::", out)
        self.assertIn("terminal=1 (acknowledged=0, new=1)", out)
        self.assertEqual("", err)

    def test_acknowledged_terminal_dead_letter_reports_once_and_never_warns(self):
        release = make_release(30558924132)
        code, out, err = self.scan(
            [release],
            [exhausted(release, ACKNOWLEDGED_ROOT)],
            {release.tag: acknowledgement(tag=release.tag)},
        )
        self.assertEqual(0, code)
        self.assertNotIn("::warning::", out)
        self.assertNotIn("::error::", out + err)
        self.assertIn(
            "::notice::1 acknowledged terminal dead letter "
            f"({release.tag}, root {ACKNOWLEDGED_ROOT}, acked 2026-09-07: "
            "superseded by later successful cycles)",
            out,
        )
        self.assertEqual(1, out.count("::notice::"))
        self.assertIn("terminal=1 (acknowledged=1, new=0)", out)
        self.assertEqual("", err)

    def test_acknowledgement_of_a_release_that_never_went_terminal_is_stale(self):
        """An acknowledgement for a healthy release, and one for a tag that is
        not in the store at all, both have to surface — otherwise the file rots."""
        release = make_release(30558924132)
        never_published = make_release(29999999999).tag
        self.assertNotEqual(release.tag, never_published)
        acknowledgements = {
            release.tag: acknowledgement(tag=release.tag),
            never_published: acknowledgement(tag=never_published),
        }
        code, out, err = self.scan(
            [release], [make_root(release, ACKNOWLEDGED_ROOT)], acknowledgements
        )
        self.assertEqual(0, code)
        self.assertIn(f"{release.tag}: complete", out)
        for tag in (release.tag, never_published):
            self.assertIn(
                f"::warning::stale acknowledgement: {tag} (root {ACKNOWLEDGED_ROOT}, "
                "acked 2026-09-07) matched no terminal dead letter in this scan; "
                "update or drop it in acknowledged_dead_letters.json",
                out,
            )
        self.assertNotIn("::notice::", out)
        self.assertIn("complete=1 terminal=0 (acknowledged=0, new=0)", out)
        self.assertEqual("", err)

    def test_acknowledgement_matches_the_exact_tag_and_the_exact_root(self):
        """A prefix of an acknowledged tag is a different release, and a
        different root under the same tag is a different dead letter."""
        prefixed = make_release(30558924132, attempt=12)
        self.assertTrue(prefixed.tag.startswith(ACKNOWLEDGED_TAG))
        self.assertNotEqual(ACKNOWLEDGED_TAG, prefixed.tag)
        code, out, _ = self.scan(
            [prefixed], [exhausted(prefixed, ACKNOWLEDGED_ROOT)], {ACKNOWLEDGED_TAG: acknowledgement()}
        )
        self.assertEqual(0, code)
        self.assertIn(f"::warning::{prefixed.tag}: terminal dead letter:", out)
        self.assertIn(f"::warning::stale acknowledgement: {ACKNOWLEDGED_TAG}", out)
        self.assertIn("terminal=1 (acknowledged=0, new=1)", out)

        release = make_release(30558924132)
        code, out, _ = self.scan(
            [release],
            [exhausted(release, ACKNOWLEDGED_ROOT + 1)],
            {release.tag: acknowledgement(tag=release.tag)},
        )
        self.assertEqual(0, code)
        self.assertIn(f"::warning::{release.tag}: terminal dead letter:", out)
        self.assertIn(f"::warning::stale acknowledgement: {release.tag}", out)
        self.assertIn("terminal=1 (acknowledged=0, new=1)", out)
        self.assertNotIn("::notice::", out)

    def test_the_real_scan_shape_summarizes_18_complete_and_1_acknowledged(self):
        """The store scanned by run 34029817676: 19 releases, 18 complete."""
        releases = [make_release(30000000000 + index) for index in range(18)]
        roots = [make_root(release, 40000000000 + index) for index, release in enumerate(releases)]
        terminal = make_release(30558924132)
        releases.append(terminal)
        roots.append(exhausted(terminal, ACKNOWLEDGED_ROOT))
        code, out, err = self.scan(
            releases, roots, {terminal.tag: acknowledgement(tag=terminal.tag)}
        )
        self.assertEqual(0, code)
        self.assertIn(
            "downstream reconciliation summary: complete=18 terminal=1 "
            "(acknowledged=1, new=0)",
            out,
        )
        self.assertNotIn("::warning::", out)
        self.assertEqual(1, out.count("::notice::"))
        self.assertEqual("", err)

    def test_an_exact_tag_run_still_fails_hard_on_an_acknowledged_root(self):
        """The tombstone silences a scheduled observation, never an operator's
        own targeted reconciliation, and never the weekly's own dispatch step."""
        release = make_release(30558924132)
        code, out, err = self.scan(
            [release],
            [exhausted(release, ACKNOWLEDGED_ROOT)],
            {release.tag: acknowledgement(tag=release.tag)},
            argv=["--tag", release.tag],
        )
        self.assertEqual(1, code)
        self.assertIn("exhausted 3 attempts", err)
        self.assertIn("::error::", err)
        self.assertNotIn("::notice::", out)
        self.assertNotIn("downstream reconciliation summary", out)

    def test_a_failed_release_never_makes_its_acknowledgement_look_stale(self):
        """A transient API failure must not advise deleting a good tombstone."""
        release = make_release(30558924132)
        acknowledgements = {ACKNOWLEDGED_TAG: acknowledgement()}
        with mock.patch.object(reconciler, "reconcile_one", side_effect=reconciler.ReconcileError("API unavailable")):
            code, out, err = self.scan([release], [make_root(release, ACKNOWLEDGED_ROOT)], acknowledgements)
        self.assertEqual(1, code)
        self.assertIn("::error::", err)
        self.assertNotIn("stale acknowledgement", out)
        self.assertIn("failed=1 terminal=0 (acknowledged=0, new=0)", out)

    def test_an_unparsable_store_aborts_before_any_api_call(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
            reconciler, "load_acknowledgements", side_effect=reconciler.ReconcileError("boom")
        ), mock.patch.object(reconciler, "published_intents") as published_intents, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = reconciler.main(["--source-repo", SOURCE_REPO])
        self.assertEqual(1, code)
        published_intents.assert_not_called()
        self.assertIn("boom", stderr.getvalue())


class AcknowledgementStoreParsingTest(unittest.TestCase):
    valid = {
        "schema_version": 1,
        "acknowledged": [
            {
                "tag": ACKNOWLEDGED_TAG,
                "root_run_id": ACKNOWLEDGED_ROOT,
                "acknowledged_on": "2026-09-07",
                "reason": "superseded by later successful cycles",
            }
        ],
    }

    def load(self, text: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="ack-store-") as temporary:
            path = Path(temporary) / "acknowledged_dead_letters.json"
            path.write_text(text, encoding="utf-8")
            return reconciler.load_acknowledgements(path)

    def reject(self, value, expected: str):
        text = value if isinstance(value, str) else json.dumps(value)
        with self.assertRaises(reconciler.ReconcileError) as caught:
            self.load(text)
        message = str(caught.exception)
        self.assertIn("acknowledged_dead_letters.json", message)
        self.assertIn(expected, message)

    def entry(self, **changes) -> dict:
        entry = dict(self.valid["acknowledged"][0])
        for key, value in changes.items():
            if value is None:
                entry.pop(key)
            else:
                entry[key] = value
        return {"schema_version": 1, "acknowledged": [entry]}

    def test_a_valid_store_parses_into_exact_identities(self):
        store = self.load(json.dumps(self.valid))
        self.assertEqual([ACKNOWLEDGED_TAG], list(store))
        entry = store[ACKNOWLEDGED_TAG]
        self.assertEqual(ACKNOWLEDGED_ROOT, entry.root_run_id)
        self.assertEqual("2026-09-07", entry.acknowledged_on)
        self.assertEqual(
            f"{ACKNOWLEDGED_TAG}, root {ACKNOWLEDGED_ROOT}, acked 2026-09-07: "
            "superseded by later successful cycles",
            entry.describe(),
        )

    def test_a_malformed_entry_is_a_hard_failure_not_an_empty_store(self):
        """Fail closed.  An unreadable store must never degrade into "nothing is
        acknowledged" (the ~650 warnings come straight back and nobody knows
        why) nor into "everything is acknowledged" (a new dead letter is lost)."""
        self.reject("{not json", "cannot read")
        self.reject([], "must be an object")
        self.reject({"schema_version": 1}, "must be an object")
        self.reject({"schema_version": 2, "acknowledged": []}, "schema_version")
        self.reject({"schema_version": True, "acknowledged": []}, "schema_version")
        self.reject({"schema_version": 1, "acknowledged": {}}, "must be a list")
        self.reject({"schema_version": 1, "acknowledged": ["x"]}, "entry 0 must be an object")
        self.reject(self.entry(reason=None), "entry 0 must be an object")
        self.reject(self.entry(extra="x"), "entry 0 must be an object")
        self.reject(self.entry(tag="latest"), "tag is not an immutable release tag")
        self.reject(self.entry(tag=ACKNOWLEDGED_TAG + " "), "tag is not an immutable release tag")
        self.reject(self.entry(root_run_id=0), "root_run_id must be a positive integer")
        self.reject(self.entry(root_run_id=str(ACKNOWLEDGED_ROOT)), "root_run_id must be")
        self.reject(self.entry(root_run_id=True), "root_run_id must be")
        self.reject(self.entry(acknowledged_on="2026-9-7"), "acknowledged_on must be YYYY-MM-DD")
        self.reject(self.entry(acknowledged_on="2026-13-01"), "acknowledged_on is not a real date")
        self.reject(self.entry(reason=""), "reason must be one non-empty line")
        self.reject(self.entry(reason="   "), "reason must be one non-empty line")
        self.reject(self.entry(reason="two\nlines"), "reason must be one non-empty line")
        self.reject(self.entry(reason="two\rlines"), "reason must be one non-empty line")

    def test_a_repeated_tag_is_rejected_rather_than_silently_last_wins(self):
        entry = self.valid["acknowledged"][0]
        self.reject(
            {"schema_version": 1, "acknowledged": [entry, dict(entry, root_run_id=1)]},
            "more than once",
        )

    def test_a_missing_store_is_an_error_not_an_empty_store(self):
        with tempfile.TemporaryDirectory(prefix="ack-store-") as temporary:
            missing = Path(temporary) / "acknowledged_dead_letters.json"
            with self.assertRaises(reconciler.ReconcileError) as caught:
                reconciler.load_acknowledgements(missing)
        self.assertIn("cannot read acknowledged_dead_letters.json", str(caught.exception))

    def test_the_committed_store_tombstones_the_audited_dead_letter(self):
        """Verified read-only against GitHub on 2026-09-07: root 30562807099 is
        completed/failure at attempt 3, and release 2026-07-30_20-09-30564612136-1
        (published 76 minutes later) has a successful root, 30568505719."""
        store = reconciler.load_acknowledgements()
        self.assertIn(ACKNOWLEDGED_TAG, store)
        entry = store[ACKNOWLEDGED_TAG]
        self.assertEqual(ACKNOWLEDGED_ROOT, entry.root_run_id)
        self.assertIn("30564612136", entry.reason)
        self.assertIn("30568505719", entry.reason)


if __name__ == "__main__":
    unittest.main()
