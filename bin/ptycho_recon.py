#!/usr/bin/env python3

"""Run one fold_slice / PtychoShelves reconstruction with host MATLAB on a GPU.

Replaces the per-scan `srun ... matlab -batch run_<scan>` line of the old
sbatch file. Pegasus stages the rendered script, the raw 4D-STEM file and
(optionally) the background / previous reconstruction into the job directory;
this wrapper lays them out the way the MATLAB template expects:

    <job dir>/
        run_<scan>.m                               (job dir placeholder resolved)
        Background subtraction.mat                 -> staged background file
        Data/<scan folder>/scan_xN_yN.raw          staged raw file (deep LFN)
        Recon_files/<scan folder>/init_recon.mat   -> staged previous recon
        Recon_result/...                           (written by MATLAB)

then runs MATLAB and packs Recon_result/ into a single tarball for stage-out.
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tarfile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

JOB_DIR_TOKEN = "__PEGASUS_JOB_DIR__"


def link(src, dst):
    """Place a staged input where the MATLAB script looks for it."""
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if os.path.lexists(dst):
        os.remove(dst)
    os.symlink(os.path.abspath(src), dst)
    logger.info(f"Linked {dst} -> {src}")


def tail(path, n=60):
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def main():
    parser = argparse.ArgumentParser(description="Ptycho reconstruction (MATLAB, GPU)")
    parser.add_argument("--script", required=True, help="Rendered run_<scan>.m")
    parser.add_argument("--raw", required=True,
                        help="Staged raw 4D-STEM file, Data/<scan folder>/scan_xN_yN.raw")
    parser.add_argument("--bg-file", default=None,
                        help="Staged 'Background subtraction.mat'")
    parser.add_argument("--init-recon", default=None,
                        help="Staged previous reconstruction (initial_probe=1)")
    parser.add_argument("--output-tar", required=True, help="Recon_result tarball")
    parser.add_argument("--log", required=True, help="MATLAB log file")
    args = parser.parse_args()

    job_dir = os.getcwd()
    script_name = os.path.splitext(os.path.basename(args.script))[0]
    logger.info(f"Scan: {script_name}  (job dir {job_dir})")

    # --- Lay out inputs where the template expects them -----------------
    # The raw file is already in place: its deep LFN is the path the template
    # reads, Data/<scan folder>/scan_xN_yN.raw
    if not os.path.isfile(args.raw):
        logger.error(f"Raw file not found at {args.raw}")
        sys.exit(1)
    scan_folder = os.path.basename(os.path.dirname(args.raw))
    if args.bg_file:
        link(args.bg_file, "Background subtraction.mat")
    if args.init_recon:
        link(args.init_recon, os.path.join("Recon_files", scan_folder, "init_recon.mat"))

    # --- Resolve the job directory placeholder ---------------------------
    with open(args.script, encoding="utf-8") as f:
        text = f.read()
    text = text.replace(JOB_DIR_TOKEN, job_dir)
    os.remove(args.script)  # the staged copy may be a link to the submit host
    with open(args.script, "w", encoding="utf-8") as f:
        f.write(text)
    os.makedirs("Recon_result", exist_ok=True)
    shutil.copy(args.script, os.path.join("Recon_result", os.path.basename(args.script)))

    # --- Run MATLAB ------------------------------------------------------
    env = dict(os.environ)
    env.pop("DISPLAY", None)  # as `unset DISPLAY` in the sbatch file

    if shutil.which("nvidia-smi"):
        subprocess.run(["nvidia-smi", "-L"])

    # matlab is on PATH: PegasusLite has sourced job-env-setup.sh
    cmd = ["matlab", "-batch", script_name]

    for path in (args.log, args.output_tar):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)

    logger.info(f"Running: {' '.join(cmd)}")
    with open(args.log, "w") as log:
        result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)
    logger.info(f"MATLAB exited with {result.returncode}")

    if result.returncode != 0:
        print(tail(args.log), file=sys.stderr)
        sys.exit(result.returncode)

    # --- Package results -------------------------------------------------
    logger.info(f"Packing Recon_result/ into {args.output_tar}")
    with tarfile.open(args.output_tar, "w:gz") as tar:
        tar.add("Recon_result", arcname=script_name)

    logger.info(f"Output: {args.output_tar}")


if __name__ == "__main__":
    main()
