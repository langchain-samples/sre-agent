"""Record a live cluster state as a replayable eval fixture.

    python evals/capture_snapshot.py --name my-case
    python evals/capture_snapshot.py --name my-case --no-redact   # see the warning

Redaction is ON by default. Fixtures are committed to a public repository, and a
raw capture is an inventory of your internal namespaces, workloads, and nodes.
Only the fields the classifier reads are captured at all, so annotations, env
values, and mounted secret names never enter the file.

After capturing, fill in the `expected` block by hand. That block is the actual
assertion, and it should be written by someone who has looked at the cluster and
decided what the right answer is.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from snapshot_fixture import normalize, redact, replay, save  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", required=True, help="fixture filename stem")
    ap.add_argument("--no-redact", action="store_true",
                    help="keep real names (DANGEROUS: this repo is public)")
    ap.add_argument("--note", default="", help="what this fixture is meant to prove")
    args = ap.parse_args()

    from scheduler import _collect_cluster_data, _format_snapshot
    from tools.k8s_client import core_v1

    now = datetime.now(timezone.utc)
    print("Collecting cluster state...")
    data = _collect_cluster_data()
    pods_raw = core_v1().list_pod_for_all_namespaces().items

    fixture = normalize(data, pods_raw, now)
    fixture["note"] = args.note
    fixture["captured_at"] = now.isoformat()
    fixture["redacted"] = not args.no_redact

    if args.no_redact:
        print("\n  WARNING: capturing UNREDACTED. This file will contain real")
        print("  namespace, pod, and node names. Do not commit it to a public repo.\n")
    else:
        fixture = redact(fixture)

    # Pre-fill expected from what the current code actually does, so the reviewer
    # edits a diff rather than writing from scratch. This is a starting point, not
    # ground truth: if the current code is wrong, the expected block is wrong too.
    replayed = replay(fixture)
    fixture["expected"]["unhealthy_pod_names"] = sorted(
        f"{p['namespace']}/{p['name']}" for p in replayed["unhealthy_pods"]
    )

    path = save(fixture, args.name)
    print(f"Wrote {path}")
    print(f"  pods captured      : {len(fixture['raw_pods'])}")
    print(f"  redacted           : {fixture['redacted']}")
    print(f"  current classifier : {len(replayed['unhealthy_pods'])} unhealthy")
    print()
    print("NEXT: edit the `expected` block. The pre-filled values describe what the")
    print("code does today, which is only correct if today's behaviour is correct.")
    print(_format_snapshot(replayed)[:600])
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    sys.exit(main())
