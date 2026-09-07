#!/usr/bin/env python3
"""Reconcile published Sefaria intents with exact Otzaria root runs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from downstream_intent import (
    INTENT_NAME_RE,
    REPO_RE,
    TARGET_REPO,
    TARGET_WORKFLOW,
    load_and_validate,
)
from release_contract import (
    TAG_RE,
    ContractError,
    read_json,
    require_int,
    sha256_file,
)
from resolve_release_chain_head import verify_release_asset_contract


ACTIVE_STATUSES = {"requested", "waiting", "pending", "queued", "in_progress"}
TERMINAL_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "neutral",
    "skipped",
    "stale",
    "startup_failure",
    "success",
    "timed_out",
}
MAX_RERUN_ATTEMPTS = 3
GITHUB_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
ACKNOWLEDGEMENT_FILE = Path(__file__).resolve().parent / "acknowledged_dead_letters.json"
ACKNOWLEDGEMENT_SCHEMA_VERSION = 1
ACKNOWLEDGEMENT_KEYS = {"tag", "root_run_id", "acknowledged_on", "reason"}
ACKNOWLEDGEMENT_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class ReconcileError(RuntimeError):
    pass


class ExhaustedIntent(ReconcileError):
    """A downstream root already emitted its bounded failure attempts."""

    def __init__(self, message: str, root_run_id: int) -> None:
        super().__init__(message)
        # The acknowledgement store binds a tag to the exact root that went
        # terminal, so the root identity has to survive the raise.
        self.root_run_id = root_run_id


def parse_github_timestamp(value: str) -> dt.datetime:
    if not isinstance(value, str) or not GITHUB_TIMESTAMP_RE.fullmatch(value):
        raise ReconcileError(f"invalid GitHub UTC timestamp: {value!r}")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError as exc:
        raise ReconcileError(f"invalid GitHub UTC timestamp: {value!r}") from exc


@dataclass(frozen=True)
class DeadLetterAcknowledgement:
    """One operator tombstone for a terminal downstream dead letter."""

    tag: str
    root_run_id: int
    acknowledged_on: str
    reason: str

    def describe(self) -> str:
        return f"{self.tag}, root {self.root_run_id}, acked {self.acknowledged_on}: {self.reason}"


def load_acknowledgements(path: Path = ACKNOWLEDGEMENT_FILE) -> dict[str, DeadLetterAcknowledgement]:
    """Read the committed acknowledgement store, or fail closed.

    A missing, unreadable, or malformed store is a hard error and never an empty
    acknowledgement set.  Degrading to "nothing is acknowledged" would silently
    restore the ~650 identical ``::warning::`` lines this file exists to remove,
    and the store would rot without anyone noticing; degrading the other way
    would hide a genuinely new dead letter.  Neither is acceptable, so the
    reconciler refuses to run on a store it cannot parse.
    """
    try:
        value = read_json(path)
    except ContractError as exc:
        raise ReconcileError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "acknowledged"}:
        raise ReconcileError(f"{path.name} must be an object of {{schema_version,acknowledged}}")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != ACKNOWLEDGEMENT_SCHEMA_VERSION
    ):
        raise ReconcileError(f"unsupported {path.name} schema_version")
    if not isinstance(value["acknowledged"], list):
        raise ReconcileError(f"{path.name} acknowledged must be a list")
    result: dict[str, DeadLetterAcknowledgement] = {}
    for index, entry in enumerate(value["acknowledged"]):
        where = f"{path.name} entry {index}"
        if not isinstance(entry, dict) or set(entry) != ACKNOWLEDGEMENT_KEYS:
            raise ReconcileError(
                f"{where} must be an object of {{tag,root_run_id,acknowledged_on,reason}}"
            )
        tag = entry["tag"]
        if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
            raise ReconcileError(f"{where} tag is not an immutable release tag")
        try:
            root_run_id = require_int(entry["root_run_id"], "root_run_id")
        except ContractError as exc:
            raise ReconcileError(f"{where} ({tag}): {exc}") from exc
        acknowledged_on = entry["acknowledged_on"]
        if not isinstance(acknowledged_on, str) or not ACKNOWLEDGEMENT_DATE_RE.fullmatch(
            acknowledged_on
        ):
            raise ReconcileError(f"{where} ({tag}): acknowledged_on must be YYYY-MM-DD")
        try:
            dt.date.fromisoformat(acknowledged_on)
        except ValueError as exc:
            raise ReconcileError(f"{where} ({tag}): acknowledged_on is not a real date") from exc
        reason = entry["reason"]
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or "\n" in reason
            or "\r" in reason
        ):
            raise ReconcileError(f"{where} ({tag}): reason must be one non-empty line")
        if tag in result:
            raise ReconcileError(f"{path.name} acknowledges {tag} more than once")
        result[tag] = DeadLetterAcknowledgement(tag, root_run_id, acknowledged_on, reason)
    return result


@dataclass(frozen=True)
class ReleaseIntent:
    tag: str
    published_at: str
    metadata_sha256: str
    intent_asset: dict
    metadata_asset: dict
    api_assets: tuple[dict, ...]

    @property
    def run_identity(self) -> tuple[int, int]:
        match = re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-([0-9]+)-([0-9]+)",
            self.tag,
        )
        if not match or int(match.group(1)) <= 0 or int(match.group(2)) <= 0:
            raise ReconcileError(f"release tag does not contain a valid run identity: {self.tag}")
        return int(match.group(1)), int(match.group(2))

    @property
    def correlation_id(self) -> str:
        run_id, run_attempt = self.run_identity
        return f"sefaria:{run_id}:{run_attempt}:{self.tag}:{self.metadata_sha256}"

    @property
    def root_title(self) -> str:
        return f"sync-manual-links correlation={self.correlation_id}"


def gh_lines(arguments: list[str]) -> list[str]:
    try:
        completed = subprocess.run(
            ["gh", *arguments],
            check=True,
            text=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise ReconcileError(f"gh {' '.join(arguments)} failed: {detail}") from exc
    return [line for line in completed.stdout.splitlines() if line]


def strict_api_asset(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"name", "size", "digest"}:
        raise ReconcileError("GitHub release asset has an unexpected shape")
    if not isinstance(value["name"], str):
        raise ReconcileError("GitHub release asset name is not a string")
    if isinstance(value["size"], bool) or not isinstance(value["size"], int) or value["size"] < 0:
        raise ReconcileError("GitHub release asset size is invalid")
    if not isinstance(value["digest"], str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", value["digest"]
    ):
        raise ReconcileError("GitHub release asset digest is not a SHA-256")
    return value


def published_intents(source_repo: str, only_tag: str = "") -> list[ReleaseIntent]:
    if not isinstance(source_repo, str) or not REPO_RE.fullmatch(source_repo):
        raise ReconcileError("source repository must be owner/name")
    jq = (
        '.[] | select(.draft|not) | '
        '{tag:.tag_name,published_at:.published_at,'
        'assets:[.assets[]|{name:.name,size:.size,digest:.digest}]} | @json'
    )
    rows = gh_lines([
        "api",
        "--paginate",
        f"repos/{source_repo}/releases?per_page=100",
        "--jq",
        jq,
    ])
    releases = []
    seen_tags = set()
    for row in rows:
        try:
            value = json.loads(row)
        except json.JSONDecodeError as exc:
            raise ReconcileError("GitHub releases API returned invalid JSON") from exc
        if not isinstance(value, dict) or set(value) != {"tag", "published_at", "assets"}:
            raise ReconcileError("GitHub release row has an unexpected shape")
        tag = value["tag"]
        published_at = value["published_at"]
        if not isinstance(tag, str) or not isinstance(published_at, str) or not isinstance(value["assets"], list):
            raise ReconcileError("GitHub release identity is invalid")
        if tag in seen_tags:
            raise ReconcileError(f"GitHub releases API repeated tag {tag}")
        seen_tags.add(tag)
        if only_tag and tag != only_tag:
            continue
        parse_github_timestamp(published_at)
        raw_assets = value["assets"]
        intent_candidates = [
            asset
            for asset in raw_assets
            if isinstance(asset, dict)
            and isinstance(asset.get("name"), str)
            and INTENT_NAME_RE.fullmatch(asset["name"])
        ]
        # Legacy releases intentionally have no delivery intent.  Do not impose
        # the newer API-digest contract on unrelated historical assets.
        if not intent_candidates:
            continue
        assets = [strict_api_asset(asset) for asset in raw_assets]
        intent_assets = [asset for asset in assets if INTENT_NAME_RE.fullmatch(asset["name"])]
        if len(intent_assets) != 1:
            raise ReconcileError(f"release {tag} has multiple downstream intents")
        metadata_assets = [asset for asset in assets if asset["name"] == "release_metadata.json"]
        if len(metadata_assets) != 1:
            raise ReconcileError(f"release {tag} must contain exactly one release_metadata.json")
        metadata_sha = INTENT_NAME_RE.fullmatch(intent_assets[0]["name"]).group(1)
        releases.append(
            ReleaseIntent(
                tag,
                published_at,
                metadata_sha,
                intent_assets[0],
                metadata_assets[0],
                tuple(assets),
            )
        )
    if only_tag and not releases:
        raise ReconcileError(f"published release {only_tag} has no durable downstream intent")
    return releases


def target_runs() -> dict[str, list[dict]]:
    jq = (
        '.workflow_runs[] | '
        '{id:.id,title:.display_title,status:.status,conclusion:.conclusion,'
        'attempt:.run_attempt,event:.event,created_at:.created_at} | @json'
    )
    rows = gh_lines([
        "api",
        "--paginate",
        f"repos/{TARGET_REPO}/actions/workflows/{TARGET_WORKFLOW}/runs?per_page=100",
        "--jq",
        jq,
    ])
    by_title: dict[str, list[dict]] = {}
    seen_ids = set()
    for row in rows:
        try:
            value = json.loads(row)
        except json.JSONDecodeError as exc:
            raise ReconcileError("GitHub workflow-runs API returned invalid JSON") from exc
        expected = {"id", "title", "status", "conclusion", "attempt", "event", "created_at"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ReconcileError("GitHub workflow-run row has an unexpected shape")
        if (
            isinstance(value["id"], bool)
            or not isinstance(value["id"], int)
            or value["id"] <= 0
            or isinstance(value["attempt"], bool)
            or not isinstance(value["attempt"], int)
            or value["attempt"] <= 0
            or not all(isinstance(value[field], str) for field in ("title", "status", "event", "created_at"))
            or (value["conclusion"] is not None and not isinstance(value["conclusion"], str))
        ):
            raise ReconcileError("GitHub workflow-run values are invalid")
        if value["id"] in seen_ids:
            continue
        seen_ids.add(value["id"])
        by_title.setdefault(value["title"], []).append(value)
    return by_title


def verify_local_release(source_repo: str, release: ReleaseIntent) -> dict:
    with tempfile.TemporaryDirectory(prefix="sefaria-downstream-intent-") as temporary:
        root = Path(temporary)
        for pattern in (release.intent_asset["name"], "release_metadata.json"):
            gh_lines([
                "release",
                "download",
                release.tag,
                "-R",
                source_repo,
                "--pattern",
                pattern,
                "--dir",
                str(root),
            ])
        intent_path = root / release.intent_asset["name"]
        metadata_path = root / "release_metadata.json"
        intent, metadata, metadata_sha = load_and_validate(
            intent_path, metadata_path, source_repo
        )
        if metadata["tag"] != release.tag or metadata_sha != release.metadata_sha256:
            raise ReconcileError(f"release {release.tag} identity differs from its intent")
        verify_release_asset_contract(
            metadata,
            metadata_path,
            list(release.api_assets),
        )
        if (
            release.intent_asset["size"] != intent_path.stat().st_size
            or release.intent_asset["digest"] != f"sha256:{sha256_file(intent_path)}"
        ):
            raise ReconcileError(f"release {release.tag} intent bytes differ from the GitHub digest")
        ref = gh_lines([
            "api",
            f"repos/{source_repo}/git/ref/tags/{release.tag}",
            "--jq",
            ".object.sha",
        ])
        if ref != [metadata["source_commit"]]:
            raise ReconcileError(f"release {release.tag} tag target differs from metadata")
        return intent


def run_matches_release(run: dict, release: ReleaseIntent) -> None:
    if run["event"] != "workflow_dispatch":
        raise ReconcileError(f"root {run['id']} was not workflow_dispatch")
    run_created = parse_github_timestamp(run["created_at"])
    release_created = parse_github_timestamp(release.published_at)
    if run_created < release_created:
        raise ReconcileError(f"root {run['id']} predates release {release.tag}")


def dispatch(intent: dict) -> None:
    # Deliberately one attempt.  A non-zero gh result has ambiguous delivery
    # semantics; the next scheduled reconciliation will observe before retrying.
    gh_lines([
        "workflow",
        "run",
        intent["target_workflow"],
        "-R",
        intent["target_repo"],
        "-f",
        f"sefaria_tag={intent['source_tag']}",
        "-f",
        f"sefaria_release_metadata_sha256={intent['source_release_metadata_sha256']}",
        "-f",
        f"sefaria_run_id={intent['source_run_id']}",
        "-f",
        f"sefaria_run_attempt={intent['source_run_attempt']}",
        "-f",
        f"correlation_id={intent['correlation_id']}",
    ])


def reconcile_one(source_repo: str, release: ReleaseIntent, runs: dict[str, list[dict]]) -> str:
    exact = runs.get(release.root_title, [])
    if len(exact) > 1:
        # A bounded operator recovery may create a second root with the same
        # immutable correlation after the original root fails.  Once any exact
        # root succeeds, downstream delivery is complete and future scheduled
        # scans must not keep failing on the now-harmless duplicate history.
        for run in exact:
            run_matches_release(run, release)
        if any(
            run["status"] == "completed" and run["conclusion"] == "success"
            for run in exact
        ):
            return "complete"
        raise ReconcileError(
            f"release {release.tag} has {len(exact)} exact roots; refusing to choose"
        )
    if not exact:
        intent = verify_local_release(source_repo, release)
        dispatch(intent)
        return "dispatched"
    run = exact[0]
    run_matches_release(run, release)
    status = run["status"]
    conclusion = run["conclusion"]
    if status in ACTIVE_STATUSES and conclusion is None:
        return f"active:{status}"
    if status != "completed" or conclusion not in TERMINAL_CONCLUSIONS:
        raise ReconcileError(f"root {run['id']} has unknown state {status}:{conclusion}")
    if conclusion == "success":
        return "complete"
    if run["attempt"] >= MAX_RERUN_ATTEMPTS:
        raise ExhaustedIntent(
            f"root {run['id']} exhausted {MAX_RERUN_ATTEMPTS} attempts ({conclusion})",
            run["id"],
        )
    verify_local_release(source_repo, release)
    gh_lines(["run", "rerun", str(run["id"]), "-R", TARGET_REPO])
    return f"rerun:{run['attempt'] + 1}"


def report_scan(
    acknowledgements: dict[str, DeadLetterAcknowledgement],
    acknowledged: list[DeadLetterAcknowledgement],
    counts: dict[str, int],
    new_dead_letters: int,
    failures: list[str],
) -> None:
    """Close a full-store scan with one acknowledgement line and one summary.

    Only the scheduled full scan reports this; an operator run targeting one
    exact tag still gets that tag's own hard failure and nothing else.
    """
    if acknowledged:
        noun = "dead letter" if len(acknowledged) == 1 else "dead letters"
        detail = "; ".join(entry.describe() for entry in acknowledged)
        print(f"::notice::{len(acknowledged)} acknowledged terminal {noun} ({detail})")
    if not failures:
        # A transient API failure on one release also leaves its acknowledgement
        # unmatched.  Reporting it as stale then tells an operator to delete a
        # perfectly good tombstone, so only a clean scan may call one stale.
        matched = {entry.tag for entry in acknowledged}
        for tag in sorted(set(acknowledgements) - matched):
            entry = acknowledgements[tag]
            # Says "matched no terminal dead letter", not "is not one": the same
            # tag may well be terminal in this scan under a *different* root, in
            # which case the entry needs its root_run_id corrected rather than
            # deleted.  Both repairs are covered by "update or drop".
            print(
                f"::warning::stale acknowledgement: {tag} (root {entry.root_run_id}, "
                f"acked {entry.acknowledged_on}) matched no terminal dead letter in "
                f"this scan; update or drop it in {ACKNOWLEDGEMENT_FILE.name}"
            )
    parts = [f"complete={counts.get('complete', 0)}"]
    for name in ("dispatched", "active", "rerun"):
        if counts.get(name):
            parts.append(f"{name}={counts[name]}")
    if failures:
        parts.append(f"failed={len(failures)}")
    terminal = len(acknowledged) + new_dead_letters
    parts.append(
        f"terminal={terminal} (acknowledged={len(acknowledged)}, new={new_dead_letters})"
    )
    print("downstream reconciliation summary: " + " ".join(parts))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-repo", required=True)
    parser.add_argument("--tag", default="")
    args = parser.parse_args(argv)
    try:
        # Parsed before the first API call so a broken store fails fast, and so
        # a scan can never reach the dead-letter branch without knowing what an
        # operator has already acknowledged.
        acknowledgements = load_acknowledgements()
        releases = published_intents(args.source_repo, args.tag)
        if not releases:
            print("no published downstream intents")
            return 0
        runs = target_runs()
        failures = []
        counts: dict[str, int] = {}
        acknowledged: list[DeadLetterAcknowledgement] = []
        new_dead_letters = 0
        for release in releases:
            try:
                result = reconcile_one(args.source_repo, release, runs)
                print(f"{release.tag}: {result}")
                bucket = result.split(":", 1)[0]
                counts[bucket] = counts.get(bucket, 0) + 1
            except ExhaustedIntent as exc:
                # A scheduled full-store scan has already reported this root's
                # bounded failures through the root runs themselves.  Treat it
                # as a dead-letter terminal state so every future timer tick
                # does not send another identical failure notification.  An
                # operator targeting the exact tag still gets a hard failure.
                if args.tag:
                    failures.append(f"{release.tag}: {exc}")
                    print(f"::error::{release.tag}: {exc}", file=sys.stderr)
                    continue
                entry = acknowledgements.get(release.tag)
                # Exact identity, never a prefix: the tombstone is bound to the
                # one root that went terminal, so a different root under the
                # same tag stays a new dead letter and keeps its ::warning::.
                if entry is not None and entry.root_run_id == exc.root_run_id:
                    acknowledged.append(entry)
                else:
                    new_dead_letters += 1
                    print(f"::warning::{release.tag}: terminal dead letter: {exc}")
            except (ContractError, ReconcileError, OSError) as exc:
                failures.append(f"{release.tag}: {exc}")
                print(f"::error::{release.tag}: {exc}", file=sys.stderr)
        if not args.tag:
            report_scan(acknowledgements, acknowledged, counts, new_dead_letters, failures)
        if failures:
            raise ReconcileError(f"{len(failures)} downstream intent(s) failed reconciliation")
        return 0
    except (ContractError, ReconcileError, OSError) as exc:
        print(f"downstream reconciliation error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
