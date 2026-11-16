#!/usr/bin/env python3

"""Fan-in: collect the per-scan summary JSONs into one CSV table.

One row per reconstructed scan, so the "Full size reconstruction result"
column of the parameters sheet can be filled in from a single file.
"""

import argparse
import csv
import json
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

COLUMNS = [
    "job_id", "scan_folder", "acqu_time", "scan_number", "mode", "Niter",
    "GPU_solver", "Nlayers", "runtime_s", "n_result_files", "full_run_files",
    "host", "tarball",
]


def main():
    parser = argparse.ArgumentParser(description="Summarize ptycho reconstructions")
    parser.add_argument("--input", action="append", required=True,
                        help="Per-scan summary JSON (repeat for each scan)")
    parser.add_argument("--output", required=True, help="Summary CSV")
    args = parser.parse_args()

    rows = []
    for path in args.input:
        logger.info(f"Reading {path}")
        with open(path) as f:
            row = json.load(f)
        row["full_run_files"] = ";".join(row.get("full_run_files", []))
        rows.append(row)
    rows.sort(key=lambda r: (r["acqu_time"], r["scan_number"]))

    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    logger.info(f"Output: {args.output} ({len(rows)} scans)")


if __name__ == "__main__":
    main()
