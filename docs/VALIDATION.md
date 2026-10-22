# Validation scope

The offline runner exercises scheduler boundaries/timezones, state isolation,
atomic saves, invalid simulated orders, missing/malformed bars, same-order
concurrency, T+1 market dates, schema compatibility, export integrity and report
attribution. It also runs the existing synthetic ledger suite.

All inputs and databases are synthetic. Python socket access is rejected during
the run. No actual quote accuracy, profitable strategy, brokerage integration,
external model connection or outside user is established by these tests.

Legacy stage 4/5 and handoff-backup scripts depend on prior operator reports;
they are not run by this ordinary test entrypoint. No old report count is added
to current test totals. The CI workflow is a local template until publication
is separately approved and an actual run is observed.

`STOCKPILOT_HOME` isolates supported paths; it is not an OS sandbox. Arbitrary
caller-supplied paths still require trust. Atomic JSON replacement protects one
file; a multi-file report export is not an atomic transaction. Manifest seals
are hashes, not signed proof of authorship. Existing truncated hashes are retained.
