from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
import time
from typing import Callable

from PySide6.QtCore import QTimer, Qt, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from d7_pmu_iap_tool.can.can_frame import CanFrame
from d7_pmu_iap_tool.motor_protocol import (
    MotorFeedback,
    decode_human_state,
    decode_motor_feedback,
    disable_frame,
    enable_frame,
    fault_reset_frame,
    position_command_frame,
    position_rad_to_raw,
    read_human_state_frame,
    read_motor_state_frame,
    set_control_source_frame,
)


@dataclass(frozen=True)
class MotorCanFdConfig:
    device_type: int
    device_index: int
    channel: int
    arbitration_baudrate: int
    data_baudrate: int


class MotorTestPage(QWidget):
    """Standalone motor bench-test page using the application's CAN driver."""

    def __init__(
        self,
        send_frame: Callable[[CanFrame], None],
        open_can_fd: Callable[[MotorCanFdConfig], None],
        close_can: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._send_frame_callback = send_frame
        self._open_can_fd_callback = open_can_fd
        self._close_can_callback = close_can
        self._connected = False
        self._holding = False
        self._latest_feedback: MotorFeedback | None = None
        self._last_feedback_monotonic: float | None = None
        self._hold_started_at: float | None = None
        self._hold_token = 0
        self._tx_count = 0
        self._first_position: float | None = None
        self._min_position: float | None = None
        self._max_position: float | None = None
        self._max_abs_error = 0.0
        self._csv_file = None
        self._csv_writer = None
        self._csv_rows_since_flush = 0

        self.position_timer = QTimer(self)
        self.position_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.position_timer.timeout.connect(self._send_position_tick)
        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(500)
        self.monitor_timer.timeout.connect(self._request_monitor_state)
        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(250)
        self.ui_timer.timeout.connect(self._refresh_live_metrics)
        self.ui_timer.start()

        self._build_ui()
        self._connect_signals()
        self.set_connected(False, "CAN FD 未连接")
        self._update_target_preview()
        self._update_plan_label()

    def _build_ui(self) -> None:
        self.connection_badge = QLabel("CAN FD 未连接")
        self.connection_badge.setObjectName("MotorConnectionBadge")
        self.connection_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        intro_title = QLabel("电机台架测试")
        intro_title.setObjectName("MotorHeroTitle")
        intro_text = QLabel("单电机供电 · 周期位置保持 · 长时间漂移观测")
        intro_text.setObjectName("MotorHeroText")
        intro = QVBoxLayout()
        intro.setContentsMargins(0, 0, 0, 0)
        intro.setSpacing(3)
        intro.addWidget(intro_title)
        intro.addWidget(intro_text)
        hero_layout = QHBoxLayout()
        hero_layout.setContentsMargins(20, 16, 20, 16)
        hero_layout.addLayout(intro)
        hero_layout.addStretch(1)
        hero_layout.addWidget(self.connection_badge)
        hero = QWidget()
        hero.setObjectName("MotorHero")
        hero.setLayout(hero_layout)

        self.device_type_input = QSpinBox()
        self.device_type_input.setRange(0, 255)
        self.device_type_input.setValue(41)
        self.device_type_input.setToolTip("ZLG USBCANFD-200U 通常为 41")
        self.device_index_input = QSpinBox()
        self.device_index_input.setRange(0, 15)
        self.channel_input = QComboBox()
        self.channel_input.addItem("CH0", 0)
        self.channel_input.addItem("CH1", 1)
        self.arbitration_baudrate_input = QComboBox()
        self.data_baudrate_input = QComboBox()
        for baudrate in (1_000_000, 500_000, 250_000):
            self.arbitration_baudrate_input.addItem(f"{baudrate / 1_000_000:g} Mbps", baudrate)
        for baudrate in (5_000_000, 4_000_000, 2_000_000, 1_000_000):
            self.data_baudrate_input.addItem(f"{baudrate / 1_000_000:g} Mbps", baudrate)
        self.open_fd_button = QPushButton("打开 CAN FD")
        self.close_fd_button = QPushButton("关闭设备")
        self.close_fd_button.setObjectName("DangerButton")
        conn_grid = QGridLayout()
        conn_grid.setContentsMargins(16, 18, 16, 16)
        conn_grid.setHorizontalSpacing(12)
        conn_grid.setVerticalSpacing(10)
        self._add_field(conn_grid, 0, 0, "设备类型", self.device_type_input)
        self._add_field(conn_grid, 0, 1, "设备索引", self.device_index_input)
        self._add_field(conn_grid, 0, 2, "通道", self.channel_input)
        self._add_field(conn_grid, 2, 0, "仲裁域", self.arbitration_baudrate_input)
        self._add_field(conn_grid, 2, 1, "数据域", self.data_baudrate_input)
        conn_actions = QHBoxLayout()
        conn_actions.addWidget(self.open_fd_button)
        conn_actions.addWidget(self.close_fd_button)
        conn_actions.addStretch(1)
        conn_grid.addLayout(conn_actions, 4, 0, 1, 3)
        connection_group = self._card("CAN FD 连接", conn_grid)

        self.device_id_input = QLineEdit("0x01")
        self.broadcast_id_input = QLineEdit("0x300")
        self.target_position_input = QDoubleSpinBox()
        self.target_position_input.setRange(-5.0, 5.0)
        self.target_position_input.setDecimals(4)
        self.target_position_input.setSingleStep(0.001)
        self.target_position_input.setValue(0.0)
        self.use_feedback_on_start = QCheckBox("启动时采用当前反馈位置")
        self.use_feedback_on_start.setChecked(True)
        self.frequency_input = QComboBox()
        for frequency in (50, 100, 500):
            self.frequency_input.addItem(f"{frequency} Hz  ({1000 // frequency} ms)", frequency)
        self.duration_input = QDoubleSpinBox()
        self.duration_input.setRange(0.0, 168.0)
        self.duration_input.setDecimals(1)
        self.duration_input.setSingleStep(1.0)
        self.duration_input.setValue(24.0)
        self.duration_input.setSuffix(" 小时")
        self.raw_target_label = QLabel("0x8000")
        self.raw_target_label.setObjectName("MotorMonoValue")
        self.plan_label = QLabel()
        self.plan_label.setObjectName("MutedLabel")
        self.plan_label.setWordWrap(True)
        control_grid = QGridLayout()
        control_grid.setContentsMargins(16, 18, 16, 16)
        control_grid.setHorizontalSpacing(12)
        control_grid.setVerticalSpacing(10)
        self._add_field(control_grid, 0, 0, "电机 ID", self.device_id_input)
        self._add_field(control_grid, 0, 1, "广播 CAN ID", self.broadcast_id_input)
        self._add_field(control_grid, 2, 0, "目标位置 (rad)", self.target_position_input)
        self._add_field(control_grid, 2, 1, "目标原始值", self.raw_target_label)
        self._add_field(control_grid, 4, 0, "发送频率", self.frequency_input)
        self._add_field(control_grid, 4, 1, "测试时长", self.duration_input)
        control_grid.addWidget(self.use_feedback_on_start, 6, 0, 1, 2)
        control_grid.addWidget(self.plan_label, 7, 0, 1, 2)
        control_group = self._card("位置保持参数", control_grid)

        self.read_state_button = QPushButton("读取电机状态")
        self.read_state_button.setObjectName("SecondaryButton")
        self.read_human_state_button = QPushButton("读取关节状态")
        self.read_human_state_button.setObjectName("SecondaryButton")
        self.reset_fault_button = QPushButton("故障复位")
        self.reset_fault_button.setObjectName("SecondaryButton")
        self.start_hold_button = QPushButton("安全启动保持")
        self.stop_hold_button = QPushButton("安全停止")
        self.stop_hold_button.setObjectName("SecondaryButton")
        self.emergency_stop_button = QPushButton("紧急失能")
        self.emergency_stop_button.setObjectName("EmergencyButton")
        action_grid = QGridLayout()
        action_grid.setContentsMargins(16, 18, 16, 16)
        action_grid.setSpacing(10)
        action_grid.addWidget(self.read_state_button, 0, 0)
        action_grid.addWidget(self.read_human_state_button, 0, 1)
        action_grid.addWidget(self.reset_fault_button, 0, 2)
        action_grid.addWidget(self.start_hold_button, 1, 0)
        action_grid.addWidget(self.stop_hold_button, 1, 1)
        action_grid.addWidget(self.emergency_stop_button, 1, 2)
        action_note = QLabel("安全启动：控制源 → 失能 → 故障复位 → 位置心跳 → 使能")
        action_note.setObjectName("MutedLabel")
        action_grid.addWidget(action_note, 2, 0, 1, 3)
        action_group = self._card("测试流程", action_grid)

        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(14)
        left_layout.addWidget(connection_group)
        left_layout.addWidget(control_group)
        left_layout.addWidget(action_group)
        left_layout.addStretch(1)
        left = QWidget()
        left.setLayout(left_layout)
        left.setMinimumWidth(430)

        self.position_value = QLabel("--")
        self.position_value.setObjectName("MotorPositionValue")
        self.position_deg_value = QLabel("-- °")
        self.position_deg_value.setObjectName("MotorSecondaryValue")
        self.error_value = QLabel("--")
        self.status_value = QLabel("等待状态")
        self.status_value.setObjectName("MotorStatusValue")
        self.human_state_value = QLabel("--")
        self.temperature_value = QLabel("--")
        self.voltage_value = QLabel("--")
        self.rx_age_value = QLabel("--")
        for label in (
            self.error_value,
            self.human_state_value,
            self.temperature_value,
            self.voltage_value,
            self.rx_age_value,
        ):
            label.setObjectName("MotorMonoValue")
        position_box = QVBoxLayout()
        position_box.setContentsMargins(18, 15, 18, 15)
        position_box.setSpacing(3)
        position_box.addWidget(QLabel("实时反馈位置"))
        position_box.addWidget(self.position_value)
        position_box.addWidget(self.position_deg_value)
        position_panel = QWidget()
        position_panel.setObjectName("PositionPanel")
        position_panel.setLayout(position_box)
        metrics = QGridLayout()
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setSpacing(10)
        metrics.addWidget(self._metric("目标误差", self.error_value), 0, 0)
        metrics.addWidget(self._metric("电机状态", self.status_value), 0, 1)
        metrics.addWidget(self._metric("关节状态", self.human_state_value), 1, 0)
        metrics.addWidget(self._metric("温度", self.temperature_value), 1, 1)
        metrics.addWidget(self._metric("母线电压", self.voltage_value), 2, 0)
        metrics.addWidget(self._metric("反馈新鲜度", self.rx_age_value), 2, 1)
        live_layout = QVBoxLayout()
        live_layout.setContentsMargins(16, 18, 16, 16)
        live_layout.setSpacing(12)
        live_layout.addWidget(position_panel)
        live_layout.addLayout(metrics)
        live_group = self._card("实时反馈", live_layout)

        self.tx_count_value = QLabel("0")
        self.actual_rate_value = QLabel("0.0 Hz")
        self.range_value = QLabel("--")
        self.max_error_value = QLabel("--")
        for label in (self.tx_count_value, self.actual_rate_value, self.range_value, self.max_error_value):
            label.setObjectName("MotorMonoValue")
        stats_grid = QGridLayout()
        stats_grid.setContentsMargins(16, 18, 16, 16)
        stats_grid.setSpacing(10)
        stats_grid.addWidget(self._metric("已发送位置帧", self.tx_count_value), 0, 0)
        stats_grid.addWidget(self._metric("实测发送频率", self.actual_rate_value), 0, 1)
        stats_grid.addWidget(self._metric("位置范围", self.range_value), 1, 0)
        stats_grid.addWidget(self._metric("最大绝对误差", self.max_error_value), 1, 1)
        stats_group = self._card("本次测试统计", stats_grid)

        self.start_csv_button = QPushButton("开始记录 CSV")
        self.start_csv_button.setObjectName("SecondaryButton")
        self.stop_csv_button = QPushButton("停止记录")
        self.stop_csv_button.setObjectName("SecondaryButton")
        self.stop_csv_button.setEnabled(False)
        self.clear_log_button = QPushButton("清空")
        self.clear_log_button.setObjectName("SecondaryButton")
        log_actions = QHBoxLayout()
        log_actions.addWidget(self.start_csv_button)
        log_actions.addWidget(self.stop_csv_button)
        log_actions.addStretch(1)
        log_actions.addWidget(self.clear_log_button)
        self.frame_table = QTableWidget(0, 5)
        self.frame_table.setHorizontalHeaderLabels(["方向", "时间", "CAN ID", "Len", "Data"])
        self.frame_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.frame_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.frame_table.verticalHeader().setVisible(False)
        self.frame_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.frame_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.frame_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.frame_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.frame_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.event_log = QPlainTextEdit()
        self.event_log.setObjectName("MotorEventLog")
        self.event_log.setReadOnly(True)
        self.event_log.setMaximumHeight(92)
        log_layout = QVBoxLayout()
        log_layout.setContentsMargins(16, 18, 16, 16)
        log_layout.setSpacing(10)
        log_layout.addLayout(log_actions)
        log_layout.addWidget(self.frame_table, 1)
        log_layout.addWidget(self.event_log)
        log_group = self._card("电机通信", log_layout)

        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(14)
        right_layout.addWidget(live_group)
        right_layout.addWidget(stats_group)
        right_layout.addWidget(log_group, 1)
        right = QWidget()
        right.setLayout(right_layout)

        columns = QHBoxLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(16)
        columns.addWidget(left, 0)
        columns.addWidget(right, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 22)
        layout.setSpacing(16)
        layout.addWidget(hero)
        layout.addLayout(columns, 1)

    def _connect_signals(self) -> None:
        self.open_fd_button.clicked.connect(self._open_can_fd)
        self.close_fd_button.clicked.connect(self._close_can)
        self.read_state_button.clicked.connect(
            lambda: self._run_manual_command("读取电机状态", read_motor_state_frame)
        )
        self.read_human_state_button.clicked.connect(
            lambda: self._run_manual_command("读取关节状态", read_human_state_frame)
        )
        self.reset_fault_button.clicked.connect(
            lambda: self._run_manual_command("故障复位", fault_reset_frame)
        )
        self.start_hold_button.clicked.connect(self.start_hold)
        self.stop_hold_button.clicked.connect(self.stop_hold)
        self.emergency_stop_button.clicked.connect(self.emergency_stop)
        self.target_position_input.valueChanged.connect(self._update_target_preview)
        self.frequency_input.currentIndexChanged.connect(self._update_plan_label)
        self.duration_input.valueChanged.connect(self._update_plan_label)
        self.start_csv_button.clicked.connect(self.start_csv_recording)
        self.stop_csv_button.clicked.connect(self.stop_csv_recording)
        self.clear_log_button.clicked.connect(self._clear_logs)

    @property
    def device_id(self) -> int:
        value = int(self.device_id_input.text().strip(), 0)
        if not 0 <= value <= 0xFF:
            raise ValueError("电机 ID 必须在 0x00～0xFF 范围内")
        return value

    @property
    def broadcast_id(self) -> int:
        value = int(self.broadcast_id_input.text().strip(), 0)
        if not 0 <= value <= 0x7FF:
            raise ValueError("广播 CAN ID 必须在 0x000～0x7FF 范围内")
        return value

    @property
    def target_position(self) -> float:
        return self.target_position_input.value()

    def _open_can_fd(self) -> None:
        config = MotorCanFdConfig(
            device_type=self.device_type_input.value(),
            device_index=self.device_index_input.value(),
            channel=self.channel_input.currentData(),
            arbitration_baudrate=self.arbitration_baudrate_input.currentData(),
            data_baudrate=self.data_baudrate_input.currentData(),
        )
        try:
            self._open_can_fd_callback(config)
        except Exception as exc:
            self._show_error(str(exc))

    def _close_can(self) -> None:
        self.emergency_stop(silent=True)
        self._close_can_callback()

    def set_connected(self, connected: bool, message: str) -> None:
        self._connected = connected
        self.connection_badge.setText(message)
        self.connection_badge.setProperty("connected", connected)
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        self.open_fd_button.setEnabled(not connected)
        self.close_fd_button.setEnabled(connected)
        for button in (
            self.read_state_button,
            self.read_human_state_button,
            self.reset_fault_button,
            self.start_hold_button,
            self.emergency_stop_button,
        ):
            button.setEnabled(connected)
        self.stop_hold_button.setEnabled(connected and self._holding)
        if not connected:
            self._stop_timers()

    @Slot()
    def start_hold(self) -> None:
        try:
            if not self._connected:
                raise RuntimeError("请先打开 CAN FD 设备")
            if self.use_feedback_on_start.isChecked():
                if self._latest_feedback is None:
                    self._send_named("读取启动位置", read_motor_state_frame(self.device_id))
                    raise RuntimeError("尚未收到位置反馈；已发送状态读取，请收到反馈后再次启动")
                self.target_position_input.setValue(self._latest_feedback.position_rad)

            device_id = self.device_id
            self._hold_token += 1
            token = self._hold_token
            self._send_named("设置控制源", set_control_source_frame(device_id))
            self._send_named("启动前失能", disable_frame(device_id))
            self._send_named("故障复位", fault_reset_frame(device_id))
            self._reset_statistics()
            self._holding = True
            self._hold_started_at = time.monotonic()
            self.position_timer.setInterval(max(1, round(1000 / self.frequency_input.currentData())))
            self._send_position_tick()
            self.position_timer.start()
            self.monitor_timer.start()
            self.stop_hold_button.setEnabled(True)
            self.start_hold_button.setEnabled(False)
            self._event("位置心跳已启动，等待使能")
            QTimer.singleShot(100, lambda: self._enable_after_heartbeat(token))
            QTimer.singleShot(250, self._request_monitor_state)
        except Exception as exc:
            self._show_error(str(exc))

    def _enable_after_heartbeat(self, token: int) -> None:
        if token != self._hold_token or not self._holding or not self._connected:
            return
        try:
            self._send_named("AutoEnable=1", enable_frame(self.device_id))
            self._event("已使能；预期电机状态变为 0x42")
        except Exception as exc:
            self.emergency_stop(silent=True)
            self._show_error(str(exc))

    @Slot()
    def stop_hold(self) -> None:
        if not self._holding:
            return
        self._hold_token += 1
        try:
            if self._connected:
                self._send_named("安全停止失能", disable_frame(self.device_id))
        except Exception as exc:
            self._show_error(str(exc))
        token = self._hold_token
        self._event("已发送失能，继续保持 80 ms 后停止心跳")
        QTimer.singleShot(80, lambda: self._finish_safe_stop(token))

    def _finish_safe_stop(self, token: int) -> None:
        if token != self._hold_token:
            return
        self._stop_timers()
        self._event("位置心跳已停止")

    @Slot()
    def emergency_stop(self, silent: bool = False) -> None:
        self._hold_token += 1
        if self._connected:
            try:
                self._send_frame_callback(disable_frame(self.device_id))
                self._send_frame_callback(disable_frame(self.device_id))
            except Exception as exc:
                if not silent:
                    self._show_error(str(exc))
        self._stop_timers()
        if not silent:
            self._event("紧急失能：已停止位置心跳")

    def _stop_timers(self) -> None:
        self.position_timer.stop()
        self.monitor_timer.stop()
        self._holding = False
        if hasattr(self, "start_hold_button"):
            self.start_hold_button.setEnabled(self._connected)
            self.stop_hold_button.setEnabled(False)

    def _send_position_tick(self) -> None:
        if not self._connected or not self._holding:
            return
        try:
            self._send_frame_callback(position_command_frame(self.device_id, self.target_position, self.broadcast_id))
            self._tx_count += 1
            duration_hours = self.duration_input.value()
            if duration_hours > 0 and self._hold_started_at is not None:
                if time.monotonic() - self._hold_started_at >= duration_hours * 3600:
                    self._event("已达到设定测试时长，自动安全停止")
                    self.stop_hold()
        except Exception as exc:
            self.emergency_stop(silent=True)
            self._show_error(f"周期位置帧发送失败：{exc}")

    def _request_monitor_state(self) -> None:
        if not self._connected:
            return
        try:
            self._send_frame_callback(read_motor_state_frame(self.device_id))
            self._send_frame_callback(read_human_state_frame(self.device_id))
        except Exception as exc:
            self._event(f"状态轮询失败：{exc}")

    def _send_named(self, name: str, frame: CanFrame) -> None:
        if not self._connected:
            raise RuntimeError("CAN FD 未连接")
        self._send_frame_callback(frame)
        self._event(name)

    def _run_manual_command(self, name: str, builder: Callable[[int], CanFrame]) -> None:
        try:
            self._send_named(name, builder(self.device_id))
        except Exception as exc:
            self._show_error(str(exc))

    def handle_frame(self, direction: str, frame: CanFrame) -> None:
        try:
            device_id = self.device_id
            broadcast_id = self.broadcast_id
        except ValueError:
            return
        if frame.id not in (device_id, 0x100 + device_id, broadcast_id):
            return
        self._append_frame_row(direction, frame)
        if direction != "RX":
            return
        feedback = decode_motor_feedback(frame, device_id)
        if feedback is not None:
            self._handle_feedback(feedback)
            return
        human_state = decode_human_state(frame, device_id)
        if human_state is not None:
            descriptions = {0: "0 · 未初始化", 1: "1 · 已停止", 2: "2 · 运行中", 3: "3 · 故障"}
            self.human_state_value.setText(descriptions.get(human_state, str(human_state)))
            if human_state == 3:
                self.human_state_value.setStyleSheet("color: #dc2626; font-weight: 900;")
                self._event("检测到关节状态 3（故障）")
            else:
                self.human_state_value.setStyleSheet("")

    def _handle_feedback(self, feedback: MotorFeedback) -> None:
        self._latest_feedback = feedback
        self._last_feedback_monotonic = time.monotonic()
        position = feedback.position_rad
        target = self.target_position
        error = position - target
        self.position_value.setText(f"{position:+.4f} rad")
        self.position_deg_value.setText(f"{math.degrees(position):+.2f} °")
        self.error_value.setText(f"{error:+.4f} rad / {math.degrees(error):+.2f}°")
        self.status_value.setText(f"0x{feedback.status:02X} · {feedback.status_text}")
        status_color = "#15803d" if feedback.status == 0x42 else "#b45309" if feedback.status == 0x40 else "#dc2626"
        self.status_value.setStyleSheet(f"color: {status_color}; font-weight: 900;")
        self.temperature_value.setText(f"电机 {feedback.motor_temperature_c}°C / 驱动 {feedback.driver_temperature_c}°C")
        self.voltage_value.setText(f"{feedback.voltage_v:.1f} V  (0x{feedback.voltage_raw:04X})")

        if self._first_position is None:
            self._first_position = position
        self._min_position = position if self._min_position is None else min(self._min_position, position)
        self._max_position = position if self._max_position is None else max(self._max_position, position)
        self._max_abs_error = max(self._max_abs_error, abs(error))
        self.range_value.setText(
            f"{self._min_position:+.4f} ～ {self._max_position:+.4f} rad"
            f"  (跨度 {math.degrees(self._max_position - self._min_position):.3f}°)"
        )
        self.max_error_value.setText(f"{self._max_abs_error:.4f} rad / {math.degrees(self._max_abs_error):.2f}°")
        self._write_csv_feedback(feedback, error)

    def _refresh_live_metrics(self) -> None:
        self.tx_count_value.setText(f"{self._tx_count:,}")
        if self._hold_started_at is not None and self._tx_count:
            elapsed = max(0.001, time.monotonic() - self._hold_started_at)
            self.actual_rate_value.setText(f"{self._tx_count / elapsed:.1f} Hz")
        else:
            self.actual_rate_value.setText("0.0 Hz")
        if self._last_feedback_monotonic is None:
            self.rx_age_value.setText("--")
        else:
            age_ms = (time.monotonic() - self._last_feedback_monotonic) * 1000
            self.rx_age_value.setText(f"{age_ms:.0f} ms")
            self.rx_age_value.setStyleSheet("color: #dc2626;" if age_ms > 500 else "")

    def _reset_statistics(self) -> None:
        self._tx_count = 0
        self._first_position = None
        self._min_position = None
        self._max_position = None
        self._max_abs_error = 0.0
        self.range_value.setText("--")
        self.max_error_value.setText("--")

    def _update_target_preview(self, *_args) -> None:
        raw = position_rad_to_raw(self.target_position)
        self.raw_target_label.setText(f"0x{raw:04X}  ·  {raw & 0xFF:02X} {(raw >> 8) & 0xFF:02X}")

    def _update_plan_label(self, *_args) -> None:
        frequency = self.frequency_input.currentData()
        hours = self.duration_input.value()
        if hours <= 0:
            self.plan_label.setText("测试时长为 0：持续发送，直到手动安全停止。")
        else:
            frames = round(frequency * hours * 3600)
            self.plan_label.setText(f"计划发送 {frames:,} 帧；界面会按实际发送计数和耗时计算真实频率。")

    def start_csv_recording(self) -> None:
        try:
            default_name = f"motor_{self.device_id:02X}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        except ValueError as exc:
            self._show_error(str(exc))
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存电机长测数据", default_name, "CSV (*.csv)")
        if not path:
            return
        try:
            self.stop_csv_recording()
            self._csv_file = Path(path).open("w", newline="", encoding="utf-8-sig")
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(
                ["time", "device_id", "status", "position_raw", "position_rad", "position_deg", "target_rad", "error_rad", "error_deg", "voltage_v", "motor_temp_c", "driver_temp_c", "alarm_hex"]
            )
            self._csv_file.flush()
            self.start_csv_button.setEnabled(False)
            self.stop_csv_button.setEnabled(True)
            self._event(f"开始记录 CSV：{path}")
        except Exception as exc:
            self.stop_csv_recording()
            self._show_error(str(exc))

    def _write_csv_feedback(self, feedback: MotorFeedback, error: float) -> None:
        if self._csv_writer is None:
            return
        self._csv_writer.writerow(
            [
                datetime.now().isoformat(timespec="milliseconds"),
                f"0x{feedback.device_id:02X}",
                f"0x{feedback.status:02X}",
                feedback.position_raw,
                f"{feedback.position_rad:.7f}",
                f"{math.degrees(feedback.position_rad):.5f}",
                f"{self.target_position:.7f}",
                f"{error:.7f}",
                f"{math.degrees(error):.5f}",
                f"{feedback.voltage_v:.3f}",
                feedback.motor_temperature_c,
                feedback.driver_temperature_c,
                feedback.alarm_bytes.hex().upper(),
            ]
        )
        self._csv_rows_since_flush += 1
        if self._csv_rows_since_flush >= 100:
            self._csv_file.flush()
            self._csv_rows_since_flush = 0

    def stop_csv_recording(self) -> None:
        if self._csv_file is not None:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            finally:
                self._csv_file = None
                self._csv_writer = None
                self._csv_rows_since_flush = 0
        if hasattr(self, "start_csv_button"):
            self.start_csv_button.setEnabled(True)
            self.stop_csv_button.setEnabled(False)

    def shutdown(self) -> None:
        self.emergency_stop(silent=True)
        self.stop_csv_recording()

    def _append_frame_row(self, direction: str, frame: CanFrame) -> None:
        if self.frame_table.rowCount() >= 500:
            self.frame_table.removeRow(0)
        row = self.frame_table.rowCount()
        self.frame_table.insertRow(row)
        values = (
            direction,
            datetime.now().strftime("%H:%M:%S.%f")[:-3],
            f"0x{frame.id:X}",
            str(frame.dlc),
            bytes(frame.data[: frame.dlc]).hex(" ").upper(),
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column in (0, 2, 3):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if column == 0:
                item.setForeground(QColor("#2563eb" if direction == "RX" else "#b45309"))
            self.frame_table.setItem(row, column, item)
        self.frame_table.scrollToBottom()

    def _clear_logs(self) -> None:
        self.frame_table.setRowCount(0)
        self.event_log.clear()

    def _event(self, message: str) -> None:
        self.event_log.appendPlainText(f"[{datetime.now():%H:%M:%S.%f}"[:-3] + f"] {message}")

    def _show_error(self, message: str) -> None:
        self._event(f"错误：{message}")
        QMessageBox.warning(self, "电机测试", message)

    @staticmethod
    def _add_field(layout: QGridLayout, row: int, column: int, title: str, widget: QWidget) -> None:
        label = QLabel(title)
        label.setObjectName("MutedLabel")
        layout.addWidget(label, row, column)
        layout.addWidget(widget, row + 1, column)

    @staticmethod
    def _metric(title: str, value: QLabel) -> QWidget:
        title_label = QLabel(title)
        title_label.setObjectName("MutedLabel")
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)
        layout.addWidget(title_label)
        layout.addWidget(value)
        widget = QWidget()
        widget.setObjectName("MotorMetric")
        widget.setLayout(layout)
        return widget

    @staticmethod
    def _card(title: str, body_layout) -> QGroupBox:
        group = QGroupBox(title)
        header_title = QLabel(title)
        header_title.setObjectName("CardTitle")
        mark = QLabel()
        mark.setObjectName("CardMark")
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(16, 12, 16, 10)
        header_layout.setSpacing(9)
        header_layout.addWidget(mark)
        header_layout.addWidget(header_title)
        header_layout.addStretch(1)
        header = QWidget()
        header.setObjectName("CardHeader")
        header.setLayout(header_layout)
        body = QWidget()
        body.setLayout(body_layout)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(body)
        group.setLayout(layout)
        return group
