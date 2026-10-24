# Local operating rules for diagnosing-bugs

Use the upstream Matt Pocock `diagnosing-bugs` method with these project rules.

## GAUNTLET LOOP
Search → confront sources → test/review → identify failures → cross-check architecture → classify severity → record decision → repeat until blocked or verified.

## Karpathy-style execution discipline
- First understand the existing system and its constraints.
- Make the smallest change that can fix the demonstrated cause.
- Prefer one change per test cycle.
- Verify each claim against source, logs, or executable checks.
- Do not create replacement campaigns, duplicate systems, or unnecessary infrastructure.
- Do not loop forever. If a dependency is blocked, record evidence, classify it as STANDBY, and continue with independent work.

## ASD-STE100 plain-language rules
- Use short sentences and one instruction per step.
- Use one name for each component.
- State the expected result and the observed result.
- Separate FACT, HYPOTHESIS, and UNVERIFIED.
- Avoid vague success claims. Report exact test, output, and status.

## Safety and user constraints
- Keep production Cloudflare Workers unchanged during diagnosis.
- Do not modify Google Ads campaigns, budgets, or the existing Search-2 campaign.
- Do not print secrets. Redact tokens, cookies, auth headers, and private identifiers.
- Do not claim an integration is connected until an end-to-end test passes.
- For every fix: reproduce the error, add or identify a regression test, apply the smallest fix on a branch, run tests, compare with baseline, and document the outcome.
- Railway remains STANDBY. Do not make it a dependency.
- If no reliable reproduction exists, do not guess or patch. Record the missing evidence and continue with another independently testable item.
