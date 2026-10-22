# Contributing locally

This project is not yet licensed for public reuse. Confirm ownership and licensing
before sharing contributions externally. Keep changes small and reproducible.

Run `python scripts/test_offline.py` with the minimal locked dependencies. Add
synthetic regressions for bugs that affect simulation, integrity or scheduling.
Never copy private reports, trades, account details, keys or market data into tests.
Do not connect real providers, brokerages or paid models as part of ordinary tests.
Do not infer research quality or current provider availability from offline success.
