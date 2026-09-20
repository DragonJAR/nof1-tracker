# NOF1 Futures Copier n8n Flow

This directory contains an n8n workflow export that reproduces the headless automation pipeline of the NOF1 tracker CLI: polling the NOF1 API, rebuilding historical context, applying the same capital-allocation and risk rules, executing futures trades on Binance, and mirroring the project’s alerting behaviour without any of the front-end pieces. The flow keeps persistent context inside n8n using workflow static data, mirroring the repository’s on-disk `order-history.json` cache. 【F:src/services/order-history-manager.ts†L1-L135】

## Files

| File | Description |
| --- | --- |
| `nof1-futures-copier.json` | n8n workflow export with all nodes preconfigured. Import it directly into your n8n instance. |
| `README.md` | This guide. |

## High-level architecture

1. **Polling and configuration** – The `Agent Poll Schedule`, `Load Config`, and `Prepare Context` nodes reproduce the CLI bootstrap: environment variables are loaded, the `lastHourlyMarker` is recomputed with the same offset and cadence used by the CLI (`2025-10-17T22:30Z`, 1-hour increments). 【F:src/config/constants.ts†L14-L47】
2. **NOF1 API access** – `Fetch Account Totals` hits the `/account-totals` endpoint exactly as the CLI does, attaching `lastHourlyMarker` and caching the raw response on the item for later processing. 【F:src/scripts/analyze-api.ts†L96-L139】
3. **Agent selection** – `Extract Agent Positions` filters the JSON payload for the configured agent ID/slug, mirroring `ApiAnalyzer.followAgent`. 【F:src/scripts/analyze-api.ts†L141-L168】
4. **Binance state refresh** – `Fetch Binance Snapshot` signs REST requests against `/fapi/v2/account` and `/fapi/v2/positionRisk` so downstream nodes can perform the same balance checks, manual-closure detection, and profit-target evaluation that the CLI performs before executing a plan. 【F:src/services/trading-executor.ts†L1-L120】【F:src/services/follow-service.ts†L129-L247】
5. **Follow-plan synthesis** – `Generate Follow Plans` rebuilds the per-agent order history, distributes capital using the proportional/fixed strategies from `FuturesCapitalManager`, detects new entries, closes, profit-target exits, and manual closures in the same order as `FollowService`. 【F:src/services/futures-capital-manager.ts†L1-L120】【F:src/services/follow-service.ts†L33-L247】
6. **Plan expansion & batching** – `Expand Plans` and `Process Plans in Batches` mirror the CLI loop that iterates over each follow plan sequentially, guaranteeing Binance calls stay in order. 【F:src/commands/follow.ts†L18-L106】
7. **Risk evaluation** – `Assess Risk` reimplements the directional price-tolerance check and leverage-driven risk scoring from `RiskManager`. 【F:src/services/risk-manager.ts†L1-L120】【F:src/services/risk-manager.ts†L200-L258】
8. **Conditional execution** – `Execute Trade?` respects the `--risk-only` flag. Orders reach Binance only if the risk gate passes, matching the CLI’s `riskOnly` switch. 【F:src/commands/follow.ts†L38-L84】
9. **Trade execution & alerts** – `Execute Binance Trade` sets margin type, leverage, and places market orders through the futures API, then emits Telegram alerts when enabled—the same sequence performed by `TradingExecutor`. 【F:src/services/trading-executor.ts†L1-L160】
10. **History persistence** – `Update History` and `Record Skip/Manual Reset` update the global static data cache just like `OrderHistoryManager` would on disk, ensuring manual closures unlock refollowing and that processed orders are not duplicated. 【F:src/services/order-history-manager.ts†L70-L150】【F:src/services/follow-service.ts†L129-L211】

## Prerequisites

Before importing the workflow, create these credentials/secrets inside n8n:

- **Environment variables** (or n8n `Set` node overrides)
  - `NOF1_AGENT_ID` – agent slug or `model_id` to follow.
  - `NOF1_API_BASE_URL` – defaults to `https://nof1.ai/api`.
  - `NOF1_PRICE_TOLERANCE`, `NOF1_TOTAL_MARGIN`, `NOF1_FIXED_AMOUNT`, `NOF1_PROFIT_TARGET`, `NOF1_AUTO_REFOLLOW`, `NOF1_MARGIN_TYPE`, `NOF1_RISK_ONLY` – match the CLI flags for price tolerance, capital allocation, profit automation, and risk-only mode. 【F:src/commands/follow.ts†L30-L65】
  - `TELEGRAM_ENABLED`, `TELEGRAM_API_TOKEN`, `TELEGRAM_CHAT_ID` – optional; enables Telegram notifications like the CLI’s `TelegramService`. 【F:src/services/trading-executor.ts†L1-L51】

- **Credentials**
  - `binanceApi` (built-in n8n credential) containing your Binance Futures API key/secret. Set the environment to “testnet” if you prefer sandbox execution.

## Import & configuration steps

1. In n8n, go to **Import**, choose “File”, and upload `nof1-futures-copier.json`.
2. Open the workflow and confirm the `Load Config` node points to your environment variables.
3. Edit the `Fetch Binance Snapshot` and `Execute Binance Trade` nodes to select your `binanceApi` credential.
4. Enable the workflow. It will poll every minute by default; adjust the `Agent Poll Schedule` node if you need a different cadence.
5. Use the n8n “Stop” button to halt the loop cleanly; the static data cache keeps history between executions.

## Customisation tips

- Adjust the polling frequency or add additional notification channels (e.g. email) by branching from `Finalize Plan Result`.
- To add stop-loss/take-profit child orders, extend `Execute Binance Trade` with additional signed requests after the market order, mimicking the CLI’s `executePlanWithStopOrders` helper. 【F:src/services/trading-executor.ts†L160-L240】
- For audit trails, attach a Database node after `Finalize Plan Result` to persist each decision along with the risk assessment payload that mirrors the CLI output. 【F:src/utils/command-helpers.ts†L1-L160】

## Safety considerations

- Keep `NOF1_RISK_ONLY=true` while validating the workflow to exercise the full pipeline without placing trades.
- Test on Binance testnet before switching to production keys; both API helpers honour the credential’s environment flag and hit the matching base URL.
- Static data lives in the workflow execution; exporting/reimporting resets history. If you need shared state across multiple workflows, replace the history nodes with the n8n Data Store node configured to mimic `order-history.json`.

## Troubleshooting

- **403 or signature errors** – verify the `binanceApi` credential has futures permissions and that the environment matches your target (production vs testnet).
- **No plans generated** – ensure the agent slug is correct and that the NOF1 API returns non-empty `positions`. You can inspect the raw response in the `Fetch Account Totals` node execution data.
- **Duplicate orders** – confirm the workflow remains single-instance; parallel executions may race on static data. For multi-agent support, duplicate the workflow per agent ID.

