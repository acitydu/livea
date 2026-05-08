"""
System Monitor - A lightweight desktop widget for monitoring system metrics.
Displays real-time data in a taskbar-integrated format.
"""

import sys
import json
import os
from pathlib import Path
from datetime import datetime

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
    "display_format": "full",
    "position": "taskbar_center_right",
    "hotkey": "`",
    "auto_hide": False,
    "opacity": 0.95,
    "font_size": 12,
    "show_on_hover": True,
}

# 昨日成交量缓存（亿元）
PREV_VOLUME_FILE = Path(__file__).parent.parent / "config" / "prev_volume.json"

# 分时成交额缓存（用于昨天此刻对比）
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
    """后台线程获取数据"""
    data_ready = pyqtSignal(dict)
    history_ready = pyqtSignal(dict)  # 历史成交量
    error_occurred = pyqtSignal(str)

    def __init__(self, indices, fetch_history=False):
        super().__init__()
        self.indices = indices
        self._running = True
        self.fetch_history = fetch_history

    def run(self):
        import urllib.request
        import urllib.error

        # 腾讯行情API（比新浪更稳定）
        # URL: http://qt.gtimg.cn/?q=s_sh000001,s_sz399001
        # 简版格式: v_s_code="flag~name~code~current~change~change%~volume~amount~..."
        # [3]=current, [4]=change, [5]=change%, [6]=volume(股), [7]=amount(万元)
        codes_param = ",".join([f"s_{c}" for c in self.indices])
        url = f"http://qt.gtimg.cn/?q={codes_param}"

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://finance.qq.com'
        }

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                data = response.read().decode('gbk')

            result = {}
            for line in data.strip().split('\n'):
                if not line.strip():
                    continue
                # 解析 v_s_sh000001="1~name~code~current~close~open~volume~...";
                # 格式: v_s_code（腾讯格式）
                if '=' in line:
                    code_part = line.split('=')[0]  # e.g. v_s_sh000001
                    parts_code = code_part.split('_')  # ['v', 's', 'sh000001']
                    raw_code = parts_code[-1] if len(parts_code) >= 3 else code_part  # sh000001
                    if raw_code in self.indices:
                        value_part = line.split('=')[1].strip().strip('";')
                        parts = value_part.split('~')
                        if len(parts) >= 8:
                            name = parts[1]
                            current = float(parts[3]) if parts[3] else 0
                            change = float(parts[4]) if parts[4] else 0  # 涨跌额
                            change_pct = float(parts[5]) if parts[5] else 0  # 涨跌幅
                            prev_close = current - change  # 昨收 = 当前价 - 涨跌额
                            # 简版格式: [3]=current, [4]=change, [5]=change%, [6]=volume(股), [7]=amount(万元)
                            vol = float(parts[6]) if len(parts) > 6 and parts[6] else 0  # 成交量(股)
                            amount_wan = float(parts[7]) if len(parts) > 7 and parts[7] else 0  # 成交额(万元)
                            amount_yi = amount_wan / 10000  # 转换为亿元

                            result[raw_code] = {
                                'name': name,
                                'current': current,
                                'prev_close': prev_close,
                                'change': change,
                                'change_pct': change_pct,
                                'volume': vol,
                                'amount_yi': amount_yi,
                            }

            self.data_ready.emit(result)

            # 获取历史成交量（仅上证和深证）
            if self.fetch_history:
                history = self.fetch_history_data()
                if history:
                    self.history_ready.emit(history)

        except Exception as e:
            import traceback
            print(f"[ERROR] Fetch failed: {e}")
            traceback.print_exc()
            self.error_occurred.emit(str(e))

    def fetch_history_data(self):
        """从腾讯K线API获取最近非今日的成交额（亿元）"""
        import urllib.request
        import json
        from datetime import datetime

        history = {}
        today = datetime.now().strftime('%Y-%m-%d')

        # 只获取上证和深证的历史数据
        for code in ['sh000001', 'sz399001']:
            if code not in self.indices:
                continue

            try:
                # 腾讯日K线API
                url = f"https://web.ifzq.gtimg.cn/appstock/app/kline/getKlineData?param={code},daily,,,5,qfq"
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Referer': 'https://finance.qq.com'
                }
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as response:
                    data = json.loads(response.read().decode('utf-8'))

                # 解析腾讯K线数据: data[data][qmqj][data] = [[date, open, close, high, low, volume, amount, ...], ...]
                qmqj = data.get('data', {}).get(code, {}).get('data', {})
                klines = qmqj.get('data', []) if isinstance(qmqj, dict) else (qmqj or [])

                # 找最近一个非今日且有成交额的数据
                for kline in klines:
                    if len(kline) >= 7:
                        date_str = kline[0]  # YYYY-MM-DD
                        amount = float(kline[6]) if kline[6] else 0  # 成交额(万元)
                        if date_str != today and amount > 0:
                            amount_yi = amount / 10000  # 万元转亿元
                            history[code] = amount_yi
                            break
            except Exception:
                pass

        return history

    def stop(self):
        self._running = False
        self.wait(1000)


class SettingsDialog(QDialog):
    """设置面板"""
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config.copy()
        self.setWindowTitle("显示设置")
        self.setFixedSize(380, 480)
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
        self.config['hotkey'] = self.hotkey_combo.currentText()
        self.config['indices'] = [
            code for code, cb in self.index_checkboxes.items() if cb.isChecked()
        ]
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

        # 隐藏状态
        self.is_hidden = False

        self.init_ui()
        self.init_timer()
        self.init_tray()
        self.init_hotkey()
        self.fetch_data(fetch_history=True)  # 首次获取历史成交量

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
            for i in reversed(range(layout.count())):
                item = layout.itemAt(i)
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

        self.vol_diff = QLabel("")
        self.vol_diff.setFont(self.font_volume)
        self.vol_diff.setStyleSheet("color: #6b7280;")
        self.vol_diff.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        vol_inner.addWidget(self.vol_diff)

        self.status_label = QLabel("")
        self.status_label.setFont(self.font_volume)
        self.status_label.setStyleSheet("color: #6b7280;")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.status_label.setFixedWidth(12)
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

        print(f"[DEBUG] Window position: x={x}, y={y}, size={self.width()}x{self.height()}, Screen: {screen_geo.width()}x{screen_geo.height()}")

    def init_timer(self):
        """初始化定时器"""
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.fetch_data)
        self.timer.start(self.config.get('refresh_interval', 30) * 1000)

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

    def fetch_data(self, fetch_history=False):
        """获取数据"""
        print("[DEBUG] Fetching data...")
        if self.fetcher and self.fetcher.isRunning():
            print("[DEBUG] Fetcher already running, skip")
            return

        self.fetcher = DataFetcher(self.config.get('indices', []), fetch_history=fetch_history)
        self.fetcher.data_ready.connect(self.update_display)
        self.fetcher.history_ready.connect(self.update_history)
        self.fetcher.error_occurred.connect(self.handle_error)
        self.fetcher.start()
        print("[DEBUG] Fetcher started")

    def update_history(self, history):
        """更新历史成交额"""
        print(f"[DEBUG] History data: {history}")
        # 计算总成交额并保存到_total
        if 'sh000001' in history and 'sz399001' in history:
            total_yi = history['sh000001'] + history['sz399001']
            self.save_prev_volume('_total', total_yi)
            print(f"[DEBUG] Saved _total prev amount: {total_yi:.2f}亿")
        elif 'sh000001' in history:
            self.save_prev_volume('_total', history['sh000001'])
        elif 'sz399001' in history:
            self.save_prev_volume('_total', history['sz399001'])

    def update_display(self, data):
        """更新显示"""
        print(f"[DEBUG] Data received: {len(data)} indices")
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

        # A股总成交额 = 上证 + 深证成交额（亿元）
        total_yi = 0
        if 'sh000001' in data:
            total_yi += data['sh000001'].get('amount_yi', 0)
        if 'sz399001' in data:
            total_yi += data['sz399001'].get('amount_yi', 0)

        self.vol_total.setText(f"{int(total_yi)}亿")

        # 计算与昨天同一时刻的差值
        prev_amount = self.get_yesterday_amount_at_same_time()
        
        if prev_amount > 0:
            diff = total_yi - prev_amount
            diff_sign = '+' if diff >= 0 else ''
            self.vol_diff.setText(f" {diff_sign}{int(diff)}")
        else:
            self.vol_diff.setText("")

        # 保存当前时刻的累计成交额到缓存（用于明天对比）
        now = datetime.now()
        if 9 <= now.hour < 16:  # 交易时间内
            cache = self.load_minute_cache()
            if 'amounts' not in cache:
                cache['amounts'] = {}
            cache['amounts'][now.strftime('%H:%M')] = total_yi
            self.save_minute_cache(cache)

    def load_prev_volume(self):
        """加载昨日成交量缓存（兼容旧版）"""
        try:
            if PREV_VOLUME_FILE.exists():
                with open(PREV_VOLUME_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def save_prev_volume(self, code, amount_yi):
        """保存成交量到缓存（兼容旧版）"""
        try:
            PREV_VOLUME_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = self.load_prev_volume()
            data[code] = amount_yi
            with open(PREV_VOLUME_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def load_amount_history(self):
        """加载历史成交额缓存（按时间点）"""
        try:
            if AMOUNT_HISTORY_FILE.exists():
                with open(AMOUNT_HISTORY_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def save_amount_history(self, history):
        """保存历史成交额缓存"""
        try:
            AMOUNT_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(AMOUNT_HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get_yesterday_amount_at_same_time(self):
        """获取昨天同一时刻的累计成交额（亿元）"""
        try:
            from datetime import datetime, timedelta
            
            now = datetime.now()
            time_key = now.strftime('%H:%M')  # 如 "14:30"
            
            # 加载昨天的分时缓存
            cache = self.load_minute_cache()
            
            # 找昨天同一时刻或最接近的数据
            if 'amounts' in cache:
                amounts = cache['amounts']
                # 找最接近当前时间的数据点
                closest_amount = 0
                closest_diff = 999999
                
                for time_str, amount in amounts.items():
                    # time_str 格式 "HH:MM"
                    h, m = map(int, time_str.split(':'))
                    curr_h, curr_m = now.hour, now.minute
                    diff = abs((curr_h * 60 + curr_m) - (h * 60 + m))
                    if diff < closest_diff:
                        closest_diff = diff
                        closest_amount = amount
                
                return closest_amount
            
            return 0
        except Exception as e:
            print(f"[DEBUG] get_yesterday_amount error: {e}")
            return 0
    
    def load_minute_cache(self):
        """加载上一交易日的分时成交额缓存"""
        try:
            if MINUTE_CACHE_FILE.exists():
                with open(MINUTE_CACHE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}
    
    def save_minute_cache(self, amounts):
        """保存分时成交额缓存"""
        try:
            MINUTE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            cache = {
                'date': datetime.now().strftime('%Y-%m-%d'),
                'amounts': amounts
            }
            with open(MINUTE_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def handle_error(self, error_msg):
        """处理错误"""
        print(f"数据获取错误: {error_msg}")
        self.status_label.setText("ERR")
        self.status_label.setStyleSheet("color: #ef4444;")
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

            # 重新获取数据
            self.fetch_data()

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
                print(f"[DEBUG] Position saved: {self.x()}, {self.y()}")

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
                    return {**DEFAULT_CONFIG, **json.load(f)}
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
            print(f"保存配置失败: {e}")

    def quit_app(self):
        """退出应用"""
        if self.fetcher and self.fetcher.isRunning():
            self.fetcher.stop()
        self.tray.hide()
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
    
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # 单实例检查
    lock_file = Path.home() / ".system-monitor" / "instance.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        _instance_lock = open(lock_file, 'w')
        msvcrt.locking(_instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
        _instance_lock.write(str(os.getpid()))
    except (IOError, OSError):
        QMessageBox.warning(None, "System Monitor", "程序已在运行中。")
        sys.exit(0)

    # 设置应用信息
    app.setApplicationName("SystemMonitor")
    app.setApplicationDisplayName("System Monitor")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
