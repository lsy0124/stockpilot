"""Run synthetic tests only, refusing network access and private operator state."""
from pathlib import Path
import os
import socket
import sys
import unittest
import uuid


def main():
    root = Path(__file__).resolve().parents[1]
    run = root / 'work' / 'offline-tests' / uuid.uuid4().hex[:12]
    run.mkdir(parents=True, exist_ok=False)
    os.environ['STOCKPILOT_HOME'] = str(run / 'state')
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.path.insert(0, str(root))
    os.chdir(root)

    def no_network(*args, **kwargs):
        raise RuntimeError('Network disabled during offline tests')

    socket.socket.connect = no_network
    socket.create_connection = no_network
    print('Isolated test run:', run, flush=True)
    suite = unittest.defaultTestLoader.loadTestsFromName('stockpilot.tests.test_quality')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    # Existing 60-check ledger test uses only synthetic databases; keep it covered.
    from stockpilot.tests import test_ledger
    test_ledger.main()
    return 1 if test_ledger.FAIL else 0


if __name__ == '__main__':
    raise SystemExit(main())
