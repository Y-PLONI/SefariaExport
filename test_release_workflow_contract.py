import subprocess
import unittest
from pathlib import Path


class ReleaseWorkflowContractTest(unittest.TestCase):
    def test_link_visibility_contract_runs_in_release_validation(self):
        root = Path(__file__).resolve().parent
        for relative in (".github/workflows/release.yml", ".github/workflows/reconcile-downstream.yml"):
            workflow = (root / relative).read_text(encoding="utf-8")
            self.assertIn("test_link_visibility.py", workflow, relative)

    def test_legacy_release_mutators_fail_closed(self):
        root = Path(__file__).resolve().parent
        for name in ("20_create_or_update_release.sh", "21_upload_release_assets.sh"):
            result = subprocess.run(
                ["bash", str(root / name)], text=True, capture_output=True, check=False
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn(".github/workflows/release.yml", result.stderr)

    def test_display_assets_are_compared_after_remote_download(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("for display_asset in CHANGELOG.md forum_changelog_diff.json", workflow)
        self.assertIn('cmp "$display_asset" "release-verification/$display_asset"', workflow)

    def test_free_disk_space_action_is_pinned_to_a_full_commit(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn(
            "jlumbroso/free-disk-space@54081f138730dfa15788a46383842cd2f914a1be",
            workflow,
        )
        self.assertNotIn("jlumbroso/free-disk-space@main", workflow)

    def test_the_apt_remove_pass_stays_off(self):
        """`large-packages` freed 4.4 GiB the job never needs and cost 1,352 log
        lines, 882 of them `Package 'php-...' is not installed, so not removed`.
        Peak disk during run 33987734987 was 42G of 145G."""
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("large-packages: false", workflow)
        self.assertNotIn("large-packages: true", workflow)

    def test_mongod_is_quiet_and_never_streamed_into_the_job_log(self):
        """6,294 mongod lines / 2.51 MB — 54% of the bytes of run 33987734987 —
        and 2,208 of them were emitted after the DB was dropped, during zstd."""
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('"mongod", "--quiet"', compose)
        self.assertIn("docker compose up --abort-on-container-exit --no-attach mongodb",
                      workflow)

    def test_mongod_warnings_and_errors_are_replayed_before_teardown(self):
        """Cutting the stream must never cut the diagnostics with it."""
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        replay = "- name: Replay every mongod warning and error"
        self.assertIn(replay, workflow)
        self.assertIn('grep -E \'"s":"(W|E|F)"\'', workflow)
        # It has to read the container logs while the container still exists.
        self.assertLess(workflow.index(replay),
                        workflow.index("- name: Stop and clean up containers"))

    def test_the_mongod_replay_step_cannot_redden_a_green_job(self):
        """`shell: bash` is `bash --noprofile --norc -eo pipefail`, so errexit and
        pipefail are ON.  A clean run has no W/E/F lines, `grep` then exits 1, and
        without these guards a diagnostics step fails the whole weekly."""
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        start = workflow.index("- name: Replay every mongod warning and error")
        step = workflow[start:workflow.index("- name: Stop and clean up containers")]
        self.assertIn("set +e", step)
        self.assertIn("set +o pipefail", step)
        self.assertIn('grep -E \'"s":"(W|E|F)"\' || true', step)
        self.assertIn("exit 0", step)

    def test_export_image_is_built_once_against_a_reusable_layer_cache(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("cache-from: type=gha", workflow)
        # The cached build and the compose run have to name the same image, or
        # `up` silently rebuilds everything the cached build just produced.
        self.assertIn("tags: sefaria-exporter:local", workflow)
        self.assertIn("image: sefaria-exporter:local", compose)
        # setup-buildx-action gives buildx the docker-container driver, which
        # keeps the result out of the local daemon unless it is loaded back —
        # without this line `up` rebuilds the image the cached build produced.
        self.assertIn("load: true", workflow)
        self.assertIn("run: docker compose up --abort-on-container-exit", workflow)
        self.assertNotIn("docker compose up --build", workflow)

    def test_initial_baseline_skips_only_the_downstream_dispatch(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn('--allow-initial-baseline "$ALLOW_INITIAL"', workflow)
        self.assertIn('echo "is_initial=$is_initial" >> "$GITHUB_OUTPUT"', workflow)
        self.assertIn("if: steps.previous.outputs.is_initial == 'false'", workflow)
        self.assertEqual(2, workflow.count("if: steps.previous.outputs.is_initial == 'false'"))

    def test_distinct_exports_use_the_durable_pending_queue(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("group: sefaria-export-release", workflow)
        self.assertIn("queue: max", workflow)

    def test_published_release_carries_a_durable_downstream_intent(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("python3 downstream_intent.py build", workflow)
        self.assertIn('intent_assets+=("$INTENT_ASSET")', workflow)
        self.assertIn("python3 downstream_intent.py validate", workflow)
        self.assertIn(
            'python3 reconcile_downstream.py --source-repo "$SOURCE_REPO" --tag "$TAG"',
            workflow,
        )
        self.assertNotIn("for attempt in 1 2 3", workflow)

    def test_scheduled_reconciler_recovers_missing_root(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/reconcile-downstream.yml").read_text(encoding="utf-8")
        self.assertIn("schedule:", workflow)
        self.assertIn("python3 reconcile_downstream.py", workflow)
        self.assertIn("secrets.PIPELINE_TOKEN", workflow)

    def test_dead_letter_acknowledgements_are_gated_wherever_the_reconciler_runs(self):
        """The store is read on both call sites, and it fails closed, so the
        gate that proves it parses has to precede the reconciler in both."""
        root = Path(__file__).resolve().parent
        self.assertTrue((root / "acknowledged_dead_letters.json").is_file())
        for relative in (".github/workflows/release.yml", ".github/workflows/reconcile-downstream.yml"):
            workflow = (root / relative).read_text(encoding="utf-8")
            self.assertIn("test_dead_letter_acknowledgements.py", workflow, relative)
            self.assertLess(
                workflow.index("test_dead_letter_acknowledgements.py"),
                workflow.index("python3 reconcile_downstream.py"),
                relative,
            )

    def test_weekly_dispatch_has_an_exact_adoptable_identity(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("orchestration_id:", workflow)
        self.assertIn("Sefaria immutable export orchestration=${{ inputs.orchestration_id", workflow)
        self.assertIn("^weekly:[1-9][0-9]*:[1-9][0-9]*$", workflow)


if __name__ == "__main__":
    unittest.main()
