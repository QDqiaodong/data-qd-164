#!/usr/bin/env bash
# Reproducible fixture-calendar walkthrough.
#
# Runs the full synchronisation story with only the Python standard library:
#   1. init a season database from teams/venues
#   2. publish revision 1 (a feasible generator envelope)
#   3. two downstream calendars subscribe to venue V1 and pull packages
#   4. revision 2 moves one fixture V1 -> V2; one side re-subscribes to V2
#      while both old packages are still pending
#   5. late acks of the frozen old packages still work, then each side applies
#      its new delta (one removal, one upsert of the same directed key)
#   6. an infeasible repair envelope and a bad revision number are rejected
#      without advancing state; already_acked / unchanged are demonstrated
#
# Usage:  scripts/calendar_demo.sh [WORKDIR]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="${1:-$ROOT/run/calendar-demo}"
mkdir -p "$WORKDIR"
DB="$WORKDIR/season.db"
PYTHON="${PYTHON:-python3}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
run() { echo "+ fixture-calendar $*"; "$PYTHON" -m fixture_scheduler.calendar "$@"; echo; }
capture() { "$PYTHON" -m fixture_scheduler.calendar "$@"; }

cat > "$WORKDIR/season.json" <<'JSON'
{"teams": ["A", "B", "C", "D"], "venues": ["V1", "V2", "V3"]}
JSON

cat > "$WORKDIR/revision1.json" <<'JSON'
{
  "schema_version": 1,
  "source_sha256": "demo-revision-1",
  "result": {
    "round_count": 3,
    "fixture_count": 6,
    "fixtures": [
      {"round": 1, "home": "A", "away": "D", "date": "d1", "venue": "V1"},
      {"round": 1, "home": "C", "away": "B", "date": "d1", "venue": "V2"},
      {"round": 2, "home": "C", "away": "A", "date": "d2", "venue": "V1"},
      {"round": 2, "home": "D", "away": "B", "date": "d2", "venue": "V3"},
      {"round": 3, "home": "A", "away": "B", "date": "d3", "venue": "V1"},
      {"round": 3, "home": "D", "away": "C", "date": "d3", "venue": "V2"}
    ]
  }
}
JSON

cat > "$WORKDIR/revision2.json" <<'JSON'
{
  "schema_version": 1,
  "source_sha256": "demo-revision-2",
  "result": {
    "round_count": 3,
    "fixture_count": 6,
    "fixtures": [
      {"round": 1, "home": "A", "away": "D", "date": "d1", "venue": "V2"},
      {"round": 1, "home": "C", "away": "B", "date": "d1", "venue": "V3"},
      {"round": 2, "home": "C", "away": "A", "date": "d2", "venue": "V1"},
      {"round": 2, "home": "D", "away": "B", "date": "d2", "venue": "V3"},
      {"round": 3, "home": "A", "away": "B", "date": "d3", "venue": "V1"},
      {"round": 3, "home": "D", "away": "C", "date": "d3", "venue": "V2"}
    ]
  }
}
JSON

cat > "$WORKDIR/infeasible.json" <<'JSON'
{
  "schema_version": 1,
  "source_sha256": "demo-infeasible",
  "result": {
    "feasible": false,
    "objective": null,
    "assignment_vector": null,
    "fixtures": []
  }
}
JSON

printf '{"teams": [], "venues": ["V1"]}' > "$WORKDIR/scope-v1.json"
printf '{"teams": [], "venues": ["V2"]}' > "$WORKDIR/scope-v2.json"
printf '{"teams": [], "venues": []}'      > "$WORKDIR/scope-empty.json"

run init      --db "$DB" --source "$WORKDIR/season.json"
run publish   --db "$DB" --revision 1 --source "$WORKDIR/revision1.json"
run subscribe --db "$DB" --subscriber team-site  --source "$WORKDIR/scope-v1.json"
run subscribe --db "$DB" --subscriber venue-board --source "$WORKDIR/scope-v1.json"

PKG_TEAM=$(capture prepare --db "$DB" --subscriber team-site | tee "$WORKDIR/pkg-team-1.json" | "$PYTHON" -c 'import json,sys;print(json.load(sys.stdin)["package_id"])')
PKG_BOARD=$(capture prepare --db "$DB" --subscriber venue-board | tee "$WORKDIR/pkg-board-1.json" | "$PYTHON" -c 'import json,sys;print(json.load(sys.stdin)["package_id"])')
echo "+ prepared packages: $PKG_TEAM / $PKG_BOARD"
echo

run publish   --db "$DB" --revision 2 --source "$WORKDIR/revision2.json"
run subscribe --db "$DB" --subscriber venue-board --source "$WORKDIR/scope-v2.json"

echo "# Re-fetching after publish + rescope returns the same frozen package:"
run prepare --db "$DB" --subscriber team-site
run prepare --db "$DB" --subscriber venue-board

echo "# Late acknowledgements of the old packages still succeed:"
run ack --db "$DB" --subscriber team-site  --package "$PKG_TEAM"
run ack --db "$DB" --subscriber venue-board --package "$PKG_BOARD"

echo "# New deltas: team-site gets a removal, venue-board gets an upsert on the same key:"
TEAM2=$(capture prepare --db "$DB" --subscriber team-site  | tee "$WORKDIR/pkg-team-2.json" | "$PYTHON" -c 'import json,sys;print(json.load(sys.stdin)["package_id"])')
BOARD2=$(capture prepare --db "$DB" --subscriber venue-board | tee "$WORKDIR/pkg-board-2.json" | "$PYTHON" -c 'import json,sys;print(json.load(sys.stdin)["package_id"])')
cat "$WORKDIR/pkg-team-2.json"; echo
cat "$WORKDIR/pkg-board-2.json"; echo

echo "# Re-acking an old id while a new package is pending is already_acked:"
run ack --db "$DB" --subscriber team-site --package "$PKG_TEAM" || true

echo "# Acknowledge the new packages, then an empty scope sends removals only:"
run ack --db "$DB" --subscriber team-site  --package "$TEAM2"
run ack --db "$DB" --subscriber venue-board --package "$BOARD2"
run subscribe --db "$DB" --subscriber team-site --source "$WORKDIR/scope-empty.json"
run prepare   --db "$DB" --subscriber team-site

echo "# Rejected inputs never advance revision or pending state:"
run publish --db "$DB" --revision 9 --source "$WORKDIR/revision2.json" || true
run publish --db "$DB" --revision 3 --source "$WORKDIR/infeasible.json" || true

echo "Artifacts written under $WORKDIR"
