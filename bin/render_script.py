#!/usr/bin/env python3

"""Render the per-scan MATLAB reconstruction script from the template.

This is Step 3 of param_config_multislice.py ("replace variables"), run as a
Pegasus job. The scan's values come from a JSON file produced by
ptycho_params.py at workflow-generation time. Paths that are only known on the
GPU worker (job directory, fold_slice location) are left as placeholders and
resolved by ptycho_recon.py.
"""

import argparse
import json
import logging
import os
import re
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

REQUIRED_VARS = {
    "exp_dir", "data_dir", "scan_number", "N_scan_x", "N_scan_y",
    "cSAXS_matlab_path", "ptycho_matlab_path",
}


def replace_single_var(var_name, var_value, string):
    """Same regex as param_config: replace the value of `\\n<var> = ...`."""
    pattern = r"(\n{}( )*=( )*['|\"|\[]?)[^\n|^;|^'|^\"|^\]]*".format(re.escape(var_name))
    # Function replacement so backslashes/group refs in values are literal
    return re.subn(pattern, lambda m: m.group(1) + var_value, string)


def main():
    parser = argparse.ArgumentParser(description="Render a ptycho MATLAB script")
    parser.add_argument("--template", required=True, help="MATLAB template (.m)")
    parser.add_argument("--params", required=True, help="Per-scan parameter JSON")
    parser.add_argument("--output", required=True, help="Rendered script (.m)")
    args = parser.parse_args()

    logger.info(f"Template: {args.template}")
    logger.info(f"Params:   {args.params}")

    with open(args.params) as f:
        scan = json.load(f)
    with open(args.template, "r", encoding="utf-8") as f:
        script = f.read()

    missing = []
    for var_name, var_value in scan["replace_vars"].items():
        script, n = replace_single_var(var_name, var_value, script)
        if n == 0:
            missing.append(var_name)
        logger.info(f"  {var_name} = {var_value}")

    # Like param_config, variables absent from the template are ignored
    # (camera_len and mag are not used by either template) -- except the ones
    # the workflow relies on to relocate data and code into the job directory.
    if missing:
        logger.warning(f"Variables not in template (ignored): {', '.join(missing)}")
    required_missing = sorted(set(missing) & REQUIRED_VARS)
    if required_missing:
        logger.error(f"Template lacks required variables: {', '.join(required_missing)}")
        sys.exit(1)

    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(script)

    logger.info(f"Output: {args.output}")


if __name__ == "__main__":
    main()
