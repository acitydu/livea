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
from breadth import Breadth
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

    def test_breadth_fits_top_right_without_overlapping_indices(self):
        self.window.update_display(self.quotes())
        self.window.update_turnover(self.comparison())
        self.window.update_breadth(Breadth(1234, 4321, datetime(2026, 10, 26, 10, 20)))
        self.window.show()
        self.app.processEvents()
        self.assertEqual(self.window.breadth_up.text(), 'Z1234')
        self.assertEqual(self.window.breadth_down.text(), 'D4321')
        self.assertIn('沪深 A 股', self.window.breadth_container.toolTip())
        self.assertIn('2026-10-26 10:20:00', self.window.breadth_container.toolTip())
        last_index = self.window.index_widgets[self.window.config['indices'][2]]['container']
        self.assertGreater(self.window.breadth_container.x(), last_index.geometry().right())
        self.assertLessEqual(self.window.breadth_container.geometry().right(),
                             self.window.row1_container.width())
        self.assertLessEqual(self.window.breadth_container.x() - last_index.geometry().right(), 9)
        last_bottom_index = self.window.index_widgets[self.window.config['indices'][-1]]['container']
        bottom_index_right = last_bottom_index.mapTo(self.window.row2_container,
                                                    last_bottom_index.rect().topRight()).x()
        self.assertGreater(self.window.volume_container.x(), bottom_index_right)
        self.assertLessEqual(self.window.volume_container.x() - bottom_index_right, 9)
        for label in (self.window.breadth_up, self.window.breadth_down):
            self.assertGreaterEqual(label.width(), label.sizeHint().width())
        self.assertEqual(self.window.height(), 38)

    def test_breadth_failure_recovery_and_settings_rebuild(self):
        self.window.update_display(self.quotes())
        breadth = Breadth(0, 5200, datetime(2026, 10, 26, 10, 20))
        self.window.update_breadth(breadth)
        self.assertEqual(self.window.breadth_up.text(), 'Z0')
        self.window.handle_breadth_error('offline')
        self.assertEqual(self.window.breadth_up.text(), 'Z--')
        self.assertEqual(self.window.breadth_down.text(), 'D--')
        self.assertIn('offline', self.window.breadth_container.toolTip())
        self.assertEqual(self.window.vol_total.text(), '400亿')
        self.assertEqual(self.window.status_label.text(), 'OK')
        self.window.update_breadth(breadth)
        self.window.create_index_widgets()
        self.assertEqual(self.window.breadth_down.text(), 'D5200')

    def test_worker_breadth_failure_preserves_successful_quotes(self):
        fetcher = main.DataFetcher(['sz399006'])
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'v_s_sz399006="51~CY~399006~2600~20~0.78~0~0";'
        quotes, errors, breadth_errors = [], [], []
        fetcher.data_ready.connect(quotes.append)
        fetcher.error_occurred.connect(errors.append)
        fetcher.breadth_error_occurred.connect(breadth_errors.append)
        with patch('urllib.request.urlopen', return_value=response), \
                patch.object(main, 'request_breadth', side_effect=ValueError('missing market')):
            fetcher.run()
        self.assertEqual(quotes[0]['sz399006']['current'], 2600)
        self.assertEqual(errors, [])
        self.assertEqual(breadth_errors, ['missing market'])
        fetcher.deleteLater()

    def test_worker_quote_failure_still_updates_breadth(self):
        fetcher = main.DataFetcher([])
        breadth = Breadth(1500, 3500, datetime(2026, 10, 26, 10, 20))
        received, errors = [], []
        fetcher.breadth_ready.connect(received.append)
        fetcher.error_occurred.connect(errors.append)
        with patch('urllib.request.urlopen', side_effect=OSError('offline')), \
                patch.object(main, 'request_breadth', return_value=breadth):
            fetcher.run()
        self.assertEqual(received, [breadth])
        self.assertEqual(errors, ['offline'])
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
