#!/bin/bash
#
# Manual test script for the ptycho workflow.
# Runs the pipeline locally, the way Pegasus would, before submitting.
#
# Step 0 (workflow generation + parameter files) needs python3 with the
# Pegasus Python API, pandas and openpyxl, and runs anywhere.
# Step 1 (the reconstruction) needs a GPU node with Apptainer, the compiled
# app (app/build_app.sh) and the MATLAB Runtime image (container/apptainer.def);
# it is skipped unless --image is given.
#
# Usage:
#   ./run_manual.sh --data-dir "/path/to/experiment/" [--mode multislice|singleslice]
#                   [--scan ACQU_TIME] [--image matlab-runtime-R2025b.sif]
#                   [--app app/build/ptycho_recon_app] [--bg-file FILE]
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
IMAGE=""
APP="${SCRIPT_DIR}/app/build/ptycho_recon_app"
BG_FILE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --data-dir)      DATA_DIR="$2"; shift 2 ;;
        --mode)          MODE="$2"; shift 2 ;;
        --scan)          SCAN="$2"; shift 2 ;;
        --image)         IMAGE="$2"; shift 2 ;;
        --app)           APP="$2"; shift 2 ;;
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
# Step 0: Generate the workflow (writes Params/<scan>.json)
# ==============================================================
log_step "0. workflow_generator.py"

# The generator only registers the app and the image; stand-ins when the
# reconstruction isn't run
GEN_APP="${APP}"
if [ ! -f "${GEN_APP}" ]; then
    if [ -n "${IMAGE}" ]; then
        log_error "No compiled app at ${APP} (build it with app/build_app.sh)"
        exit 1
    fi
    GEN_APP="${OUTPUT_DIR}/ptycho_recon_app"
    touch "${GEN_APP}"
fi
GEN_IMAGE="$(cd "$(dirname "${IMAGE:-/}")" && pwd)/$(basename "${IMAGE:-matlab-runtime.sif}")"

( cd "${OUTPUT_DIR}" && python3 "${SCRIPT_DIR}/workflow_generator.py" \
    --data-dir "${DATA_DIR}" --mode "${MODE}" \
    --site-catalog-file usc-discovery.yml --bg-file "${BG_FILE}" \
    --app "${GEN_APP}" --container-image "${GEN_IMAGE}" )

check_file "${OUTPUT_DIR}/workflow.yml" "Workflow generated"

if [ -n "${SCAN}" ]; then
    PARAMS=$(ls "${SCRIPT_DIR}"/Params/"${SCAN}"_*.json 2>/dev/null | head -1 || true)
else
    PARAMS=$(ls "${SCRIPT_DIR}"/Params/*.json 2>/dev/null | head -1 || true)
fi
if [ -z "${PARAMS}" ]; then
    log_error "No parameter file found${SCAN:+ for scan ${SCAN}}"
    exit 1
fi
check_file "${PARAMS}" "Parameter file"

log_info "Parameters:"
cat "${PARAMS}"

# ==============================================================
# Step 1: ptycho_recon (GPU + MATLAB)
# ==============================================================
log_step "1. ptycho_recon"

if [ -z "${IMAGE}" ]; then
    log_warning "Skipping the reconstruction (pass --image on a GPU node with Apptainer)."
else
    # The scan folder and raw file, as the generator found them
    JOB_ID=$(basename "${PARAMS}" .json)
    ACQU=${JOB_ID%%_*}
    SCAN_FOLDER=$(cd "${DATA_DIR}" && ls -d "${ACQU}"_*/ | head -1)
    SCAN_FOLDER=${SCAN_FOLDER%/}
    RAW=$(ls "${DATA_DIR}/${SCAN_FOLDER}"/scan_x*_y*.raw | head -1)

    WORK="${OUTPUT_DIR}/${JOB_ID}"
    mkdir -p "${WORK}"
    cp "${PARAMS}" "${APP}" "${WORK}/"
    # Place the raw file at its deep LFN, as Pegasus would. A copy: the
    # container sees only the job directory, where a link would dangle
    mkdir -p "${WORK}/Data/${SCAN_FOLDER}"
    cp "${RAW}" "${WORK}/Data/${SCAN_FOLDER}/"
    RECON_ARGS=(
        --app "$(basename "${APP}")"
        --params "$(basename "${PARAMS}")"
        --raw "Data/${SCAN_FOLDER}/$(basename "${RAW}")"
        --output-tar "${JOB_ID}/${JOB_ID}_recon.tar.gz"
        --log "${JOB_ID}/${JOB_ID}_matlab.log"
    )
    if [ -f "${BG_FILE}" ]; then
        cp "${BG_FILE}" "${WORK}/background_subtraction.mat"
        RECON_ARGS+=(--bg-file background_subtraction.mat)
    fi
    cp "${SCRIPT_DIR}/bin/ptycho_recon.py" "${WORK}/"
    # In the container, with only the job directory mounted at /srv, as PegasusLite does
    ( cd "${WORK}" && apptainer exec --no-home --nv --bind "${WORK}:/srv" --pwd /srv "${IMAGE}" \
        python3 ptycho_recon.py "${RECON_ARGS[@]}" )
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
