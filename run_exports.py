#!/usr/bin/env python3
"""
Run a narrow Sefaria export tailored to what the SefariaSqlite generator
actually consumes:

  * Only the JSON merged format (drop txt / cltk-full / cltk-flat).
  * Only the Hebrew (`he`) language (skip the English merged pass entirely).
  * Plus links / schemas / TOC.

The cuts are applied at the source (Sefaria's `export_formats` tuple and a
custom `export_all_merged` loop), so we save both disk IO and CPU compared
to running the full upstream export.
"""
import contextlib
import io
import json
import os
import re
import sys
import time
import traceback
from collections import namedtuple
from pathlib import Path

# --- progress and accounting -------------------------------------------------
#
# Run 33987734987 spent 12,759 log lines (42% of the whole job) echoing every
# title twice, and still could not say what happened to 4 of them.  The rules
# here: one bounded progress line per stage instead of one line per item, an
# arithmetic identity in every summary, and a name for everything that did not
# get written.  Nothing upstream prints is discarded — anything we do not
# recognise is replayed verbatim (see `classify_title_output`).

EXPORT_REPORT_DIRNAME = "metadata"
EXPORT_REPORT_FILENAME = "export_report.json"
EXPORT_REPORT_SCHEMA_VERSION = 1

# Set EXPORT_VERBOSE_TITLES=1 to get the old per-title firehose back.
VERBOSE_TITLES_ENV = "EXPORT_VERBOSE_TITLES"

# How many names of one class to spell out in the log before deferring to
# export_report.json, which always carries the complete list.
NAMES_IN_LOG = 30


def verbose_titles() -> bool:
    return os.environ.get(VERBOSE_TITLES_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def format_duration(seconds) -> str:
    """`45s` / `9m17s` / `1h02m` — short enough to sit inside a progress line."""
    seconds = int(round(max(0.0, float(seconds))))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def format_progress(done, total, elapsed, counters=None) -> str:
    """One progress line: `  …6400/6474 (98.9%) written=6161 … elapsed 45s eta ~1s`.

    `total` may be 0/None when the stage cannot count its work up front; the
    line then drops the percentage and the ETA rather than inventing them.
    """
    if total:
        parts = [f"  …{done}/{total} ({done * 100.0 / total:.1f}%)"]
    else:
        parts = [f"  …{done}"]
    if counters:
        parts.append(" ".join(f"{k}={v}" for k, v in counters.items()))
    parts.append(f"elapsed {format_duration(elapsed)}")
    if total and 0 < done < total:
        parts.append(f"eta ~{format_duration(elapsed * (total - done) / done)}")
    return " ".join(parts)


class ProgressTicker:
    """Fires on the first item, then every `every` items or `seconds` seconds,
    whichever comes first — so a stage is visibly alive from the start without
    costing one log line per item.

    The clock is injectable so the cadence is testable without sleeping.
    """

    def __init__(self, total, every=100, seconds=60.0, clock=time.monotonic):
        self.total = total or 0
        self.every = max(1, int(every))
        self.seconds = float(seconds)
        self._clock = clock
        self.started = clock()
        self._last_at = self.started
        self._last_done = 0
        self._fired = False

    @property
    def elapsed(self) -> float:
        return self._clock() - self.started

    def due(self, done) -> bool:
        if not self._fired:
            return True
        if done - self._last_done >= self.every:
            return True
        return (self._clock() - self._last_at) >= self.seconds

    def tick(self, done, counters=None, force=False):
        """The line to print, or None when it is not time for one yet.

        `force` closes a stage at 100% without repeating a tick that already
        reported that same count.
        """
        if force:
            if self._fired and self._last_done == done:
                return None
        elif not self.due(done):
            return None
        now = self._clock()
        self._fired = True
        self._last_at = now
        self._last_done = done
        return format_progress(done, self.total, now - self.started, counters)


def format_named(label, names, limit=NAMES_IN_LOG) -> str:
    """`   252 no_he_version: A, B, … and 222 more` — bounded, never silent.

    The tail is counted rather than dropped, and export_report.json carries the
    whole list, so "which books fell out this week" is always answerable.
    """
    if not names:
        return ""
    line = f"   {len(names)} {label}: " + ", ".join(str(n) for n in names[:limit])
    if len(names) > limit:
        line += f" … and {len(names) - limit} more (see {EXPORT_REPORT_FILENAME})"
    return line


def print_named(counts, names, order) -> None:
    for label in order:
        if counts.get(label):
            line = format_named(label, names.get(label, []))
            if line:
                print(line, flush=True)


TitleChatter = namedtuple("TitleChatter", "versions outcome reason residual")

_VERSIONS_RE = re.compile(r"^(\d+) versions in \w+$")


def classify_title_output(title, captured) -> TitleChatter:
    """Split upstream's per-title chatter into facts, and keep the rest.

    `sefaria.export.prepare_text_for_export` prints the title it is about to
    prepare and then, on two paths, returns None:

      * `Skipping <title> - <error>` when `library.get_index` raised;
      * **nothing at all** when the index has virtual leaf nodes, which upstream
        drops on the floor silently.

    That silent return is why run 33987734987 wrote 6,216 of the 6,220 titles
    that had a Hebrew version while reporting neither a skip nor an error for
    the other 4.  `write_text_doc_to_disk` has a third one: `Skipping <title> -
    no content`, which returns *after* the caller has already decided the doc
    was good — counted as written today, though nothing reached disk.

    Recognised lines are dropped as duplication (the counters carry them).
    Everything else comes back in `residual` for the caller to replay verbatim.
    """
    versions = None
    outcome = None
    reason = ""
    residual = []
    prefix = f"Skipping {title} - "
    for line in captured.splitlines():
        if not line.strip():
            continue
        match = _VERSIONS_RE.match(line)
        if match:
            versions = int(match.group(1))
            continue
        if line == title:
            continue
        if line.startswith(prefix):
            reason = line[len(prefix):]
            outcome = "no_content" if reason == "no content" else "index_error"
            continue
        residual.append(line)
    return TitleChatter(versions, outcome, reason, residual)


def replay_chatter(title, lines) -> None:
    """Print upstream output we captured but did not account for."""
    for line in lines:
        print(f"  [{title}] {line}", flush=True)


def list_dir_limited(base: str) -> None:
    for root, dirs, files in os.walk(base):
        level = root.replace(base, '').count(os.sep)
        indent = ' ' * 2 * level
        print(f"{indent}{os.path.basename(root)}/")
        subindent = ' ' * 2 * (level + 1)
        for file in files[:10]:
            print(f"{subindent}{file}")
        if len(files) > 10:
            print(f"{subindent}... and {len(files) - 10} more files")
        if level > 2:
            break


# Every title lands in exactly one of these, so the classes sum to the title
# count by construction and the summary can print an identity instead of three
# numbers that happen not to add up.
MERGED_CLASSES = (
    "written",
    "blank_title",      # `distinct` handed back "" / None
    "bad_ref",          # Ref(title) raised — not a citable text
    "no_he_version",    # 0 non-copyright Hebrew versions
    "virtual_nodes",    # upstream refuses indexes with virtual leaf nodes
    "index_error",      # library.get_index raised
    "no_content",       # make_json produced nothing; write was a no-op
    "errors",
)

# Everything that is neither written nor an error, in the order it is reported.
MERGED_SKIP_CLASSES = tuple(
    c for c in MERGED_CLASSES if c not in ("written", "errors")
)


def _export_one_merged_title(ex, ref_cls, title, verbose) -> tuple:
    """Export one title. Returns `(class, detail)` and never raises.

    Upstream's per-title prints are captured rather than streamed: they are the
    42% of the job log that says nothing the counters do not already say.  The
    capture is also the only place that can tell "no Hebrew version" apart from
    "upstream skipped a virtual-node index", which is the accounting hole.
    """
    if not title:
        return "blank_title", repr(title)
    try:
        ref_cls(title)
    except Exception:
        return "bad_ref", ""

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            prepped = ex.prepare_merged_text_for_export(title, lang="he")
            if prepped:
                ex.write_text_doc_to_disk(prepped)
    except Exception as e:  # pragma: no cover - upstream failure path
        replay_chatter(title, buf.getvalue().splitlines())
        print(f"⚠️  {title}: {e}", flush=True)
        return "errors", f"{type(e).__name__}: {e}"

    chatter = classify_title_output(title, buf.getvalue())
    replay_chatter(title, buf.getvalue().splitlines() if verbose else chatter.residual)

    if chatter.outcome:
        return chatter.outcome, chatter.reason
    if prepped:
        return "written", ""
    if chatter.versions == 0:
        return "no_he_version", ""
    # Upstream returned None without a word: prepare_text_for_export drops any
    # index whose leaf nodes are virtual (dictionary entries and the like).
    return "virtual_nodes", ""


def run_merged_export_he_only(ex) -> dict:
    """Replacement for `ex.export_all_merged()` — Hebrew only.

    Mirrors the upstream loop (see sefaria/export.py::export_all_merged) but
    drops the English pass to halve the number of slow Mongo lookups and
    skip writes we don't need.
    """
    from sefaria.system.database import db
    from sefaria.model.text import Ref

    titles = db.texts.find().distinct("title")
    total = len(titles)
    print(f"📋 {total} distinct titles to export (he only)", flush=True)

    counts = {name: 0 for name in MERGED_CLASSES}
    names = {name: [] for name in MERGED_CLASSES if name != "written"}
    verbose = verbose_titles()
    ticker = ProgressTicker(total)

    for idx, title in enumerate(titles, 1):
        outcome, detail = _export_one_merged_title(ex, Ref, title, verbose)
        counts[outcome] += 1
        if outcome != "written":
            names[outcome].append(
                f"{title} ({detail})" if title and detail else (title or detail))
        # Ticked *after* the work, so the last tick can never disagree with the
        # summary the way `written=6215` vs `written=6216` did in run
        # 33987734987.  written + skipped + errors == the N in N/M, always.
        line = ticker.tick(idx, {
            "written": counts["written"],
            "skipped": idx - counts["written"] - counts["errors"],
            "errors": counts["errors"],
        }, force=(idx == total))
        if line:
            print(line, flush=True)

    written, errors = counts["written"], counts["errors"]
    skipped = total - written - errors
    print(f"✅ merged export done in {format_duration(ticker.elapsed)}: "
          f"titles={total} = written={written} + skipped={skipped} + errors={errors}")
    print("   skipped breakdown: " + ", ".join(
        f"{c}={counts[c]}" for c in MERGED_SKIP_CLASSES))
    accounted = sum(counts.values())
    if accounted != total:  # pragma: no cover - guards a future refactor
        print(f"⚠️  merged export accounting does not close: "
              f"{accounted} classified of {total}")
    print_named(counts, names, MERGED_SKIP_CLASSES + ("errors",))
    return {"titles": total, "counts": counts, "names": names}


def _version_text_is_empty(node) -> bool:
    """True when a version's text tree contains no non-whitespace string."""
    if node is None:
        return True
    if isinstance(node, str):
        return not node.strip()
    if isinstance(node, (list, tuple)):
        return all(_version_text_is_empty(child) for child in node)
    if isinstance(node, dict):
        return all(_version_text_is_empty(value) for value in node.values())
    return False


VERSION_CLASSES = (
    "written",
    "empty",            # the version's text tree holds no non-whitespace string
    "no_version_title", # version document without a usable versionTitle
    "bad_ref",          # Ref(title) raised
    "virtual_nodes",    # upstream refuses indexes with virtual leaf nodes
    "index_error",      # library.get_index raised
    "no_content",       # make_json produced nothing; write was a no-op
    "errors",
)

VERSION_SKIP_CLASSES = tuple(
    c for c in VERSION_CLASSES if c not in ("written", "errors")
)


def run_versions_export_he_only(ex) -> dict:
    """Per-version export for titles with 2+ Hebrew versions.

    merged.json is a per-segment mosaic decided by version `priority`; the
    individual editions (including ones fully shadowed by a higher-priority
    version) are invisible downstream. This pass writes every non-copyright
    Hebrew version of every multi-version title via the stock
    `prepare_text_for_export`, landing as `<versionTitle>.json` next to
    merged.json after `flatten_hebrew_dirs`. Single-version titles are
    skipped: their merged.json IS their only version.
    """
    from sefaria.system.database import db
    from sefaria.model.text import Ref

    counts = {}
    for doc in db.texts.find({"language": "he"}, {"title": 1, "license": 1}):
        title = doc.get("title")
        if title and not ex.text_is_copyright(doc):
            counts[title] = counts.get(title, 0) + 1
    multi = sorted(t for t, n in counts.items() if n > 1)
    total = sum(counts[t] for t in multi)
    print(f"📋 {len(multi)} titles with 2+ non-copyright Hebrew versions "
          f"(of {len(counts)} titles) → {total} version docs to export", flush=True)

    tally = {name: 0 for name in VERSION_CLASSES}
    names = {name: [] for name in VERSION_CLASSES if name != "written"}
    copyright_excluded = 0
    verbose = verbose_titles()
    ticker = ProgressTicker(total)
    done = 0
    seen_filenames = set()

    def record(outcome, label):
        tally[outcome] += 1
        if outcome != "written":
            names[outcome].append(label)

    def report_progress(force=False):
        line = ticker.tick(done, {
            "written": tally["written"],
            "skipped": done - tally["written"] - tally["errors"],
            "errors": tally["errors"],
        }, force=force)
        if line:
            print(line, flush=True)

    for text in db.texts.find({"language": "he", "title": {"$in": multi}}):
        title = text.get("title")
        if not title or ex.text_is_copyright(text):
            # Copyrighted versions were never part of `total`; count them so the
            # cursor's size is explainable, but keep them out of the identity.
            copyright_excluded += 1
            continue
        done += 1
        version_title = text.get("versionTitle")
        if not isinstance(version_title, str) or not version_title.strip():
            record("no_version_title", title)
            print(f"⚠️  {title}: version without versionTitle skipped", flush=True)
            report_progress()
            continue
        try:
            Ref(title)
        except Exception:
            record("bad_ref", title)
            report_progress()
            continue

        filename = ex.remove_illegal_file_chars(version_title)
        # A version sanitizing to "merged" would land on merged.json (and the
        # SefariaSqlite generator matches that name case-insensitively); two
        # versions collapsing to one filename would overwrite each other.
        # Both silently corrupt data — abort the run instead.
        if not filename or filename.lower() == "merged":
            raise RuntimeError(
                f"version filename collides with merged.json: {title} / {version_title!r}")
        key = (title, filename.lower())
        if key in seen_filenames:
            raise RuntimeError(
                f"two versions collapse to the same filename: {title} / {version_title!r}")
        seen_filenames.add(key)

        label = f"{title} / {version_title}"
        if _version_text_is_empty(text.get("chapter")):
            record("empty", label)
            report_progress()
            continue

        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                prepped = ex.prepare_text_for_export(text)
                if prepped:
                    ex.write_text_doc_to_disk(prepped)
        except Exception as e:  # pragma: no cover
            replay_chatter(label, buf.getvalue().splitlines())
            record("errors", f"{label}: {type(e).__name__}: {e}")
            print(f"⚠️  {label}: {e}", flush=True)
            report_progress()
            continue

        chatter = classify_title_output(title, buf.getvalue())
        replay_chatter(label, buf.getvalue().splitlines() if verbose else chatter.residual)
        if chatter.outcome:
            record(chatter.outcome, f"{label} ({chatter.reason})")
        elif prepped:
            record("written", label)
        else:
            record("virtual_nodes", label)
        report_progress()

    report_progress(force=True)

    written, errors = tally["written"], tally["errors"]
    skipped = done - written - errors
    print(f"✅ versions export done in {format_duration(ticker.elapsed)}: "
          f"docs={done} = written={written} + skipped={skipped} + errors={errors}"
          + (f" (+{copyright_excluded} copyrighted, never in scope)"
             if copyright_excluded else ""))
    print("   skipped breakdown: " + ", ".join(
        f"{c}={tally[c]}" for c in VERSION_SKIP_CLASSES))
    if done != total:
        print(f"⚠️  versions export saw {done} non-copyright docs but expected {total}")
    print_named(tally, names, VERSION_SKIP_CLASSES + ("errors",))
    return {
        "expected_docs": total,
        "docs": done,
        "copyright_excluded": copyright_excluded,
        "counts": tally,
        "names": names,
    }


# Link-visibility bits. Mirrors the three display filters Sefaria applies in
# `get_links()` (sefaria/client/wrapper.py). A side with mask 0 is displayed.
SUPPRESS_ANCHOR_NOT_SEGMENT = 1
SUPPRESS_OTHER_TOO_COARSE = 2
SUPPRESS_WHOLE_PEREK = 4
SUPPRESS_WHOLE_PARASHA = 8

SUPPRESSION_BITS = {
    SUPPRESS_ANCHOR_NOT_SEGMENT: "anchor_not_segment_level",
    SUPPRESS_OTHER_TOO_COARSE: "other_side_too_coarse",
    SUPPRESS_WHOLE_PEREK: "whole_talmud_perek",
    SUPPRESS_WHOLE_PARASHA: "whole_parasha",
}


def _node_depth(oref):
    """`index_node.depth`, or None when the node can't supply one."""
    try:
        return getattr(oref.index_node, "depth", None)
    except Exception:
        return None


def _side_mask(anchor, other, anchor_ref, perek_refs, parasha_refs) -> int:
    """Why Sefaria would refuse to surface this link on `anchor`'s side.

    Each bit is one `continue` in get_links(). Note the second filter measures
    the OTHER side against the OTHER side's own depth, not the anchor's.
    """
    mask = 0
    depth = _node_depth(anchor)
    if depth is None or len(anchor.sections) != depth:
        mask |= SUPPRESS_ANCHOR_NOT_SEGMENT
    other_depth = _node_depth(other)
    if other_depth is None or len(other.sections) + 1 < other_depth:
        mask |= SUPPRESS_OTHER_TOO_COARSE
    if anchor_ref in perek_refs:
        mask |= SUPPRESS_WHOLE_PEREK
    if anchor_ref in parasha_refs:
        mask |= SUPPRESS_WHOLE_PARASHA
    return mask


def _sefaria_project_sha(project_dir=None) -> str:
    """The exact Sefaria-Project checkout whose helpers produced the masks."""
    import subprocess

    # The Docker entrypoint clones Sefaria beside this script under
    # /app/Sefaria-Project; the process itself runs from /app, which is not a
    # Git checkout. Never derive provenance from the caller's working dir.
    cwd = project_dir or Path(__file__).resolve().parent / "Sefaria-Project"
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd, text=True, stderr=subprocess.STDOUT
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"cannot resolve Sefaria-Project commit at {cwd}: {exc}") from exc
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise RuntimeError(f"invalid Sefaria-Project commit returned by git: {sha!r}")
    return sha


def format_category_timings(cat_seconds, cat_rows, top=5) -> str:
    """`Talmud 3m51s/1804233 rows · Tanakh 1m38s/…` — slowest categories first.

    557s of `export_links` printed nothing between its two banner lines in run
    33987734987; if it doubles there has to be somewhere to look first.
    """
    if not cat_seconds:
        return ""
    ranked = sorted(cat_seconds.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    return " · ".join(
        f"{cat} {format_duration(secs)}/{cat_rows.get(cat, 0)} rows"
        for cat, secs in ranked
    )


def run_links_export_extended() -> dict:
    """Replacement for `ex.export_links()` — adds word-level anchor fields.

    Mirrors the upstream loop (see sefaria/export.py::export_links) — same
    file naming, chunking, column order and aggregate files — but appends
    two columns the upstream export drops on the floor:

      * `Char Level Data 1/2` — JSON dict per ref side with
        startChar/endChar (or startWord/endWord for Tanakh verses) plus the
        versionTitle+language the offsets were computed against
        (mongo `links.charLevelData`).

    (`highlightedWords` is intentionally NOT exported: the 2026-07-02 run
    showed zero populated documents, so the column would be dead weight.)

    Consumers that index columns by header name are unaffected by the
    trailing additions.
    """
    import hashlib
    import json
    import unicodecsv as csv
    from collections import Counter

    from sefaria.helper.text import get_parasha_ref_set, get_talmud_perek_ref_set
    from sefaria.model.text import Ref
    from sefaria.system.database import db
    from sefaria.system.exceptions import InputError

    # Authoritative sets, straight from Sefaria's own helpers. Hoisted out of
    # the row loop: they are lru_cached but this runs millions of times.
    perek_refs = get_talmud_perek_ref_set()
    parasha_refs = get_parasha_ref_set()
    print(f"Link visibility: {len(perek_refs)} perek refs, {len(parasha_refs)} parasha refs")
    suppressed_by_side_and_bit = Counter()
    suppressed_sides = Counter()

    export_base = os.environ["SEFARIA_EXPORT_PATH"]

    links_by_book = Counter()
    links_by_book_without_commentary = Counter()
    field_counts = Counter()
    cat_seconds = Counter()
    cat_rows = Counter()
    unparsable_refs = []

    path = os.path.join(export_base, "links")
    os.makedirs(path, exist_ok=True)

    def dumps(value) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    # Cheap (it reads collection metadata, not documents) and only used to put
    # a denominator and an ETA on a stage that ran 9m17s in silence.
    try:
        total_links = int(db.links.estimated_document_count())
    except Exception:  # pragma: no cover - driver/permission dependent
        total_links = 0
    print(f"Exporting links (extended): {total_links or 'an unknown number of'} "
          f"link documents...", flush=True)
    # Every 10% of the collection or every 60s, whichever lands first.
    ticker = ProgressTicker(total_links,
                            every=max(1, total_links // 10) if total_links else 10 ** 9,
                            seconds=60.0)

    link_file_number = 0
    csvfile = None
    writer = None
    seen = 0

    def report_progress(force=False):
        line = ticker.tick(seen, {
            "written": field_counts["written"],
            "charLevelData": field_counts["charLevelData"],
            "unparsable": field_counts["refs_unparsable"],
            "malformed": field_counts["refs_malformed"] + field_counts["charLevelData_malformed"],
        }, force=force)
        if line:
            print(line, flush=True)

    links = db.links.find().sort([["refs.0", 1]])
    new_links_file_size = 300000
    for i, link in enumerate(links):
        row_started = time.perf_counter()
        seen = i + 1
        if i % new_links_file_size == 0:
            filename = os.path.join(path, f"links{link_file_number}.csv")
            if csvfile is not None:
                csvfile.close()
            csvfile = open(filename, 'wb')
            writer = csv.writer(csvfile)
            writer.writerow([
                    "Citation 1",
                    "Citation 2",
                    "Conection Type",
                    "Text 1",
                    "Text 2",
                    "Category 1",
                    "Category 2",
                    "Char Level Data 1",
                    "Char Level Data 2",
                    "Suppression Mask 1",
                    "Suppression Mask 2",
            ])
            link_file_number += 1

        # A malformed link document (missing/short refs) must not kill a
        # 50-minute export run — skip it, but keep it visible in the summary.
        refs = link.get("refs")
        if not isinstance(refs, list) or len(refs) < 2:
            field_counts["refs_malformed"] += 1
            print(f"⚠️  malformed refs on link {link.get('_id')}: {refs!r}")
            report_progress()
            continue

        try:
            oref1 = Ref(refs[0])
            oref2 = Ref(refs[1])
        except InputError:
            # Upstream drops these on the floor.  Keep the count (and the first
            # few refs) so "the CSV is short" is answerable without a re-run.
            field_counts["refs_unparsable"] += 1
            if len(unparsable_refs) < NAMES_IN_LOG:
                unparsable_refs.append(f"{refs[0]} ↔ {refs[1]}")
            report_progress()
            continue

        char_level = link.get("charLevelData")
        char_cells = ["", ""]
        if isinstance(char_level, list) and len(char_level) == 2:
            char_cells = [dumps(char_level[0]), dumps(char_level[1])]
            field_counts["charLevelData"] += 1
        elif char_level is not None:
            field_counts["charLevelData_malformed"] += 1
            print(f"⚠️  malformed charLevelData on {link['refs']}: {char_level!r}")

        # Per-side visibility, decided here because this is the only place the
        # TermSet, index_node depths and both Refs exist together.
        mask1 = _side_mask(oref1, oref2, refs[0], perek_refs, parasha_refs)
        mask2 = _side_mask(oref2, oref1, refs[1], perek_refs, parasha_refs)
        for side, mask in ((1, mask1), (2, mask2)):
            if mask:
                suppressed_sides[side] += 1
                for bit, name in SUPPRESSION_BITS.items():
                    if mask & bit:
                        suppressed_by_side_and_bit[(side, name)] += 1

        link_type = link.get("type", "")
        category = oref1.index.categories[0]
        writer.writerow([
            refs[0],
            refs[1],
            link_type,
            oref1.book,
            oref2.book,
            category,
            oref2.index.categories[0],
            char_cells[0],
            char_cells[1],
            mask1,
            mask2,
        ])

        book_link = tuple(sorted([oref1.index.title, oref2.index.title]))
        links_by_book[book_link] += 1
        if link_type not in ("commentary", "Commentary", "targum", "Targum"):
            links_by_book_without_commentary[book_link] += 1
        field_counts["written"] += 1
        cat_rows[category] += 1
        cat_seconds[category] += time.perf_counter() - row_started
        report_progress()

    report_progress(force=True)

    if csvfile is not None:
        csvfile.close()

    def write_aggregate_file(counter, filename):
        with open(os.path.join(path, filename), 'wb') as aggfile:
            agg_writer = csv.writer(aggfile)
            agg_writer.writerow([
                "Text 1",
                "Text 2",
                "Link Count",
            ])
            for link in counter.most_common():
                agg_writer.writerow([
                    link[0][0],
                    link[0][1],
                    link[1],
                ])

    write_aggregate_file(links_by_book, "links_by_book.csv")
    write_aggregate_file(links_by_book_without_commentary, "links_by_book_without_commentary.csv")

    # QA sidecar. NOT the source of the decision — that ships per row above —
    # but it lets a consumer verify its own derivation against Sefaria's sets.
    def digest(refs_set) -> str:
        return hashlib.sha256("\n".join(sorted(refs_set)).encode("utf-8")).hexdigest()

    meta_dir = os.path.join(export_base, "metadata")
    os.makedirs(meta_dir, exist_ok=True)
    sefaria_project_sha = _sefaria_project_sha()
    visibility = {
        "schema_version": 1,
        "sefaria_project_sha": sefaria_project_sha,
        "mask_bits": {str(bit): name for bit, name in SUPPRESSION_BITS.items()},
        "counts": {
            "perek_refs": len(perek_refs),
            "parasha_refs": len(parasha_refs),
            "suppressed_side_1": suppressed_sides[1],
            "suppressed_side_2": suppressed_sides[2],
            "suppressed_by_side_and_bit": {
                str(side): {
                    name: suppressed_by_side_and_bit[(side, name)]
                    for name in sorted(SUPPRESSION_BITS.values())
                }
                for side in (1, 2)
            },
        },
        "perek_refs_sha256": digest(perek_refs),
        "parasha_refs_sha256": digest(parasha_refs),
        "perek_refs": sorted(perek_refs),
        "parasha_refs": sorted(parasha_refs),
    }
    with open(os.path.join(meta_dir, "link-visibility-v1.json"), "w", encoding="utf-8") as vf:
        json.dump(visibility, vf, ensure_ascii=False, indent=2, sort_keys=True)

    print(f"✅ links export done in {format_duration(ticker.elapsed)}: "
          f"links={seen} = written={field_counts['written']} "
          f"+ refs_unparsable={field_counts['refs_unparsable']} "
          f"+ refs_malformed={field_counts['refs_malformed']}; "
          f"charLevelData={field_counts['charLevelData']}, "
          f"malformed={field_counts['charLevelData_malformed'] + field_counts['refs_malformed']}")
    timings = format_category_timings(cat_seconds, cat_rows)
    if timings:
        print(f"   slowest categories: {timings}")
    if unparsable_refs:
        print(format_named("unparsable refs", unparsable_refs))
    print(f"   visibility: sides suppressed 1={suppressed_sides[1]} 2={suppressed_sides[2]}, "
          f"by side/bit={dict(sorted(suppressed_by_side_and_bit.items()))}, "
          f"sefaria={sefaria_project_sha[:12]}")
    return {
        "links": seen,
        "estimated_documents": total_links,
        "counts": {
            "written": field_counts["written"],
            "refs_unparsable": field_counts["refs_unparsable"],
            "refs_malformed": field_counts["refs_malformed"],
            "charLevelData": field_counts["charLevelData"],
            "charLevelData_malformed": field_counts["charLevelData_malformed"],
        },
        "names": {"refs_unparsable": unparsable_refs},
    }


AUTHORS_EXPORT_FILENAME = "authors.json"


def _author_titles(doc) -> list:
    """The `titles` array of one topic doc, normalized and ordered.

    Sefaria stores every name form a person is known by — including the
    honorific and acronym forms (``רבנו נסים מגירונה (ר"ן)``, ``ראב"ד``) that
    the bare `authors[].he` on a book schema does not carry. Each entry keeps
    its `lang` and `primary` flags so the consumer can pick a form per
    language rather than guessing.

    Primary forms come first, then the rest in a stable order, so two runs over
    the same dump produce byte-identical output.
    """
    raw = doc.get("titles")
    # The dump holds documents that predate schema changes and never went
    # through Topic._normalize(), so nothing about the shape is guaranteed.
    # A malformed one must be skipped, not crash the whole export.
    if not isinstance(raw, list):
        return []
    out = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        text = str(t.get("text") or "").strip()
        if not text:
            continue
        entry = {
            "text": text,
            "lang": str(t.get("lang") or ""),
            "primary": bool(t.get("primary")),
        }
        # Two people can share a name; Sefaria tells them apart with this.
        # Without it the consumer sees two slugs with an identical primaryHe.
        disambiguation = t.get("disambiguation")
        if disambiguation:
            entry["disambiguation"] = str(disambiguation)
        out.append(entry)
    # Dedupe on (text, lang) — the dump does carry repeats.
    seen = set()
    deduped = []
    for t in out:
        key = (t["text"], t["lang"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(t)
    deduped.sort(key=lambda t: (not t["primary"], t["lang"], t["text"]))
    return deduped


def _primary(titles, lang) -> str:
    """The primary title for `lang`, else the first title in that language."""
    for t in titles:
        if t["lang"] == lang and t["primary"]:
            return t["text"]
    for t in titles:
        if t["lang"] == lang:
            return t["text"]
    return ""


def run_authors_export() -> None:
    """Write `exports/authors.json` — every author topic with all its titles.

    The `topics` collection is already in the dump we restore, but nothing
    exported it, so downstream only ever saw the single bare `he` name that
    sits on each book's schema. This is the whole author-name vocabulary:
    slug plus every title form, which is what lets a consumer show
    ``מהר"ם מפאדובה`` instead of ``מאיר בן יצחק קצנלנבוגן``.

    Keyed by slug because that is the identifier a book schema's
    `authors[].slug` already points at.
    """
    from sefaria.system.database import db

    export_base = os.environ.get("SEFARIA_EXPORT_PATH") or os.path.join(
        os.environ.get("GITHUB_WORKSPACE", os.getcwd()), "exports"
    )
    os.makedirs(export_base, exist_ok=True)

    records = []
    cursor = db.topics.find(
        {"subclass": "author"},
        {"slug": 1, "titles": 1, "_id": 0},
    )
    for doc in cursor:
        slug = (doc.get("slug") or "").strip()
        if not slug:
            continue
        titles = _author_titles(doc)
        if not titles:
            continue
        records.append({
            "slug": slug,
            "primaryHe": _primary(titles, "he"),
            "primaryEn": _primary(titles, "en"),
            "titles": titles,
        })

    # Sorted here rather than in Mongo: `topics` carries no index after the
    # --noIndexRestore restore, and sorting in Python also breaks the tie
    # between two documents that somehow share a slug.
    records.sort(key=lambda r: (r["slug"], r["primaryHe"], r["primaryEn"]))

    with_he = sum(1 for r in records if r["primaryHe"])

    # Fail loudly, on both ways this can go wrong. No records at all means the
    # dump lacks author topics or the `subclass` marker moved. No Hebrew name on
    # any record is the subtler one: a change to Sefaria's language codes
    # (`he` -> `he-IL`, say) would leave every primaryHe empty while the export
    # still reported success — precisely the silent honorific-stripping this
    # whole step exists to prevent.
    if not records:
        raise RuntimeError(
            "no author topics found in db.topics (subclass='author') — "
            "refusing to write an empty authors.json"
        )
    if not with_he:
        raise RuntimeError(
            f"{len(records)} author topics found but not one has a Hebrew title "
            "(lang='he') — refusing to write an authors.json with no Hebrew names"
        )

    out_path = os.path.join(export_base, AUTHORS_EXPORT_FILENAME)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=1, sort_keys=False)
        fh.write("\n")

    total_titles = sum(len(r["titles"]) for r in records)
    print(
        f"✅ authors export: {len(records)} author topics "
        f"({with_he} with a Hebrew primary, {total_titles} title forms) -> {out_path}"
    )


def build_export_report(merged=None, versions=None, links=None) -> dict:
    """The machine-readable half of the accounting.

    The log names at most 30 titles per class; this names all of them, and it
    ships inside the archive (next to link-visibility-v1.json) so a consumer
    asking "which books are missing from this release" has an answer that does
    not require the CI log to still exist.

    Deliberately free of timings and timestamps: the file is hashed into
    manifest.txt, and two runs over the same dump should differ only where the
    data differs.
    """
    report = {"schema_version": EXPORT_REPORT_SCHEMA_VERSION}
    for name, section in (("merged", merged), ("versions", versions), ("links", links)):
        if section is not None:
            report[name] = section
    return report


def write_export_report(export_base, report) -> str:
    meta_dir = os.path.join(export_base, EXPORT_REPORT_DIRNAME)
    os.makedirs(meta_dir, exist_ok=True)
    out_path = os.path.join(meta_dir, EXPORT_REPORT_FILENAME)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return out_path


def flatten_hebrew_dirs(export_base: str) -> None:
    """Move the contents of every `.../Hebrew/` directory one level up.

    Sefaria's `make_path` writes to `json/<cat>/<book>/Hebrew/merged.json`.
    The SefariaSqlite generator expects `json/<cat>/<book>/merged.json`, so
    we collapse the language layer in-place.
    """
    import shutil

    targets = []
    for root, dirs, _files in os.walk(export_base):
        for d in dirs:
            if d == "Hebrew":
                targets.append(os.path.join(root, d))

    print(f"📦 Flattening {len(targets)} Hebrew/ directories under {export_base}")
    for src in targets:
        parent = os.path.dirname(src)
        for entry in os.listdir(src):
            shutil.move(os.path.join(src, entry), os.path.join(parent, entry))
        try:
            os.rmdir(src)
        except OSError:
            pass


def main() -> int:
    workspace = os.environ.get('GITHUB_WORKSPACE', os.getcwd())
    proj_dir = os.path.join(workspace, 'Sefaria-Project')
    sys.path.insert(0, os.path.abspath(proj_dir))
    os.chdir(proj_dir)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sefaria.settings")

    export_base = os.path.join(workspace, 'exports')
    os.environ["SEFARIA_EXPORT_PATH"] = export_base

    print(f"📁 Export base directory: {export_base}")
    print(f"📁 Current working directory: {os.getcwd()}")

    import django
    django.setup()

    from django.conf import settings
    print(f"📋 Django SEFARIA_EXPORT_PATH: {getattr(settings, 'SEFARIA_EXPORT_PATH', 'NOT SET')}")

    from sefaria import export as ex

    # Drop txt / cltk-full / cltk-flat formats at the source. This also
    # saves the CPU spent by make_cltk_* on every book.
    print(f"🪓 Restricting export_formats from {[f[0] for f in ex.export_formats]} -> ['json']")
    ex.export_formats = (('json', ex.make_json),)

    try:
        # First, and deliberately: seconds of work that prove the DB is
        # reachable and that Sefaria's author model still looks the way we
        # expect. Failing here costs nothing; failing after the merged /
        # versions / links exports costs hours of runner time for no artifact.
        print(f"\n{'='*60}\n▶️  Running authors export...\n{'='*60}")
        run_authors_export()

        print("\n" + "="*60)
        print("▶️  Running merged export (Hebrew + JSON only)")
        print("="*60)
        merged_stats = run_merged_export_he_only(ex)

        print(f"\n{'='*60}\n▶️  Running versions export (multi-version titles, Hebrew + JSON only)\n{'='*60}")
        versions_stats = run_versions_export_he_only(ex)

        print(f"\n{'='*60}\n▶️  Running export_links (extended)...\n{'='*60}")
        links_stats = run_links_export_extended()
        print("✅ export_links (extended) completed")

        for fn_name in ("export_schemas", "export_toc"):
            print(f"\n{'='*60}\n▶️  Running {fn_name}...\n{'='*60}")
            stage_started = time.monotonic()
            getattr(ex, fn_name)()
            print(f"✅ {fn_name} completed in "
                  f"{format_duration(time.monotonic() - stage_started)}")
    except Exception as e:  # pragma: no cover
        print(f"❌ export step failed: {e}")
        traceback.print_exc()
        return 1

    # Collapse `json/<cat>/<book>/Hebrew/` -> `json/<cat>/<book>/` to match
    # the layout the SefariaSqlite generator expects.
    flatten_hebrew_dirs(export_base)

    report_path = write_export_report(
        export_base,
        build_export_report(merged_stats, versions_stats, links_stats),
    )
    print(f"🧾 Export report written: {report_path}")

    print(f"\n📂 Final layout of {export_base}:")
    if os.path.isdir(export_base):
        list_dir_limited(export_base)

    print("\n✅ All exports completed successfully")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
