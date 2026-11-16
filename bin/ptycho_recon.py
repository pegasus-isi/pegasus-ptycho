#!/usr/bin/env python3

"""Run one fold_slice / PtychoShelves reconstruction with host MATLAB on a GPU.

Replaces the per-scan `srun ... matlab -batch run_<scan>` line of the old
sbatch file. Pegasus stages the rendered script, the raw 4D-STEM file and
(optionally) the background / previous reconstruction into the job directory;
this wrapper lays them out the way the MATLAB template expects:

    <job dir>/
        run_<id>.m                                 (placeholders resolved)
        Background subtraction.mat                 -> staged background file
        Data/<scan folder>/scan_xN_yN.raw          -> staged raw file
        Recon_files/<scan folder>/init_recon.mat   -> staged previous recon
        Recon_result/...                           (written by MATLAB)

then runs MATLAB and packs Recon_result/ into a single tarball for stage-out.
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

JOB_DIR_TOKEN = "__PEGASUS_JOB_DIR__"
FOLD_SLICE_TOKEN = "__FOLD_SLICE_DIR__"


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
    parser.add_argument("--script", required=True, help="Rendered run_<id>.m")
    parser.add_argument("--params", required=True, help="Per-scan parameter JSON")
    parser.add_argument("--raw", required=True, help="Staged raw 4D-STEM file")
    parser.add_argument("--bg-file", default=None,
                        help="Staged 'Background subtraction.mat'")
    parser.add_argument("--init-recon", default=None,
                        help="Staged previous reconstruction (initial_probe=1)")
    parser.add_argument("--fold-slice", required=True,
                        help="fold_slice directory: a basename transferred into the "
                             "job dir by CondorIO, or an absolute path on the worker")
    parser.add_argument("--matlab-module", default="",
                        help="Environment module to load first (e.g. matlab/2021b)")
    parser.add_argument("--matlab-cmd", default="matlab", help="MATLAB executable")
    parser.add_argument("--output-tar", required=True, help="Recon_result tarball")
    parser.add_argument("--log", required=True, help="MATLAB log file")
    parser.add_argument("--summary", required=True, help="Per-scan summary JSON")
    args = parser.parse_args()

    job_dir = os.getcwd()
    with open(args.params) as f:
        scan = json.load(f)
    logger.info(f"Scan: {scan['job_id']}  (job dir {job_dir})")

    fold_slice = os.path.abspath(args.fold_slice)
    if not os.path.isdir(os.path.join(fold_slice, "ptycho")):
        logger.error(f"fold_slice not found (no ptycho/ under {fold_slice})")
        sys.exit(1)

    # --- Lay out inputs where the template expects them -----------------
    data_dir = scan["scan_folder"]
    link(args.raw, os.path.join("Data", data_dir, scan["raw_name"]))
    if scan["background_subtraction"] == 1:
        if not args.bg_file:
            logger.error("background_subtraction=1 but no --bg-file given")
            sys.exit(1)
        link(args.bg_file, "Background subtraction.mat")
    if scan["initial_probe"] == 1:
        if not args.init_recon:
            logger.error("initial_probe=1 but no --init-recon given")
            sys.exit(1)
        link(args.init_recon, os.path.join("Recon_files", data_dir, "init_recon.mat"))

    # --- Resolve worker-side placeholders -------------------------------
    # MATLAB runs the script by name, so it must be <script_name>.m in cwd.
    script_file = f"{scan['script_name']}.m"
    with open(args.script, encoding="utf-8") as f:
        text = f.read()
    text = text.replace(FOLD_SLICE_TOKEN, fold_slice).replace(JOB_DIR_TOKEN, job_dir)
    if os.path.exists(script_file):
        os.remove(script_file)  # may be the staged (unresolved) input itself
    with open(script_file, "w", encoding="utf-8") as f:
        f.write(text)
    os.makedirs("Recon_result", exist_ok=True)
    shutil.copy(script_file, os.path.join("Recon_result", script_file))

    # --- Run MATLAB ------------------------------------------------------
    env = dict(os.environ)
    env.pop("DISPLAY", None)  # as `unset DISPLAY` in the sbatch file

    if shutil.which("nvidia-smi"):
        subprocess.run(["nvidia-smi", "-L"])

    matlab = f"{args.matlab_cmd} -batch {scan['script_name']}"
    if args.matlab_module:
        cmd = ["bash", "-lc", f"module load {args.matlab_module} && {matlab}"]
    else:
        cmd = ["bash", "-c", matlab]

    log_dir = os.path.dirname(args.log)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    logger.info(f"Running: {' '.join(cmd)}")
    start = time.time()
    with open(args.log, "w") as log:
        result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)
    runtime = time.time() - start
    logger.info(f"MATLAB exited with {result.returncode} after {runtime:.0f} s")

    if result.returncode != 0:
        print(tail(args.log), file=sys.stderr)
        sys.exit(result.returncode)

    # --- Package results -------------------------------------------------
    result_files = []
    for root, _, files in os.walk("Recon_result"):
        result_files.extend(os.path.join(root, f) for f in files)
    if len(result_files) <= 1:  # only the script copy
        logger.error("MATLAB succeeded but Recon_result/ is empty")
        print(tail(args.log), file=sys.stderr)
        sys.exit(1)

    for path in (args.output_tar, args.summary):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

    logger.info(f"Packing {len(result_files)} files into {args.output_tar}")
    with tarfile.open(args.output_tar, "w:gz") as tar:
        tar.add("Recon_result", arcname=scan["job_id"])

    summary = {
        "job_id": scan["job_id"],
        "scan_folder": data_dir,
        "acqu_time": scan["acqu_time"],
        "scan_number": scan["scan_number"],
        "mode": scan.get("mode", ""),
        "Niter": scan["replace_vars"].get("Niter", ""),
        "GPU_solver": scan["replace_vars"].get("GPU_solver", ""),
        "Nlayers": scan["replace_vars"].get("Nlayers", "1"),
        "runtime_s": round(runtime, 1),
        "n_result_files": len(result_files),
        "full_run_files": sorted(
            os.path.relpath(p, "Recon_result")
            for p in result_files if "Full_run" in p
        ),
        "host": os.uname().nodename,
        "tarball": os.path.basename(args.output_tar),
    }
    with open(args.summary, "w") as f:
        json.dump(summary, f, indent=2)

    logger.info(f"Output: {args.output_tar}, {args.summary}")


if __name__ == "__main__":
    main()
