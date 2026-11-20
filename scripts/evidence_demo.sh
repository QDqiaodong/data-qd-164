#!/usr/bin/env bash
# Reproducible fixture-evidence walkthrough.
#
# Runs one match at a time with only the Python standard library:
#   1. the hand-worked example: two recorders, offsets in [-1,1], one
#      alignment demanding equal offsets -> both files are needed, 9 bytes;
#   2. dropping the alignment exposes an uncovered offset combination;
#   3. a budget one byte short of the optimum is over_budget;
#   4. two windows sharing one bundle beat two per-window cheapest cuts;
#   5. contradictions in the observations are inconsistent_clock.
#
# Usage:  scripts/evidence_demo.sh [WORKDIR]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="${1:-$ROOT/run/evidence-demo}"
mkdir -p "$WORKDIR"
PYTHON="${PYTHON:-python3}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
run() { echo "+ fixture-evidence --source $1"; "$PYTHON" -m fixture_scheduler.evidence --source "$1" --output "$WORKDIR/$(basename "$1")"; echo; }

run "$ROOT/fixtures/evidence_hand_example.json"
run "$ROOT/fixtures/evidence_uncovered.json"
run "$ROOT/fixtures/evidence_over_budget.json"
run "$ROOT/fixtures/evidence_shared_window.json"
run "$ROOT/fixtures/evidence_interior_gap.json"
run "$ROOT/fixtures/evidence_inconsistent.json"

echo "Artifacts written under $WORKDIR"
