"""Build V1 round trips without enabling attribution."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.lookup import lookup
from audit.purge import purge
from audit.workspace import initialize_workspace, open_workspace
from close.finalize import seal
from dispatch.submit import advance
from intake.batches import apply

DAY = "2025-03-01"
CUTOFF = "2025-03-02"


def prepare_legacy(local, external, peak):
    apply(local, DAY, "boot", "initial", [("east", "a", 2), ("east", "b", 3)])
    earlier = seal(local, DAY, "first")
    advance(local, external, DAY)
    apply(local, DAY, "c1", "correction", [("east", "b", peak)])
    apply(local, DAY, "c2", "correction", [("east", "b", 3)])
    later = seal(local, DAY, "second")
    receipt = advance(local, external, DAY).receipt
    return earlier, later, receipt


def inspect_legacy(root, peak):
    initialize_workspace(root)
    with open_workspace(root) as work:
        earlier, later, receipt = prepare_legacy(work.local, work.external, peak)
        removed = purge(work.local, CUTOFF).removed
    with open_workspace(root) as work:
        review = lookup(work.local, later.seal_id)
        original_rows = work.local.execute("SELECT COUNT(*) FROM source_rows").fetchone()[0]
        changed = [n for (n,) in work.local.execute(
            "SELECT changed FROM batches WHERE day = ? AND sequence > ? ORDER BY sequence",
            (DAY, earlier.source_watermark),
        )]
    return {
        "totals": [earlier.total, later.total],
        "watermarks": [earlier.source_watermark, later.source_watermark],
        "delta": review.delta,
        "original_receipt": review.receipt is not None and review.receipt == receipt,
        "removed": [entry.batch_id for entry in removed],
        "remaining_rows": original_rows,
        "changed": changed,
    }


def main():
    expected = {
        "totals": [5, 5], "watermarks": [1, 3], "delta": 0,
        "original_receipt": True, "removed": ["boot", "c1", "c2"], "remaining_rows": 0,
    }
    with TemporaryDirectory(prefix="estuary-attribution-v1-") as folder:
        for peak, changed in ((10, [1, 1]), (8, [1, 1]), (3, [0, 0])):
            actual = inspect_legacy(Path(folder) / str(peak), peak)
            if actual != dict(expected, changed=changed):
                raise AssertionError(f"V1 peak={peak}: {actual!r}")
            print(json.dumps({"peak_before_purge": peak, **actual}, sort_keys=True))


if __name__ == "__main__":
    main()
