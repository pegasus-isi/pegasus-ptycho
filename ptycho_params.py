#!/usr/bin/env python3

"""
Turn "Ptycho parameters.xlsx" + "rbf.xlsx" into one parameter set per scan.

This is the spreadsheet logic of param_config_multislice.py, separated from
the MATLAB script generation so that:

  * workflow_generator.py can import it to decide which scans become jobs, and
  * it can be run on its own (see run_manual.sh) to dump the per-scan JSON
    files that bin/render_script.py consumes.

Each scan yields a dict:

    {
      "job_id":       "1302_14pt5Mx_CL230mm_df10nm_25mrad_scan128_15pA_Scan1",
      "script_name":  "run_1302_14pt5Mx_CL230mm_df10nm_25mrad_scan128_15pA_Scan1",
      "scan_folder":  "1302_14.5Mx_CL230mm_df10nm_25mrad_scan128_15pA",
      "acqu_time":    1302,
      "scan_number":  1,
      "raw_name":     "scan_x128_y128.raw",
      "raw_path":     "/abs/path/to/scan_x128_y128.raw",   (None if missing)
      "background_subtraction": 1,
      "initial_probe": 0,
      "init_recon_path": None,
      "replace_vars": {"rot_ang": "0", "camera_len": "230", ...}
    }

Usage (stand-alone):
    ./ptycho_params.py --data-dir "/path/to/experiment/" --mode multislice \
        --out-dir params/
"""

import argparse
import json
import logging
import os
import re
import sys

logger = logging.getLogger(__name__)

# Placeholders the render step writes into the MATLAB script. They are
# resolved by bin/ptycho_recon.py on the worker, where the job directory and
# the fold_slice location are finally known.
JOB_DIR_TOKEN = "__PEGASUS_JOB_DIR__"
FOLD_SLICE_TOKEN = "__FOLD_SLICE_DIR__"

# First 26 columns are shared by every sheet layout seen so far.
COMMON_COLS = [
    "Date", "Sample", "ROI", "Tool", "Voltage", "Acqu_time",
    "Additional_identifider", "Conv_angle", "Camera_len", "Mag", "Defocus",
    "Screen_current", "Scan_rot", "Scan_size", "Scale",
    "Image_shear", "Resample_factor", "dScanXY", "Diff_pattern_size",
    "background_subtraction", "Niter", "Probe_states", "Probe_modes",
    "GPU_solver", "errmetric", "initial_probe",
]
MULTISLICE_COLS = ["previous_object", "regularization", "thickness", "Nlayers"]
TRAILING_COLS = ["Result", "Notes"]

# 28 columns: single-slice sheet (also what "Ptycho parameters template.xlsx" uses)
# 32 columns: multislice sheet, as expected by param_config_multislice.py
SHEET_LAYOUTS = {
    len(COMMON_COLS) + len(TRAILING_COLS): COMMON_COLS + TRAILING_COLS,
    len(COMMON_COLS) + len(MULTISLICE_COLS) + len(TRAILING_COLS):
        COMMON_COLS + MULTISLICE_COLS + TRAILING_COLS,
}

RBF_COLS = ["voltage", "camera_len", "conv_angle", "rbf"]

COMMON_VARS = [
    "rot_ang", "camera_len", "dScanX", "dScanY", "exp_dir",
    "data_dir", "N_scan_x", "N_scan_y", "voltage", "ADU", "alpha0",
    "rbf", "scan_step_size", "data_descriptor", "mag", "scan_number",
    "resample_factor", "defocus", "background_subtraction", "Niter",
    "Np_crop_pad", "Nprobe", "variable_probe_modes", "GPU_solver",
    "errmetric", "initial_probe",
    "cSAXS_matlab_path", "ptycho_matlab_path",
]
REPLACE_VARS = {
    "singleslice": COMMON_VARS,
    "multislice": COMMON_VARS + MULTISLICE_COLS,
}

ADU_BY_VOLTAGE = {80: 151, 200: 393, 300: 591}
ALPHA0_BY_CONV_ANGLE = {18: 18.44, 25.2: 24.92, 36: 36.4, 54: 55.45}
# Field of view (angstrom) of a 128-step scan at each UI magnification (Mx)
FOV128_BY_MAG = {
    5.1: 1.182, 7.2: 0.839, 10: 0.605, 14.5: 0.429,
    20.5: 0.297, 29: 0.208, 41: 0.147,
}


class ScanError(ValueError):
    """A spreadsheet row that cannot be turned into a reconstruction."""


def _lookup(table, key, what):
    for k, v in table.items():
        if abs(float(k) - float(key)) < 1e-6:
            return v
    raise ScanError(f"{what}({key}) not found; known values: {sorted(table)}")


def _num(value):
    """Spreadsheet number -> int when integral, so MATLAB sees 128 not 128.0."""
    f = float(value)
    return int(f) if f.is_integer() else f


def _is_blank(value):
    return value is None or (isinstance(value, float) and value != value)


def format_scan_folder(scan_folder):
    """Same renaming as param_config: MATLAB script names cannot hold + - ."""
    name = re.sub(r"\+", "_plus", scan_folder)
    name = re.sub("-", "_minus", name)
    name = re.sub(r"\.", "pt", name)
    # Anything else that is not a MATLAB/Pegasus-safe identifier character
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def read_params_sheet(param_xlsx):
    import pandas as pd

    df = pd.read_excel(param_xlsx)
    names = SHEET_LAYOUTS.get(df.shape[1])
    if names is None:
        raise ValueError(
            f"{param_xlsx} has {df.shape[1]} columns; expected one of "
            f"{sorted(SHEET_LAYOUTS)} (single-slice or multislice layout)"
        )
    df.columns = names
    return df


def read_rbf_sheet(rbf_xlsx):
    import pandas as pd

    return pd.read_excel(rbf_xlsx, names=RBF_COLS)


def get_rbf(rbf_df, voltage, camera_len, conv_angle):
    rbfs = rbf_df.loc[
        (rbf_df["voltage"] == voltage)
        & (rbf_df["camera_len"] == camera_len)
        & (abs(rbf_df["conv_angle"] - conv_angle) < 1e-6)
    ]
    combo = f"voltage({voltage}), camera_len({camera_len}), conv_angle({conv_angle})"
    if rbfs.shape[0] == 0:
        raise ScanError(f"{combo} combination not found in rbf table")
    if rbfs.shape[0] > 1:
        raise ScanError(f"Multiple {combo} combinations found in rbf table")
    return _num(rbfs.iloc[0]["rbf"])


def find_scan_folders(data_dir):
    """Scan folders are the sub-folders whose name starts with the Acqu. Time."""
    folders = []
    for parent in (data_dir, os.path.join(data_dir, "Data")):
        if not os.path.isdir(parent):
            continue
        for name in sorted(next(os.walk(parent))[1]):
            if name.split("_")[0].isdigit():
                folders.append((name, os.path.join(parent, name)))
    return folders


def build_scan_params(row, scan_folder, scan_path, scan_number, rbf_df, mode,
                      data_dir, defaults):
    """One spreadsheet row -> one parameter dict (raises ScanError)."""
    if _is_blank(row["Scan_size"]):
        raise ScanError("Scan size is empty")

    N_scan = int(_num(row["Scan_size"]))
    voltage = _num(row["Voltage"])
    camera_len = _num(row["Camera_len"])
    conv_angle = _num(row["Conv_angle"])
    mag = _num(row["Mag"])
    diff_size = int(_num(row["Diff_pattern_size"]))

    sample = "" if _is_blank(row["Sample"]) else str(row["Sample"])
    roi = "" if _is_blank(row["ROI"]) else str(row["ROI"])

    values = {
        "rot_ang": _num(row["Scan_rot"]),
        "camera_len": camera_len,
        "dScanX": _num(row["dScanXY"]),
        "dScanY": _num(row["dScanXY"]),
        "exp_dir": f"{JOB_DIR_TOKEN}/",
        "data_dir": scan_folder,
        "N_scan_x": N_scan,
        "N_scan_y": N_scan,
        "voltage": voltage,
        "ADU": _lookup(ADU_BY_VOLTAGE, voltage, "Voltage"),
        "alpha0": _lookup(ALPHA0_BY_CONV_ANGLE, conv_angle, "Conv_angle"),
        "rbf": get_rbf(rbf_df, voltage, camera_len, conv_angle),
        "scan_step_size": _lookup(FOV128_BY_MAG, mag, "mag") / N_scan * 128,
        "data_descriptor": re.sub(r"[\W]+", "_", sample) + "_"
        + re.sub(r"[\W]+", "_", roi),
        "mag": mag,
        "scan_number": scan_number,
        "resample_factor": _num(row["Resample_factor"]),
        "defocus": _num(row["Defocus"]),
        "background_subtraction": int(_num(row["background_subtraction"])),
        "Niter": int(_num(row["Niter"])),
        "Np_crop_pad": f"{diff_size},{diff_size}",
        "Nprobe": int(_num(row["Probe_states"])),
        "variable_probe_modes": int(_num(row["Probe_modes"])),
        "GPU_solver": row["GPU_solver"],
        "errmetric": row["errmetric"],
        "initial_probe": int(_num(row["initial_probe"])),
        "cSAXS_matlab_path": FOLD_SLICE_TOKEN,
        "ptycho_matlab_path": f"{FOLD_SLICE_TOKEN}/ptycho",
    }

    if mode == "multislice":
        for col in MULTISLICE_COLS:
            if col in row.index and not _is_blank(row[col]):
                values[col] = _num(row[col])
            else:
                values[col] = defaults[col]

    formatted = format_scan_folder(scan_folder)
    job_id = f"{formatted}_Scan{scan_number}"

    raw_name = f"scan_x{N_scan}_y{N_scan}.raw"
    raw_path = os.path.join(scan_path, raw_name)

    init_recon_path = None
    if values["initial_probe"] == 1:
        # Template: dir(<exp_dir>/Recon_files/<data_dir>/*.mat), one file expected
        recon_dir = os.path.join(data_dir, "Recon_files", scan_folder)
        mats = sorted(
            f for f in (os.listdir(recon_dir) if os.path.isdir(recon_dir) else [])
            if f.endswith(".mat")
        )
        if len(mats) != 1:
            raise ScanError(
                f"initial_probe=1 needs exactly one .mat in {recon_dir}, "
                f"found {len(mats)}"
            )
        init_recon_path = os.path.join(recon_dir, mats[0])

    return {
        "job_id": job_id,
        "script_name": f"run_{job_id}",
        "mode": mode,
        "scan_folder": scan_folder,
        "acqu_time": int(scan_folder.split("_")[0]),
        "scan_number": scan_number,
        "raw_name": raw_name,
        "raw_path": raw_path if os.path.isfile(raw_path) else None,
        "background_subtraction": values["background_subtraction"],
        "initial_probe": values["initial_probe"],
        "init_recon_path": init_recon_path,
        "replace_vars": {
            k: str(values[k]) for k in REPLACE_VARS[mode]
        },
    }


def collect_scans(data_dir, param_xlsx, rbf_xlsx, mode, defaults,
                  acqu_times=None):
    """Return (scans, problems): valid scan dicts and per-scan error strings."""
    param_df = read_params_sheet(param_xlsx)
    rbf_df = read_rbf_sheet(rbf_xlsx)

    scans, problems = [], []
    for scan_folder, scan_path in find_scan_folders(data_dir):
        acqu_time = int(scan_folder.split("_")[0])
        if acqu_times and acqu_time not in acqu_times:
            continue

        rows = param_df.loc[param_df["Acqu_time"] == acqu_time].reset_index()
        if rows.shape[0] == 0:
            problems.append(
                f"{scan_folder}: no row in parameters sheet for Acqu. Time {acqu_time}"
            )
            continue

        for index, row in rows.iterrows():
            scan_number = index + 1
            try:
                scans.append(
                    build_scan_params(row, scan_folder, scan_path, scan_number,
                                      rbf_df, mode, data_dir, defaults)
                )
            except ScanError as e:
                problems.append(f"{scan_folder} Scan{scan_number}: {e}")
    return scans, problems


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", required=True,
                        help="Experiment folder holding the scan folders")
    parser.add_argument("--params-xlsx", default=None,
                        help='Default: <data-dir>/"Ptycho parameters.xlsx"')
    parser.add_argument("--rbf-xlsx", default=os.path.join(os.getcwd(), "config", "rbf.xlsx"),
                        help="rbf calibration table (default: ./config/rbf.xlsx)")
    parser.add_argument("--mode", choices=["multislice", "singleslice"],
                        default="multislice")
    parser.add_argument("--out-dir", required=True,
                        help="Write one <job_id>.json per scan here")
    parser.add_argument("--regularization", type=float, default=0.1)
    parser.add_argument("--thickness", type=float, default=13)
    parser.add_argument("--nlayers", type=int, default=2)
    parser.add_argument("--previous-object", type=int, default=0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    params_xlsx = args.params_xlsx or os.path.join(args.data_dir, "Ptycho parameters.xlsx")
    defaults = {
        "regularization": args.regularization,
        "thickness": args.thickness,
        "Nlayers": args.nlayers,
        "previous_object": args.previous_object,
    }
    scans, problems = collect_scans(args.data_dir, params_xlsx, args.rbf_xlsx,
                                    args.mode, defaults)
    for p in problems:
        logger.warning(p)

    os.makedirs(args.out_dir, exist_ok=True)
    for scan in scans:
        path = os.path.join(args.out_dir, f"{scan['job_id']}.json")
        with open(path, "w") as f:
            json.dump(scan, f, indent=2)
        logger.info(f"Wrote {path} (raw file {'found' if scan['raw_path'] else 'MISSING'})")
    logger.info(f"{len(scans)} scan parameter set(s) written to {args.out_dir}")
    sys.exit(0 if scans else 1)


if __name__ == "__main__":
    main()
