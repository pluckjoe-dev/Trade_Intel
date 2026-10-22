# Trade Intel

An hourly market brief for an AI trading assistant to read before making decisions.

**Latest brief (for Grok):** https://raw.githubusercontent.com/pluckjoe-dev/Trade_Intel/main/BRIEF.md
**Machine-readable version:** https://raw.githubusercontent.com/pluckjoe-dev/Trade_Intel/main/brief.json

The brief is analysis, not trade instructions. It contains no account data.

## What it covers

FX majors (EURUSD, GBPUSD, USDJPY, AUDUSD), indices (SPX500, NSDQ100, UK100, GER40), gold, US mega-cap stocks (TSLA, NVDA, AAPL, MSFT, AMZN, META, GOOG) and crypto (BTC, ETH).

For each instrument:

- Price and 1/5/20-day change
- Trend on 1h, 4h and daily, and whether they agree
- Volatility regime (current 20-day volatility ranked against the last ~6 months)
- ATR (typical daily move), suggested stop distance and sizing per $1 risked
- Key levels: today, prior day, prior week, prior month, 20-day range, pivots, moving averages
- Session ranges (Asia, London, New York)
- Invalidation level for the current bias
- Stand-aside flags: imminent high-impact events, earnings, extreme volatility, conflicting timeframes, extended moves, closed markets

Across markets: risk tone, US dollar direction, highly correlated pairs (the same bet twice), economic calendar for the next 72 hours and earnings for the next 14 days.

## How it runs

A GitHub Action (`.github/workflows/brief.yml`) runs `brief.py` every hour at about :07 UTC and commits the new `BRIEF.md` and `brief.json`. GitHub can start scheduled runs late during busy periods.

To refresh on demand: **Actions → Hourly market brief → Run workflow**.

## Data sources

- Prices: eToro Public API (`public-api.etoro.com`), the same prices the account trades on
- Economic calendar: ForexFactory public feed
- Earnings dates: Nasdaq earnings calendar

## Setup

Add two repository secrets under **Settings → Secrets and variables → Actions**:

- `ETORO_API_KEY`: the Public API key
- `ETORO_USER_KEY`: the User key

Secrets are never shown in the public repo or the Action logs.

Instrument limits (minimum exposure, leverage) are hard-coded in `brief.py` from the account's eToro eligibility on 2026-10-22.
