#!/usr/bin/env python3

"""
The per-scan parameters of the MATLAB templates, shared by the workflow
generator and the build of the compiled app.

param_config_multislice.py wrote each scan's values into a copy of the
template (`<var> = <value>;`). A compiled app cannot run such a copy, so:

  * workflow_generator.py writes the values into a parameter file per scan,
    Params/<scan>.json, read with write_params()
  * app/build_app.sh compiles the templates once, after make_app_source() has
    turned each `<var> = <value>;` line into `<var> = P.<var>;`, where P holds
    the parameter file the app was started with (app/ptycho_recon_app.m)

Only needs the standard library, so it runs on the build machine as is:

    python3 ptycho_params.py --app-sources app/build/src
"""

import argparse
import json
import math
import os
import re

TEMPLATES = {
    "multislice": "templates/Ptychography_reconstruction_multislice_template.m",
    "singleslice": "templates/Ptychography_reconstruction_singleslice_template.m",
}

# Sheet columns only a 32-column sheet has, after initial_probe
MULTISLICE_HEADER = ["previous_object", "regularization", "thickness", "Nlayers"]

# The template variables set per scan (param_config's list). camera_len and
# mag have no line in the current templates; they are written all the same.
REPLACE_VARS = [
    "rot_ang", "camera_len", "dScanX", "dScanY", "exp_dir",
    "data_dir", "N_scan_x", "N_scan_y", "voltage", "ADU", "alpha0",
    "rbf", "scan_step_size", "data_descriptor", "mag", "scan_number",
    "resample_factor", "defocus", "background_subtraction", "Niter",
    "Np_crop_pad", "Nprobe", "variable_probe_modes", "GPU_solver",
    "errmetric", "initial_probe", "cSAXS_matlab_path", "ptycho_matlab_path",
]

# Set by the app in the job directory, not by the parameter file
JOB_VARS = ["exp_dir", "cSAXS_matlab_path", "ptycho_matlab_path"]


def template_vars(mode):
    return REPLACE_VARS + (MULTISLICE_HEADER if mode == "multislice" else [])


# ----------------------------------------------------------------------
# Parameter file (workflow generator)
# ----------------------------------------------------------------------
def _json_value(value):
    """numpy scalars from pandas to Python; an empty cell (NaN) to null."""
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def write_params(path, mode, values):
    """Write the scan's values for the template variables, plus the mode."""
    params = {"mode": mode}
    for var in template_vars(mode):
        if var not in JOB_VARS:
            params[var] = _json_value(values[var])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(params, f, indent=2)
        f.write("\n")


# ----------------------------------------------------------------------
# App sources (build of the compiled app)
# ----------------------------------------------------------------------
def _from_params(var, rhs):
    """The expression reading `var` from P, of the type the template gave it."""
    rhs = rhs.lstrip()
    if rhs.startswith('"'):
        return f"string(P.{var})"
    if rhs.startswith("'"):
        return f"char(P.{var})"
    return f"P.{var}"


def make_app_source(template_str, mode, name):
    """The template as a script the app runs, reading the per-scan values from P."""
    src = template_str
    for var in template_vars(mode):
        src = re.sub(r"^({}\s*=\s*)([^;\n]*);".format(re.escape(var)),
                     lambda m, var=var: f"{m.group(1)}{_from_params(var, m.group(2))};"
                                        f" % from the parameter file",
                     src, flags=re.M)
    # A compiled app contains its code: adding fold_slice to the path would
    # at best do nothing
    src = re.sub(r"^(addpath\(.*\);)", r"if ~isdeployed, \1 end", src, flags=re.M)
    # The script runs inside ptycho_recon_app: clear would remove P
    src = re.sub(r"^clear;\s*clc;", "% clear;clc; (removed: would clear the parameters P)",
                 src, flags=re.M)
    header = (f"% {name}.m - generated from {TEMPLATES[mode]} by ptycho_params.py\n"
              f"% for the compiled app (app/ptycho_recon_app.m); do not edit\n\n")
    return header + src


def write_app_sources(out_dir, base_dir):
    os.makedirs(out_dir, exist_ok=True)
    for mode, template in TEMPLATES.items():
        name = f"ptycho_recon_{mode}"
        with open(os.path.join(base_dir, template), encoding="utf-8") as f:
            src = make_app_source(f.read(), mode, name)
        missing = [v for v in template_vars(mode)
                   if v not in ("camera_len", "mag")
                   and not re.search(r"^{}\s*=\s*{}".format(re.escape(v), "(string|char)?\\(?P\\."),
                                     src, flags=re.M)]
        if missing:
            raise SystemExit(f"{template}: no `<var> = ...;` line for {', '.join(missing)}")
        path = os.path.join(out_dir, f"{name}.m")
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        print(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app-sources", metavar="DIR", required=True,
                        help="Write ptycho_recon_<mode>.m for the compiled app into DIR")
    args = parser.parse_args()
    write_app_sources(args.app_sources, os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    main()
