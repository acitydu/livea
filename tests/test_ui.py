"""Qt integration with isolated config files and deterministic quote data."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import main
from turnover import Comparison, MARKET_CODES
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QDialog


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        root = ROOT / ".test-tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.config_path = Path(self.temp.name) / "settings.json"
        self.cache_path = Path(self.temp.name) / "minute_cache.json"
        for patcher in [patch.object(main, 'CONFIG_FILE', self.config_path),
                        patch.object(main, 'MINUTE_CACHE_FILE', self.cache_path),
                        patch.object(main.MainWindow, 'fetch_data'),
                        patch.object(main.MainWindow, 'fetch_turnover'),
                        patch.object(main.MainWindow, 'init_tray', lambda w: setattr(w, 'tray', Mock()))]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = main.MainWindow()
        self.addCleanup(self.destroy_window)

    def destroy_window(self):
        self.window.timer.stop()
        self.window.turnover_timer.stop()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def quotes(self):
        return {c: {"current": 3800, "change": 3, "timestamp": datetime(2026, 10, 14, 10, 20, 27),
                    "amount_yuan": Decimal('20000000000')} for c in self.window.config['indices']}

    def comparison(self, difference=-10000000000):
        return Comparison('20261014', '20261013', '1020', Decimal('40000000000'),
                          Decimal('40000000000') - difference)

    def test_total_difference_tooltip_and_layout(self):
        self.window.update_display(self.quotes())
        self.window.update_turnover(self.comparison())
        self.window.show()
        self.app.processEvents()
        self.assertEqual(self.window.vol_total.text(), '400亿')
        self.assertEqual(self.window.vol_diff.text(), ' -100亿')
        self.assertIn('2026-10-13', self.window.volume_container.toolTip())
        self.assertIn('10:20', self.window.volume_container.toolTip())
        self.assertLessEqual(self.window.volume_container.geometry().right(), self.window.row2_container.width())
        self.assertGreaterEqual(self.window.vol_diff.width(), self.window.vol_diff.sizeHint().width())

    def test_positive_difference(self):
        self.window.update_display(self.quotes())
        self.window.update_turnover(self.comparison(10000000000))
        self.assertEqual(self.window.vol_diff.text(), ' +100亿')

    def test_missing_one_market_does_not_show_partial_total(self):
        data = self.quotes()
        del data[MARKET_CODES[1]]
        self.window.update_display(data)
        self.window.update_turnover(self.comparison())
        self.assertEqual(self.window.vol_total.text(), '--')
        self.assertEqual(self.window.vol_diff.text(), ' --')

    def test_network_error_clears_turnover_and_recovery_restores(self):
        self.window.update_display(self.quotes())
        self.window.update_turnover(self.comparison())
        self.window.handle_error('offline')
        self.assertEqual(self.window.vol_total.text(), '--')
        self.assertEqual(self.window.vol_diff.text(), ' --')
        self.window.update_display(self.quotes())
        self.assertEqual(self.window.vol_total.text(), '400亿')

    def test_missing_market_amount_also_hides_comparison(self):
        data = self.quotes()
        del data[MARKET_CODES[1]]['amount_yuan']
        self.window.update_display(data)
        self.window.update_turnover(self.comparison())
        self.assertEqual(self.window.vol_total.text(), '--')
        self.assertEqual(self.window.vol_diff.text(), ' --')

    def test_comparison_error_preserves_realtime_total(self):
        self.window.update_display(self.quotes())
        self.window.handle_turnover_error('missing previous minute')
        self.assertEqual(self.window.vol_total.text(), '400亿')
        self.assertEqual(self.window.vol_diff.text(), ' --')

    def test_date_rollover_hides_old_comparison(self):
        self.window.update_turnover(self.comparison())
        data = self.quotes()
        for value in data.values():
            value['timestamp'] = datetime(2026, 10, 15, 9, 31)
        self.window.update_display(data)
        self.assertEqual(self.window.vol_diff.text(), ' --')

    def test_settings_apply_persist_restart_and_preserve_order(self):
        self.window.config['indices'] = list(reversed(self.window.config['indices']))
        order = self.window.config['indices'][:]
        def accept(dialog):
            dialog.compare_combo.setCurrentIndex(dialog.compare_combo.findData(15))
            dialog.save_settings()
            return QDialog.DialogCode.Accepted
        with patch.object(main.SettingsDialog, 'exec', accept):
            self.window.show_settings()
        self.assertEqual(self.window.turnover_timer.interval(), 900000)
        self.assertEqual(self.window.load_config()['turnover_compare_minutes'], 15)
        self.assertEqual(json.loads(self.config_path.read_text())['indices'], order)

    def test_all_compare_choices(self):
        for interval in (1, 5, 15, 30):
            dialog = main.SettingsDialog({**main.DEFAULT_CONFIG, 'turnover_compare_minutes': interval})
            self.assertEqual(dialog.compare_combo.currentData(), interval)
            dialog.deleteLater()

    def test_quote_fetcher_always_requests_both_markets(self):
        fetcher = main.DataFetcher(['sz399006'])
        self.assertTrue(set(MARKET_CODES).issubset(fetcher.indices))
        fetcher.deleteLater()

    def test_quit_waits_for_active_worker_without_destroying_it(self):
        worker = Mock()
        worker.isRunning.return_value = True
        self.window.turnover_fetcher = worker
        with patch.object(main.QApplication, 'quit') as quit_app:
            self.window.quit_app()
            worker.requestInterruption.assert_called_once()
            quit_app.assert_not_called()
            worker.isRunning.return_value = False
            self.window.finish_quit()
            quit_app.assert_called_once()
        self.window.turnover_fetcher = None


if __name__ == '__main__':
    unittest.main()
