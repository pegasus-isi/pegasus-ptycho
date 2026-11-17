#!/bin/bash
#
# Manual test script for the ptycho workflow.
# Runs each pipeline step locally, the way Pegasus would, before submitting.
#
# Steps 0 and 2 need only python3 + pandas + openpyxl and run anywhere.
# Step 1 (the MATLAB reconstruction) needs a GPU node with MATLAB and
# fold_slice; it is skipped unless --fold-slice is given.
#
# Usage:
#   ./run_manual.sh --data-dir "/path/to/experiment/" [--mode multislice|singleslice]
#                   [--scan ACQU_TIME] [--fold-slice /path/to/fold_slice]
#                   [--matlab-module matlab/2021b] [--bg-file FILE]
#
# Example (no MATLAB run, on the bundled sample dataset):
#   ./run_manual.sh --data-dir "../Ptycho cluster script/Sample dataset" --scan 1302
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
            sed -n '10,13p' "$0"
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
mkdir -p "${OUTPUT_DIR}/params"

# ==============================================================
# Step 0: Spreadsheet -> per-scan parameter JSON + rendered MATLAB script
# (done by workflow_generator.py at generation time)
# ==============================================================
log_step "0. Read Ptycho parameters.xlsx and render MATLAB scripts"

( cd "${SCRIPT_DIR}" && python3 ptycho_params.py \
    --data-dir "${DATA_DIR}" --mode "${MODE}" --out-dir "${OUTPUT_DIR}/params" \
    --scripts-dir "${OUTPUT_DIR}/scripts" )

if [ -n "${SCAN}" ]; then
    PARAMS=$(ls "${OUTPUT_DIR}"/params/"${SCAN}"_*.json 2>/dev/null | head -1 || true)
else
    PARAMS=$(ls "${OUTPUT_DIR}"/params/*.json | head -1)
fi
if [ -z "${PARAMS}" ]; then
    log_error "No parameter set found${SCAN:+ for scan ${SCAN}}"
    exit 1
fi
check_file "${PARAMS}" "Parameters"

JOB_ID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['job_id'])" "${PARAMS}")
SCRIPT_NAME=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['script_name'])" "${PARAMS}")
RAW_PATH=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['raw_path'] or '')" "${PARAMS}")
log_info "Testing scan ${JOB_ID}"

check_file "${OUTPUT_DIR}/scripts/${SCRIPT_NAME}.m" "Rendered script"

# The job directory, as Pegasus would stage it for ptycho_recon
WORK="${OUTPUT_DIR}/${JOB_ID}"
mkdir -p "${WORK}"
cp "${OUTPUT_DIR}/scripts/${SCRIPT_NAME}.m" "${WORK}/"
cp "${PARAMS}" "${WORK}/params.json"

log_info "Rendered parameter block:"
sed -n '/^%% Step 1/,/^Niter =/p' "${WORK}/${SCRIPT_NAME}.m" | grep -v "^ *%" | head -40

# ==============================================================
# Step 1: ptycho_recon (GPU + MATLAB)
# ==============================================================
log_step "1. ptycho_recon"

if [ -z "${FOLD_SLICE}" ]; then
    log_warning "Skipping MATLAB reconstruction (pass --fold-slice on a GPU node with MATLAB)."
    log_info "Writing a stand-in summary so the summarize step can be tested."
    python3 - "${PARAMS}" "${WORK}/${JOB_ID}_summary.json" <<'EOF'
import json, sys
scan = json.load(open(sys.argv[1]))
json.dump({"job_id": scan["job_id"], "scan_folder": scan["scan_folder"],
           "acqu_time": scan["acqu_time"], "scan_number": scan["scan_number"],
           "mode": scan["mode"], "Niter": scan["replace_vars"]["Niter"],
           "GPU_solver": scan["replace_vars"]["GPU_solver"],
           "Nlayers": scan["replace_vars"].get("Nlayers", "1"),
           "runtime_s": 0, "n_result_files": 0, "full_run_files": [],
           "host": "manual-test (recon skipped)", "tarball": ""},
          open(sys.argv[2], "w"), indent=2)
EOF
else
    if [ -z "${RAW_PATH}" ]; then
        log_error "Raw file for ${JOB_ID} not found; cannot reconstruct"
        exit 1
    fi
    RECON_ARGS=(
        --script "${SCRIPT_NAME}.m"
        --params params.json
        --raw "${RAW_PATH}"
        --fold-slice "${FOLD_SLICE}"
        --output-tar "${JOB_ID}/${JOB_ID}_recon.tar.gz"
        --log "${JOB_ID}/${JOB_ID}_matlab.log"
        --summary "${JOB_ID}_summary.json"
    )
    [ -n "${MATLAB_MODULE}" ] && RECON_ARGS+=(--matlab-module "${MATLAB_MODULE}")
    if python3 -c "import json,sys; sys.exit(json.load(open(sys.argv[1]))['background_subtraction'] != 1)" "${PARAMS}"; then
        RECON_ARGS+=(--bg-file "${BG_FILE}")
    fi
    ( cd "${WORK}" && python3 "${SCRIPT_DIR}/bin/ptycho_recon.py" "${RECON_ARGS[@]}" )
    check_file "${WORK}/${JOB_ID}/${JOB_ID}_recon.tar.gz" "ptycho_recon"
fi
check_file "${WORK}/${JOB_ID}_summary.json" "Per-scan summary"

# ==============================================================
# Step 2: summarize
# ==============================================================
log_step "2. summarize"

python3 "${SCRIPT_DIR}/bin/summarize.py" \
    --input "${WORK}/${JOB_ID}_summary.json" \
    --output "${OUTPUT_DIR}/recon_summary.csv"

check_file "${OUTPUT_DIR}/recon_summary.csv" "summarize"
cat "${OUTPUT_DIR}/recon_summary.csv"

# ==============================================================
# Summary
# ==============================================================
echo ""
echo "=============================================="
echo "  TEST COMPLETED SUCCESSFULLY!"
echo "=============================================="
echo "Output files in: ${OUTPUT_DIR}"
ls -lhR "${OUTPUT_DIR}" | head -40
log_success "All steps passed! Ready to run with Pegasus."
