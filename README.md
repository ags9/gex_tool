# gexbot — Phase 0: Data Pipeline

Resumable downloader/converter for Massive (Polygon) flat files.
Pulls whole-market OPRA day files, keeps only SPX/SPXW/XSP/SPY options
plus SPX/VIX index values, writes date-partitioned zstd Parquet, and
deletes raw files immediately. At most one raw day ever sits on disk.

## Setup (Mac mini)
```bash
brew install uv                    # if not installed
cd gexbot
uv venv && source .venv/bin/activate
uv pip install -e .
cp .env.example .env               # then fill in Massive keys + drive path
```
S3 flat-file credentials come from the Massive dashboard (Flat Files
section) — they are separate from the REST API key.

## Run the backfill
```bash
python -m gexbot backfill --dataset opra_trades      # start small: trades only
python -m gexbot status                              # manifest summary anytime
python -m gexbot backfill                            # everything (quotes = big)
```
Safe to Ctrl-C and rerun; finished days are skipped via the DuckDB
manifest. Halts itself if drive free space drops under 200 GB.

Suggested first run: `--start 2024-01-02 --end 2024-03-28` (one quarter)
to calibrate download times and storage before committing to full history.

## Output layout
```
parquet/opra_trades/date=YYYY-MM-DD/data.parquet   # + root/expiry/right/strike cols
parquet/opra_quotes/date=YYYY-MM-DD/data.parquet
parquet/index_values/date=YYYY-MM-DD/data.parquet
manifest.duckdb
```
Query everything directly, e.g.:
```python
import polars as pl
pl.scan_parquet("parquet/opra_trades/**/*.parquet") \
  .filter(pl.col("root") == "SPXW").group_by("expiry").len().collect()
```

## Notes
- Ports for later services are reserved in `.env` (8741 dashboard, 8742 API)
  to avoid colliding with other dev work on this machine.
- Quotes days are large (tens of GB raw). Run overnight; the pipeline is
  sequential by design to be kind to the drive and the connection.
- Next module: backtest harness (ledger reconstruction + spec replay).
