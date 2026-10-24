#!/usr/bin/env bash
# Human-in-the-loop reproduction loop template from mattpocock/skills.
# Edit the steps below. Do not capture secrets or authentication headers.
set -euo pipefail
step() {
  printf '\n>>> %s\n' "$1"
  read -r -p "    [Enter when done] " _
}
capture() {
  local var="$1" question="$2" answer
  printf '\n>>> %s\n' "$question"
  read -r -p "    > " answer
  printf -v "$var" '%s' "$answer"
}
# Replace these example steps with a minimal reproduction for the specific bug.
step "Run the exact reproduction command in a safe test environment."
capture RESULT "Did the exact reported symptom occur? (yes/no)"
capture ERROR_SUMMARY "Provide a short, redacted error summary (or none):"
printf '\n--- Captured ---\nRESULT=%s\nERROR_SUMMARY=%s\n' "$RESULT" "$ERROR_SUMMARY"
