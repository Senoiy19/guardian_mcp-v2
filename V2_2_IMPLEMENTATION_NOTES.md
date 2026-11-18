# BOLD Guardian V2.2 implementation notes

## Included in this build

- `/status [0xWatchedWallet]` now refreshes and reports the Troves belonging to the user's watched wallet(s). It no longer performs the old RPC/API/database service-health report.
- `/stop [0xWallet]` replaces `/unwatch`. With exactly one watched wallet, `/stop` can be used without an address. With multiple watches, an address is required.
- `/rate [0xWatchedWallet]` reports the current rate and estimated annual borrowing-interest cost for watched Troves.
- `/simulate DEBT CURRENT_RATE NEW_RATE [DAYS]` compares simple-interest estimates at two rates.
- `/yieldwatch` opts a Telegram chat into BOLD venue alerts; `/yieldstop` unsubscribes it.
- Yield observations are persisted. A first successful check establishes a baseline. Later checks can alert on newly detected venues and relative APR drops at or above `GUARDIAN_YIELD_DROP_THRESHOLD_PCT` (default 20%).
- Existing SQLite tables are preserved. New yield tables are created additively at startup.

## Important limits

This is a V2.2 foundation release, not the complete Rate Intelligence vision.

- `/rate` does not yet compare a Trove against a complete branch-wide rate distribution.
- `/rate` does not estimate redemption probability or recommend a new rate.
- `/simulate` is a simple-interest estimate with a constant debt balance. It excludes batch management fees, compounding, transaction costs, protocol-specific rate-adjustment constraints and redemption exposure.
- Loan comparisons across other lending protocols are not included.
- Yield alerts compare reported APR only. They do not rank risk-adjusted returns, validate venue safety, or infer that a higher APR is a better venue.
- New-venue detection means a venue key not previously recorded by this database. It is not independent confirmation that the venue has just launched.
- Yield monitoring runs on the existing `GUARDIAN_WATCH_INTERVAL_SECONDS` cycle. The first successful observation after subscribing seeds the baseline without sending retrospective alerts.

## Deployment

Keep the existing Railway environment variables. Add or retain:

- `GUARDIAN_DB_PATH=/data/guardian.sqlite3` when using a Railway Volume mounted at `/data`.
- Optional `GUARDIAN_YIELD_DROP_THRESHOLD_PCT=20`.

Start command remains:

`python guardian_telegram.py`

Deploy only one polling instance for a given Telegram bot token. Use a separate bot token for staging.

## Validation performed

- Python syntax compilation passed for the Telegram bot, core, and tests.
- V2.2 offline tests passed for SQLite migrations, per-user watch isolation, yield baseline/drop/new-venue detection, alert deduplication, rate simulation arithmetic, `/status`, `/rate`, and `/stop`.
- Existing health-classification tests passed under a lightweight Web3 import stub.
- No live Ethereum RPC, Liquity contract, yield API, Telegram API, or Railway deployment test was performed in this environment.
