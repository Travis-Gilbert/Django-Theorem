#!/bin/sh
# Keep the R runtime preflight ahead of queue consumption. Fly invokes this
# script as one process command, avoiding shell-tokenization differences in
# fly.toml process definitions.
set -eu

python -m apps.orchestration.r_runtime --check
exec celery -A theorem_control worker -l info -Q offload.r
