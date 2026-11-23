#!/bin/bash

set -e

apptainer build --build-arg RELEASE=R2025b --build-arg UPDATE=3  matlab-runtime-R2025b.sif apptainer.def
