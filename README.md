# Ptycho Pegasus Workflow

A [Pegasus WMS](https://pegasus.isi.edu/) workflow for batch electron
ptychography reconstruction with [fold_slice](https://github.com/yijiang1/fold_slice)
(PtychoShelves). It replaces `param_config_multislice.py` and the per-scan
`sbatch` routine. Every scan in an experiment folder that has a row in
`Ptycho parameters.xlsx` becomes one GPU job. Pegasus stages the data, runs
the reconstructions in parallel, retries failures, and brings the results back.

## Pipeline Overview

```
Ptycho parameters.xlsx ─┐   workflow_generator.py
rbf.xlsx ───────────────┼──> (generation time) ──> Scripts/run_<scan>_Scan<n>.m
MATLAB template ────────┤
scan folders ───────────┘
                                        │   (workflow)
        ┌───────────────────────────────┤
        ├── ptycho_recon(scan A, GPU) ──> <scan A>_recon.tar.gz, <scan A>_matlab.log
        ├── ptycho_recon(scan B, GPU) ──> ...
        └── ptycho_recon(scan N, GPU) ──> ...
```

`workflow_generator.py` follows `param_config_multislice.py` step for step.
It uses the same lookup functions (`get_rbf`, `get_ADU`, `get_alpha0`,
`get_scan_step_size`), the same loop over scan folders and sheet rows, and the
same regex replacement to write `Scripts/run_<scan>_Scan<n>.m`. Where the old
script added a line to an sbatch file, the generator adds a Pegasus job. The
jobs are independent, exactly like the old per-scan `srun` lines.

| Step | Tool | Description |
|------|------|-------------|
| ptycho_recon | host MATLAB + fold_slice, 1 GPU | Lays out the staged inputs as the template expects, runs `matlab -batch run_<scan>`, and packs `Recon_result/` into a tarball. |

## Directory Structure

```
pegasus-ptycho/
├── workflow_generator.py        # Pegasus workflow generator (param_config logic + catalogs)
├── bin/
│   └── ptycho_recon.py          # MATLAB/GPU reconstruction wrapper
├── templates/
│   ├── Ptychography_reconstruction_multislice_template.m
│   └── Ptychography_reconstruction_singleslice_template.m
├── config/
│   └── rbf.xlsx                 # rbf calibration (voltage, camera length, conv. angle)
├── run_manual.sh                # Local test
└── README.md
```

## Prerequisites

- [Pegasus WMS](https://pegasus.isi.edu/) >= 5.1.3 and [HTCondor](https://htcondor.org/) >= 10.2 on the submit host. 5.1.3 is the first release with [hosted site catalogs](https://pegasus.isi.edu/docs/5.1.3/reference-guide/catalogs.html#centrally-hosted-site-catalogs) and job tagging. The planner also needs outbound HTTPS to GitHub.
- Python 3.8+ with `pandas` and `openpyxl` on the submit host (the generator reads the `.xlsx` files)
- A compute resource described by one of the hosted site catalogs (an HTCondor pool or a SLURM cluster), with GPUs. On the GPU workers:
  - MATLAB with the Parallel Computing Toolbox, through `module load matlab/2021b` or on `PATH`
  - `python3`
- A fold_slice checkout on a filesystem shared by the GPU workers

No container is used. MATLAB needs a license, so `ptycho_recon` runs directly
on the GPU worker and loads MATLAB exactly as the old sbatch file did.

## Prepare the experiment folder

The layout is the one `param_config_multislice.py` expects, with scan folders
directly inside the experiment folder:

```
<experiment>/
├── Ptycho parameters.xlsx
├── Background subtraction.mat          # needed if any row has background subtraction = 1
├── Recon_files/<scan folder>/*.mat     # only for rows with "Previous initial probe" = 1
├── 1302_14.5Mx_CL230mm_df10nm_25mrad_scan128_15pA/
│   └── scan_x128_y128.raw
└── 1305_.../
    └── scan_x128_y128.raw
```

- **Scan folders** are the sub-folders whose name starts with the acquisition
  time (`1302_...`). Each is matched to the sheet rows with the same
  *Acqu. Time*. If several rows share a time, they become `Scan1`, `Scan2`, ...
- **Skipped scans.** As in `param_config`, problems are printed and the scan
  is skipped. Examples: no sheet row, an unknown voltage / camera length /
  convergence angle, an empty scan size, a missing raw file, or
  no background file (`<experiment>/Background subtraction.mat`, or
  `--bg-file`) for a row that needs it.
- **Sheet layout.** The current 28-column sheets have no multislice columns.
  In multislice mode, `previous_object`, `regularization`, `thickness` and
  `Nlayers` then come from `MULTISLICE_DEFAULTS` at the top of
  `workflow_generator.py`. A 32-column sheet with those four columns after
  *Previous initial probe* is read directly.

## Settings

As with `code_dir` in `param_config` and the partition and time settings in
the old submit script, the settings that rarely change are constants at the
top of `workflow_generator.py`:

| Constant | Default | Meaning |
|----------|---------|---------|
| `TEMPLATES` | `templates/...` | MATLAB template per `--mode` |
| `RBF_XLSX` | `config/rbf.xlsx` | rbf calibration table |
| `MATLAB_MODULE` | `matlab/2021b` | Module loaded before MATLAB (`''` if `matlab` is on `PATH`) |
| `RECON_RESOURCES` | 40 GB, 4 cores, 1 GPU, 14400 s | Per recon job, as in the old sbatch file |
| `RECON_TAG` | `gpu` | Job tag that selects the hosted catalog's GPU profiles |
| `MULTISLICE_DEFAULTS` | `0`, `0.1`, `13`, `2` | previous_object, regularization, thickness, Nlayers |

## Execution site: hosted site catalogs

The execution site is not described in this repository. Pegasus'
[centrally hosted site catalogs](https://pegasus.isi.edu/docs/5.1.3/reference-guide/catalogs.html#centrally-hosted-site-catalogs)
provide it:

- `--site-catalog-file` sets `pegasus.catalog.site.repo.file`. At plan time,
  the planner downloads that file from
  [pegasushub/pegasus-site-catalogs](https://github.com/pegasushub/pegasus-site-catalogs/tree/main/conf),
  from the branch matching its major.minor version (e.g. `6.0`).
- Jobs are planned for the site named **`compute`**.
- The local `sites.yml` describes only the submit host (`local`: `scratch/`
  and `output/`). Pegasus merges it over the downloaded catalog.
- Recon jobs are tagged `gpu`, so a catalog's GPU `x-tags` (e.g. a `gpu`
  queue) apply. Several catalogs read `${RESOURCE_PROJECT}` from the
  environment when planning.

| Hosted file | Resource |
|-------------|----------|
| `access-pegasus.yml`, `access-pegasus-expanse.yml` | ACCESS Pegasus (HTCondor glideins) |
| `ospool-ap40.yml`, `ospool-ap41.yml` | OSPool access points |
| `usc-discovery.yml`, `usc-laguna.yml`, `unity.yml`, `purdue-anvil.yml` | SLURM clusters (direct submission) |
| `nersc-perlmutter.yml` | NERSC Perlmutter (SFAPI) |

**fold_slice.** `--fold-slice-dir` is the absolute path of fold_slice on the
workers' shared filesystem. Pegasus does not transfer it: like
`param_config_multislice.py`, the generator writes the path into each MATLAB
script, so it must exist at that path on every worker.

## Usage

### Generate the workflow

```bash
./workflow_generator.py \
    --data-dir "/scratch/zhangych/Data/20230805_WSe2_EMPAD/" \
    --site-catalog-file access-pegasus.yml \
    --fold-slice-dir /home/zhangych/fold_slice \
    --mode multislice
```

On a SLURM cluster:

```bash
export RESOURCE_PROJECT=myalloc
./workflow_generator.py \
    --data-dir "/scratch/zhangych/Data/20230805_WSe2_EMPAD/" \
    --site-catalog-file usc-discovery.yml \
    --fold-slice-dir /home/zhangych/fold_slice
```

This writes `workflow.yml` and the catalogs to the current directory, and the
rendered MATLAB scripts to `Scripts/` next to the generator. Check or diff
those scripts before planning, the same way as the old `Scripts/` folder.

### CLI options

| Option | Default | Description |
|--------|---------|-------------|
| `--data-dir` | (required) | Experiment folder |
| `--site-catalog-file` | (required) | Hosted site catalog file name, e.g. `access-pegasus.yml` |
| `--fold-slice-dir` | (required) | Absolute path of fold_slice on the workers' shared filesystem |
| `--bg-file` | `<data-dir>/Background subtraction.mat` | Background file for rows with background subtraction = 1 |
| `--mode` | `multislice` | `multislice` or `singleslice` template |
| `-e`, `--execution-site-name` | `compute` | Execution site name |
| `-s`, `--skip-sites-catalog` | off | Don't write the local `sites.yml` |
| `-o`, `--output` | `workflow.yml` | Output workflow file |

### Plan and submit

```bash
pegasus-plan --submit --dir submit --sites compute --output-sites local workflow.yml
```

### Monitor

```bash
pegasus-status -l submit/$USER/pegasus/ptycho/run0001
pegasus-analyzer submit/$USER/pegasus/ptycho/run0001    # on failure
pegasus-statistics submit/$USER/pegasus/ptycho/run0001
```

When a recon job fails, `pegasus-analyzer` shows the last 60 lines of the
MATLAB log.

## Outputs

Final outputs are staged to `output/`:

| Output | Description |
|--------|-------------|
| `<scan>_Scan<n>/<scan>_Scan<n>_recon.tar.gz` | The whole `Recon_result/` tree: `init_probe_*.mat`, `*_params.mat`, the `Reduced_run_*` / `Full_run_*` iterations, and the script with paths resolved. |
| `<scan>_Scan<n>/<scan>_Scan<n>_matlab.log` | MATLAB stdout/stderr (the old `Log/Data_<scan>.txt`). |

The intermediate `data_roi*_dp.hdf5` that the template writes next to the raw
data stays on the worker. It is not staged back.

## How the template is adapted to Pegasus

Pegasus runs each job in its own scratch directory, so the hard-coded
`exp_dir` in the templates cannot be a fixed path. The generator renders it as
the placeholder `__PEGASUS_JOB_DIR__/`, and `ptycho_recon.py` replaces that with
the job directory on the worker. `cSAXS_matlab_path` and `ptycho_matlab_path`
are rendered directly from `--fold-slice-dir` (`<dir>` and `<dir>/ptycho`).

The raw file is registered as a **deep LFN**,
`Data/<scan folder>/scan_xN_yN.raw`. That is the path the template reads
(`<exp_dir>/Data/<data_dir>/`), so Pegasus places the file exactly there in
each job directory. It also keeps raw files from different scans apart even
though they share a file name. Sheet rows that share an acquisition time
(`Scan1`, `Scan2`) use the same raw file. The `Data/` prefix comes from the
template; the experiment folder itself needn't have a `Data/` sub-folder.

Before starting MATLAB, the wrapper links the other staged inputs to the
paths the template reads: `Background subtraction.mat` and
`Recon_files/<scan folder>/init_recon.mat`.
Nothing else in the templates was changed. As in `param_config`, the
`defocus` value goes into the script exactly as written in the sheet.

## Testing locally

```bash
./run_manual.sh --data-dir "/scratch/zhangych/Data/20230805_WSe2_EMPAD/" --scan 1302
```

This generates the workflow and renders the scripts, and runs anywhere. To also run the
MATLAB reconstruction, run it on a GPU node with MATLAB and add
`--fold-slice /path/to/fold_slice --matlab-module matlab/2021b`, where the
path is where fold_slice lives on that node.

The bundled `Sample dataset` has no `Background subtraction.mat`, so pass
one with `--bg-file` or its scan 1302 is skipped. Its scan 0102 has no raw file.

## Resource Requirements

| Step | Memory | Cores | GPUs | Runtime |
|------|--------|-------|------|---------|
| ptycho_recon | 40 GB | 4 | 1 | 4 h |

Each recon job receives one raw file (about 1 GB for a 128×128 scan).
fold_slice is read in place from the shared filesystem.

## Dependencies

- Submit host: Pegasus >= 5.1.3, HTCondor, Python 3 with `pandas`, `openpyxl`
- GPU workers: MATLAB (R2021b, as in the original scripts) with the Parallel Computing Toolbox, CUDA-capable GPU, `python3`
