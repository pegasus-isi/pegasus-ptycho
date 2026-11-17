#!/bin/bash
#
# Manual test script for the ptycho workflow.
# Runs the pipeline locally, the way Pegasus would, before submitting.
#
# Step 0 (workflow generation + MATLAB script rendering) needs python3 with
# the Pegasus Python API, pandas and openpyxl, and runs anywhere.
# Step 1 (the MATLAB reconstruction) needs a GPU node with MATLAB and
# fold_slice; it is skipped unless --fold-slice is given.
#
# Usage:
#   ./run_manual.sh --data-dir "/path/to/experiment/" [--mode multislice|singleslice]
#                   [--scan ACQU_TIME] [--fold-slice /path/to/fold_slice]
#                   [--matlab-module matlab/2021b] [--bg-file FILE]
#
# Example (no MATLAB run):
#   ./run_manual.sh --data-dir "/scratch/zhangych/Data/20230805_WSe2_EMPAD/" --scan 1302
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${SCRIPT_DIR}/test_output"

DATA_DIR=""
MODE="multislice"
SCAN=""
FOLD_SLICE=""
MATLAB_MODULE=""
BG_FILE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --data-dir)      DATA_DIR="$2"; shift 2 ;;
        --mode)          MODE="$2"; shift 2 ;;
        --scan)          SCAN="$2"; shift 2 ;;
        --fold-slice)    FOLD_SLICE="$2"; shift 2 ;;
        --matlab-module) MATLAB_MODULE="$2"; shift 2 ;;
        --bg-file)       BG_FILE="$2"; shift 2 ;;
        *)
            echo "Unknown argument: $1"
            sed -n '11,14p' "$0"
            exit 1
            ;;
    esac
done

if [ -z "${DATA_DIR}" ]; then
    echo "Error: --data-dir is required"
    exit 1
fi
DATA_DIR="$(cd "${DATA_DIR}" && pwd)"
BG_FILE="${BG_FILE:-${DATA_DIR}/Background subtraction.mat}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()    { echo ""; echo -e "${GREEN}========================================${NC}"; echo -e "${GREEN}STEP: $1${NC}"; echo -e "${GREEN}========================================${NC}"; }

check_file() {
    if [ -s "$1" ]; then
        log_success "$2"
        ls -lh "$1"
    else
        log_error "$2 failed: $1 missing or empty"
        exit 1
    fi
}

echo ""
echo "=============================================="
echo "  Ptycho Workflow Manual Test"
echo "=============================================="
echo "  Data dir: ${DATA_DIR}"
echo "  Mode:     ${MODE}"
echo "  Output:   ${OUTPUT_DIR}"
echo ""

rm -rf "${OUTPUT_DIR}"
mkdir -p "${OUTPUT_DIR}"

# ==============================================================
# Step 0: Generate the workflow (renders Scripts/run_<scan>.m)
# ==============================================================
log_step "0. workflow_generator.py"

# Written into the MATLAB scripts; a placeholder when MATLAB isn't run
FS_DIR="${FOLD_SLICE:-/path/to/fold_slice}"

( cd "${OUTPUT_DIR}" && python3 "${SCRIPT_DIR}/workflow_generator.py" \
    --data-dir "${DATA_DIR}" --mode "${MODE}" \
    --site-catalog-file access-pegasus.yml --bg-file "${BG_FILE}" --fold-slice-dir "${FS_DIR}" )

check_file "${OUTPUT_DIR}/workflow.yml" "Workflow generated"

if [ -n "${SCAN}" ]; then
    SCRIPT=$(ls "${SCRIPT_DIR}"/Scripts/run_"${SCAN}"_*.m 2>/dev/null | head -1 || true)
else
    SCRIPT=$(ls "${SCRIPT_DIR}"/Scripts/run_*.m 2>/dev/null | head -1 || true)
fi
if [ -z "${SCRIPT}" ]; then
    log_error "No rendered script found${SCAN:+ for scan ${SCAN}}"
    exit 1
fi
check_file "${SCRIPT}" "Rendered script"

log_info "Rendered parameter block:"
sed -n '/^%% Step 1/,/^Niter =/p' "${SCRIPT}" | grep -v "^ *%" | head -40

# ==============================================================
# Step 1: ptycho_recon (GPU + MATLAB)
# ==============================================================
log_step "1. ptycho_recon"

if [ -z "${FOLD_SLICE}" ]; then
    log_warning "Skipping MATLAB reconstruction (pass --fold-slice on a GPU node with MATLAB)."
else
    # The scan folder and raw file, as the generator found them
    SCRIPT_BASE=$(basename "${SCRIPT}" .m)
    JOB_ID=${SCRIPT_BASE#run_}
    ACQU=${JOB_ID%%_*}
    SCAN_FOLDER=$(cd "${DATA_DIR}" && ls -d "${ACQU}"_*/ | head -1)
    SCAN_FOLDER=${SCAN_FOLDER%/}
    RAW=$(ls "${DATA_DIR}/${SCAN_FOLDER}"/scan_x*_y*.raw | head -1)

    WORK="${OUTPUT_DIR}/${JOB_ID}"
    mkdir -p "${WORK}"
    cp "${SCRIPT}" "${WORK}/"
    # Place the raw file at its deep LFN, as Pegasus would
    mkdir -p "${WORK}/Data/${SCAN_FOLDER}"
    ln -sf "${RAW}" "${WORK}/Data/${SCAN_FOLDER}/$(basename "${RAW}")"
    RECON_ARGS=(
        --script "$(basename "${SCRIPT}")"
        --raw "Data/${SCAN_FOLDER}/$(basename "${RAW}")"
        --matlab-module "${MATLAB_MODULE}"
        --output-tar "${JOB_ID}/${JOB_ID}_recon.tar.gz"
        --log "${JOB_ID}/${JOB_ID}_matlab.log"
    )
    if [ -f "${BG_FILE}" ]; then
        RECON_ARGS+=(--bg-file "${BG_FILE}")
    fi
    ( cd "${WORK}" && python3 "${SCRIPT_DIR}/bin/ptycho_recon.py" "${RECON_ARGS[@]}" )
    check_file "${WORK}/${JOB_ID}/${JOB_ID}_recon.tar.gz" "ptycho_recon"
fi

# ==============================================================
# Summary
# ==============================================================
echo ""
echo "=============================================="
echo "  TEST COMPLETED SUCCESSFULLY!"
echo "=============================================="
echo "Output files in: ${OUTPUT_DIR}"
ls -lh "${OUTPUT_DIR}"
log_success "All steps passed! Ready to run with Pegasus."
