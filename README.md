# BOLD Guardian Telegram Bot — Public V2.2

This package is the public multi-user Guardian build.

## What changed

- Removed single-chat authorization. `GUARDIAN_TELEGRAM_CHAT_ID` and `TELEGRAM_CHAT_ID` are no longer used for access control.
- Every Telegram user gets responses in their own chat.
- Added per-user command rate limiting.
- Added short-lived wallet-result caching so repeated `/get` calls do not repeat the same RPC work.
- Added short-lived BOLD yield caching so `/yield` and `/opportunities` share one fetch.
- Added SQLite persistence for watched wallets, position snapshots and deduplicated alerts.
- Added `/watch`, `/stop`, `/watching`, `/history`, and watched-Trove `/status`.
- Added `/rate` to calculate estimated annual borrowing interest for watched Troves.
- Added `/simulate` to compare simple-interest costs at two rates over a selected period.
- Added opt-in `/yieldwatch` and `/yieldstop` subscriptions for significant APR drops and newly detected venues.
- Added persistent yield observations and per-user alert deduplication.
- Added periodic watch checks inside the bot process.
- Added alerts for material liquidation-buffer deterioration, risky/liquidatable status, and borrowing-rate changes.
- A missing wallet result during a watch cycle does **not** automatically trigger a liquidation claim.
- `/opportunities` now presents market context rather than pretending that a yield APR is directly comparable to a borrowing rate.
- Yield output warns when the API explicitly reports `$0` TVL.
- Telegram output remains HTML-safe and split at venue/position boundaries.
- Existing Alchemy-indexed Trove discovery + on-chain ownership verification + exhaustive fallback are retained.

## Environment

Required:

- `GUARDIAN_TELEGRAM_BOT_TOKEN`
- `ETH_RPC_URL`

Optional:

- `GUARDIAN_DB_PATH` (default `guardian.sqlite3`)
- `GUARDIAN_CACHE_TTL_SECONDS` (default `60`)
- `GUARDIAN_USER_RATE_LIMIT_SECONDS` (default `10`)
- `GUARDIAN_WATCH_INTERVAL_SECONDS` (default `900`)
- `GUARDIAN_WATCH_LIMIT_PER_USER` (default `5`), `GUARDIAN_MAX_TOTAL_WATCHES` (default `2000`)
- `GUARDIAN_WATCH_WORKERS` (default `4`), `GUARDIAN_HANDLER_WORKERS` (default `8`)
- `GUARDIAN_BUFFER_DROP_POINTS` (default `5`), `GUARDIAN_BUFFER_WINDOW_HOURS` (default `24`)
- `GUARDIAN_RISK_REMINDER_HOURS` (default `6`), `GUARDIAN_SNAPSHOT_RETENTION_DAYS` (default `30`)
- `GUARDIAN_YIELD_DROP_THRESHOLD_PCT` (default `20`, relative APR drop percentage)

Do not set a single-user chat authorization variable. It is intentionally ignored.

## Railway persistence

SQLite persists only on persistent storage. For Railway, attach a volume and set:

`GUARDIAN_DB_PATH=/data/guardian.sqlite3`

Without a Railway volume, the bot still works, but watch/history data can disappear when the service is redeployed or restarted.

## Start command

`python guardian_telegram.py`

## Commands

- `/start`
- `/get 0xWallet`
- `/yield`
- `/eth`
- `/opportunities 0xWallet`
- `/watch 0xWallet`
- `/stop 0xWallet`
- `/watching`
- `/history 0xWallet` (only for wallets you watch)
- `/status [0xWatchedWallet]`
- `/rate [0xWatchedWallet]`
- `/simulate DEBT CURRENT_RATE NEW_RATE [DAYS]`
- `/yieldwatch`
- `/yieldstop`
- `/help`

## Safety / product semantics

Guardian is read-only. It does not ask for private keys and does not execute transactions.

Risk labels such as `HEALTHY`, `SICKLY`, `RISKY`, and `LIQUIDATABLE` are Guardian presentation heuristics, not official Liquity classifications.

`/rate` currently reports estimated borrowing cost only. `/simulate` uses simple interest and a constant debt balance. Neither command recommends a rate. Branch-wide rate ranking, validated redemption-context modelling, external loan comparisons, automatic rate changes and yield risk-adjusted ranking are not included in this release. Yield alerts require users to opt in with `/yieldwatch`; the first successful check establishes a baseline and does not generate historical-change alerts.

## MCP server (new in this package)

This release also includes a separate read-only BOLD Guardian MCP server. See `README_MCP_V1.md` for its six tools, four resources, two prompts, local stdio setup and secured Streamable HTTP deployment instructions.

The Telegram bot remains a separate process. The MCP server reuses `guardian_core.py` and does not require the Telegram bot token. Set `ETH_RPC_URL` for on-chain tools. Remote HTTP mode additionally requires `MCP_API_TOKEN` and `MCP_ALLOWED_HOSTS`.
