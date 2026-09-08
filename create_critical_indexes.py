#!/usr/bin/env python3
"""
Re-create all the MongoDB indexes Sefaria expects after a fresh restore.

Upstream Sefaria-Project ships the canonical list of indexes in
`sefaria.system.database.ensure_indices()` (≈80 specs covering texts,
links, index, term, vstate, history, …). The dump's metadata-only
restore leaves several of these out — most painfully `links.refs.0`,
without which `export_links()` does a 2.44 GB in-memory sort.

Calling the upstream helper keeps us in sync with whatever queries
Sefaria adds later without having to hand-maintain a parallel list.
"""
import os
import sys
import time


def main() -> int:
    workspace = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
    proj_dir = os.path.join(workspace, "Sefaria-Project")
    sys.path.insert(0, os.path.abspath(proj_dir))
    os.chdir(proj_dir)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sefaria.settings")

    import django
    django.setup()

    from sefaria.system.database import ensure_indices

    # This is the longest silent stretch in the whole job: 388s in run
    # 33987734987 (19:47:03 -> 19:53:31) with not one line between these two
    # prints.  ensure_indices() is upstream and gives no callback, so the least
    # we can do is say how long it took, and how far into the job that puts us.
    print("🔧 Running sefaria.system.database.ensure_indices() "
          "(~6-7 min, no output until it finishes) ...", flush=True)
    started = time.monotonic()
    ensure_indices()
    elapsed = time.monotonic() - started
    print(f"✅ All Sefaria indexes ensured in {elapsed:.0f}s.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
