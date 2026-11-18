# BOLD Guardian MCP V1

A read-only MCP server that exposes selected BOLD Guardian capabilities to MCP-compatible AI applications. It reuses `guardian_core.py`; it does not duplicate the on-chain position calculations.

## Included in V1

### Tools

1. `get_wallet_positions(wallet, force_refresh=False)` — finds currently owned active/Zombie Troves across Guardian's configured WETH, wstETH and rETH branches.
2. `get_trove(branch, trove_id)` — reads one Trove by branch and Trove NFT ID, verifying current ownership on-chain.
3. `get_position_health(wallet, force_refresh=False)` — returns CR, MCR, liquidation price, distance to liquidation and Guardian's presentation-only health label.
4. `get_bold_yields(force_refresh=False)` — retrieves Liquity's published BOLD venue feed and converts its fractional `weekly_apr` field into percentage APR.
5. `compare_yield_venues(asset=None, min_tvl_usd=None, limit=25)` — filters venues and sorts by reported APR. This is not a risk-adjusted ranking.
6. `simulate_borrowing_cost(debt_bold, current_rate_pct, proposed_rate_pct, days=365)` — deterministic simple-interest comparison with explicit exclusions.

### Resources

- `liquity://protocol/overview`
- `liquity://protocol/contracts`
- `liquity://guardian/methodology`
- `liquity://guardian/glossary`

### Prompts

- `analyse_liquity_wallet(wallet)`
- `compare_bold_yield_opportunities(asset="BOLD")`

## Requirements

- Python 3.11+
- Ethereum mainnet RPC URL in `ETH_RPC_URL`
- Dependencies from `requirements.txt`

Python 3.13 is suitable for the existing Guardian environment, subject to dependency installation.

## Local setup (stdio)

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set `ETH_RPC_URL`, then run:

```bash
python guardian_mcp_server.py --transport stdio
```

Configure your MCP client to launch this command as a local stdio server, with the working directory set to this project and `ETH_RPC_URL` supplied in its environment.

Example generic client configuration:

```json
{
  "mcpServers": {
    "bold-guardian": {
      "command": "python",
      "args": ["/absolute/path/to/guardian_mcp_server.py", "--transport", "stdio"],
      "env": {
        "ETH_RPC_URL": "https://YOUR_ETHEREUM_RPC_URL"
      }
    }
  }
}
```

Do not commit RPC keys or tokens to Git.

## Remote deployment (Streamable HTTP)

Remote mode is protected by a bearer token and host allowlist. Set:

- `ETH_RPC_URL`
- `MCP_API_TOKEN` (at least 24 characters; use a randomly generated secret)
- `MCP_ALLOWED_HOSTS` (comma-separated exact service hostnames; include `hostname:*` if requests include a port)
- `MCP_ALLOWED_ORIGINS` (only browser client origins, if applicable)
- `PORT` (Railway supplies this automatically)

Start command:

```bash
python guardian_mcp_server.py --transport streamable-http
```

MCP endpoint: `https://YOUR_HOST/mcp`

Do not disable bearer authentication on a publicly reachable deployment. Do not expose this server behind a public URL until the hostname allowlist and token are configured. Use one service instance initially; the current in-memory cache is per process.

## Data and calculation notes

- On-chain positions are read from Ethereum mainnet. The core validates the configured TroveManager deployments against Liquity's CollateralRegistry before scanning.
- Wallet discovery can use Alchemy's indexed ERC-721 transfer API; if unavailable or incomplete, the existing core falls back to a slower on-chain scan.
- Each position includes its block number and whether a fallback price was used.
- Empty wallet results mean no active/Zombie Troves were found by the configured discovery method; they are not proof of liquidation.
- Health labels are Guardian presentation heuristics relative to the branch MCR, not official Liquity statuses.
- Yield data comes from `https://api.liquity.org/v2/website/bold-venues.json`. A source-reported `weekly_apr` fraction is multiplied by 100. APR is not guaranteed or risk-adjusted.
- Borrowing simulation assumes simple interest, constant debt and a 365-day year. It excludes batch management fees, compounding, rate-adjustment costs/constraints and redemption exposure.
- No tool signs transactions, moves funds, changes Trove settings or accesses private keys.

## Validation

Run pure logic tests:

```bash
python -m unittest test_mcp_logic.py -v
```

Compile all Python files:

```bash
python -m py_compile guardian_core.py guardian_mcp_logic.py guardian_mcp_server.py test_mcp_logic.py
```

Then test the MCP surface with the official MCP Inspector. The server has not been live-tested against an Ethereum RPC, Liquity contracts, Liquity yield API or a deployed Railway service in this build environment.

## Current scope limits

V1 does not implement branch-wide rate distribution, redemption queue reconstruction, redemption event indexing, Stability Pool depositor accounting, LQTY/PIL analytics, cross-protocol lending comparisons, authenticated per-user saved data, or transaction execution. These should be added only after their data pipelines and calculations are independently validated.
