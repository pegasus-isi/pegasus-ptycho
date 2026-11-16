# Ptycho Pegasus Workflow

A [Pegasus WMS](https://pegasus.isi.edu/) workflow for batch electron
ptychography reconstruction with [fold_slice](https://github.com/yijiang1/fold_slice)
(PtychoShelves). It replaces the `param_config_multislice.py` + per-scan
`sbatch` routine. Every scan in an experiment folder that has a row in
`Ptycho parameters.xlsx` becomes one branch of a single DAG. Pegasus stages the
data, runs the reconstructions in parallel on GPU nodes, retries failures, and
brings the results back.

## Pipeline Overview

```
Ptycho parameters.xlsx ─┐      (read by workflow_generator.py)
rbf.xlsx ───────────────┤
scan folders ───────────┘
        │
        ├── render_script(scan A) ──> ptycho_recon(scan A, GPU) ──┐
        ├── render_script(scan B) ──> ptycho_recon(scan B, GPU) ──┼──> summarize ──> recon_summary.csv
        └── render_script(scan N) ──> ptycho_recon(scan N, GPU) ──┘
```

| Step | Tool | Description |
|------|------|-------------|
| 0. (generation time) | `ptycho_params.py` (pandas) | Reads the parameters and rbf sheets and works out ADU, alpha0, rbf and scan step size for each scan. This is the spreadsheet half of `param_config_multislice.py`. |
| 1. render_script | Python | Fills in the MATLAB template for one scan, using the same regex replacement as `param_config`. |
| 2. ptycho_recon | host MATLAB + fold_slice, 1 GPU | Lays out the staged inputs as the template expects, runs `matlab -batch run_<scan>`, and packs `Recon_result/` into a tarball. |
| 3. summarize | Python | Combines the per-scan summaries into one CSV. |

## Directory Structure

```
ptycho-workflow/
├── workflow_generator.py        # Pegasus workflow generator
├── ptycho_params.py             # Spreadsheet -> per-scan parameters (also a CLI)
├── bin/
│   ├── render_script.py         # Template -> run_<scan>.m
│   ├── ptycho_recon.py          # MATLAB/GPU reconstruction wrapper
│   └── summarize.py             # Fan-in: recon_summary.csv
├── templates/
│   ├── Ptychography_reconstruction_multislice_template.m
│   └── Ptychography_reconstruction_singleslice_template.m
├── config/
│   └── rbf.xlsx                 # rbf calibration (voltage, camera length, conv. angle)
├── Apptainer/
│   └── Ptycho_Container.def     # Container for the Python steps
├── run_manual.sh                # Step-by-step local test
└── README.md
```

## Prerequisites

- [Pegasus WMS](https://pegasus.isi.edu/) >= 5.1.3 and [HTCondor](https://htcondor.org/) >= 10.2 on the submit host
- Python 3.8+ with `pandas` and `openpyxl` on the submit host (the generator reads the `.xlsx` files)
- Pegasus >= 5.1.3 for [hosted site catalogs](https://pegasus.isi.edu/docs/5.1.3/reference-guide/catalogs.html#centrally-hosted-site-catalogs) and job tagging. The planner also needs outbound HTTPS to GitHub.
- A compute resource described by one of the hosted site catalogs (an HTCondor pool, or a SLURM cluster), with GPUs. On the GPU workers:
  - MATLAB with the Parallel Computing Toolbox, either through `module load matlab/2021b` or on `PATH`
  - `python3` (the recon wrapper runs on the host, not in a container)
- A fold_slice checkout
- [Apptainer](https://apptainer.org/), to build the small container for the Python steps (optional, see `--no-container`)

### Why MATLAB runs on the host

MATLAB needs a license, so it cannot be baked into a container the way pip
tools can. `ptycho_recon` runs directly on the GPU worker and loads MATLAB
exactly as the old sbatch file did. Only `render_script` and `summarize` use
the container, and they need nothing beyond the Python standard library.

## Setup

### 1. Build the Apptainer container

```bash
cd ptycho-workflow
apptainer build Ptycho_Container.sif Apptainer/Ptycho_Container.def
```

The generator looks for `./Ptycho_Container.sif` by default. Use
`--container-image` to point it elsewhere, or `--no-container` to run the
Python steps with the workers' own `python3`.

### 2. Prepare the experiment folder

The folder layout is the same one `param_config_multislice.py` expects (the
Zaratan layout, with scan folders directly inside it; a `Data/` sub-folder also works):

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

- **Scan folders** are the sub-folders whose name starts with the
  acquisition time (`1302_...`). Each folder is matched to the sheet rows
  with the same *Acqu. Time*. If several rows share a time, they become
  `Scan1`, `Scan2`, ... as before.
- **Sheet layouts.** A 28-column sheet (the current single-slice layout, as
  in `Ptycho parameters template.xlsx`) and a 32-column multislice sheet (with
  `previous_object`, `regularization`, `thickness` and `Nlayers` after
  *Previous initial probe*) are both accepted. With a 28-column sheet in
  multislice mode, those four values come from `--previous-object`,
  `--regularization`, `--thickness` and `--nlayers`.

## Execution site: hosted site catalogs

The generator does not describe the execution site itself. It uses Pegasus'
[centrally hosted site catalogs](https://pegasus.isi.edu/docs/5.1.3/reference-guide/catalogs.html#centrally-hosted-site-catalogs):

- `--site-catalog-file` sets `pegasus.catalog.site.repo.file`. At plan time,
  the planner downloads that file from
  [pegasushub/pegasus-site-catalogs](https://github.com/pegasushub/pegasus-site-catalogs/tree/main/conf),
  from the branch matching its own major.minor version (e.g. `6.0`). It saves
  the file next to `workflow.yml` and downloads it again once it is more than
  a day old.
- Jobs are planned for the site named **`compute`**, the name every hosted
  catalog uses for its execution site.
- The `sites.yml` the generator writes describes only the submit host
  (`local`: `scratch/` and `output/`). Pegasus merges it over the downloaded
  catalog, and local entries win.

| Hosted file | Resource |
|-------------|----------|
| `access-pegasus.yml`, `access-pegasus-expanse.yml` | ACCESS Pegasus (HTCondor glideins) |
| `ospool-ap40.yml`, `ospool-ap41.yml` | OSPool access points |
| `usc-discovery.yml`, `usc-laguna.yml`, `unity.yml`, `purdue-anvil.yml` | SLURM clusters (direct submission) |
| `nersc-perlmutter.yml` | NERSC Perlmutter (SFAPI) |

**Job tags.** `ptycho_recon` jobs are tagged `gpu`, and `render_script` /
`summarize` jobs are tagged `cpu`. When the hosted entry has `x-tags`, the
planner adds that tag's profiles. With `usc-discovery.yml`, for example,
recon jobs go to the `gpu` queue and the Python steps stay on `main`.
Resources set in the transformation catalog take precedence over the site
catalog. Recon jobs always ask for 4 cores, 1 GPU, `--recon-memory` (40 GB)
and `--recon-runtime` (4 h). The runtime matters on SLURM catalogs, which
otherwise default to 30 minutes.

**Overriding the hosted entry.** Use `--site-profile NS.KEY=VALUE`
(repeatable). Each one becomes a
`pegasus.catalog.site.sites.compute.profiles.NS.KEY` property, e.g.
`--site-profile pegasus.project=myalloc` or `--site-profile pegasus.queue=gpu-a100`.
Several hosted catalogs read `${RESOURCE_PROJECT}` from the environment when
planning, so set that variable as well when your catalog uses it.

**fold_slice on batch clusters.** `--fold-slice-dir` ships fold_slice through
HTCondor file transfer, which suits HTCondor pools (access-pegasus, ospool).
On SLURM catalogs, the workers usually share a filesystem with your home
directory, so use `--fold-slice-worker-path` instead.

## Usage

### Generate the workflow

```bash
./workflow_generator.py \
    --data-dir "/scratch/zhangych/Data/20230805_WSe2_EMPAD/" \
    --site-catalog-file access-pegasus.yml \
    --fold-slice-dir /home/zhangych/fold_slice \
    --mode multislice \
    --output workflow.yml
```

On a SLURM cluster with a hosted catalog, with fold_slice already on the
shared filesystem:

```bash
export RESOURCE_PROJECT=myalloc
./workflow_generator.py \
    --data-dir "/scratch/zhangych/Data/20230805_WSe2_EMPAD/" \
    --site-catalog-file usc-discovery.yml \
    --fold-slice-worker-path /home/zhangych/fold_slice \
    --site-profile pegasus.project=myalloc \
    --output workflow.yml
```

To try it on the bundled sample dataset (which has no
`Background subtraction.mat`, and whose scan 0102 has no raw file):

```bash
./workflow_generator.py \
    --data-dir "../Sample dataset" \
    --site-catalog-file access-pegasus.yml \
    --fold-slice-dir /home/zhangych/fold_slice \
    --bg-file "/path/to/Background subtraction.mat" \
    --skip-missing \
    --output workflow.yml
```

The generator checks every scan before it writes anything. A missing raw file,
an unknown voltage / camera length / convergence angle combination, an empty
scan size, or a missing background file stops generation with a list of the
problems. `--skip-missing` drops those scans and continues.

### CLI options

| Option | Default | Description |
|--------|---------|-------------|
| `--data-dir` | (required) | Experiment folder |
| `--site-catalog-file` | (required) | Hosted site catalog file name, e.g. `access-pegasus.yml` |
| `--site-profile` | none | Override a profile of the hosted `compute` site, `NS.KEY=VALUE` (repeatable) |
| `--site-catalog-refresh` | 1 day | Re-download the hosted catalog when older than this many seconds |
| `--fold-slice-dir` | one of the two is required | fold_slice on the submit host, transferred to each recon job by HTCondor |
| `--fold-slice-worker-path` | | fold_slice already present on every worker (shared FS); nothing is transferred |
| `--mode` | `multislice` | `multislice` or `singleslice` template |
| `--template` | per `--mode` | Use a different MATLAB template |
| `--params-xlsx` | `<data-dir>/Ptycho parameters.xlsx` | Parameters sheet |
| `--rbf-xlsx` | `config/rbf.xlsx` | rbf calibration table |
| `--bg-file` | `<data-dir>/Background subtraction.mat` | Background file |
| `--scans` | all | Only these acquisition times, e.g. `--scans 1302 1305` |
| `--skip-missing` | off | Skip invalid scans instead of failing |
| `--regularization` / `--thickness` / `--nlayers` / `--previous-object` | `0.1` / `13` / `2` / `0` | Multislice values used when the sheet lacks those columns |
| `--matlab-module` | `matlab/2021b` | Module to load before MATLAB; `''` to skip |
| `--matlab-cmd` | `matlab` | MATLAB executable |
| `--recon-memory` | `40 GB` | Memory per recon job |
| `--recon-runtime` | `14400` | Walltime per recon job in seconds (batch sites) |
| `--gpu-requirements` | none | Extra HTCondor `requirements`, e.g. `'(GPUs_GlobalMemoryMb >= 40000)'` |
| `--container-image` | `./Ptycho_Container.sif` | Container for the Python steps |
| `--no-container` | off | Run the Python steps without a container |
| `--retry` | `1` | DAGMan retries per job |
| `--output-dir` | `./output` | Where final outputs are staged |
| `-e`, `--execution-site-name` | `compute` | Execution site name in the hosted catalog |
| `-s`, `--skip-sites-catalog` | off | Don't write the local `sites.yml` (Pegasus then uses its default `local` site) |
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
| `run_<scan>_Scan<n>.m` | The rendered MATLAB script, kept as a record of the exact parameters. |
| `<scan>_Scan<n>/<scan>_Scan<n>_recon.tar.gz` | The whole `Recon_result/` tree: `init_probe_*.mat`, `*_params.mat`, the `Reduced_run_*` / `Full_run_*` iterations, and the script with paths resolved. |
| `<scan>_Scan<n>/<scan>_Scan<n>_matlab.log` | MATLAB stdout/stderr (the old `Log/Data_<scan>.txt`). |
| `recon_summary.csv` | One row per scan: runtime, host, solver, Niter, layers, and `Full_run` files. |

The intermediate `data_roi*_dp.hdf5` that the template writes next to the raw
data stays on the worker. It is not staged back.

## How the template is adapted to Pegasus

Pegasus runs each job in its own scratch directory, so the hard-coded
`/home/...` paths in the templates cannot be used. `render_script` writes two
placeholders, and `ptycho_recon.py` resolves them on the worker:

| Template variable | Rendered as | Resolved on the worker to |
|-------------------|-------------|---------------------------|
| `exp_dir` | `__PEGASUS_JOB_DIR__/` | the job directory |
| `cSAXS_matlab_path`, `ptycho_matlab_path` | `__FOLD_SLICE_DIR__[/ptycho]` | the transferred (or worker-local) fold_slice |

Before starting MATLAB, the wrapper links the staged inputs to the paths the
template reads: `Data/<scan folder>/scan_xN_yN.raw`,
`Background subtraction.mat`, and `Recon_files/<scan folder>/init_recon.mat`.
Nothing else in the templates was changed. As in `param_config`, the
`defocus` value goes into the script exactly as written in the sheet.

## Testing locally

```bash
./run_manual.sh --data-dir "../Sample dataset" --scan 1302
```

This runs the spreadsheet parsing, `render_script` and `summarize` anywhere.
To also run the MATLAB reconstruction, run it on a GPU node with MATLAB and add
`--fold-slice /path/to/fold_slice --matlab-module matlab/2021b`.

## Resource Requirements

| Step | Memory | Cores | GPUs |
|------|--------|-------|------|
| render_script | 1 GB | 1 | 0 |
| ptycho_recon | 40 GB (`--recon-memory`) | 4 | 1 |
| summarize | 1 GB | 1 | 0 |

Each recon job receives one raw file (about 1 GB for a 128×128 scan) and, with
`--fold-slice-dir`, a copy of fold_slice. If the workers share a filesystem
with fold_slice on it, use `--fold-slice-worker-path` to skip that copy.

## Dependencies

- Submit host: Pegasus >= 5.1.3, HTCondor, Python 3 with `pandas`, `openpyxl`
- GPU workers: MATLAB (R2021b tested by the original scripts) with the Parallel Computing Toolbox, CUDA-capable GPU, `python3`
- Container (`python:3.12-slim`): Python standard library only
