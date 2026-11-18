# BOLD Guardian MCP V1 — implementation notes

## What was added

- `guardian_mcp_server.py`: official MCP Python SDK v2 MCPServer, exposing six read-only tools, four resources and two prompts.
- `guardian_mcp_logic.py`: pure validation, yield-feed normalisation, venue comparison and borrowing-cost calculations.
- `test_mcp_logic.py`: six offline unit tests.
- `README_MCP_V1.md`: setup, client configuration, deployment, data limitations and test instructions.
- `.env.example`: environment variable template.
- `guardian_core.py`: added `inspect_trove(branch, trove_id)` to resolve the current NFT owner and read an individual Trove safely.

## Validation performed here

- `py_compile` passed for all changed/new Python modules.
- Six pure logic tests passed.
- Attempted to install the MCP SDK and Web3 dependencies for an import/transport test, but this environment has no outbound DNS/network access. Therefore the MCP server has not been imported with its real dependencies, started, inspected with MCP Inspector, connected to an RPC, or deployed.

## SDK choice

The project targets the current v2 line of the official Python SDK and uses `mcp.server.MCPServer`. Streamable HTTP is used for remote deployment; stdio is used for local client-launched servers. Official references:

- https://py.sdk.modelcontextprotocol.io/
- https://py.sdk.modelcontextprotocol.io/run/

## Important before public deployment

- Set `ETH_RPC_URL` to a working Ethereum mainnet RPC.
- For remote HTTP, set `MCP_API_TOKEN` to a randomly generated secret of at least 24 characters.
- Set `MCP_ALLOWED_HOSTS` to the exact Railway hostname (plus `hostname:*` if needed).
- Do not expose the HTTP server without bearer authentication and a correct host allowlist.
- Test with MCP Inspector and a real MCP client before announcing availability.
- The initial HTTP cache is process-local; deploy one instance until shared caching/rate limiting is designed.
