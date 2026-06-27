#!/usr/bin/env bash
# Rebuild the wxparser DB from the transcript store — a pure re-derivation.
#
# Use after improving a correction (place_names, stt_terms, an extraction regex)
# to retroactively fix ALL history. Stops capture so nothing writes mid-rebuild;
# the replay is fast (no STT — that work is already in the transcripts). On
# restart the service primes from the freshly-rebuilt DB.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "stopping capture..."
sudo systemctl stop wxparser
echo "rebuilding DB from transcripts..."
/usr/bin/python3 -m wxparser.reprocess "$@"
echo "restarting capture..."
sudo systemctl start wxparser
echo "done."
