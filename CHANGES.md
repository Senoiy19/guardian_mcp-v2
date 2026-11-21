# Changes in this release (audit fixes)

Every item below was reproduced against V1 before being fixed, and has a regression test
(`test_bot.py`, `test_core_rpc.py`, `test_mcp_server.py`, `test_mcp_logic.py`).

## Alerting (Telegram)
- Alerts are recorded as delivered only after Telegram accepts them; failed sends are retried next cycle.
- A blocked/removed chat is purged; one failing subscriber no longer stops a broadcast.
- Dedup is state-based per user (no permanent fingerprints): no spam while a buffer drifts, and a Trove that
  recovers and later re-enters risk alerts again. Recovery notices added.
- Buffer deterioration is measured against the 24h high, so slow declines alert. Per-user state fixes the
  two-users-one-wallet case.
- New alerts: Trove no longer found (confirmed over 2 checks), monitoring degraded/recovered.
- All due watches are processed (was 20 per 15 min), each wallet read once per cycle, on worker threads.
- Yield drops compare against the 24h high with a cooldown; unavailable APR is shown as unavailable, never 0%.

## Security / privacy
- RPC URLs/API keys and the Telegram token are redacted from errors, logs and tracebacks.
- MCP: deny-by-default auth on every path, failed-login throttling (429), placeholder/weak tokens refused,
  `/healthz` for deploy checks.
- `/history` only for wallets you watch; snapshots are written only by watch cycles and pruned after 30 days.
- Bounded caches and rate-limit tables; one rate-limit notice per window.

## Correctness
- Trove IDs are strings in MCP input and output (256-bit values were corrupted as JSON numbers).
- Cache hits report the real fetch time; cached results are copied; `force_refresh` is throttled.
- Reverts (burned NFTs) are not retried; all reads of a wallet are pinned to one block; MCR/price read once per branch.
- TroveNFT and BorrowerOperations are cross-checked against each TroveManager (soft if a getter is missing).
- Price source distinguishes oracle / oracle-failure flag / last-good price. Health caveats returned by MCP tools.
- Fallback scan stops once all Troves are found and is capped (`GUARDIAN_MAX_FALLBACK_SCAN`).
- `/simulate` shares the bounded MCP maths; MCP tool errors now reach the model as readable messages.

## Ops
- SQLite: connections closed, WAL, versioned migrations (v1 databases upgrade in place).
- `.env` loading, Dockerfile, Procfile, lockfile; dead `monitor_clean_v2.py` removed; bot ignores plain chat and `/cmd@otherbot`.

## Known limits (unchanged)
Redemption exposure, branch shutdown thresholds, Stability Pool positions and ENS names are not modelled.
Nothing here was tested against a live RPC, the live Liquity yield feed, Telegram, or Railway.


## Rate Intelligence update
- `/rate` now compares each watched Trove's rate with an evenly spaced sample of up to 100 active/zombie Troves in the same collateral branch.
- Reports sample median, share of sampled Troves with lower rates, and estimated annual interest difference versus the sample median.
- Reports sample size, total Trove ID count and pinned Ethereum block; branch samples are cached for 15 minutes by default.
- Adds redemption-order context without predicting redemption probability or timing.
- Labels the comparison as sample-based (not a complete branch census) and retains simple-interest / constant-debt caveats.
- New configuration: `GUARDIAN_RATE_SAMPLE_SIZE` (10–250, default 100) and `GUARDIAN_RATE_SAMPLE_TTL_SECONDS` (minimum 60, default 900).
