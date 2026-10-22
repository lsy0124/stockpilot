"""Offline regressions. Run with scripts/test_offline.py to isolate all state."""
from contextlib import closing
import concurrent.futures
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import stockpilot as sp
from stockpilot import scheduler, simulation as sim, handoff_v5 as handoff, ledger


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=sp.BASE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def database(self):
        path = str(self.root / 'synthetic.db')
        sim.init_db(path)
        return path

    def order(self, **kwargs):
        return dict(action='buy', code='600000', price=5.0, shares=100, **kwargs)

    def test_scheduler_boundaries(self):
        cases = [('08:59', True), ('09:00', True), ('09:01', False),
                 ('09:30', False), ('11:30', False), ('12:00', False),
                 ('13:00', False), ('15:00', False), ('15:29', False), ('15:30', True)]
        with patch.object(scheduler, '_load_trade_calendar', return_value=None):
            for clock, expected in cases:
                with self.subTest(clock=clock):
                    result = scheduler.should_run(dt.datetime.fromisoformat('2026-10-22T'+clock))
                    self.assertEqual(result[0], expected)
                    if clock == '12:00':
                        self.assertNotIn('已收盘', result[1])

    def test_scheduler_converts_foreign_timezone(self):
        # 04:00 UTC is noon in the market's timezone, regardless of host location.
        with patch.object(scheduler, '_load_trade_calendar', return_value=None):
            self.assertFalse(scheduler.should_run(dt.datetime(2026, 10, 22, 4, tzinfo=dt.timezone.utc))[0])
            self.assertTrue(scheduler.in_trading_session(dt.datetime(2026, 10, 22, 2, tzinfo=dt.timezone.utc)))

    def test_force_and_closed_day_are_explicit(self):
        with patch.object(scheduler, '_load_trade_calendar', return_value={'2026-10-23'}):
            self.assertIn('非交易日', scheduler.should_run(dt.datetime(2026, 10, 22, 12))[1])
            self.assertIn('强制', scheduler.should_run(dt.datetime(2026, 10, 22, 12), force=True)[1])

    def test_calendar_uses_isolated_home(self):
        p = Path(sp.DATA) / 'trade_calendar.json'
        self.assertFalse(p.exists())
        try:
            sp.save_json({'days': ['2026-10-22']}, p)
            self.assertEqual(scheduler._load_trade_calendar(), {'2026-10-22'})
        finally:
            p.unlink(missing_ok=True)

    def test_timestamp_has_market_timezone(self):
        self.assertEqual(sp.now().utcoffset(), dt.timedelta(hours=8))
        self.assertEqual(dt.datetime.fromisoformat(sp.iso()).utcoffset(), dt.timedelta(hours=8))

    def test_atomic_save_preserves_existing_json_on_failure(self):
        p = self.root / 'report.json'
        sp.save_json({'original': True}, p)
        circular = []; circular.append(circular)
        with self.assertRaises(ValueError):
            sp.save_json(circular, p)
        self.assertEqual(sp.load_json(p), {'original': True})
        self.assertEqual(list(self.root.glob('.stockpilot-*.tmp')), [])

    def test_save_accepts_filename_without_directory(self):
        before = os.getcwd()
        try:
            os.chdir(self.root)
            sp.save_json({'ok': True}, 'simple.json')
            self.assertTrue(Path('simple.json').exists())
        finally:
            os.chdir(before)

    def test_invalid_orders_never_write(self):
        db = self.database()
        cases = [('buy', '600000', 5, -100), ('sell', '600000', 5, 0),
                 ('buy', '600000', float('nan'), 100), ('buy', '600000', float('inf'), 100),
                 ('buy', '600000', -5, 100), ('buy', '600000', 5, 1.5),
                 ('buy', '600000', True, 100), ('buy', '600000', 5, True),
                 ('hold', '600000', 5, 100), ('buy', '../bad', 5, 100)]
        for action, code, price, shares in cases:
            with self.subTest(action=action, price=price, shares=shares):
                with self.assertRaises(ValueError):
                    sim.sim_order(None, action, code, price=price, shares=shares, path=db)
        with closing(sqlite3.connect(db)) as con:
            self.assertEqual(con.execute('select count(*) from sim_orders').fetchone()[0], 0)
            self.assertEqual(con.execute('select cash from sim_account').fetchone()[0], 1000)

    def test_missing_market_date_never_falls_back(self):
        bars = [{'date': '2026-10-21', 'low': 4, 'high': 6},
                {'date': '2026-10-23', 'low': 3, 'high': 7}]
        with patch.object(sim.q, 'sina_daily', return_value=(bars, {})):
            bar, meta = sim.fetch_bar_for_rule('600000', '2026-10-22')
        self.assertIsNone(bar)
        self.assertIn('unknown', meta['note'])

    def test_exact_market_date_selected(self):
        exact = {'date': '2026-10-22', 'low': 4, 'high': 6}
        with patch.object(sim.q, 'sina_daily', return_value=([exact], {})):
            self.assertEqual(sim.fetch_bar_for_rule('600000', exact['date'])[0], exact)

    def test_malformed_bars_do_not_fill(self):
        for bar in [None, {}, {'date': '2026-10-22', 'high': float('nan'), 'low': 4},
                    {'date': '2026-10-22', 'high': 3, 'low': 4},
                    {'date': 'not-date', 'high': 6, 'low': 4}]:
            with self.subTest(bar=bar):
                self.assertEqual(sim.evaluate_fill(self.order(), bar, {})[0], sim.STATUS_UNKNOWN)

    def test_sqlite_row_and_existing_slippage_behavior(self):
        db = self.database()
        oid = sim.sim_order(None, 'buy', '600000', price=5, shares=100, path=db)
        with closing(sim.connect(db)) as con:
            row = con.execute('select * from sim_orders where id=?', (oid,)).fetchone()
        status, price, _ = sim.evaluate_fill(row, {'date': '2026-10-22', 'low': 4, 'high': 6}, {})
        self.assertEqual(status, sim.STATUS_ASSUMED_DAILY)
        self.assertGreater(price, 5)

    def test_stale_provider_response_does_not_change_cash(self):
        db = self.database()
        oid = sim.sim_order(None, 'buy', '600000', price=5, shares=100, path=db)
        with patch.object(sim, 'fetch_bar_for_rule', return_value=({'date': '2026-10-21', 'low': 4, 'high': 6}, {})), patch.object(sim, 'fetch_prev_close', return_value=None):
            self.assertEqual(sim.process_order(oid, '2026-10-22', db)['status'], sim.STATUS_UNKNOWN)
        self.assertEqual(sim.sim_portfolio(db)['cash'], 1000)

    def test_concurrent_processing_fills_only_once(self):
        db = self.database()
        oid = sim.sim_order(None, 'buy', '600000', price=5, shares=100, path=db)
        bar = {'date': '2026-10-22', 'low': 4, 'high': 6}
        with patch.object(sim, 'fetch_bar_for_rule', return_value=(bar, {})), patch.object(sim, 'fetch_prev_close', return_value=None):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: sim.process_order(oid, bar['date'], db), range(2)))
        with closing(sqlite3.connect(db)) as con:
            self.assertEqual(con.execute('select count(*) from sim_trades').fetchone()[0], 1)
            self.assertEqual(con.execute('select source from sim_trades').fetchone()[0], 'simulated')

    def test_market_date_is_preserved_for_t_plus_one(self):
        db = self.database()
        bar = {'date': '2020-01-06', 'low': 4, 'high': 6}
        with patch.object(sim, 'fetch_bar_for_rule', return_value=(bar, {})), patch.object(sim, 'fetch_prev_close', return_value=None), patch.object(sim, 'resolve_fee_config') as config:
            from stockpilot import fee_model
            config.return_value = (False, fee_model.fee_manager.list_configs()[0])
            buy = sim.sim_order(None, 'buy', '600000', price=5, shares=100, path=db)
            sim.process_order(buy, bar['date'], db)
            sell = sim.sim_order(None, 'sell', '600000', price=5, shares=100, path=db)
            result = sim.process_order(sell, bar['date'], db)
        self.assertEqual(result['status'], sim.STATUS_NOT_MET)
        self.assertIn('T+1', result['rule'])
        with closing(sqlite3.connect(db)) as con:
            self.assertEqual(con.execute('select trade_date from sim_trades').fetchone()[0], bar['date'])

    def test_exception_releases_transaction_and_does_not_fill(self):
        db = self.database()
        oid = sim.sim_order(None, 'buy', '600000', price=5, shares=100, path=db)
        with patch.object(sim, 'fetch_bar_for_rule', side_effect=RuntimeError('synthetic source failure')):
            with self.assertRaises(RuntimeError):
                sim.process_order(oid, '2026-10-22', db)
        with closing(sqlite3.connect(db, timeout=0.2)) as con:
            con.execute('BEGIN IMMEDIATE')
            self.assertEqual(con.execute('select count(*) from sim_trades').fetchone()[0], 0)

    def test_simulated_schema_migrates_without_rewriting_trades(self):
        db = str(self.root / 'old.db')
        with closing(sqlite3.connect(db)) as con:
            con.executescript(sim.SCHEMA.replace(' trade_date TEXT,', ''))
            con.execute("insert into sim_trades(filled_at,code,action,price,shares,amount) values(?,?,?,?,?,?)",
                        ('2020-01-06T10:00:00+08:00', '600000', 'buy', 5, 100, 500))
            con.commit()
        sim.init_db(db)
        sim.init_db(db)
        with closing(sqlite3.connect(db)) as con:
            self.assertIn('trade_date', [r[1] for r in con.execute('pragma table_info(sim_trades)')])
            self.assertEqual(con.execute('select filled_at,trade_date,shares from sim_trades').fetchone(),
                             ('2020-01-06T10:00:00+08:00', None, 100))

    def test_manifest_rejects_paths_before_file_reads(self):
        for name in ['../private.json', '/private.json', 'C:\\secret', '..\\outside', 'x:y', '']:
            with self.subTest(name=name):
                p = self.root / 'manifest.json'
                sp.save_json({'files': [{'name': name, 'present': True}]}, p)
                with patch.object(handoff, '_sha256_file', side_effect=AssertionError('must not read')):
                    self.assertFalse(handoff.verify_manifest(str(p), str(self.root))['ok'])

    def test_manifest_rejects_empty_and_duplicate_entries(self):
        for files in [[], [{'name': 'a.json'}, {'name': 'A.json'}], [None]]:
            p = self.root / 'manifest.json'
            sp.save_json({'files': files}, p)
            self.assertFalse(handoff.verify_manifest(str(p), str(self.root))['ok'])

    def test_clean_custom_directory_handoff_and_tamper_detection(self):
        ledger.init_db()
        snapshot = {'trade_date': '2026-10-22', 'collected_at': sp.iso(), 'indices': [], 'sources': {}}
        portfolio = ledger.portfolio()
        output = self.root / 'export'
        result = handoff.export_handoff_v5(snapshot, {}, {'events': []}, {'candidates': []},
                                           {}, {}, {'overall': 'unknown'}, portfolio, out_dir=str(output))
        self.assertTrue(result['verification']['ok'])
        self.assertTrue(Path(result['zip']).is_file())
        self.assertTrue(Path(result['package']).is_file())
        self.assertTrue(all((output / n).is_file() for n in handoff.REQUIRED_FILES))
        manifest = output / 'manifest.json'
        self.assertTrue(handoff.verify_manifest(str(manifest))['ok'])
        sp.save_json({'tampered': True}, output / 'research.json')
        self.assertFalse(handoff.verify_manifest(str(manifest))['ok'])

    def test_history_summary_uses_the_same_prediction_window(self):
        from stockpilot import review
        predictions = [{'resolved': True, 'outcome': {'change_pct': 1, 'direction_correct': True}} for _ in range(150)]
        predictions.append({'resolved': False})
        summary = review.review_summary(predictions=predictions)
        self.assertEqual(summary['resolved_total'], 150)
        self.assertEqual(summary['pending_total'], 1)
        self.assertEqual(summary['accuracy_pct'], 100)

    def test_report_does_not_invent_model_or_universe_size(self):
        from stockpilot import report
        text = report.build_decision_report({}, {}, {}, [], {}, {}, {}, {}, report.default_rules())
        self.assertNotIn('5576', text)
        self.assertNotIn('由智谱 GLM5.3 生成', text)
        self.assertIn('仅模拟', text)


if __name__ == '__main__':
    unittest.main()
