# Security and private state

This is a local research and simulated-trading project. Do not use it to execute
real orders. Existing manual ledger records are operator data, not execution
authorization. The isolated tests must never be pointed at an operator database.

Treat `data/`, `reports/`, `audit/`, `logs/`, vaults, handoff archives and backups as
private. Do not put credentials into source or bug reports. If a real credential
has been shared, revoke/rotate it at its issuer and review usage; deleting the
current file does not retract copies or history.

Until a private reporting channel is configured, report suspected issues privately
to the project owner. No unconfirmed contact email is published here. A source
secret scan is heuristic and does not audit private state, archives or every
dependency. Data-source access and redistribution rights require separate review.
