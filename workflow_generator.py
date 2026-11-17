#!/usr/bin/env python3

"""
Pegasus workflow generator for electron ptychography reconstruction.

Pegasus version of param_config_multislice.py: the same loop over scan folders
and "Ptycho parameters.xlsx" rows writes Scripts/run_<scan>_Scan<n>.m from the
MATLAB template, but instead of an sbatch line per scan it adds a Pegasus job:

    ptycho_recon (one per scan, all in parallel) - run the script with host
    MATLAB + fold_slice on one GPU, return Recon_result/ as a tarball

The execution site comes from the centrally hosted Pegasus site catalogs
(github.com/pegasushub/pegasus-site-catalogs, property
pegasus.catalog.site.repo.file); jobs are planned for the site "compute".

Usage:
    ./workflow_generator.py --data-dir "/scratch/zhangych/Data/" \\
        --site-catalog-file access-pegasus.yml --fold-slice-dir ~/fold_slice
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from Pegasus.api import *

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Settings (edit here, as code_dir / sbatch settings in the old scripts)
# ----------------------------------------------------------------------
TEMPLATES = {
    "multislice": "templates/Ptychography_reconstruction_multislice_template.m",
    "singleslice": "templates/Ptychography_reconstruction_singleslice_template.m",
}
RBF_XLSX = "config/rbf.xlsx"
MATLAB_MODULE = "matlab/2021b"  # '' if matlab is already on PATH

# As the old sbatch file: --cpus-per-task=4 --gpus-per-task=1
# --mem-per-gpu=40GB --time=4:00:00. The "gpu" tag picks the gpu x-tags
# profiles (e.g. a GPU queue) from the hosted site catalog.
RECON_RESOURCES = {"memory": "40 GB", "cores": 4, "gpus": 1, "runtime": 14400}
RECON_TAG = "gpu"

# Used in multislice mode when the sheet has no such columns
MULTISLICE_DEFAULTS = {
    "previous_object": 0, "regularization": 0.1, "thickness": 13, "Nlayers": 2,
}

COMPUTE_SITE = "compute"

# Placeholders for paths only known on the worker; resolved by ptycho_recon.py
JOB_DIR_TOKEN = "__PEGASUS_JOB_DIR__"
FOLD_SLICE_TOKEN = "__FOLD_SLICE_DIR__"

# Sheet columns. The current sheets have 28 columns; a 32-column sheet also
# has the four multislice columns after initial_probe.
PARAM_HEADER = [
    "Date", "Sample", "ROI", "Tool", "Voltage", "Acqu_time",
    "Additional_identifider", "Conv_angle", "Camera_len", "Mag", "Defocus",
    "Screen_current", "Scan_rot", "Scan_size", "Scale",
    "Image_shear", "Resample_factor", "dScanXY", "Diff_pattern_size",
    "background_subtraction", "Niter", "Probe_states", "Probe_modes",
    "GPU_solver", "errmetric", "initial_probe",
]
MULTISLICE_HEADER = ["previous_object", "regularization", "thickness", "Nlayers"]
TRAILING_HEADER = ["Result", "Notes"]
RBF_HEADER = ["voltage", "camera_len", "conv_angle", "rbf"]

REPLACE_VARS = [
    "rot_ang", "camera_len", "dScanX", "dScanY", "exp_dir",
    "data_dir", "N_scan_x", "N_scan_y", "voltage", "ADU", "alpha0",
    "rbf", "scan_step_size", "data_descriptor", "mag", "scan_number",
    "resample_factor", "defocus", "background_subtraction", "Niter",
    "Np_crop_pad", "Nprobe", "variable_probe_modes", "GPU_solver",
    "errmetric", "initial_probe", "cSAXS_matlab_path", "ptycho_matlab_path",
]


# ----------------------------------------------------------------------
# Lookups (as in param_config_multislice.py)
# ----------------------------------------------------------------------
def get_rbf(rbf_df, voltage, camera_len, conv_angle):
    rbfs = rbf_df.loc[(rbf_df["voltage"] == voltage) & (rbf_df["camera_len"] == camera_len)
                      & (rbf_df["conv_angle"] == conv_angle)]
    if rbfs.shape[0] == 0:
        print(f"voltage({voltage}), camera_len({camera_len}), conv_angle({conv_angle}) combination not found")
    elif rbfs.shape[0] > 1:
        print(f"Multiple voltage({voltage}), camera_len({camera_len}), conv_angle({conv_angle}) combinations found")
    else:
        return rbfs.iloc[0]["rbf"]


def get_ADU(voltage):
    if voltage == 80:
        return 151
    elif voltage == 200:
        return 393
    elif voltage == 300:
        return 591
    else:
        print(f"Voltage({voltage}) not found, ADU not defined!")


def get_alpha0(conv_angle):
    if conv_angle == 18:
        return 18.44
    elif conv_angle == 25.2:
        return 24.92
    elif conv_angle == 36:
        return 36.4
    elif conv_angle == 54:
        return 55.45
    else:
        print(f"Conv_angle({conv_angle}) not found, alpha0 not defined!")


def get_scan_step_size(mag, N_scan_x):
    if mag == 5.1:
        return 1.182 / N_scan_x * 128
    elif mag == 7.2:
        return 0.839 / N_scan_x * 128
    elif mag == 10:
        return 0.605 / N_scan_x * 128
    elif mag == 14.5:
        return 0.429 / N_scan_x * 128
    elif mag == 20.5:
        return 0.297 / N_scan_x * 128
    elif mag == 29:
        return 0.208 / N_scan_x * 128
    elif mag == 41:
        return 0.147 / N_scan_x * 128
    else:
        print(f"mag({mag}) not found, scan_step_size not defined!")


def replace_single_var(var_name, var_value, string):
    """Same regex as param_config: replace the value of `\\n<var> = ...`."""
    return re.sub(r"(\n{}( )*=( )*['|\"|\[]?)[^\n|^;|^'|^\"|^\]]*".format(var_name),
                  lambda m: m.group(1) + var_value, string)


class PtychoWorkflow:
    """One ptycho_recon job per (scan folder, spreadsheet row)."""

    wf = None
    sc = None
    tc = None
    rc = None
    props = None

    dagfile = None
    wf_dir = None
    shared_scratch_dir = None
    local_storage_dir = None
    wf_name = "ptycho"

    def __init__(self, args, dagfile="workflow.yml"):
        self.dagfile = dagfile
        self.wf_dir = str(Path(__file__).parent.resolve())
        self.shared_scratch_dir = os.path.join(self.wf_dir, "scratch")
        self.local_storage_dir = os.path.join(self.wf_dir, "output")
        self.script_dir = os.path.join(self.wf_dir, "Scripts")
        self.args = args

    def write(self):
        """Write all catalogs and workflow to files."""
        if self.sc is not None:
            self.sc.write()
        self.props.write()
        self.rc.write()
        self.tc.write()
        self.wf.write(file=self.dagfile)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    def create_pegasus_properties(self):
        self.props = Properties()
        self.props["pegasus.transfer.threads"] = "16"
        self.props["dagman.retry"] = "1"
        # Download the compute site from the hosted site catalogs
        self.props["pegasus.catalog.site.repo.file"] = self.args.site_catalog_file

    # ------------------------------------------------------------------
    # Site Catalog — only the submit host; "compute" is downloaded
    # ------------------------------------------------------------------
    def create_sites_catalog(self):
        self.sc = SiteCatalog()

        local = Site("local").add_directories(
            Directory(
                Directory.SHARED_SCRATCH, self.shared_scratch_dir
            ).add_file_servers(
                FileServer("file://" + self.shared_scratch_dir, Operation.ALL)
            ),
            Directory(
                Directory.LOCAL_STORAGE, self.local_storage_dir
            ).add_file_servers(
                FileServer("file://" + self.local_storage_dir, Operation.ALL)
            ),
        )

        self.sc.add_sites(local)

    # ------------------------------------------------------------------
    # Transformation Catalog
    # ------------------------------------------------------------------
    def create_transformation_catalog(self, exec_site_name=COMPUTE_SITE):
        self.tc = TransformationCatalog()

        # No container: MATLAB is licensed, so the job uses the site's MATLAB
        ptycho_recon = Transformation(
            "ptycho_recon",
            site=exec_site_name,
            pfn=os.path.join(self.wf_dir, "bin/ptycho_recon.py"),
            is_stageable=True,
        ).add_pegasus_profile(**RECON_RESOURCES)

        if not self.args.fold_slice_shared:
            # CondorIO: HTCondor copies fold_slice into each job directory
            ptycho_recon.add_profiles(Namespace.CONDOR, key="transfer_input_files",
                                      value=self.args.fold_slice_dir)

        self.tc.add_transformations(ptycho_recon)

    # ------------------------------------------------------------------
    # Replica Catalog — per-scan files are added in create_workflow()
    # ------------------------------------------------------------------
    def create_replica_catalog(self):
        self.rc = ReplicaCatalog()

        self.bg_file = None
        bg_path = self.args.bg_file
        if os.path.isfile(bg_path):
            self.bg_file = File("background_subtraction.mat")
            self.rc.add_replica("local", self.bg_file, "file://" + bg_path)

    # ------------------------------------------------------------------
    # Workflow DAG — the loop of param_config_multislice.py
    # ------------------------------------------------------------------
    def create_workflow(self):
        import pandas as pd

        self.wf = Workflow(self.wf_name, infer_dependencies=True)
        main_dir = self.args.data_dir
        mode = self.args.mode
        os.makedirs(self.script_dir, exist_ok=True)

        # Step 1: read table header
        param_df = pd.read_excel(self.args.params_xlsx)
        multislice_cols = param_df.shape[1] == len(PARAM_HEADER + MULTISLICE_HEADER + TRAILING_HEADER)
        param_df.columns = (PARAM_HEADER + (MULTISLICE_HEADER if multislice_cols else [])
                            + TRAILING_HEADER)[:param_df.shape[1]]
        rbf_df = pd.read_excel(os.path.join(self.wf_dir, RBF_XLSX), names=RBF_HEADER)

        with open(os.path.join(self.wf_dir, TEMPLATES[mode]), "r", encoding="utf-8") as file:
            template_str = file.read()

        # Only folders whose name starts with a number (the Acqu. Time) are scans
        scan_folders = [f for f in next(os.walk(main_dir))[1] if f.split("_")[0].isdigit()]

        for scan_folder in sorted(scan_folders):
            acqu_time = int(scan_folder.split("_")[0])
            scans = param_df.loc[param_df["Acqu_time"] == acqu_time].reset_index()

            if scans.shape[0] == 0:
                print(f"No scans found in the Ptycho parameters.xlsx for scan: {scan_folder}")
                continue

            for index, row in scans.iterrows():
                scan_number = index + 1
                scan_folder_formatted = re.sub(r"\+", "_plus", scan_folder)
                scan_folder_formatted = re.sub("-", "_minus", scan_folder_formatted)
                scan_folder_formatted = re.sub(r"\.", "pt", scan_folder_formatted)
                job_id = f"{scan_folder_formatted}_Scan{scan_number}"

                if pd.isna(row["Scan_size"]):
                    print(f"{job_id}: Scan size is empty, skipped")
                    continue

                # Step 2: read table content
                values = {
                    "rot_ang": row["Scan_rot"],
                    "camera_len": row["Camera_len"],
                    "dScanX": row["dScanXY"],
                    "dScanY": row["dScanXY"],
                    "exp_dir": f"{JOB_DIR_TOKEN}/",
                    "data_dir": scan_folder,
                    "N_scan_x": int(row["Scan_size"]),
                    "N_scan_y": int(row["Scan_size"]),
                    "voltage": row["Voltage"],
                    "ADU": get_ADU(row["Voltage"]),
                    "alpha0": get_alpha0(row["Conv_angle"]),
                    "rbf": get_rbf(rbf_df, row["Voltage"], row["Camera_len"], row["Conv_angle"]),
                    "scan_step_size": get_scan_step_size(row["Mag"], int(row["Scan_size"])),
                    # (an empty Sample/ROI cell is NaN; use "" like an empty string)
                    "data_descriptor": re.sub(r"[\W]+", "_", "" if pd.isna(row["Sample"]) else str(row["Sample"]))
                    + "_" + re.sub(r"[\W]+", "_", "" if pd.isna(row["ROI"]) else str(row["ROI"])),
                    "mag": row["Mag"],
                    "scan_number": scan_number,
                    "resample_factor": row["Resample_factor"],
                    "defocus": row["Defocus"],
                    "background_subtraction": int(row["background_subtraction"]),
                    "Niter": row["Niter"],
                    "Np_crop_pad": f"{row['Diff_pattern_size']},{row['Diff_pattern_size']}",
                    "Nprobe": row["Probe_states"],
                    "variable_probe_modes": row["Probe_modes"],
                    "GPU_solver": row["GPU_solver"],
                    "errmetric": row["errmetric"],
                    "initial_probe": int(row["initial_probe"]),
                    "cSAXS_matlab_path": FOLD_SLICE_TOKEN,
                    "ptycho_matlab_path": f"{FOLD_SLICE_TOKEN}/ptycho",
                }
                replace_vars = list(REPLACE_VARS)
                if mode == "multislice":
                    for col in MULTISLICE_HEADER:
                        values[col] = row[col] if multislice_cols else MULTISLICE_DEFAULTS[col]
                    replace_vars += MULTISLICE_HEADER

                if any(values[v] is None for v in ("ADU", "alpha0", "rbf", "scan_step_size")):
                    print(f"{job_id}: skipped (see message above)")
                    continue

                raw_name = f"scan_x{values['N_scan_x']}_y{values['N_scan_y']}.raw"
                raw_path = os.path.join(main_dir, scan_folder, raw_name)
                if not os.path.isfile(raw_path):
                    print(f"{job_id}: {raw_path} not found, skipped")
                    continue
                if values["background_subtraction"] == 1 and self.bg_file is None:
                    print(f"{job_id}: background_subtraction=1 but "
                          f"{self.args.bg_file} not found (use --bg-file), skipped")
                    continue

                # Step 3: replace variables
                file_str = template_str
                for var_name in replace_vars:
                    file_str = replace_single_var(var_name, str(values[var_name]), file_str)

                script_path = os.path.join(self.script_dir, f"run_{job_id}.m")
                with open(script_path, "w", encoding="utf-8") as file:
                    file.write(file_str)

                # Step 4: add the reconstruction job (was: a line in the sbatch file)
                self._add_recon_job(job_id, scan_folder, script_path, raw_path,
                                    raw_name, values)

        print(f"{len(self.wf.jobs)} jobs added, scripts in {self.script_dir}/")

    def _add_recon_job(self, job_id, scan_folder, script_path, raw_path, raw_name, values):
        script = File(os.path.basename(script_path))
        # Raw files share a name across scans, so the LFN is made unique
        raw = File(f"{job_id}_{raw_name}")
        recon_tar = File(f"{job_id}/{job_id}_recon.tar.gz")
        matlab_log = File(f"{job_id}/{job_id}_matlab.log")

        self.rc.add_replica("local", script, "file://" + script_path)
        self.rc.add_replica("local", raw, "file://" + raw_path)

        job = (
            Job("ptycho_recon", _id=f"recon_{job_id}", node_label=f"recon_{job_id}")
            .add_pegasus_profile(tag=RECON_TAG)
            .add_args("--script", script, "--scan-folder", scan_folder,
                      "--raw", raw, "--raw-name", raw_name,
                      "--fold-slice", self.args.fold_slice_arg,
                      "--matlab-module", MATLAB_MODULE,
                      "--output-tar", recon_tar, "--log", matlab_log)
            .add_inputs(script, raw)
            .add_outputs(recon_tar, matlab_log, stage_out=True, register_replica=False)
        )

        if values["background_subtraction"] == 1:
            job.add_args("--bg-file", self.bg_file).add_inputs(self.bg_file)

        if values["initial_probe"] == 1:
            # The template loads the single .mat in Recon_files/<scan folder>/
            recon_dir = os.path.join(self.args.data_dir, "Recon_files", scan_folder)
            mats = [f for f in os.listdir(recon_dir) if f.endswith(".mat")]
            init_recon = File(f"{job_id}_init_recon.mat")
            self.rc.add_replica("local", init_recon,
                                "file://" + os.path.join(recon_dir, mats[0]))
            job.add_args("--init-recon", init_recon).add_inputs(init_recon)

        self.wf.add_jobs(job)


# ======================================================================
# main() — CLI argument parsing
# ======================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Generate a Pegasus workflow that reconstructs every scan "
                    "of a ptychography experiment (fold_slice / PtychoShelves).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --data-dir /scratch/zhangych/Data/ --site-catalog-file access-pegasus.yml \\
      --fold-slice-dir ~/fold_slice
  %(prog)s --data-dir /scratch/zhangych/Data/ --site-catalog-file usc-discovery.yml \\
      --fold-slice-dir /home/zhangych/fold_slice --fold-slice-shared --mode singleslice

Then: pegasus-plan --submit --sites compute --output-sites local workflow.yml
""",
    )

    # --- Standard Pegasus arguments ---
    parser.add_argument("-s", "--skip-sites-catalog", action="store_true",
                        help="Skip site catalog creation")
    parser.add_argument("-e", "--execution-site-name", metavar="STR", type=str,
                        default=COMPUTE_SITE,
                        help=f"Execution site name (default: {COMPUTE_SITE})")
    parser.add_argument("-o", "--output", metavar="STR", type=str,
                        default="workflow.yml",
                        help="Output file (default: workflow.yml)")

    # --- Workflow-specific arguments ---
    parser.add_argument("--data-dir", required=True,
                        help='Experiment folder: scan folders, "Ptycho parameters.xlsx", '
                             '"Background subtraction.mat"')
    parser.add_argument("--site-catalog-file", required=True,
                        help="Hosted site catalog (pegasushub/pegasus-site-catalogs conf/), "
                             "e.g. access-pegasus.yml, usc-discovery.yml")
    parser.add_argument("--fold-slice-dir", required=True,
                        help="fold_slice directory (sent to each job unless --fold-slice-shared)")
    parser.add_argument("--fold-slice-shared", action="store_true",
                        help="--fold-slice-dir already exists on every worker; don't transfer it")
    parser.add_argument("--bg-file", default=None,
                        help='Background file (default: <data-dir>/"Background subtraction.mat")')
    parser.add_argument("--mode", choices=sorted(TEMPLATES), default="multislice",
                        help="MATLAB template to use (default: multislice)")

    args = parser.parse_args()

    # --- Input validation ---
    args.data_dir = os.path.abspath(args.data_dir)
    args.params_xlsx = os.path.join(args.data_dir, "Ptycho parameters.xlsx")
    args.bg_file = os.path.abspath(
        args.bg_file or os.path.join(args.data_dir, "Background subtraction.mat"))
    if not os.path.isfile(args.bg_file):
        logger.warning(f"No background file at {args.bg_file}; scans with "
                       "background_subtraction=1 will be skipped (use --bg-file)")
    if not os.path.isfile(args.params_xlsx):
        parser.error(f"not found: {args.params_xlsx}")
    if not args.fold_slice_shared:
        args.fold_slice_dir = os.path.abspath(args.fold_slice_dir.rstrip("/"))
        if not os.path.isdir(os.path.join(args.fold_slice_dir, "ptycho")):
            parser.error(f"--fold-slice-dir has no ptycho/ subfolder: {args.fold_slice_dir}")
    # What the job is told: the transferred copy's name, or the worker path
    args.fold_slice_arg = (args.fold_slice_dir if args.fold_slice_shared
                           else os.path.basename(args.fold_slice_dir))

    logger.info("=" * 70)
    logger.info("PTYCHO WORKFLOW GENERATOR")
    logger.info("=" * 70)
    logger.info(f"Working in {args.data_dir} ({args.mode})")
    logger.info(f"Execution site: {args.execution_site_name} "
                f"(hosted catalog {args.site_catalog_file})")
    logger.info(f"Output file: {args.output}")
    logger.info("=" * 70)

    try:
        workflow = PtychoWorkflow(args, dagfile=args.output)

        workflow.create_pegasus_properties()

        if not args.skip_sites_catalog:
            workflow.create_sites_catalog()

        workflow.create_transformation_catalog(exec_site_name=args.execution_site_name)
        workflow.create_replica_catalog()
        workflow.create_workflow()
        if not workflow.wf.jobs:
            sys.exit("Error: no scans to reconstruct.")
        workflow.write()

        logger.info(f"Workflow written to {args.output}")
        logger.info(
            f"Submit: pegasus-plan --submit "
            f"--sites {args.execution_site_name} --output-sites local {args.output}"
        )

    except Exception as e:
        logger.error(f"Failed to generate workflow: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
