# Dependency scopes

The minimal offline lock contains requests and its four dependencies. It supports
synthetic regression tests and report generation; it does not validate live feeds.
certifi is pinned to 2026.10.19 in this lock; existing virtual environments are not
silently upgraded. TLS verification must remain enabled.

`scripts/requirements.txt` is retained as the earlier all-in-one environment,
including experiments. The inspected local environment differs from it: pandas
3.0.6 vs the declared 2.3.3, numpy 2.5.4 vs 2.3.5, matplotlib 3.11.2 vs 3.10.9,
and installed pyarrow 26.0.0 vs an unpinned declaration. Do not treat that
environment as a successful locked installation or change it without a rollback.

Optional backtest experiments use Backtrader (GPL) and vectorbt (license includes
Commons Clause). Do not relabel them as MIT or assume identical redistribution
rights. The project owner's license is still undecided. The full optional
dependency graph, advisories and data-source terms require a separate release
audit. This revision does not delete any existing experiment or dependency.
