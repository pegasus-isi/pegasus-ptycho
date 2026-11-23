#!/bin/bash
#
# Compile the ptycho reconstruction into a standalone app (MATLAB Compiler),
# run by the workflow's jobs in the MATLAB Runtime container
# (container/apptainer.def).
#
# Needs MATLAB with the MATLAB Compiler, of the release of the Runtime in the
# container (R2025b for matlab-runtime-R2025b.sif), and python3. On USC CARC,
# on a compute node (MATLAB crashes on the login nodes):
#
#   salloc --partition=gpu --gres=gpu:1 --cpus-per-task=4 --mem=32G --time=1:00:00
#   module load matlab/2025b
#   app/build_app.sh /home1/vahi/pegasus-ptycho/software/fold_slice
#
# fold_slice's GPU MEX files must already be built for this release (mexcuda),
# as they are copied into the app as they are.
#
# Output: app/build/ptycho_recon_app, passed to workflow_generator.py with --app

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 /path/to/fold_slice"
    exit 1
fi
FS="$(cd "$1" && pwd)"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="${APP_DIR}/build"

rm -rf "${BUILD}"
mkdir -p "${BUILD}/src"

# The templates as scripts that read the per-scan values from the parameter file
python3 "${APP_DIR}/../ptycho_params.py" --app-sources "${BUILD}/src"

# fold_slice calls engines, detectors and scan-position functions by name,
# which mcc's dependency analysis cannot follow: add those packages whole.
# -I: where mcc finds the rest (the template's addpath list).
mcc -m "${APP_DIR}/ptycho_recon_app.m" \
    -a "${BUILD}/src/ptycho_recon_multislice.m" \
    -a "${BUILD}/src/ptycho_recon_singleslice.m" \
    -o ptycho_recon_app -d "${BUILD}" -R -nodisplay \
    -I "${FS}" -I "${FS}/ptycho" -I "${FS}/ptycho/utils" -I "${FS}/ptycho/utils_electron" \
    -a "${FS}/ptycho/+core" -a "${FS}/ptycho/+engines" \
    -a "${FS}/ptycho/+detector" -a "${FS}/ptycho/+scans"

echo ""
echo "Built ${BUILD}/ptycho_recon_app"
echo "Runtime components (requiredMCRProducts.txt): $(cat "${BUILD}/requiredMCRProducts.txt")"
echo "Check ${BUILD}/mccExcludedFiles.log for fold_slice files mcc left out."
