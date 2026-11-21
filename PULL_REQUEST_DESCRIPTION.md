# PR: Upgrade Guardian `/rate` and simplify `/simulate`

## Summary

This PR makes Guardian's rate commands more useful and easier to operate:

- `/rate` now reports current borrowing cost and compares each watched Trove against an evenly-spaced, bounded sample of rates from its collateral branch.
- `/rate` reports the sample median, the share of sampled Troves with lower rates, and the estimated annual-interest difference at the sample median.
- Branch samples are read at a single Ethereum block, cached in memory, and labelled as complete or partial. Partial samples are not presented as full branch statistics.
- `/simulate` is now guided. Users send `/simulate` to select a watched Trove, then `/simulate NUMBER NEW_RATE [DAYS]`. Guardian fetches the selected Trove's latest debt and rate rather than asking the user to enter them.
- The simulation verifies the Trove is still owned by the watched wallet, validates inputs, and clearly states that no transaction is executed.
- Batch-related and redemption-order limitations are disclosed in the Telegram output and documentation.

## Example user flow

```text
/simulate
/simulate 1 3 90
```

## Configuration

- `GUARDIAN_RATE_SAMPLE_SIZE=100`
- `GUARDIAN_RATE_CACHE_TTL_SECONDS=900`
- `GUARDIAN_SIMULATION_SELECTION_TTL_SECONDS=900`

## Tests

- `python -m unittest test_rate_intelligence.py -v`
- `python test_v22.py`
- `python -m unittest test_mcp_logic.py -v`
- Python syntax compilation for the changed modules and tests.

These are offline/pure-logic and mocked-reader tests. This PR has not been tested against a live Ethereum RPC, live Telegram API, or Railway deployment.

## Limitations

A partial branch sample's median is only the sample median, not the exact branch median or a full redemption queue reconstruction. The simulation assumes constant debt and simple interest, excludes batch management fees and transaction costs, and does not estimate redemption probability or execute a rate change.
