"""
System Monitor - A lightweight desktop widget for monitoring system metrics.
Displays real-time data in a taskbar-integrated format.
"""

import sys
import json
import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime

from turnover import (MARKET_CODES, COMPARE_INTERVALS, YUAN_PER_YI,
                      TurnoverService, amount, format_yi)

from PyQt6.QtWidgets import (
    QApplication, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QSystemTrayIcon, QMenu, QDialog,
    QCheckBox, QComboBox, QPushButton, QSpinBox,
    QGridLayout, QGroupBox, QLineEdit, QMessageBox,
    QSizePolicy
)
from PyQt6.QtCore import (
    Qt, QTimer, QPoint, QThread, pyqtSignal,
    QSettings, QPropertyAnimation, QEasingCurve
)
from PyQt6.QtGui import (
    QFont, QColor, QPalette, QIcon, QPainter,
    QFontDatabase, QAction, QKeySequence, QFontMetrics, QShortcut
)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply


# ============ 配置 ============
CONFIG_FILE = Path(__file__).parent.parent / "config" / "settings.json"
DEFAULT_CONFIG = {
    "indices": ["sh000001", "sz399001", "sz399006", "sh000300", "sh000016", "sh000852"],
    "refresh_interval": 10,
    "turnover_compare_minutes": 1,
    "display_format": "full",
    "position": "taskbar_center_right",
    "hotkey": "`",
    "auto_hide": False,
    "opacity": 0.95,
    "font_size": 12,
    "show_on_hover": True,
}

# 按交易日保存的分时成交额缓存（金额单位：元）
MINUTE_CACHE_FILE = Path(__file__).parent.parent / "config" / "minute_cache.json"

INDEX_NAMES = {
    "sh000001": ("SH", "上证指数"),
    "sz399001": ("SZ", "深证成指"),
    "sz399006": ("CY", "创业板指"),
    "sh000688": ("KC", "科创50"),
    "sh000300": ("IF", "沪深300"),
    "sh000905": ("ZZ", "中证500"),
    "sh000016": ("IH", "上证50"),
    "sz399005": ("ZX", "中小板指"),
    "sh000852": ("IM", "中证1000"),
}


class DataFetcher(QThread):
    """后台获取指数，同时始终获取沪深成交额和行情时间。"""
    data_ready = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)

    def __init__(self, indices):
        super().__init__()
        self.indices = list(dict.fromkeys([*indices, *MARKET_CODES]))

    def run(self):
        import urllib.request
        # 保持现有指数简版行情；两市完整版提供金额精度和交易日期。
        codes = [f"s_{c}" for c in self.indices] + list(MARKET_CODES)
        url = "https://qt.gtimg.cn/?q=" + ",".join(codes)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com"
            })
            with urllib.request.urlopen(req, timeout=10) as response:
                raw = response.read().decode("gbk", errors="replace")
            result = {}
            full_quotes = {}
            for line in raw.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                fields = value.strip().strip(';"').split("~")
                if key.startswith("v_s_") and len(fields) >= 8:
                    code = key[4:]
                    current, change = float(fields[3]), float(fields[4])
                    result[code] = {
                        "name": fields[1], "current": current,
                        "prev_close": current - change, "change": change,
                        "change_pct": float(fields[5]),
                    }
                elif key[2:] in MARKET_CODES and len(fields) > 37:
                    full_quotes[key[2:]] = fields
            for code, fields in full_quotes.items():
                if code in result:
                    try:
                        result[code]["timestamp"] = datetime.strptime(fields[30], "%Y%m%d%H%M%S")
                        result[code]["amount_yuan"] = amount(fields[35].split("/")[2])
                    except (ValueError, IndexError):
                        logging.warning("%s 完整版成交额不可用", code)
            if not result:
                raise ValueError("行情接口未返回有效指数")
            if not self.isInterruptionRequested():
                self.data_ready.emit(result)
        except Exception as exc:
            logging.exception("获取行情失败")
            if not self.isInterruptionRequested():
                self.error_occurred.emit(str(exc))


class TurnoverFetcher(QThread):
    data_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, service):
        super().__init__()
        self.service = service

    def run(self):
        try:
            comparison = self.service.refresh(self.isInterruptionRequested)
            if not self.isInterruptionRequested():
                self.data_ready.emit(comparison)
        except InterruptedError:
            pass
        except Exception as exc:
            logging.warning("成交额对比暂不可用：%s", exc)
            if not self.isInterruptionRequested():
                self.error_occurred.emit(str(exc))


class SettingsDialog(QDialog):
    """设置面板"""
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config.copy()
        self.setWindowTitle("显示设置")
        self.resize(400, 560)
        self.setMinimumWidth(380)
        self.setStyleSheet("""
            QDialog {
                background: rgba(255, 255, 255, 0.95);
                border: 1px solid rgba(0, 0, 0, 0.08);
                border-radius: 12px;
            }
            QLabel {
                color: #374151;
                font-size: 13px;
                font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
            }
            QGroupBox {
                color: #374151;
                font-size: 13px;
                font-weight: 600;
                border: 1px solid rgba(0, 0, 0, 0.06);
                border-radius: 8px;
                margin-top: 12px;
                padding-top: 16px;
                padding: 16px;
                font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }
            QCheckBox {
                color: #374151;
                font-size: 13px;
                spacing: 8px;
                font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 3px;
                border: 1px solid rgba(0, 0, 0, 0.2);
            }
            QCheckBox::indicator:checked {
                background: #0078d4;
                border-color: #0078d4;
            }
            QComboBox {
                padding: 4px 8px;
                border-radius: 4px;
                border: 1px solid rgba(0, 0, 0, 0.15);
                background: white;
                font-size: 12px;
                color: #374151;
                min-width: 120px;
                font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
            }
            QSpinBox {
                padding: 4px 8px;
                border-radius: 4px;
                border: 1px solid rgba(0, 0, 0, 0.15);
                background: white;
                font-size: 12px;
                color: #374151;
                min-width: 80px;
                font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
            }
            QPushButton {
                padding: 6px 16px;
                border-radius: 4px;
                border: none;
                font-size: 12px;
                font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
                cursor: pointer;
            }
            QPushButton#primary {
                background: #0078d4;
                color: white;
            }
            QPushButton#primary:hover {
                background: #006cbd;
            }
            QPushButton#secondary {
                background: rgba(0, 0, 0, 0.05);
                color: #374151;
            }
            QPushButton#secondary:hover {
                background: rgba(0, 0, 0, 0.08);
            }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        # 标题
        title = QLabel("显示设置")
        title.setStyleSheet("font-size: 14px; font-weight: 600; color: #1f2937;")
        layout.addWidget(title)

        # 分隔线
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet("background: rgba(0, 0, 0, 0.06);")
        layout.addWidget(line)

        # 基本设置
        basic_group = QGroupBox("基本设置")
        basic_layout = QGridLayout(basic_group)
        basic_layout.setSpacing(10)

        # 刷新间隔
        basic_layout.addWidget(QLabel("刷新间隔:"), 0, 0)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(5, 300)
        self.interval_spin.setValue(self.config.get('refresh_interval', 30))
        self.interval_spin.setSuffix(" 秒")
        basic_layout.addWidget(self.interval_spin, 0, 1)

        # 老板键
        basic_layout.addWidget(QLabel("老板键:"), 1, 0)
        self.hotkey_combo = QComboBox()
        self.hotkey_combo.addItems(["` ~", "F12", "Ctrl+`", "Esc", "禁用"])
        current_hotkey = self.config.get('hotkey', '`')
        idx = self.hotkey_combo.findText(current_hotkey)
        if idx < 0:
            idx = 0
        self.hotkey_combo.setCurrentIndex(idx)
        basic_layout.addWidget(self.hotkey_combo, 1, 1)

        basic_layout.addWidget(QLabel("成交额对比间隔:"), 2, 0)
        self.compare_combo = QComboBox()
        for minutes in COMPARE_INTERVALS:
            self.compare_combo.addItem(f"{minutes} 分钟", minutes)
        self.compare_combo.setCurrentIndex(max(0, self.compare_combo.findData(
            self.config.get('turnover_compare_minutes', 1))))
        basic_layout.addWidget(self.compare_combo, 2, 1)

        layout.addWidget(basic_group)

        # 指数选择
        indices_group = QGroupBox("显示指数")
        indices_layout = QVBoxLayout(indices_group)
        indices_layout.setSpacing(8)

        self.index_checkboxes = {}
        all_indices = [
            ("sh000001", "SH (上证指数)"),
            ("sz399001", "SZ (深证成指)"),
            ("sz399006", "CY (创业板指)"),
            ("sh000300", "HS (沪深300)"),
            ("sh000016", "SZ50 (上证50)"),
            ("sh000852", "ZZ1000 (中证1000)"),
            ("sh000688", "KC (科创50)"),
            ("sh000905", "ZZ (中证500)"),
            ("sz399005", "ZX (中小板指)"),
        ]

        for code, label in all_indices:
            cb = QCheckBox(label)
            cb.setChecked(code in self.config.get('indices', []))
            self.index_checkboxes[code] = cb
            indices_layout.addWidget(cb)

        layout.addWidget(indices_group)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("保存")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self.save_settings)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

    def save_settings(self):
        self.config['refresh_interval'] = self.interval_spin.value()
        self.config['turnover_compare_minutes'] = self.compare_combo.currentData()
        self.config['hotkey'] = self.hotkey_combo.currentText()
        selected = [code for code, cb in self.index_checkboxes.items() if cb.isChecked()]
        # 修改对比间隔时保留用户已有的指数顺序。
        original = [code for code in self.config.get('indices', []) if code in selected]
        self.config['indices'] = original + [code for code in selected if code not in original]
        self.accept()

    def get_config(self):
        return self.config


class MainWindow(QWidget):
    """主窗口 - 伪装成任务栏一部分"""

    def __init__(self):
        super().__init__()

        # 加载配置
        self.config = self.load_config()

        # 数据
        self.index_data = {}
        self.fetcher = None
        self.turnover_fetcher = None
        self.turnover_service = TurnoverService(MINUTE_CACHE_FILE)
        self.comparison = None
        self.comparison_error = "正在获取分时数据"
        self.quote_error = ""
        self._closing = False

        # 隐藏状态
        self.is_hidden = False

        self.init_ui()
        self.init_timer()
        self.init_tray()
        self.init_hotkey()
        self.fetch_data()
        self.fetch_turnover()

    def paintEvent(self, event):
        """绘制完全透明背景"""
        pass

    def init_ui(self):
        """初始化UI - 无边框、透明、置顶"""
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowOpacity(0.3)  # 半透明，文字可见但背景融入

        # 设置窗口大小和位置
        self.update_position()

        # 主布局 - 两行：第一行指数，第二行成交量
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(4, 1, 4, 1)
        self.main_layout.setSpacing(0)
        # 第一行：指数row1（前3个）
        self.row1_container = QWidget()
        self.row1_container.setStyleSheet("background: transparent; border: none;")
        self.row1_container.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.row1_layout = QHBoxLayout(self.row1_container)
        self.row1_layout.setContentsMargins(0, 0, 0, 0)
        self.row1_layout.setSpacing(16)
        self.main_layout.addWidget(self.row1_container)

        # 第二行：指数row2（后3个）+ 成交额（结构在create_index_widgets里创建）
        self.row2_container = QWidget()
        self.row2_container.setStyleSheet("background: transparent; border: none;")
        self.row2_layout = QHBoxLayout(self.row2_container)
        self.row2_layout.setContentsMargins(0, 0, 0, 0)
        self.row2_layout.setSpacing(16)
        self.main_layout.addWidget(self.row2_container)

        # 固定宽度450像素
        self.setFixedWidth(450)
        # 固定高度：两行指数(每行约17px) + 间距(1px) + 上下边距(4px) ≈ 38px
        self.setFixedHeight(38)

        # 设置字体
        self.font_code = QFont("Segoe UI Variable", 8)
        self.font_code.setWeight(QFont.Weight.Medium)

        # 数值用Consolas等宽字体确保对齐（实时值保持较大）
        self.font_value = QFont("Consolas", 10)
        self.font_value.setWeight(QFont.Weight.DemiBold)

        # 涨跌幅（百分比）稍小
        self.font_percent = QFont("Consolas", 8)
        self.font_percent.setWeight(QFont.Weight.Normal)

        self.font_volume = QFont("Consolas", 8)
        self.font_volume.setWeight(QFont.Weight.Normal)

        # 创建指数显示项
        self.index_widgets = {}
        self.create_index_widgets()

        # 样式 - 完全透明，融入背景
        self.setStyleSheet("")
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)

        # 鼠标事件
        self.setMouseTracking(True)

    def create_index_widgets(self):
        """创建指数显示组件 - 两行HBoxLayout"""
        # 清除旧组件
        for layout in [self.row1_layout, self.row2_layout]:
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

        self.index_widgets = {}

        # 前3个放第一行
        indices = self.config.get('indices', [])
        for code in indices[:3]:
            if code not in INDEX_NAMES:
                continue
            self.add_index_to_layout(code, self.row1_layout)

        # 第二行的三个指标包在一个固定宽度的容器里
        indices_container = QWidget()
        indices_container.setStyleSheet("background: transparent; border: none;")
        indices_container.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        indices_layout = QHBoxLayout(indices_container)
        indices_layout.setContentsMargins(0, 0, 0, 0)
        indices_layout.setSpacing(16)

        # 剩余放第二行的内层容器
        for code in indices[3:]:
            if code not in INDEX_NAMES:
                continue
            self.add_index_to_layout(code, indices_layout)

        self.row2_layout.addWidget(indices_container)
        self.row2_layout.addStretch()
        vol_container = QWidget()
        self.volume_container = vol_container
        vol_container.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        vol_inner = QHBoxLayout(vol_container)
        vol_inner.setContentsMargins(0, 0, 0, 0)
        vol_inner.setSpacing(0)

        vol_title = QLabel("V:")
        vol_title.setFont(self.font_volume)
        vol_title.setStyleSheet("color: #6b7280;")
        vol_title.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        vol_inner.addWidget(vol_title)

        self.vol_total = QLabel("--")
        self.vol_total.setFont(self.font_volume)
        self.vol_total.setStyleSheet("color: #6b7280;")
        self.vol_total.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        vol_inner.addWidget(self.vol_total)

        self.vol_diff = QLabel(" --")
        self.vol_diff.setFont(self.font_volume)
        self.vol_diff.setStyleSheet("color: #6b7280;")
        self.vol_diff.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        vol_inner.addWidget(self.vol_diff)

        self.status_label = QLabel("")
        self.status_label.setFont(self.font_volume)
        self.status_label.setStyleSheet("color: #6b7280;")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.status_label.setFixedWidth(24)
        self.status_label.setContentsMargins(4, 0, 0, 0)
        vol_inner.addWidget(self.status_label)

        self.row2_layout.addWidget(vol_container)

    def add_index_to_layout(self, code, layout):
        """添加一个指数（3个标签包在一个容器里）"""
        short_name, full_name = INDEX_NAMES[code]

        container = QWidget()
        container.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        inner = QHBoxLayout(container)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(2)

        code_label = QLabel(short_name)
        code_label.setFont(self.font_code)
        code_label.setStyleSheet("color: #374151;")
        code_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        inner.addWidget(code_label)

        value_label = QLabel("--")
        value_label.setFont(self.font_value)
        value_label.setStyleSheet("color: #111827;")
        inner.addWidget(value_label)

        change_label = QLabel("--")
        change_label.setFont(self.font_percent)
        change_label.setStyleSheet("color: #374151;")
        inner.addWidget(change_label)

        layout.addWidget(container)

        self.index_widgets[code] = {
            'container': container,
            'code': code_label,
            'value': value_label,
            'change': change_label,
        }

    def update_position(self):
        """更新窗口位置 - 使用保存的位置或默认任务栏中间偏右"""
        screen = QApplication.primaryScreen()
        screen_geo = screen.geometry()
        taskbar_height = 48  # Win11任务栏高度

        # 检查是否有保存的位置
        saved_pos = self.config.get('window_pos')
        if saved_pos:
            x, y = saved_pos
            # 确保在屏幕范围内
            x = max(0, min(x, screen_geo.width() - 100))
            y = max(0, min(y, screen_geo.height() - 30))
        else:
            # 默认位置：屏幕底部，中间偏右
            width = 450
            x = int(screen_geo.width() * 0.65)
            y = screen_geo.height() - taskbar_height + 1
            if x + width > screen_geo.width() - 200:
                x = screen_geo.width() - width - 200

        self.move(x, y)

        logging.debug("Window position: x=%s, y=%s", x, y)

    def init_timer(self):
        """初始化定时器"""
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.fetch_data)
        self.timer.start(self.config.get('refresh_interval', 30) * 1000)
        self.turnover_timer = QTimer(self)
        self.turnover_timer.timeout.connect(self.fetch_turnover)
        self.turnover_timer.start(self.config['turnover_compare_minutes'] * 60000)

    def init_tray(self):
        """初始化系统托盘"""
        self.tray = QSystemTrayIcon(self)

        # 创建简单的图标
        icon_pixmap = self.create_tray_icon()
        self.tray.setIcon(QIcon(icon_pixmap))

        # 托盘菜单
        menu = QMenu()
        menu.setStyleSheet("""
            QMenu {
                background: rgba(255, 255, 255, 0.95);
                border: 1px solid rgba(0, 0, 0, 0.08);
                border-radius: 8px;
                padding: 8px;
                font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
                font-size: 13px;
            }
            QMenu::item {
                padding: 8px 24px;
                border-radius: 4px;
                color: #374151;
            }
            QMenu::item:selected {
                background: rgba(0, 0, 0, 0.05);
            }
            QMenu::separator {
                height: 1px;
                background: rgba(0, 0, 0, 0.06);
                margin: 6px 12px;
            }
        """)

        show_action = QAction("显示", self)
        show_action.triggered.connect(self.show_window)
        menu.addAction(show_action)

        settings_action = QAction("设置", self)
        settings_action.triggered.connect(self.show_settings)
        menu.addAction(settings_action)

        menu.addSeparator()

        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()

    def create_tray_icon(self):
        """创建托盘图标"""
        from PyQt6.QtGui import QPixmap, QPainter, QBrush

        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(QColor("#6b7280")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(2, 2, 12, 12)
        painter.end()

        return pixmap

    def init_hotkey(self):
        """初始化老板键"""
        hotkey = self.config.get('hotkey', '`')
        if hotkey == '禁用':
            return

        if hotkey == 'F12':
            shortcut = QShortcut(QKeySequence("F12"), self)
        elif hotkey == 'Esc':
            shortcut = QShortcut(QKeySequence("Esc"), self)
        elif hotkey == 'Ctrl+`':
            shortcut = QShortcut(QKeySequence("Ctrl+`"), self)
        else:
            shortcut = QShortcut(QKeySequence("`"), self)

        shortcut.activated.connect(self.toggle_visibility)

    def fetch_data(self):
        if self._closing or (self.fetcher and self.fetcher.isRunning()):
            return
        if self.fetcher:
            self.fetcher.deleteLater()
        self.fetcher = DataFetcher(self.config.get('indices', []))
        self.fetcher.data_ready.connect(self.update_display)
        self.fetcher.error_occurred.connect(self.handle_error)
        self.fetcher.finished.connect(self.finish_quit)
        self.fetcher.start()

    def fetch_turnover(self):
        if self._closing or (self.turnover_fetcher and self.turnover_fetcher.isRunning()):
            return
        if self.turnover_fetcher:
            self.turnover_fetcher.deleteLater()
        self.turnover_fetcher = TurnoverFetcher(self.turnover_service)
        self.turnover_fetcher.data_ready.connect(self.update_turnover)
        self.turnover_fetcher.error_occurred.connect(self.handle_turnover_error)
        self.turnover_fetcher.finished.connect(self.finish_quit)
        self.turnover_fetcher.start()

    def update_turnover(self, comparison):
        self.comparison = comparison
        self.comparison_error = ""
        self.render_turnover()
        logging.info("成交额对比 %s/%s %s：%s",
                     comparison.trading_date, comparison.previous_date,
                     comparison.minute, format_yi(comparison.difference_yuan, signed=True))

    def handle_turnover_error(self, message):
        self.comparison = None
        self.comparison_error = message
        self.render_turnover()

    def update_display(self, data):
        """更新显示"""
        logging.debug("Data received: %s indices", len(data))
        self.index_data = data
        self.status_label.setText("OK")
        self.status_label.setStyleSheet("color: #6b7280;")

        for code, widgets in self.index_widgets.items():
            if code in data:
                d = data[code]

                # 更新当前值 - 统一格式保证对齐
                current = d['current']
                widgets['value'].setText(f"{current:.2f}")

                # 更新涨跌额（绝对值）
                change = d['change']
                sign_change = '+' if change >= 0 else ''
                widgets['change'].setText(f"{sign_change}{change:.2f}")
            else:
                widgets['value'].setText("--")
                widgets['change'].setText("--")

        self.quote_error = ""
        self.render_turnover()

    def render_turnover(self):
        tooltip = ["沪深合计成交额（不含北交所）"]
        quote_date = None
        try:
            markets = [self.index_data[c] for c in MARKET_CODES]
            timestamps = [m["timestamp"] for m in markets]
            if len({t.date() for t in timestamps}) != 1 or self.quote_error:
                raise ValueError("两市行情不完整")
            total = sum(m["amount_yuan"] for m in markets)
            quote_date = timestamps[0].strftime("%Y%m%d")
            self.vol_total.setText(format_yi(total))
            tooltip.append(f"实时总额：{total / YUAN_PER_YI:,.2f} 亿")
            tooltip.append("行情时间：" + min(timestamps).strftime("%Y-%m-%d %H:%M:%S"))
        except (KeyError, ValueError):
            self.vol_total.setText("--")
            tooltip.append("实时总额暂不可用：" + (self.quote_error or "等待沪深两市完整行情"))

        comparison = self.comparison
        if comparison and comparison.trading_date == quote_date:
            self.vol_diff.setText(" " + format_yi(comparison.difference_yuan, signed=True))
            minute = comparison.minute[:2] + ":" + comparison.minute[2:]
            today = datetime.strptime(comparison.trading_date, "%Y%m%d").strftime("%Y-%m-%d")
            previous = datetime.strptime(comparison.previous_date, "%Y%m%d").strftime("%Y-%m-%d")
            tooltip.extend([
                f"较上一交易日同刻（截至 {minute}）：",
                f"{today}：{comparison.current_yuan / YUAN_PER_YI:,.2f} 亿",
                f"{previous}：{comparison.previous_yuan / YUAN_PER_YI:,.2f} 亿",
                f"差额：{comparison.difference_yuan / YUAN_PER_YI:+,.2f} 亿",
            ])
        else:
            self.vol_diff.setText(" --")
            tooltip.append("较昨同刻暂不可用：" + (self.comparison_error or "等待同一交易日完整数据"))
        tooltip.append(f"差额每 {self.config['turnover_compare_minutes']} 分钟更新，使用已完成分钟")
        self.volume_container.setToolTip("\n".join(tooltip))
        # 保留两行布局，成交额变长时扩展窗口，避免金额或指数被截断。
        self.main_layout.invalidate()
        width = max(450, self.main_layout.sizeHint().width(), self.width())
        self.setFixedWidth(width)

    def handle_error(self, error_msg):
        """处理错误"""
        logging.warning("行情显示暂不可用：%s", error_msg)
        self.status_label.setText("ERR")
        self.status_label.setStyleSheet("color: #ef4444;")
        self.quote_error = error_msg
        self.render_turnover()
        # 显示最后已知数据或保持原样

    def toggle_visibility(self):
        """切换显示/隐藏"""
        if self.is_hidden:
            self.show_window()
        else:
            self.hide_window()

    def hide_window(self):
        """隐藏窗口"""
        self.is_hidden = True
        self.hide()

    def show_window(self):
        """显示窗口"""
        self.is_hidden = False
        self.show()
        # 恢复保存的位置
        saved_pos = self.config.get('window_pos')
        if saved_pos:
            self.move(saved_pos[0], saved_pos[1])
        else:
            self.update_position()

    def show_settings(self):
        """显示设置面板"""
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config = dialog.get_config()
            # 保留窗口位置
            current_pos = self.config.get('window_pos')
            self.save_config()
            # 恢复位置（save_config可能覆盖）
            if current_pos:
                self.config['window_pos'] = current_pos
                self.save_config()

            # 重新创建UI
            self.create_index_widgets()

            # 重启定时器
            self.timer.stop()
            self.timer.start(self.config.get('refresh_interval', 30) * 1000)
            self.turnover_timer.start(self.config['turnover_compare_minutes'] * 60000)

            # 重新获取数据
            self.fetch_data()
            self.render_turnover()
            self.fetch_turnover()

    def tray_activated(self, reason):
        """托盘图标激活"""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_window()

    def mousePressEvent(self, event):
        """鼠标点击 - 左键开始拖拽，右键显示设置"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.globalPosition().toPoint()
            self._drag_window_pos = self.pos()
            self._is_dragging = False
        elif event.button() == Qt.MouseButton.RightButton:
            self.show_settings()

    def mouseMoveEvent(self, event):
        """鼠标移动 - 拖拽窗口"""
        if event.buttons() == Qt.MouseButton.LeftButton:
            current_pos = event.globalPosition().toPoint()
            delta = current_pos - self._drag_start_pos
            # 移动超过5px才认为是拖拽（区分点击和拖拽）
            if delta.manhattanLength() > 5 or self._is_dragging:
                self._is_dragging = True
                new_pos = self._drag_window_pos + delta
                self.move(new_pos)

    def mouseReleaseEvent(self, event):
        """鼠标释放 - 保存位置"""
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._is_dragging:
                # 没有拖拽，只是点击，刷新数据
                self.fetch_data()
            else:
                # 拖拽结束，保存位置到配置
                self.config['window_pos'] = [self.x(), self.y()]
                self.save_config()
                logging.debug("Position saved: %s, %s", self.x(), self.y())

    def enterEvent(self, event):
        """鼠标进入 - 显示tooltip效果"""
        # 可以在这里显示详细tooltip
        pass

    def leaveEvent(self, event):
        """鼠标离开"""
        pass

    def load_config(self):
        """加载配置"""
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = {**DEFAULT_CONFIG, **json.load(f)}
                    if config['turnover_compare_minutes'] not in COMPARE_INTERVALS:
                        config['turnover_compare_minutes'] = 1
                    return config
        except Exception:
            pass
        return DEFAULT_CONFIG.copy()

    def save_config(self):
        """保存配置"""
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.exception("保存配置失败")

    def quit_app(self):
        """退出应用"""
        self._closing = True
        self.timer.stop()
        self.turnover_timer.stop()
        self.hide()
        self.tray.hide()
        for worker in (self.fetcher, self.turnover_fetcher):
            if worker and worker.isRunning():
                worker.requestInterruption()
        self.finish_quit()

    def finish_quit(self):
        if self._closing and not any(
            worker and worker.isRunning() for worker in (self.fetcher, self.turnover_fetcher)
        ):
            logging.info("应用正常退出")
            QApplication.quit()

    def paintEvent(self, event):
        """绘制背景 - 模拟任务栏风格"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 半透明背景
        bg_color = QColor(243, 243, 243, int(255 * self.config.get('opacity', 0.95)))
        painter.fillRect(self.rect(), bg_color)

        # 顶部细线
        painter.setPen(QColor(255, 255, 255, 153))
        painter.drawLine(0, 0, self.width(), 0)


class TooltipWindow(QWidget):
    """Tooltip窗口 - 显示详细信息"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.ToolTip
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)

        self.setStyleSheet("""
            QWidget {
                background: rgba(255, 255, 255, 0.95);
                border: 1px solid rgba(0, 0, 0, 0.08);
                border-radius: 8px;
            }
            QLabel {
                font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
                font-size: 12px;
            }
        """)

        self.labels = []
        for _ in range(5):
            label = QLabel()
            label.setStyleSheet("color: #374151;")
            layout.addWidget(label)
            self.labels.append(label)

        self.hide()

    def update_data(self, data):
        """更新数据"""
        for i, (code, d) in enumerate(data.items()):
            if i >= len(self.labels):
                break
            short_name, full_name = INDEX_NAMES.get(code, (code, code))
            text = f"{full_name}  {d['current']:.2f}  {'+' if d['change'] >= 0 else ''}{d['change']:.2f}  {'+' if d['change_pct'] >= 0 else ''}{d['change_pct']:.2f}%"
            self.labels[i].setText(text)

        for j in range(i + 1, len(self.labels)):
            self.labels[j].setText("")

        self.adjustSize()


import sys
import os

# 单实例锁
import msvcrt
_instance_lock = None

def main():
    global _instance_lock

    log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers = [RotatingFileHandler(log_dir / "app.log", maxBytes=1_000_000,
                                   backupCount=2, encoding="utf-8")]
    if sys.stderr is not None and sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.excepthook = lambda kind, value, tb: logging.critical(
        "未处理异常", exc_info=(kind, value, tb))
    
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # 单实例检查
    lock_file = Path.home() / ".system-monitor" / "instance.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        _instance_lock = open(lock_file, 'a+b')
        _instance_lock.seek(0)
        msvcrt.locking(_instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
        _instance_lock.seek(0)
        _instance_lock.truncate()
        _instance_lock.write(str(os.getpid()).encode('ascii'))
        _instance_lock.flush()
    except (IOError, OSError):
        QMessageBox.warning(None, "System Monitor", "程序已在运行中。")
        sys.exit(0)

    # 设置应用信息
    app.setApplicationName("SystemMonitor")
    app.setApplicationDisplayName("System Monitor")

    window = MainWindow()
    window.show()
    logging.info("应用启动：pid=%s，成交额对比间隔=%s 分钟", os.getpid(),
                 window.config['turnover_compare_minutes'])

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
