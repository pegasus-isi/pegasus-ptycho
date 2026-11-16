#!/usr/bin/env python3

"""
Pegasus workflow generator for electron ptychography reconstruction.

Replaces the param_config_multislice.py + sbatch pair: instead of writing one
MATLAB script and one sbatch file per scan and submitting them by hand, every
scan listed in "Ptycho parameters.xlsx" that has a matching scan folder becomes
an independent branch of one Pegasus DAG.

Pipeline steps (per scan, all scans in parallel):
1. render_script - fill the MATLAB template with this scan's parameters
2. ptycho_recon  - run fold_slice / PtychoShelves with host MATLAB on one GPU
Then, once for the whole experiment:
3. summarize     - collect the per-scan summaries into recon_summary.csv

The execution site is not described here: the planner downloads it from the
centrally hosted Pegasus site catalogs (github.com/pegasushub/pegasus-site-catalogs,
property pegasus.catalog.site.repo.file) and jobs are planned for the site
named "compute". The local sites.yml written here only describes the submit
host ("local") and is merged over the downloaded catalog.

Usage:
    ./workflow_generator.py --data-dir "/scratch/zhangych/Data/" \\
        --site-catalog-file access-pegasus.yml \\
        --fold-slice-dir /home/zhangych/fold_slice --mode multislice
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from Pegasus.api import *

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# Per-tool resource configuration. ptycho_recon mirrors the old sbatch file:
# --cpus-per-task=4 --gpus-per-task=a100:1 --mem-per-gpu=40GB
# "tag" selects the matching x-tags profiles (e.g. a GPU queue) from the
# hosted site catalog for the compute site.
TOOL_CONFIGS = {
    "render_script": {"memory": "1 GB", "cores": 1, "container": True, "tag": "cpu"},
    "ptycho_recon": {"memory": "40 GB", "cores": 4, "gpus": 1, "container": False,
                     "tag": "gpu"},
    "summarize": {"memory": "1 GB", "cores": 1, "container": True, "tag": "cpu"},
}

# Site name the hosted catalogs use for the execution site
COMPUTE_SITE = "compute"

# Files in pegasushub/pegasus-site-catalogs (conf/) at the time of writing
KNOWN_HOSTED_CATALOGS = [
    "access-pegasus.yml", "access-pegasus-expanse.yml", "nersc-perlmutter.yml",
    "ospool-ap40.yml", "ospool-ap41.yml", "purdue-anvil.yml", "unity.yml",
    "usc-discovery.yml", "usc-laguna.yml",
]

TEMPLATES = {
    "multislice": "templates/Ptychography_reconstruction_multislice_template.m",
    "singleslice": "templates/Ptychography_reconstruction_singleslice_template.m",
}


class PtychoWorkflow:
    """One reconstruction branch per (scan folder, spreadsheet row)."""

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

    def __init__(self, scans, args, dagfile="workflow.yml"):
        self.dagfile = dagfile
        self.wf_dir = str(Path(__file__).parent.resolve())
        self.shared_scratch_dir = os.path.join(self.wf_dir, "scratch")
        self.local_storage_dir = args.output_dir or os.path.join(self.wf_dir, "output")
        self.params_dir = os.path.join(self.wf_dir, "params")
        self.scans = scans
        self.args = args

        self.template_lfn = f"ptycho_{args.mode}_template.m"
        self.template_path = args.template or os.path.join(self.wf_dir, TEMPLATES[args.mode])

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
        # A GPU node occasionally dies mid-run; retry each job once
        self.props["dagman.retry"] = str(self.args.retry)

        # Hosted site catalog: the planner downloads this file from
        # github.com/pegasushub/pegasus-site-catalogs (branch = Pegasus
        # major.minor) and merges the local sites.yml over it.
        self.props["pegasus.catalog.site.repo.file"] = self.args.site_catalog_file
        if self.args.site_catalog_refresh is not None:
            self.props["pegasus.catalog.site.repo.interval"] = str(
                self.args.site_catalog_refresh
            )

        # Site-specific overrides of the downloaded compute entry, e.g.
        # pegasus.queue=gpu or pegasus.project=myalloc
        for ns, key, value in self.args.site_profile:
            self.props.add_site_profile(self.args.execution_site_name, ns, key, value)

    # ------------------------------------------------------------------
    # Site Catalog
    # ------------------------------------------------------------------
    def create_sites_catalog(self):
        """Only the submit host. The compute site comes from the hosted catalog."""
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

        # Container for the small Python steps only. ptycho_recon runs on the
        # host so it can use the site's licensed MATLAB (module load).
        container = None
        if not self.args.no_container:
            container = Container(
                "ptycho_container",
                container_type=Container.SINGULARITY,
                image="file://" + os.path.abspath(self.args.container_image),
                image_site="local",
            )
            self.tc.add_containers(container)

        transformations = []
        for tool_name, config in TOOL_CONFIGS.items():
            tx = Transformation(
                tool_name,
                site=exec_site_name,
                pfn=os.path.join(self.wf_dir, f"bin/{tool_name}.py"),
                is_stageable=True,
                container=container if config["container"] else None,
            ).add_pegasus_profile(
                memory=(self.args.recon_memory if tool_name == "ptycho_recon"
                        else config["memory"]),
                cores=config.get("cores", 1),
            )
            if config.get("gpus"):
                tx.add_pegasus_profile(gpus=config["gpus"])
            if tool_name == "ptycho_recon":
                # Walltime for batch-scheduler sites (hosted SLURM catalogs
                # default to 30 min); the old sbatch file asked for 4 h.
                tx.add_pegasus_profile(runtime=self.args.recon_runtime)
            transformations.append(tx)

            if tool_name == "ptycho_recon":
                if self.args.fold_slice_dir:
                    # CondorIO: HTCondor copies the whole fold_slice tree into
                    # each job's directory as ./<basename>
                    tx.add_profiles(
                        Namespace.CONDOR,
                        key="transfer_input_files",
                        value=os.path.abspath(self.args.fold_slice_dir.rstrip("/")),
                    )
                if self.args.gpu_requirements:
                    tx.add_profiles(
                        Namespace.CONDOR,
                        key="requirements",
                        value=self.args.gpu_requirements,
                    )

        self.tc.add_transformations(*transformations)

    # ------------------------------------------------------------------
    # Replica Catalog
    # ------------------------------------------------------------------
    def create_replica_catalog(self):
        self.rc = ReplicaCatalog()

        self.rc.add_replica("local", self.template_lfn,
                            "file://" + os.path.abspath(self.template_path))

        if any(s["background_subtraction"] == 1 for s in self.scans):
            self.rc.add_replica("local", "background_subtraction.mat",
                                "file://" + os.path.abspath(self.args.bg_file))

        os.makedirs(self.params_dir, exist_ok=True)
        for scan in self.scans:
            sid = scan["job_id"]

            # Per-scan parameters, written now and staged to the render job
            params_path = os.path.join(self.params_dir, f"{sid}.json")
            with open(params_path, "w") as f:
                json.dump(scan, f, indent=2)
            self.rc.add_replica("local", f"{sid}_params.json", "file://" + params_path)

            # Raw files share a name across scans (scan_x128_y128.raw), so the
            # LFN is made unique; ptycho_recon.py links it back to its real name.
            self.rc.add_replica("local", f"{sid}_{scan['raw_name']}",
                                "file://" + scan["raw_path"])

            if scan["init_recon_path"]:
                self.rc.add_replica("local", f"{sid}_init_recon.mat",
                                    "file://" + scan["init_recon_path"])

    # ------------------------------------------------------------------
    # Workflow DAG
    # ------------------------------------------------------------------
    def create_workflow(self):
        self.wf = Workflow(self.wf_name, infer_dependencies=True)

        template = File(self.template_lfn)
        bg_file = File("background_subtraction.mat")

        summaries = []
        for scan in self.scans:
            summaries.append(self._add_scan_pipeline(scan, template, bg_file))

        summary_csv = File("recon_summary.csv")
        summarize_job = (
            Job("summarize", _id="summarize", node_label="summarize")
            .add_pegasus_profile(tag=TOOL_CONFIGS["summarize"]["tag"])
            .add_args(*[a for s in summaries for a in ("--input", s)],
                      "--output", summary_csv)
            .add_inputs(*summaries)
            .add_outputs(summary_csv, stage_out=True, register_replica=False)
        )
        self.wf.add_jobs(summarize_job)

    def _add_scan_pipeline(self, scan, template, bg_file):
        """render_script -> ptycho_recon for one scan; returns its summary File."""
        sid = scan["job_id"]

        params = File(f"{sid}_params.json")
        raw = File(f"{sid}_{scan['raw_name']}")
        script = File(f"{scan['script_name']}.m")
        recon_tar = File(f"{sid}/{sid}_recon.tar.gz")
        matlab_log = File(f"{sid}/{sid}_matlab.log")
        summary = File(f"{sid}_summary.json")

        render_job = (
            Job("render_script", _id=f"render_{sid}", node_label=f"render_{sid}")
            .add_pegasus_profile(tag=TOOL_CONFIGS["render_script"]["tag"])
            .add_args("--template", template, "--params", params, "--output", script)
            .add_inputs(template, params)
            .add_outputs(script, stage_out=True, register_replica=False)
        )
        self.wf.add_jobs(render_job)

        recon_args = [
            "--script", script,
            "--params", params,
            "--raw", raw,
            "--fold-slice", self.fold_slice_arg(),
            "--matlab-cmd", self.args.matlab_cmd,
            "--output-tar", recon_tar,
            "--log", matlab_log,
            "--summary", summary,
        ]
        if self.args.matlab_module:
            recon_args += ["--matlab-module", self.args.matlab_module]
        recon_inputs = [script, params, raw]

        if scan["background_subtraction"] == 1:
            recon_args += ["--bg-file", bg_file]
            recon_inputs.append(bg_file)
        if scan["init_recon_path"]:
            init_recon = File(f"{sid}_init_recon.mat")
            recon_args += ["--init-recon", init_recon]
            recon_inputs.append(init_recon)

        recon_job = (
            Job("ptycho_recon", _id=f"recon_{sid}", node_label=f"recon_{sid}")
            .add_pegasus_profile(tag=TOOL_CONFIGS["ptycho_recon"]["tag"])
            .add_args(*recon_args)
            .add_inputs(*recon_inputs)
            .add_outputs(recon_tar, matlab_log, stage_out=True, register_replica=False)
            .add_outputs(summary, stage_out=False, register_replica=False)
        )
        self.wf.add_jobs(recon_job)
        return summary

    def fold_slice_arg(self):
        """What ptycho_recon.py is told: CondorIO basename or worker path."""
        if self.args.fold_slice_dir:
            return os.path.basename(self.args.fold_slice_dir.rstrip("/"))
        return self.args.fold_slice_worker_path


# ======================================================================
# main() — CLI argument parsing
# ======================================================================
def main():
    wf_dir = str(Path(__file__).parent.resolve())

    parser = argparse.ArgumentParser(
        description="Generate a Pegasus workflow that reconstructs every scan "
                    "of a ptychography experiment (fold_slice / PtychoShelves).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # HTCondor pool (ACCESS Pegasus); fold_slice shipped with each job
  %(prog)s --data-dir /scratch/zhangych/Data/ --site-catalog-file access-pegasus.yml \\
      --fold-slice-dir ~/fold_slice

  # SLURM cluster; fold_slice already on the shared filesystem, GPU queue override
  %(prog)s --data-dir /scratch/zhangych/Data/ --site-catalog-file usc-discovery.yml \\
      --fold-slice-worker-path /home/zhangych/fold_slice --mode singleslice \\
      --site-profile pegasus.project=myalloc --scans 1302 1305

  Then: pegasus-plan --submit --sites compute --output-sites local workflow.yml
""",
    )

    # --- Standard Pegasus arguments ---
    parser.add_argument("-s", "--skip-sites-catalog", action="store_true",
                        help="Do not write the local sites.yml (Pegasus then uses "
                             "its default 'local' site)")
    parser.add_argument("-e", "--execution-site-name", metavar="STR", type=str,
                        default=COMPUTE_SITE,
                        help=f"Execution site name in the hosted catalog "
                             f"(default: {COMPUTE_SITE})")

    # --- Hosted site catalog ---
    g = parser.add_argument_group("hosted site catalog")
    g.add_argument("--site-catalog-file", required=True, metavar="FILE",
                   help="Hosted site catalog to download from "
                        "github.com/pegasushub/pegasus-site-catalogs (conf/), e.g. "
                        + ", ".join(KNOWN_HOSTED_CATALOGS))
    g.add_argument("--site-catalog-refresh", type=int, default=None, metavar="SECONDS",
                   help="Re-download the hosted catalog if older than this "
                        "(Pegasus default: 1 day)")
    g.add_argument("--site-profile", action="append", default=[],
                   metavar="NS.KEY=VALUE",
                   help="Override a profile of the downloaded compute site, e.g. "
                        "pegasus.queue=gpu or pegasus.project=myalloc (repeatable)")
    parser.add_argument("-o", "--output", metavar="STR", type=str,
                        default="workflow.yml",
                        help="Output file (default: workflow.yml)")
    parser.add_argument("--output-dir", default=None,
                        help="Where final outputs are staged (default: ./output)")

    # --- Experiment inputs ---
    g = parser.add_argument_group("experiment inputs")
    g.add_argument("--data-dir", required=True,
                   help="Experiment folder: scan folders named <AcquTime>_..., "
                        '"Ptycho parameters.xlsx", "Background subtraction.mat"')
    g.add_argument("--params-xlsx", default=None,
                   help='Parameters sheet (default: <data-dir>/"Ptycho parameters.xlsx")')
    g.add_argument("--rbf-xlsx", default=os.path.join(wf_dir, "config", "rbf.xlsx"),
                   help="rbf calibration table (default: config/rbf.xlsx)")
    g.add_argument("--bg-file", default=None,
                   help='Background file (default: <data-dir>/"Background subtraction.mat")')
    g.add_argument("--scans", type=int, nargs="+", default=None, metavar="ACQU_TIME",
                   help="Only reconstruct these acquisition times (default: all)")
    g.add_argument("--skip-missing", action="store_true",
                   help="Skip scans with problems (missing raw file, unknown rbf, ...) "
                        "instead of failing")

    # --- Reconstruction ---
    g = parser.add_argument_group("reconstruction")
    g.add_argument("--mode", choices=sorted(TEMPLATES), default="multislice",
                   help="Which MATLAB template to use (default: multislice)")
    g.add_argument("--template", default=None,
                   help="Override the MATLAB template file for --mode")
    g.add_argument("--regularization", type=float, default=0.1,
                   help="multislice default when the sheet has no such column (0.1)")
    g.add_argument("--thickness", type=float, default=13,
                   help="multislice default sample thickness in angstrom (13)")
    g.add_argument("--nlayers", type=int, default=2,
                   help="multislice default number of slices (2)")
    g.add_argument("--previous-object", type=int, default=0, choices=[0, 1],
                   help="multislice default for previous_object (0)")

    # --- Execution environment ---
    g = parser.add_argument_group("execution environment")
    fs = g.add_mutually_exclusive_group(required=True)
    fs.add_argument("--fold-slice-dir",
                    help="fold_slice checkout on the submit host; sent to every "
                         "recon job via HTCondor file transfer")
    fs.add_argument("--fold-slice-worker-path",
                    help="fold_slice path that already exists on every GPU worker "
                         "(shared filesystem); nothing is transferred")
    g.add_argument("--matlab-module", default="matlab/2021b",
                   help="Module loaded before MATLAB; '' to skip (default: matlab/2021b)")
    g.add_argument("--matlab-cmd", default="matlab",
                   help="MATLAB executable on the workers (default: matlab)")
    g.add_argument("--recon-memory", default="40 GB",
                   help="Memory for each recon job (default: 40 GB)")
    g.add_argument("--recon-runtime", type=int, default=14400, metavar="SECONDS",
                   help="Walltime for each recon job on batch sites (default: 14400 = 4 h)")
    g.add_argument("--gpu-requirements", default=None,
                   help='Extra HTCondor requirements for recon jobs, '
                        'e.g. \'(GPUs_GlobalMemoryMb >= 40000)\'')
    g.add_argument("--container-image", default=os.path.join(wf_dir, "Ptycho_Container.sif"),
                   help="Apptainer image for the Python steps (default: ./Ptycho_Container.sif)")
    g.add_argument("--no-container", action="store_true",
                   help="Run the Python steps with the workers' own python3")
    g.add_argument("--retry", type=int, default=1,
                   help="DAGMan retries per job (default: 1)")

    args = parser.parse_args()

    # --- Input validation (before any Pegasus API calls) ---
    if os.path.basename(args.site_catalog_file) != args.site_catalog_file:
        parser.error("--site-catalog-file is a file name in the hosted repository "
                     f"(e.g. access-pegasus.yml), not a path: {args.site_catalog_file}")
    if args.site_catalog_file not in KNOWN_HOSTED_CATALOGS:
        logger.warning(f"{args.site_catalog_file} is not one of the hosted catalogs "
                       f"known to this generator: {', '.join(KNOWN_HOSTED_CATALOGS)}")
    site_profiles = []
    for spec in args.site_profile:
        nskey, sep, value = spec.partition("=")
        ns, dot, key = nskey.partition(".")
        if not sep or not dot or not ns or not key:
            parser.error(f"--site-profile must look like NS.KEY=VALUE: {spec}")
        site_profiles.append((ns, key, value))
    args.site_profile = site_profiles

    args.data_dir = os.path.abspath(args.data_dir)
    if not os.path.isdir(args.data_dir):
        parser.error(f"--data-dir not found: {args.data_dir}")
    args.params_xlsx = args.params_xlsx or os.path.join(args.data_dir, "Ptycho parameters.xlsx")
    args.bg_file = args.bg_file or os.path.join(args.data_dir, "Background subtraction.mat")
    for label, path in (("--params-xlsx", args.params_xlsx), ("--rbf-xlsx", args.rbf_xlsx)):
        if not os.path.isfile(path):
            parser.error(f"{label} not found: {path}")
    template = args.template or os.path.join(wf_dir, TEMPLATES[args.mode])
    if not os.path.isfile(template):
        parser.error(f"MATLAB template not found: {template}")
    if args.fold_slice_dir and not os.path.isdir(os.path.join(args.fold_slice_dir, "ptycho")):
        parser.error(f"--fold-slice-dir has no ptycho/ subfolder: {args.fold_slice_dir}")
    if not args.no_container and not os.path.isfile(args.container_image):
        logger.warning(f"Container image not built yet: {args.container_image} "
                       "(see README: apptainer build)")

    try:
        import ptycho_params
    except ImportError as e:
        sys.exit(f"Error: {e}. The generator needs pandas and openpyxl "
                 "(pip install pandas openpyxl).")

    defaults = {
        "regularization": args.regularization,
        "thickness": args.thickness,
        "Nlayers": args.nlayers,
        "previous_object": args.previous_object,
    }
    scans, problems = ptycho_params.collect_scans(
        args.data_dir, args.params_xlsx, args.rbf_xlsx, args.mode, defaults,
        acqu_times=set(args.scans) if args.scans else None,
    )

    for scan in scans:
        if scan["raw_path"] is None:
            problems.append(f"{scan['job_id']}: raw file {scan['raw_name']} not in "
                            f"{scan['scan_folder']}/")
        if scan["background_subtraction"] == 1 and not os.path.isfile(args.bg_file):
            problems.append(f"{scan['job_id']}: background_subtraction=1 but "
                            f"{args.bg_file} does not exist (use --bg-file)")
    bad = {p.split(":")[0] for p in problems}
    scans = [s for s in scans if s["job_id"] not in bad]

    for p in problems:
        logger.warning(p)
    if problems and not args.skip_missing:
        sys.exit(f"Error: {len(problems)} problem(s) above. Fix them or pass "
                 "--skip-missing to reconstruct only the valid scans.")
    if not scans:
        sys.exit("Error: no scans to reconstruct.")

    logger.info("=" * 70)
    logger.info("PTYCHO WORKFLOW GENERATOR")
    logger.info("=" * 70)
    logger.info(f"Experiment: {args.data_dir}")
    logger.info(f"Mode:       {args.mode}")
    for s in scans:
        logger.info(f"  {s['job_id']}")
    logger.info(f"Scans:      {len(scans)}")
    logger.info(f"Execution site: {args.execution_site_name} "
                f"(hosted catalog {args.site_catalog_file})")
    logger.info(f"Output file: {args.output}")
    logger.info("=" * 70)

    try:
        workflow = PtychoWorkflow(scans, args, dagfile=args.output)

        workflow.create_pegasus_properties()

        if not args.skip_sites_catalog:
            workflow.create_sites_catalog()

        workflow.create_transformation_catalog(exec_site_name=args.execution_site_name)
        workflow.create_replica_catalog()
        workflow.create_workflow()
        workflow.write()

        logger.info(f"Workflow written to {args.output}")
        logger.info(
            f"Submit: pegasus-plan --submit "
            f"-s {args.execution_site_name} -o local {args.output}"
        )

    except Exception as e:
        logger.error(f"Failed to generate workflow: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
