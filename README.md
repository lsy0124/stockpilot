# StockPilot

[简体中文](README.zh-CN.md)

A local research-evidence and paper-trading project. It collects and labels input
data, produces review/handoff reports, and models explicitly assumed daily fills
in an independent simulated account. It is not a brokerage execution service.

This engineering revision is **0.5.1**. No public release, outside adoption,
investment return, or live data availability is claimed. The main backtest path
uses snapshots; it is not a validated long-history backtest engine.

## Offline setup and checks

Python 3.12 is the validated environment. Run from the project directory:

```powershell
python -m venv .venv-offline
.\.venv-offline\Scripts\python.exe -m pip install -r scripts/requirements-offline-lock.txt
.\.venv-offline\Scripts\python.exe scripts/test_offline.py
```

The runner creates a new `work/offline-tests/<id>/state`, sets `STOCKPILOT_HOME`
before imports, and rejects Python socket connections. It tests synthetic inputs,
including report exports, and runs the existing synthetic ledger checks. It does
not run the legacy stage 4/5 acceptance scripts that need prior research reports.
Do not run those scripts against your private reports as ordinary unit tests.

Optional package installation is `python -m pip install .`; install the pinned
offline lock first when reproducing the tested dependency set. The wheel includes
Python modules, not private state, scripts, old reports, or source data.

## State and scheduler

For a separate workspace, set an absolute `STOCKPILOT_HOME` **before import**:

```powershell
$env:STOCKPILOT_HOME = Join-Path $PWD 'work\my-paper-workspace'
python -m stockpilot.scheduler status
```

With the variable unset, existing data/log/report paths are preserved. The
`.env.example` file is documentation, not an automatic environment loader.
Market timestamps and scheduling use Asia/Shanghai even on computers in other
timezones. The complete daily pipeline is deferred from 09:01 until 15:30,
including lunch; 09:00 or earlier is a premarket preparation window. A cached
calendar is used when present; otherwise weekday fallback cannot identify holidays.
`--force` remains explicit. The tool does not register a system schedule.

Do not run `scheduler run`, collectors, `forward_sim`, or legacy demos during
offline verification: they can contact sources or update state. The original
`scripts/requirements.txt` is retained for compatibility, including optional
experiments. Its complete dependency tree has not been validated by the minimal
offline checks. See [dependency notes](docs/DEPENDENCIES.md).

## Simulation and data limits

- Invalid directions, codes, non-finite/nonpositive prices and nonpositive or
  fractional quantities are refused before a simulated order is written.
- Missing data for the requested date remains `unknown`; another day's bar does
  not stand in for the requested date. Daily range fills remain assumptions.
- `simulation.init_db()` adds a nullable `trade_date` column to older **simulated**
  databases. It preserves prior rows. New trades record market date separately
  from processing time; legacy rows retain their timestamp fallback. Tests use
  synthetic databases; the operator's existing database is not migrated by tests.
- Same-order processing is serialized by SQLite. Source lookups occur inside the
  transaction, so slow sources can delay another worker up to the connection timeout.
- Existing manual ledger records are retained. They do not grant permission to
  connect a broker, submit real orders, or use real funds.

Fees are stored configuration and stress-test assumptions; this revision does
not independently revalidate current tax, exchange or broker rates. Source access
does not confer permission to redistribute quotations, news, or announcements.

## Sharing and license

The owner has **not selected a license** for this project. This local revision
does not grant public reuse rights. Do not upload the development directory,
private databases, report bundles, credential files, or its history. A `.gitignore`
does not remove already-tracked material. Third-party dependencies keep their own
terms. Public publication needs a separate rights/security review and approval.

See [security guidance](SECURITY.md), [contributing](CONTRIBUTING.md),
[changes](CHANGELOG.md), and [validation limits](docs/VALIDATION.md).
