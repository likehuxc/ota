from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from PySide6.QtCore import QTimer, Qt, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from d7_pmu_iap_tool.can.can_frame import CanFrame


MAX_VISIBLE_FRAMES = 2_000


@dataclass(frozen=True)
class CanDebugConfig:
    dll_path: str
    device_type: int
    channel: int
    arbitration_baudrate: int
    data_baudrate: int


class CanFrameParser(Protocol):
    def __call__(self, direction: str, frame: CanFrame) -> str | None: ...


@dataclass(frozen=True)
class DebugFrameRecord:
    sequence: int
    timestamp: str
    direction: str
    frame: CanFrame
    parsed: str = ""


LIGHT_MODES = (
    ("熄灭", 0),
    ("常亮", 1),
    ("闪烁后熄灭", 2),
    ("呼吸", 3),
    ("闪烁后常亮", 4),
    ("自定义 RGB", 5),
    ("彩虹渐变", 6),
    ("紧急红灯", 7),
    ("向左跑马", 8),
    ("向右流水", 9),
    ("待机呼吸", 10),
    ("关机", 11),
)

LIGHT_COLORS = (
    ("黑色", 0, "#000000"),
    ("白色", 1, "#ffffff"),
    ("红色", 2, "#ff0000"),
    ("绿色", 3, "#00ff00"),
    ("蓝色", 4, "#0000ff"),
    ("橙色", 5, "#ffa500"),
    ("黄色", 6, "#ffff00"),
    ("天蓝", 7, "#0066ff"),
)


def xor_checksum(data: bytes) -> int:
    checksum = 0
    for value in data:
        checksum ^= value
    return checksum


def build_battery_frame(can_id: int, subcommand: int, value: int, pack_index: int = 0) -> CanFrame:
    if not 0x41 <= can_id <= 0x4F:
        raise ValueError("电池 CAN ID 必须在 0x41 到 0x4F 之间")
    body = bytes((
        0x82,
        subcommand & 0xFF,
        (value >> 24) & 0xFF,
        (value >> 16) & 0xFF,
        (value >> 8) & 0xFF,
        value & 0xFF,
        ((pack_index & 0x0F) << 4) | 0x01,
    ))
    return CanFrame(id=can_id, data=body + bytes((xor_checksum(body),)))


def build_light_frame(
    can_id: int,
    strip_id: int,
    color_id: int,
    mode: int,
    count: int,
    cycle_ms: int,
    rgb: tuple[int, int, int],
) -> CanFrame:
    if not 200 <= strip_id <= 203:
        raise ValueError("灯带 ID 必须在 200 到 203 之间")
    if not 0 <= mode < 12:
        raise ValueError("灯效模式无效")
    if mode == 5:
        params = rgb
    else:
        params = (count & 0xFF, (cycle_ms >> 8) & 0xFF, cycle_ms & 0xFF)
    body = bytes((0x90, strip_id, color_id & 0xFF, mode, *params))
    return CanFrame(id=can_id, data=body + bytes((xor_checksum(body),)))


class CanDebugPage(QWidget):
    """Classic CAN bench page for battery simulation, light control, and capture."""

    def __init__(
        self,
        send_frame: Callable[[CanFrame], None],
        open_can: Callable[[CanDebugConfig], None],
        close_can: Callable[[], None],
        default_canfd_dll: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._send_frame_callback = send_frame
        self._open_can_callback = open_can
        self._close_can_callback = close_can
        self._canfd_dll_path = default_canfd_dll
        self._connected = False
        self._display_paused = False
        self._sequence = 0
        self._records: list[DebugFrameRecord] = []
        self._known_ids: set[int] = set()
        self._parser: CanFrameParser | None = None
        self._custom_color = QColor("#00a878")
        self._compact_layout: bool | None = None

        self.battery_timer = QTimer(self)
        self.battery_timer.timeout.connect(self._send_battery_cycle)

        self._build_ui()
        self._connect_signals()
        self.set_connected(False, "CAN 未连接")
        self._update_parse_status()

    def _build_ui(self) -> None:
        title = QLabel("CAN 总线调试台")
        title.setObjectName("CanDebugTitle")
        subtitle = QLabel("电池节点模拟 · Orin 灯控 · 实时帧观测")
        subtitle.setObjectName("MutedLabel")
        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(2)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        self.connection_badge = QLabel("CAN 未连接")
        self.connection_badge.setObjectName("CanDebugConnectionBadge")
        self.connection_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.channel_input = QComboBox()
        self.channel_input.addItem("CH0", 0)
        self.channel_input.addItem("CH1", 1)
        self.baudrate_input = QComboBox()
        for baudrate in (1_000_000, 500_000, 250_000, 125_000, 100_000):
            self.baudrate_input.addItem(f"{baudrate:,} bps", baudrate)
        self.data_baudrate_input = QComboBox()
        for baudrate in (5_000_000, 4_000_000, 2_000_000, 1_000_000):
            self.data_baudrate_input.addItem(f"{baudrate / 1_000_000:g} Mbps", baudrate)
        self.dll_button = QPushButton("CAN FD DLL")
        self.dll_button.setObjectName("SecondaryButton")
        self.dll_button.setToolTip(self._canfd_dll_path or "请选择 ControlCANFD.dll")
        self.open_button = QPushButton("打开 CAN")
        self.close_button = QPushButton("关闭设备")
        self.close_button.setObjectName("DangerButton")

        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.addLayout(title_box)
        heading.addStretch(1)
        heading.addWidget(self.connection_badge)
        config_grid = QGridLayout()
        config_grid.setContentsMargins(0, 0, 0, 0)
        config_grid.setHorizontalSpacing(10)
        config_grid.setVerticalSpacing(8)
        config_grid.addWidget(QLabel("设备"), 0, 0)
        config_grid.addWidget(QLabel("USBCANFD-200U · Index 0"), 0, 1, 1, 3)
        config_grid.addWidget(QLabel("通道"), 1, 0)
        config_grid.addWidget(self.channel_input, 1, 1)
        config_grid.addWidget(QLabel("仲裁域"), 1, 2)
        config_grid.addWidget(self.baudrate_input, 1, 3)
        config_grid.addWidget(QLabel("数据域"), 2, 0)
        config_grid.addWidget(self.data_baudrate_input, 2, 1)
        config_actions = QHBoxLayout()
        config_actions.setContentsMargins(0, 0, 0, 0)
        config_actions.addWidget(self.dll_button)
        config_actions.addWidget(self.open_button)
        config_actions.addWidget(self.close_button)
        config_actions.addStretch(1)
        config_grid.addLayout(config_actions, 2, 2, 1, 2)
        config_grid.setColumnStretch(1, 1)
        config_grid.setColumnStretch(3, 1)
        connection = QVBoxLayout()
        connection.setContentsMargins(18, 12, 18, 12)
        connection.setSpacing(10)
        connection.addLayout(heading)
        connection.addLayout(config_grid)
        connection_bar = QWidget()
        connection_bar.setObjectName("CanDebugConnectionBar")
        connection_bar.setLayout(connection)

        self.battery_id_input = QComboBox()
        self.battery_id_input.addItem("电池 1 · 0x41", (0x41,))
        self.battery_id_input.addItem("电池 2 · 0x42", (0x42,))
        self.battery_id_input.addItem("两块电池", (0x41, 0x42))
        self.battery_interval_input = QSpinBox()
        self.battery_interval_input.setRange(100, 60_000)
        self.battery_interval_input.setSingleStep(100)
        self.battery_interval_input.setValue(1_000)
        self.battery_interval_input.setSuffix(" ms")
        self.battery_voltage_input = QDoubleSpinBox()
        self.battery_voltage_input.setRange(0.0, 100.0)
        self.battery_voltage_input.setDecimals(2)
        self.battery_voltage_input.setSingleStep(0.1)
        self.battery_voltage_input.setValue(52.0)
        self.battery_voltage_input.setSuffix(" V")
        self.battery_voltage_input.setMaximumWidth(105)
        self.battery_current_input = QDoubleSpinBox()
        self.battery_current_input.setRange(-100.0, 100.0)
        self.battery_current_input.setDecimals(2)
        self.battery_current_input.setSingleStep(0.1)
        self.battery_current_input.setValue(-1.0)
        self.battery_current_input.setSuffix(" A")
        self.battery_current_input.setMaximumWidth(105)
        self.battery_capacity_input = QSpinBox()
        self.battery_capacity_input.setRange(0, 100)
        self.battery_capacity_input.setValue(80)
        self.battery_capacity_input.setSuffix(" %")
        self.battery_capacity_input.setMaximumWidth(90)
        self.battery_capacity_input.setToolTip("作为电池 SOC（子命令 0x81）发送")
        self.battery_start_button = QPushButton("开始周期发送")
        self.battery_stop_button = QPushButton("停止")
        self.battery_stop_button.setObjectName("SecondaryButton")
        self.battery_status = QLabel("已停止")
        self.battery_status.setObjectName("MutedLabel")
        battery_grid = QGridLayout()
        battery_grid.setContentsMargins(16, 18, 16, 16)
        battery_grid.setHorizontalSpacing(10)
        battery_grid.setVerticalSpacing(10)
        battery_grid.addWidget(QLabel("模拟节点"), 0, 0)
        battery_grid.addWidget(self.battery_id_input, 0, 1)
        battery_grid.addWidget(QLabel("上报周期"), 1, 0)
        battery_grid.addWidget(self.battery_interval_input, 1, 1)
        battery_grid.addWidget(QLabel("电压"), 2, 0)
        battery_grid.addWidget(self.battery_voltage_input, 2, 1)
        battery_grid.addWidget(QLabel("电流"), 3, 0)
        battery_grid.addWidget(self.battery_current_input, 3, 1)
        battery_grid.addWidget(QLabel("容量/SOC"), 4, 0)
        battery_grid.addWidget(self.battery_capacity_input, 4, 1)
        battery_actions = QHBoxLayout()
        battery_actions.addWidget(self.battery_start_button)
        battery_actions.addWidget(self.battery_stop_button)
        battery_actions.addStretch(1)
        battery_actions.addWidget(self.battery_status)
        battery_grid.addLayout(battery_actions, 5, 0, 1, 2)
        battery_grid.setColumnStretch(1, 1)
        battery_card = self._card("电池接收测试", battery_grid)

        self.light_can_id_input = QLineEdit("0x08")
        self.light_can_id_input.setMaximumWidth(100)
        self.light_strip_input = QComboBox()
        for strip_id in range(200, 204):
            self.light_strip_input.addItem(f"灯带 {strip_id}", (strip_id,))
        self.light_strip_input.addItem("全部灯带", tuple(range(200, 204)))
        self.light_mode_input = QComboBox()
        for name, value in LIGHT_MODES:
            self.light_mode_input.addItem(name, value)
        self.light_color_input = QComboBox()
        for name, color_id, color in LIGHT_COLORS:
            self.light_color_input.addItem(name, (color_id, color))
        self.light_color_input.setCurrentIndex(2)
        self.custom_color_button = QPushButton("自定义颜色")
        self.custom_color_button.setObjectName("ColorSwatchButton")
        self.light_count_input = QSpinBox()
        self.light_count_input.setRange(0, 255)
        self.light_cycle_input = QSpinBox()
        self.light_cycle_input.setRange(0, 65_535)
        self.light_cycle_input.setSingleStep(100)
        self.light_cycle_input.setValue(500)
        self.light_cycle_input.setSuffix(" ms")
        self.light_send_button = QPushButton("发送灯控")
        light_grid = QGridLayout()
        light_grid.setContentsMargins(16, 18, 16, 16)
        light_grid.setHorizontalSpacing(10)
        light_grid.setVerticalSpacing(10)
        light_grid.addWidget(QLabel("目标 ID"), 0, 0)
        light_grid.addWidget(self.light_can_id_input, 0, 1)
        light_grid.addWidget(QLabel("灯带"), 0, 2)
        light_grid.addWidget(self.light_strip_input, 0, 3)
        light_grid.addWidget(QLabel("模式"), 1, 0)
        light_grid.addWidget(self.light_mode_input, 1, 1)
        light_grid.addWidget(QLabel("颜色"), 1, 2)
        light_grid.addWidget(self.light_color_input, 1, 3)
        light_grid.addWidget(QLabel("次数"), 2, 0)
        light_grid.addWidget(self.light_count_input, 2, 1)
        light_grid.addWidget(QLabel("周期"), 2, 2)
        light_grid.addWidget(self.light_cycle_input, 2, 3)
        light_actions = QHBoxLayout()
        light_actions.addWidget(self.custom_color_button)
        light_actions.addStretch(1)
        light_actions.addWidget(self.light_send_button)
        light_grid.addLayout(light_actions, 3, 0, 1, 4)
        light_grid.setColumnStretch(1, 1)
        light_grid.setColumnStretch(3, 1)
        light_card = self._card("Orin 灯控模拟", light_grid)

        self.battery_card = battery_card
        self.light_card = light_card
        self.controls_layout = QGridLayout()
        self.controls_layout.setContentsMargins(0, 0, 0, 0)
        self.controls_layout.setSpacing(14)
        self.controls_layout.addWidget(self.battery_card, 0, 0)
        self.controls_layout.addWidget(self.light_card, 0, 1)
        self.controls_layout.setColumnStretch(0, 4)
        self.controls_layout.setColumnStretch(1, 7)

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("过滤 CAN ID，例如 0x41, 0x08")
        self.direction_input = QComboBox()
        self.direction_input.addItem("全部方向", "ALL")
        self.direction_input.addItem("仅 RX", "RX")
        self.direction_input.addItem("仅 TX", "TX")
        self.apply_filter_button = QPushButton("应用过滤")
        self.apply_filter_button.setObjectName("SecondaryButton")
        self.pause_button = QPushButton("暂停显示")
        self.pause_button.setObjectName("SecondaryButton")
        self.clear_button = QPushButton("清空")
        self.clear_button.setObjectName("SecondaryButton")
        self.save_button = QPushButton("导出 CSV")
        self.save_button.setObjectName("SecondaryButton")
        self.parse_input = QCheckBox("开启解析")
        self.parse_status = QLabel()
        self.parse_status.setObjectName("MutedLabel")
        self.frame_status = QLabel("显示 0 / 0 帧")
        self.frame_status.setObjectName("MutedLabel")

        toolbar = QGridLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setHorizontalSpacing(8)
        toolbar.setVerticalSpacing(8)
        toolbar.addWidget(self.filter_input, 0, 0)
        toolbar.addWidget(self.direction_input, 0, 1)
        toolbar.addWidget(self.apply_filter_button, 0, 2)
        toolbar.addWidget(self.parse_input, 1, 0)
        toolbar.addWidget(self.parse_status, 1, 1)
        toolbar.addWidget(self.frame_status, 1, 2)
        toolbar.addWidget(self.pause_button, 1, 3)
        toolbar.addWidget(self.clear_button, 1, 4)
        toolbar.addWidget(self.save_button, 1, 5)
        toolbar.setColumnStretch(0, 1)

        self.frame_table = QTableWidget(0, 7)
        self.frame_table.setHorizontalHeaderLabels(["序号", "时间", "方向", "CAN ID", "Len", "Data", "解析"])
        self.frame_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.frame_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.frame_table.setAlternatingRowColors(True)
        self.frame_table.verticalHeader().setVisible(False)
        header = self.frame_table.horizontalHeader()
        for column in (0, 1, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(6, 220)
        self.frame_table.setColumnHidden(6, True)
        self.frame_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.frame_table.setMinimumHeight(180)

        monitor_layout = QVBoxLayout()
        monitor_layout.setContentsMargins(16, 18, 16, 16)
        monitor_layout.setSpacing(10)
        monitor_layout.addLayout(toolbar)
        monitor_layout.addWidget(self.frame_table, 1)
        monitor = self._card("实时 CAN 数据", monitor_layout)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 22)
        layout.setSpacing(14)
        layout.addWidget(connection_bar)
        layout.addLayout(self.controls_layout)
        layout.addWidget(monitor, 1)

        self._update_color_swatch()
        self._update_light_inputs()

    def _connect_signals(self) -> None:
        self.open_button.clicked.connect(self.open_can)
        self.dll_button.clicked.connect(self.choose_canfd_dll)
        self.close_button.clicked.connect(self._close_can_callback)
        self.battery_start_button.clicked.connect(self.start_battery_simulation)
        self.battery_stop_button.clicked.connect(self.stop_battery_simulation)
        self.battery_interval_input.valueChanged.connect(self.battery_timer.setInterval)
        self.light_send_button.clicked.connect(self.send_light_command)
        self.custom_color_button.clicked.connect(self.choose_custom_color)
        self.light_mode_input.currentIndexChanged.connect(self._update_light_inputs)
        self.apply_filter_button.clicked.connect(self.render_records)
        self.filter_input.returnPressed.connect(self.render_records)
        self.direction_input.currentIndexChanged.connect(self.render_records)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.clear_button.clicked.connect(self.clear_records)
        self.save_button.clicked.connect(self.save_records)
        self.parse_input.toggled.connect(self._parse_toggled)

    @staticmethod
    def _card(title: str, layout) -> QGroupBox:
        card = QGroupBox(title)
        mark = QLabel()
        mark.setObjectName("CardMark")
        title_label = QLabel(title)
        title_label.setObjectName("CardTitle")
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(16, 12, 16, 10)
        header_layout.setSpacing(9)
        header_layout.addWidget(mark)
        header_layout.addWidget(title_label)
        header_layout.addStretch(1)
        header = QWidget()
        header.setObjectName("CardHeader")
        header.setLayout(header_layout)
        body = QWidget()
        body.setLayout(layout)
        card_layout = QVBoxLayout()
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        card_layout.addWidget(header)
        card_layout.addWidget(body, 1)
        card.setLayout(card_layout)
        return card

    @staticmethod
    def _add_field(layout: QGridLayout, row: int, column: int, label: str, widget: QWidget) -> None:
        field_label = QLabel(label)
        field_label.setObjectName("MutedLabel")
        layout.addWidget(field_label, row, column)
        layout.addWidget(widget, row + 1, column)

    def set_parser(self, parser: CanFrameParser | None) -> None:
        self._parser = parser
        self._update_parse_status()

    def set_connected(self, connected: bool, message: str) -> None:
        self._connected = connected
        self.connection_badge.setText(message)
        self.connection_badge.setProperty("connected", connected)
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        self.open_button.setEnabled(not connected)
        self.close_button.setEnabled(connected)
        self.battery_start_button.setEnabled(connected and not self.battery_timer.isActive())
        self.battery_stop_button.setEnabled(connected and self.battery_timer.isActive())
        self.light_send_button.setEnabled(connected)
        for widget in (self.channel_input, self.baudrate_input, self.data_baudrate_input, self.dll_button):
            widget.setEnabled(not connected)
        if not connected:
            self.stop_battery_simulation()

    @Slot()
    def open_can(self) -> None:
        try:
            if not self._canfd_dll_path:
                raise RuntimeError("请选择 64 位 ControlCANFD.dll")
            self._open_can_callback(CanDebugConfig(
                dll_path=self._canfd_dll_path,
                device_type=41,
                channel=int(self.channel_input.currentData()),
                arbitration_baudrate=int(self.baudrate_input.currentData()),
                data_baudrate=int(self.data_baudrate_input.currentData()),
            ))
        except Exception as exc:
            self._show_error(str(exc))

    @Slot()
    def choose_canfd_dll(self) -> None:
        start_path = str(Path(self._canfd_dll_path).parent) if self._canfd_dll_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 64 位 ControlCANFD.dll",
            start_path,
            "ControlCANFD.dll (ControlCANFD.dll);;DLL (*.dll)",
        )
        if path:
            self._canfd_dll_path = path
            self.dll_button.setToolTip(path)

    @Slot()
    def start_battery_simulation(self) -> None:
        if not self._connected:
            self._show_error("请先打开 CAN 设备")
            return
        self.battery_timer.setInterval(self.battery_interval_input.value())
        self.battery_timer.start()
        self._send_battery_cycle()
        self.battery_status.setText(f"运行中 · {self.battery_interval_input.value()} ms")
        self.battery_start_button.setEnabled(False)
        self.battery_stop_button.setEnabled(True)

    @Slot()
    def stop_battery_simulation(self) -> None:
        self.battery_timer.stop()
        if hasattr(self, "battery_status"):
            self.battery_status.setText("已停止")
            self.battery_start_button.setEnabled(self._connected)
            self.battery_stop_button.setEnabled(False)

    @Slot()
    def _send_battery_cycle(self) -> None:
        try:
            frames: list[CanFrame] = []
            voltage_mv = round(self.battery_voltage_input.value() * 1_000)
            current_ma = round(self.battery_current_input.value() * 1_000)
            capacity_percent = self.battery_capacity_input.value()
            for can_id in self.battery_id_input.currentData():
                frames.extend((
                    build_battery_frame(can_id, 0x02, voltage_mv),
                    build_battery_frame(can_id, 0x0E, current_ma & 0xFFFFFFFF),
                    build_battery_frame(can_id, 0x81, capacity_percent),
                ))
            for frame in frames:
                self._send_frame_callback(frame)
        except Exception as exc:
            self.stop_battery_simulation()
            self._show_error(f"电池周期发送已停止：{exc}")

    @Slot()
    def send_light_command(self) -> None:
        try:
            can_id = int(self.light_can_id_input.text().strip(), 0)
            color_id, _ = self.light_color_input.currentData()
            rgb = (self._custom_color.red(), self._custom_color.green(), self._custom_color.blue())
            for strip_id in self.light_strip_input.currentData():
                self._send_frame_callback(build_light_frame(
                    can_id=can_id,
                    strip_id=strip_id,
                    color_id=color_id,
                    mode=int(self.light_mode_input.currentData()),
                    count=self.light_count_input.value(),
                    cycle_ms=self.light_cycle_input.value(),
                    rgb=rgb,
                ))
        except Exception as exc:
            self._show_error(str(exc))

    @Slot()
    def choose_custom_color(self) -> None:
        color = QColorDialog.getColor(self._custom_color, self, "选择 RGB 颜色")
        if color.isValid():
            self._custom_color = color
            self._update_color_swatch()
            rgb_index = self.light_mode_input.findData(5)
            if rgb_index >= 0:
                self.light_mode_input.setCurrentIndex(rgb_index)

    def _update_color_swatch(self) -> None:
        self.custom_color_button.setText(self._custom_color.name().upper())
        foreground = "#ffffff" if self._custom_color.lightness() < 140 else "#111827"
        self.custom_color_button.setStyleSheet(
            f"background: {self._custom_color.name()}; color: {foreground}; border-color: #64748b;"
        )

    @Slot()
    def _update_light_inputs(self) -> None:
        rgb_mode = self.light_mode_input.currentData() == 5
        self.custom_color_button.setEnabled(rgb_mode)
        self.light_color_input.setEnabled(not rgb_mode)
        self.light_count_input.setEnabled(not rgb_mode)
        self.light_cycle_input.setEnabled(not rgb_mode)

    def handle_frame(self, direction: str, frame: CanFrame) -> None:
        parsed = ""
        if self.parse_input.isChecked() and self._parser is not None:
            try:
                parsed = self._parser(direction, frame) or ""
            except Exception as exc:
                parsed = f"解析失败：{exc}"
        self._sequence += 1
        record = DebugFrameRecord(
            sequence=self._sequence,
            timestamp=datetime.now().strftime("%H:%M:%S.%f")[:-3],
            direction=direction,
            frame=frame,
            parsed=parsed,
        )
        self._records.append(record)
        self._known_ids.add(frame.id)
        if not self._display_paused:
            try:
                visible = self._record_passes_filter(record)
            except ValueError:
                visible = False
            if visible:
                self._add_row(record)
                while self.frame_table.rowCount() > MAX_VISIBLE_FRAMES:
                    self.frame_table.removeRow(0)
        self._update_frame_status()

    def _filter_ids(self) -> set[int]:
        text = self.filter_input.text().replace(",", " ").strip()
        if not text:
            return set()
        try:
            return {int(token, 0) if token.lower().startswith("0x") else int(token, 16) for token in text.split()}
        except ValueError as exc:
            raise ValueError("CAN ID 过滤格式无效，请使用 0x41, 0x08") from exc

    def _record_passes_filter(self, record: DebugFrameRecord) -> bool:
        ids = self._filter_ids()
        direction = self.direction_input.currentData()
        return (not ids or record.frame.id in ids) and (direction == "ALL" or record.direction == direction)

    @Slot()
    def render_records(self) -> None:
        try:
            visible = [record for record in self._records if self._record_passes_filter(record)]
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self.frame_table.setRowCount(0)
        for record in visible[-MAX_VISIBLE_FRAMES:]:
            self._add_row(record, scroll=False)
        self._update_frame_status(len(visible))
        self.frame_table.scrollToBottom()

    def _add_row(self, record: DebugFrameRecord, scroll: bool = True) -> None:
        row = self.frame_table.rowCount()
        self.frame_table.insertRow(row)
        values = (
            str(record.sequence),
            record.timestamp,
            record.direction,
            f"0x{record.frame.id:X}",
            str(record.frame.dlc),
            bytes(record.frame.data[: record.frame.dlc]).hex(" ").upper(),
            record.parsed,
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column in (0, 2, 3, 4):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if column == 2:
                item.setForeground(Qt.GlobalColor.blue if record.direction == "RX" else Qt.GlobalColor.darkYellow)
            self.frame_table.setItem(row, column, item)
        if scroll:
            self.frame_table.scrollToBottom()

    @Slot()
    def toggle_pause(self) -> None:
        self._display_paused = not self._display_paused
        self.pause_button.setText("继续显示" if self._display_paused else "暂停显示")
        if not self._display_paused:
            self.render_records()
        else:
            self._update_frame_status()

    @Slot()
    def clear_records(self) -> None:
        self._records.clear()
        self._sequence = 0
        self.frame_table.setRowCount(0)
        self._update_frame_status(0)

    @Slot()
    def save_records(self) -> None:
        default_name = f"can_debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "导出 CAN 数据", default_name, "CSV (*.csv)")
        if not path:
            return
        try:
            self._save_records(path)
        except Exception as exc:
            self._show_error(str(exc))

    def _save_records(self, path: str | Path) -> int:
        with Path(path).open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.writer(output)
            writer.writerow(("sequence", "time", "direction", "can_id", "len", "data", "parsed"))
            for record in self._records:
                writer.writerow((
                    record.sequence,
                    record.timestamp,
                    record.direction,
                    f"0x{record.frame.id:X}",
                    record.frame.dlc,
                    bytes(record.frame.data[: record.frame.dlc]).hex(" ").upper(),
                    record.parsed,
                ))
        return len(self._records)

    @Slot(bool)
    def _parse_toggled(self, _enabled: bool) -> None:
        self._update_parse_status()
        self.render_records()

    def _update_parse_status(self) -> None:
        self.frame_table.setColumnHidden(6, not self.parse_input.isChecked())
        if not self.parse_input.isChecked():
            self.parse_status.setText("")
        elif self._parser is None:
            self.parse_status.setText("暂无解析器")
        else:
            self.parse_status.setText("解析器已接入")

    def _update_frame_status(self, visible_count: int | None = None) -> None:
        if visible_count is None:
            visible_count = self.frame_table.rowCount()
        prefix = "暂停" if self._display_paused else "显示"
        self.frame_status.setText(f"{prefix} {visible_count} / {len(self._records)} 帧")

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        compact = event.size().width() < 980
        if compact != self._compact_layout:
            self._compact_layout = compact
            if compact:
                self.controls_layout.addWidget(self.battery_card, 0, 0, 1, 2)
                self.controls_layout.addWidget(self.light_card, 1, 0, 1, 2)
            else:
                self.controls_layout.addWidget(self.battery_card, 0, 0)
                self.controls_layout.addWidget(self.light_card, 0, 1)
        super().resizeEvent(event)

    @staticmethod
    def _show_error(message: str) -> None:
        QMessageBox.warning(None, "CAN 调试", message)
