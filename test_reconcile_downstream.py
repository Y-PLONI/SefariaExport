import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import reconcile_downstream as reconciler


class ReconcileDownstreamTest(unittest.TestCase):
    sha = "a" * 64
    tag = "2026-07-21_03-00-123-1"

    def release(self):
        intent_asset = {
            "name": f"downstream-intent-{self.sha}.json",
            "size": 2,
            "digest": "sha256:" + "b" * 64,
        }
        metadata_asset = {
            "name": "release_metadata.json",
            "size": 2,
            "digest": "sha256:" + self.sha,
        }
        return reconciler.ReleaseIntent(
            self.tag,
            "2026-07-21T03:00:00Z",
            self.sha,
            intent_asset,
            metadata_asset,
            (intent_asset, metadata_asset),
        )

    def root_run(self, **changes):
        value = {
            "id": 987,
            "title": self.release().root_title,
            "status": "completed",
            "conclusion": "success",
            "attempt": 1,
            "event": "workflow_dispatch",
            "created_at": "2026-07-21T03:01:00Z",
        }
        value.update(changes)
        return value

    def intent(self):
        return {
            "target_workflow": "sync-manual-links.yml",
            "target_repo": "Otzaria/otzaria-library",
            "source_tag": self.tag,
            "source_release_metadata_sha256": self.sha,
            "source_run_id": 123,
            "source_run_attempt": 1,
            "correlation_id": self.release().correlation_id,
        }

    def test_release_title_is_exact_and_attempt_scoped(self):
        release = self.release()
        self.assertEqual((123, 1), release.run_identity)
        self.assertEqual(
            f"sync-manual-links correlation=sefaria:123:1:{self.tag}:{self.sha}",
            release.root_title,
        )

    @mock.patch.object(reconciler, "dispatch")
    @mock.patch.object(reconciler, "verify_local_release")
    def test_missing_root_is_verified_then_dispatched_once(self, verify, dispatch):
        verify.return_value = self.intent()
        result = reconciler.reconcile_one("Otzaria/SefariaExport", self.release(), {})
        self.assertEqual("dispatched", result)
        verify.assert_called_once()
        dispatch.assert_called_once_with(self.intent())

    @mock.patch.object(reconciler, "verify_local_release")
    def test_active_and_successful_roots_are_noops(self, verify):
        release = self.release()
        active = self.root_run(status="waiting", conclusion=None)
        self.assertEqual(
            "active:waiting",
            reconciler.reconcile_one("Otzaria/SefariaExport", release, {release.root_title: [active]}),
        )
        self.assertEqual(
            "complete",
            reconciler.reconcile_one(
                "Otzaria/SefariaExport", release, {release.root_title: [self.root_run()]}
            ),
        )
        verify.assert_not_called()

    @mock.patch.object(reconciler, "gh_lines")
    @mock.patch.object(reconciler, "verify_local_release")
    def test_failed_root_is_verified_and_bounded_before_rerun(self, verify, gh_lines):
        release = self.release()
        verify.return_value = self.intent()
        failed = self.root_run(conclusion="failure", attempt=2)
        self.assertEqual(
            "rerun:3",
            reconciler.reconcile_one(
                "Otzaria/SefariaExport", release, {release.root_title: [failed]}
            ),
        )
        gh_lines.assert_called_once_with(
            ["run", "rerun", "987", "-R", "Otzaria/otzaria-library"]
        )

    @mock.patch.object(reconciler, "verify_local_release")
    def test_exhausted_or_duplicate_roots_fail_closed(self, verify):
        release = self.release()
        with self.assertRaises(reconciler.ExhaustedIntent):
            reconciler.reconcile_one(
                "Otzaria/SefariaExport",
                release,
                {release.root_title: [self.root_run(conclusion="failure", attempt=3)]},
            )
        with self.assertRaises(reconciler.ReconcileError):
            reconciler.reconcile_one(
                "Otzaria/SefariaExport",
                release,
                {
                    release.root_title: [
                        self.root_run(conclusion="failure"),
                        self.root_run(id=988, conclusion="cancelled"),
                    ]
                },
            )
        verify.assert_not_called()

    @mock.patch.object(reconciler, "verify_local_release")
    def test_duplicate_roots_are_complete_after_exact_recovery_succeeds(self, verify):
        release = self.release()
        result = reconciler.reconcile_one(
            "Otzaria/SefariaExport",
            release,
            {
                release.root_title: [
                    self.root_run(conclusion="failure"),
                    self.root_run(id=988),
                ]
            },
        )
        self.assertEqual("complete", result)
        verify.assert_not_called()

    @mock.patch.object(reconciler, "load_acknowledgements", return_value={})
    @mock.patch.object(reconciler, "target_runs")
    @mock.patch.object(reconciler, "published_intents")
    def test_full_scan_dead_letters_exhausted_but_exact_tag_fails(
        self, published_intents, target_runs, load_acknowledgements
    ):
        # Empty store on purpose: with the committed one this scan would also
        # print a *stale* acknowledgement warning, whose text contains "terminal
        # dead letter" too, and the assertion below would stop distinguishing
        # the unacknowledged-dead-letter path it exists to guard.
        release = self.release()
        failed = self.root_run(conclusion="failure", attempt=3)
        published_intents.return_value = [release]
        target_runs.return_value = {release.root_title: [failed]}

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = reconciler.main(["--source-repo", "Otzaria/SefariaExport"])
        self.assertEqual(0, result)
        self.assertIn(
            f"::warning::{release.tag}: terminal dead letter: "
            "root 987 exhausted 3 attempts (failure)",
            stdout.getvalue(),
        )
        self.assertEqual("", stderr.getvalue())

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = reconciler.main(
                [
                    "--source-repo",
                    "Otzaria/SefariaExport",
                    "--tag",
                    release.tag,
                ]
            )
        self.assertEqual(1, result)
        self.assertIn("exhausted 3 attempts", stderr.getvalue())

    def test_prepublication_or_non_dispatch_root_is_rejected(self):
        release = self.release()
        for run in (
            self.root_run(created_at="2026-07-21T02:59:59Z"),
            self.root_run(event="repository_dispatch"),
        ):
            with self.subTest(run=run):
                with self.assertRaises(reconciler.ReconcileError):
                    reconciler.reconcile_one(
                        "Otzaria/SefariaExport", release, {release.root_title: [run]}
                    )

    def test_timestamps_must_be_exact_utc_seconds(self):
        self.assertEqual(
            "+00:00",
            reconciler.parse_github_timestamp("2026-07-21T03:00:00Z").strftime("%z")[:3]
            + ":00",
        )
        for value in ("2026-07-21 03:00:00", "2026-07-21T03:00:00", "not-a-date"):
            with self.subTest(value=value), self.assertRaises(reconciler.ReconcileError):
                reconciler.parse_github_timestamp(value)

    @mock.patch.object(reconciler, "gh_lines")
    def test_release_listing_ignores_legacy_and_requires_unique_intent(self, gh_lines):
        legacy = {
            "tag": "legacy",
            "published_at": "2026-07-20T00:00:00Z",
            "assets": [{"name": "old-display.txt", "size": 1, "digest": None}],
        }
        current = {
            "tag": self.tag,
            "published_at": "2026-07-21T03:00:00Z",
            "assets": list(self.release().api_assets),
        }
        gh_lines.return_value = [json.dumps(legacy), json.dumps(current)]
        result = reconciler.published_intents("Otzaria/SefariaExport")
        self.assertEqual([self.release()], result)

        current["assets"].append(dict(current["assets"][0]))
        gh_lines.return_value = [json.dumps(current)]
        with self.assertRaises(reconciler.ReconcileError):
            reconciler.published_intents("Otzaria/SefariaExport")

    @mock.patch.object(reconciler, "gh_lines")
    def test_api_listing_failure_or_malformed_run_never_means_zero(self, gh_lines):
        gh_lines.side_effect = reconciler.ReconcileError("API unavailable")
        with self.assertRaises(reconciler.ReconcileError):
            reconciler.target_runs()
        gh_lines.side_effect = None
        gh_lines.return_value = [json.dumps({"id": 1})]
        with self.assertRaises(reconciler.ReconcileError):
            reconciler.target_runs()

    @mock.patch.object(reconciler, "gh_lines")
    def test_dispatch_uses_argv_and_does_not_retry_ambiguous_failure(self, gh_lines):
        gh_lines.side_effect = reconciler.ReconcileError("response lost")
        with self.assertRaises(reconciler.ReconcileError):
            reconciler.dispatch(self.intent())
        self.assertEqual(1, gh_lines.call_count)
        argv = gh_lines.call_args.args[0]
        self.assertEqual(["workflow", "run", "sync-manual-links.yml"], argv[:3])
        self.assertIn(f"correlation_id={self.release().correlation_id}", argv)


if __name__ == "__main__":
    unittest.main()
