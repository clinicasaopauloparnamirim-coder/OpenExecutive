---
name: diagnosing-bugs
description: Diagnosis loop for hard bugs and performance regressions. Use when the user says "diagnose"/"debug this", or reports something broken/throwing/failing/slow.
---

# Diagnosing Bugs

A discipline for hard bugs. Skip phases only when explicitly justified.

When exploring the codebase, read `GLOSSARY.md` (if it exists) to get a clear mental model of the relevant modules, and check ADRs in the area you're touching.

## Redact

This skill has you show commands, outputs and captured artifacts. **Redact every secret first**: write `<REDACTED>` in its place. Build loops against env vars, so the credential stays in the environment rather than in what you show. Captured artifacts carry auth headers: quote only the lines that carry the signal.

If the redacted output is not enough to diagnose the bug, say so and ask the user.

## Phase 1: Build a feedback loop

**This is the skill.** Everything else is mechanical. If you don't have a tight pass/fail signal for this bug, no amount of staring at code will save you.

### Ways to construct one, in roughly this order

1. Failing test at whatever seam reaches the bug: unit, integration, e2e.
2. Curl / HTTP script against a running dev server.
3. CLI invocation with a fixture input, diffing stdout against a known-good snapshot.
4. Headless browser script (Playwright / Puppeteer) that drives the UI and asserts on DOM/console/network.
5. Replay a captured trace.
6. Throwaway harness exercising the bug code path.
7. Property / fuzz loop for intermittent wrong output.
8. Bisection harness for a regression between known states.
9. Differential loop: same input through old and new versions.
10. Human-in-the-loop script as a last resort.

### Tighten the loop

Make it fast, deterministic, agent-runnable, and specific to the user's exact symptom. A loop that merely does not crash is not enough.

### Non-deterministic bugs

Repeat the trigger and increase reproduction rate until the bug can be debugged.

### When you cannot build a loop

Stop and state what you tried. Ask for access to the reproducing environment, a redacted artifact, or permission for temporary production instrumentation. Do not continue with unsupported hypotheses.

### Completion criterion

Name one command that has already been run and produces a red result on the exact bug. It must be reproducible, fast, and agent-runnable. Do not hypothesise before this command exists.

## Phase 2: Reproduce and minimise

Run the loop. Confirm that it reproduces the user's exact symptom. Capture the error and reduce the scenario until every remaining element is necessary to reproduce it. Do not proceed until the failure is reproduced and minimised.

## Phase 3: Hypothesise

Generate 3–5 ranked, falsifiable hypotheses before testing. For each, state what observation would confirm or reject it. Share the ranked list with the user, then continue without blocking if the user is unavailable.

## Phase 4: Instrument

Change one variable at a time. Prefer a debugger or REPL, then targeted logs. Prefix temporary debug logs with a unique `[DEBUG-...]` tag and remove them before completion. For performance issues, measure a baseline before changing code.

## Phase 5: Fix and regression test

Write a regression test before the fix when a correct test seam exists. Confirm that the test fails before the fix, passes after it, and that the original reproduction now passes. If no correct seam exists, document that finding.

## Phase 6: Cleanup

Before declaring completion:
- Re-run the original reproduction.
- Confirm the regression test passes or document the missing test seam.
- Remove all temporary `[DEBUG-...]` instrumentation.
- Remove or clearly isolate throwaway prototypes.
- Record the confirmed root cause in the commit or PR description.

## Safety and change control

- Never expose secrets, tokens, cookies, personal data, or full auth headers in logs.
- Read production configuration before proposing changes.
- Prefer read-only inspection and isolated branches.
- Do not deploy to production or change budgets, campaigns, OAuth credentials, DNS, secrets, or live Worker code without an explicit, specific approval.
- Never report a fix as complete until the reproduction and regression checks pass.
