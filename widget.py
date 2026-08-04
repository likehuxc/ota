# This Python file uses the following encoding: utf-8
from __future__ import annotations

import csv
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from d7_pmu_iap_tool.can.can_frame import CanDriver, CanFrame
from d7_pmu_iap_tool.can.zlg_vci_can_driver import ZlgVciCanDriver
from d7_pmu_iap_tool.can_debug_page import CanDebugConfig, CanDebugPage
from d7_pmu_iap_tool.iap.firmware_image import FirmwareImage
from d7_pmu_iap_tool.iap.iap_protocol import CMD_FILL_SEGMENT_DATA, IapProtocol
from d7_pmu_iap_tool.iap.iap_upgrade_controller import (
    IapUpgradeController,
    UpgradeOptions,
    build_upgrade_preview_frames,
)
from d7_pmu_iap_tool.machine_info_page import MachineInfoCanConfig, MachineInfoPage
from d7_pmu_iap_tool.motor_test_page import MotorCanFdConfig, MotorTestPage


def _find_default_canfd_dll() -> Path:
    software_root = Path.home() / "Desktop" / "绿色软件"
    patterns = (
        "CANFD分析仪资料*/二次开发库*/x64/ControlCANFD.dll",
        "CANFD分析仪资料*/调试工具/*/bin/x64/ControlCANFD.dll",
    )
    for pattern in patterns:
        matches = sorted(software_root.glob(pattern))
        if matches:
            return matches[0]
    return Path("ControlCANFD.dll")


DEFAULT_CANFD_DLL_PATH = _find_default_canfd_dll()
DEFAULT_DLL_PATH = DEFAULT_CANFD_DLL_PATH
DEFAULT_DEVICE_TYPE = 41  # ZCAN_USBCANFD_200U
DEFAULT_DEVICE_INDEX = 0
DEFAULT_DATA_BAUDRATE = 5_000_000
MAX_CAN_ROWS = 500
MAX_SYSTEM_LOG_BLOCKS = 2_000
IAP_CONNECTION_MODE = "iap_canfd"


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    target_id: int
    can_id: int
    pre_upgrade_wakeup_ms: int = 0
    disable_target_can_messages: bool = False
    wait_data_frame_ack: bool = False
    ignore_validate_ack_failure: bool = False
    app_start_wait_ms: int = 1000
    app_total_wait_ms: int = 5000


DEVICE_PROFILES = (
    DeviceProfile("D7-CT01", 0x18, 0x7FF),
    DeviceProfile("D7-CT02", 0x19, 0x7FF),
    DeviceProfile(
        "电池升级",
        0x41,
        0x7FF,
        pre_upgrade_wakeup_ms=1000,
        disable_target_can_messages=True,
        ignore_validate_ack_failure=True,
        app_start_wait_ms=10000,
        app_total_wait_ms=25000,
    ),
)
DEFAULT_DEVICE_PROFILE_NAME = "D7-CT02"


class UpgradeWorker(QObject):
    log = Signal(str)
    progress = Signal(int)
    can_frame = Signal(str, object)
    succeeded = Signal()
    failed = Signal(str)
    finished = Signal()

    def __init__(self, controller: IapUpgradeController, image: FirmwareImage, options: UpgradeOptions) -> None:
        super().__init__()
        self.controller = controller
        self.image = image
        self.options = options

    @Slot()
    def run(self) -> None:
        try:
            self.controller.upgrade(self.image, self.options)
            self.succeeded.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    def handle_can_frame(self, direction: str, frame: CanFrame) -> None:
        if (
            frame.dlc >= 3
            and frame.data[0] == 0x16
            and frame.data[2] == CMD_FILL_SEGMENT_DATA
        ):
            return
        self.can_frame.emit(direction, frame)

    @Slot()
    def cancel(self) -> None:
        self.controller.cancel()


class Widget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("D7 CAN IAP")
        self.resize(1280, 820)

        self.driver: CanDriver | None = None
        self.upgrade_thread: QThread | None = None
        self.upgrade_worker: UpgradeWorker | None = None
        self._can_connected = False
        self._connection_mode = IAP_CONNECTION_MODE
        self.known_can_ids: set[int] = set()
        self.filter_can_ids: set[int] = set()
        self.can_filter_buttons: dict[int, QPushButton] = {}
        self._can_records: list[tuple[str, str, CanFrame]] = []
        self._can_save_records: list[tuple[str, str, CanFrame]] = []
        self._rx_save_records: list[tuple[str, CanFrame]] = []
        self._can_display_paused = False
        self.selected_dll_path = DEFAULT_DLL_PATH
        self._upgrade_started_at: float | None = None
        self._monotonic = time.monotonic

        self.can_poll_timer = QTimer(self)
        self.can_poll_timer.setInterval(10)
        self.can_poll_timer.timeout.connect(self._poll_can_frames)

        self._build_ui()
        self._connect_signals()
        self._apply_style()
        self._set_connection_status(False, "未连接")

    def _build_ui(self) -> None:
        style = self.style()

        self.app_icon_label = QLabel("IAP")
        self.app_icon_label.setObjectName("AppIcon")
        self.title_label = QLabel("D7 CAN IAP")
        self.title_label.setObjectName("TitleLabel")
        self.device_profile_input = QComboBox()
        self.device_profile_input.setObjectName("DeviceProfileInput")
        for profile in DEVICE_PROFILES:
            self.device_profile_input.addItem(profile.name, profile)
        default_profile_index = self.device_profile_input.findText(DEFAULT_DEVICE_PROFILE_NAME)
        if default_profile_index >= 0:
            self.device_profile_input.setCurrentIndex(default_profile_index)
        self.fixed_device_label = QLabel("USBCANFD-200U · Index 0")
        self.fixed_device_label.setObjectName("SummaryValue")
        self.can_status_dot = QLabel()
        self.can_status_dot.setObjectName("StatusDot")
        self.can_status_dot.setFixedSize(10, 10)
        self.can_status_label = QLabel()
        self.can_status_label.setObjectName("StatusLabel")

        title_text_layout = QVBoxLayout()
        title_text_layout.setContentsMargins(0, 0, 0, 0)
        title_text_layout.setSpacing(3)
        title_text_layout.addWidget(self.title_label)
        title_text_layout.addWidget(self.device_profile_input)

        self.status_pill = QWidget()
        self.status_pill.setObjectName("StatusPill")
        status_layout = QHBoxLayout()
        status_layout.setContentsMargins(12, 7, 12, 7)
        status_layout.setSpacing(8)
        status_layout.addWidget(self.can_status_dot)
        status_layout.addWidget(self.can_status_label)
        self.status_pill.setLayout(status_layout)

        self.iap_nav_button = QPushButton("IAP 升级")
        self.iap_nav_button.setObjectName("HeaderNavButton")
        self.iap_nav_button.setCheckable(True)
        self.iap_nav_button.setChecked(True)
        self.motor_nav_button = QPushButton("电机测试")
        self.motor_nav_button.setObjectName("HeaderNavButton")
        self.motor_nav_button.setCheckable(True)
        self.can_debug_nav_button = QPushButton("CAN 调试")
        self.can_debug_nav_button.setObjectName("HeaderNavButton")
        self.can_debug_nav_button.setCheckable(True)
        self.machine_info_nav_button = QPushButton("MachineInfo")
        self.machine_info_nav_button.setObjectName("HeaderNavButton")
        self.machine_info_nav_button.setCheckable(True)

        brand_layout = QHBoxLayout()
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(14)
        brand_layout.addWidget(self.app_icon_label)
        brand_layout.addLayout(title_text_layout)
        brand_layout.addStretch(1)
        brand_layout.addWidget(self.status_pill)
        nav_layout = QHBoxLayout()
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(10)
        nav_layout.addStretch(1)
        nav_layout.addWidget(self.iap_nav_button)
        nav_layout.addWidget(self.can_debug_nav_button)
        nav_layout.addWidget(self.machine_info_nav_button)
        nav_layout.addWidget(self.motor_nav_button)
        title_layout = QVBoxLayout()
        title_layout.setContentsMargins(24, 10, 24, 10)
        title_layout.setSpacing(8)
        title_layout.addLayout(brand_layout)
        title_layout.addLayout(nav_layout)
        self.header_bar = QWidget()
        self.header_bar.setObjectName("HeaderBar")
        self.header_bar.setLayout(title_layout)

        self.step_title_labels: list[QLabel] = []
        self.step_desc_labels: list[QLabel] = []
        self.step_number_labels: list[QLabel] = []
        self.step_widgets: list[QWidget] = []
        stepper_layout = QHBoxLayout()
        stepper_layout.setContentsMargins(16, 14, 16, 14)
        stepper_layout.setSpacing(10)
        for number, title, desc in (
            ("1", "连接设备", "等待打开"),
            ("2", "选择固件", "等待读取"),
            ("3", "查询角色", "等待查询"),
            ("4", "升级写入", "等待开始"),
            ("5", "完成校验", "等待完成"),
        ):
            stepper_layout.addWidget(self._make_step(number, title, desc, "StepPending"), 1)
        self.stepper = QWidget()
        self.stepper.setObjectName("Stepper")
        self.stepper.setLayout(stepper_layout)

        self.dll_path_label = QLabel(str(DEFAULT_DLL_PATH))
        self.dll_path_label.setWordWrap(True)
        self.dll_path_label.setObjectName("SummaryValue")

        self.channel_input = QComboBox()
        self.channel_input.addItem("CH0", 0)
        self.channel_input.addItem("CH1", 1)

        self.baudrate_input = QComboBox()
        for value in (1_000_000, 500_000, 250_000, 125_000, 100_000):
            self.baudrate_input.addItem(f"{value:,}", value)
        self.baudrate_input.setMinimumWidth(180)

        current_profile = self._selected_device_profile()
        self.target_id_input = QLineEdit(self._format_can_id(current_profile.target_id))
        self.target_id_input.setMinimumWidth(96)
        self.can_id_input = QLineEdit(self._format_can_id(current_profile.can_id))
        self.can_id_input.setMinimumWidth(110)

        conn_layout = QGridLayout()
        conn_layout.setContentsMargins(18, 20, 18, 18)
        conn_layout.setHorizontalSpacing(18)
        conn_layout.setVerticalSpacing(12)
        self._add_labeled_widget(conn_layout, 0, 0, "通道", self.channel_input)
        self._add_labeled_widget(conn_layout, 0, 1, "仲裁波特率", self.baudrate_input)
        self._add_labeled_widget(conn_layout, 2, 0, "目标 ID", self.target_id_input)
        self._add_labeled_widget(conn_layout, 2, 1, "IAP CAN ID", self.can_id_input)
        conn_layout.setColumnStretch(0, 1)
        conn_layout.setColumnStretch(1, 1)

        self.open_button = QPushButton("打开设备")
        self.open_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.close_button = QPushButton("关闭设备")
        self.close_button.setObjectName("DangerButton")
        self.close_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogCancelButton))
        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 8, 0, 0)
        button_row.setSpacing(10)
        button_row.addWidget(self.open_button)
        button_row.addWidget(self.close_button)
        button_row.addStretch(1)
        hint_label = QLabel("升级过程中配置项会自动锁定，避免误操作")
        hint_label.setObjectName("MutedLabel")
        button_row.addWidget(hint_label)
        conn_layout.addLayout(button_row, 4, 0, 1, 2)

        self.conn_group = QGroupBox("设备连接")
        self._set_card_layout(self.conn_group, conn_layout)

        self.bin_path_input = QLineEdit()
        self.bin_path_input.setPlaceholderText("请选择 APP bin 固件")
        self.bin_browse_button = QPushButton("选择固件")
        self.bin_browse_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        self.bin_browse_button.setToolTip("选择 APP bin")

        file_layout = QGridLayout()
        file_layout.setContentsMargins(16, 18, 16, 16)
        file_layout.setHorizontalSpacing(12)
        file_layout.setVerticalSpacing(12)
        file_layout.addWidget(QLabel("APP 固件"), 0, 0)
        file_layout.addWidget(self.bin_path_input, 0, 1)
        file_layout.addWidget(self.bin_browse_button, 0, 2)

        self.firmware_size_value = QLabel("-")
        self.firmware_sp_value = QLabel("-")
        self.firmware_reset_value = QLabel("-")
        self.firmware_image_value = QLabel("APP")
        for value in (
            self.firmware_size_value,
            self.firmware_sp_value,
            self.firmware_reset_value,
            self.firmware_image_value,
        ):
            value.setObjectName("MetricValue")

        self.firmware_metric_title_labels: list[QLabel] = []
        file_layout.addWidget(self._metric("Size", self.firmware_size_value, self.firmware_metric_title_labels), 1, 0)
        file_layout.setColumnStretch(1, 1)

        self.file_group = QGroupBox("固件文件")
        self._set_card_layout(self.file_group, file_layout)

        self.query_role_button = QPushButton("重新查询角色")
        self.query_role_button.setObjectName("SecondaryButton")
        self.query_version_button = QPushButton("重新查询版本")
        self.query_version_button.setObjectName("SecondaryButton")
        self.start_upgrade_button = QPushButton("开始升级")
        self.start_upgrade_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.simulate_battery_button = QPushButton("模拟电池升级")
        self.simulate_battery_button.setObjectName("SecondaryButton")
        self.simulate_battery_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self.stop_button = QPushButton("停止升级")
        self.stop_button.setObjectName("SecondaryButton")
        self.stop_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_BrowserStop))

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.upgrade_elapsed_value = QLabel("--:--:--.---")
        self.upgrade_elapsed_value.setObjectName("MetricValue")

        self.role_value_label = QLabel("APP")
        self.role_value_label.setObjectName("RoleValue")
        role_title_label = QLabel("当前设备角色")
        role_title_label.setObjectName("MutedLabel")
        role_text_layout = QVBoxLayout()
        role_text_layout.setContentsMargins(0, 0, 0, 0)
        role_text_layout.setSpacing(4)
        role_text_layout.addWidget(role_title_label)
        role_text_layout.addWidget(self.role_value_label)

        self.version_value_label = QLabel("--")
        self.version_value_label.setObjectName("RoleValue")
        version_title_label = QLabel("当前软件版本")
        version_title_label.setObjectName("MutedLabel")
        version_text_layout = QVBoxLayout()
        version_text_layout.setContentsMargins(0, 0, 0, 0)
        version_text_layout.setSpacing(4)
        version_text_layout.addWidget(version_title_label)
        version_text_layout.addWidget(self.version_value_label)

        role_panel = QWidget()
        role_panel_layout = QHBoxLayout(role_panel)
        role_panel_layout.setContentsMargins(0, 0, 14, 0)
        role_panel_layout.setSpacing(10)
        role_panel_layout.addLayout(role_text_layout)
        role_panel_layout.addStretch(1)
        role_panel_layout.addWidget(self.query_role_button)

        version_panel = QWidget()
        version_panel.setObjectName("VersionPanel")
        version_panel_layout = QHBoxLayout(version_panel)
        version_panel_layout.setContentsMargins(14, 0, 0, 0)
        version_panel_layout.setSpacing(10)
        version_panel_layout.addLayout(version_text_layout)
        version_panel_layout.addStretch(1)
        version_panel_layout.addWidget(self.query_version_button)

        self.role_band = QWidget()
        self.role_band.setObjectName("RoleBand")
        role_layout = QHBoxLayout()
        role_layout.setContentsMargins(14, 12, 14, 12)
        role_layout.setSpacing(0)
        role_layout.addWidget(role_panel, 1)
        role_layout.addWidget(version_panel, 1)
        self.role_band.setLayout(role_layout)

        progress_title = QLabel("写入进度")
        progress_title.setObjectName("SectionLabel")
        self.error_title_label = QLabel()
        self.error_title_label.setObjectName("ErrorTitle")
        self.error_text_label = QLabel()
        self.error_text_label.setObjectName("ErrorText")
        self.error_text_label.setWordWrap(True)
        error_layout = QVBoxLayout()
        error_layout.setContentsMargins(12, 10, 12, 10)
        error_layout.setSpacing(5)
        error_layout.addWidget(self.error_title_label)
        error_layout.addWidget(self.error_text_label)
        self.error_box = QWidget()
        self.error_box.setObjectName("ErrorBox")
        self.error_box.setLayout(error_layout)
        self.error_box.hide()

        upgrade_actions = QHBoxLayout()
        upgrade_actions.addWidget(self.start_upgrade_button)
        upgrade_actions.addWidget(self.simulate_battery_button)
        upgrade_actions.addWidget(self.stop_button)
        upgrade_actions.addStretch(1)
        upgrade_layout = QVBoxLayout()
        upgrade_layout.setContentsMargins(16, 18, 16, 16)
        upgrade_layout.setSpacing(12)
        upgrade_layout.addWidget(self.role_band)
        upgrade_layout.addWidget(progress_title)
        upgrade_layout.addWidget(self.progress_bar)
        upgrade_layout.addWidget(self._metric("升级耗时", self.upgrade_elapsed_value))
        upgrade_layout.addWidget(self.error_box)
        upgrade_layout.addLayout(upgrade_actions)
        self.upgrade_group = QGroupBox("升级控制")
        self._set_card_layout(self.upgrade_group, upgrade_layout)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(MAX_SYSTEM_LOG_BLOCKS)
        self.log_output.setObjectName("LogOutput")
        self.system_log_group = QGroupBox("系统日志")
        self.save_log_button = QPushButton("导出日志")
        self.save_log_button.setObjectName("SecondaryButton")
        log_toolbar = QHBoxLayout()
        log_toolbar.addStretch(1)
        log_toolbar.addWidget(self.save_log_button)
        system_log_layout = QVBoxLayout()
        system_log_layout.setContentsMargins(16, 18, 16, 16)
        system_log_layout.addLayout(log_toolbar)
        system_log_layout.addWidget(self.log_output)
        self._set_card_layout(self.system_log_group, system_log_layout)

        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(16)
        left_layout.addWidget(self.conn_group)
        left_layout.addWidget(self.file_group)
        left_layout.addWidget(self.upgrade_group)
        left_layout.addStretch(1)
        self.left_panel = QWidget()
        self.left_panel.setObjectName("LeftPanel")
        self.left_panel.setLayout(left_layout)
        self.left_panel.setMinimumWidth(520)

        self.send_can_id_input = QLineEdit(self._format_can_id(current_profile.can_id))
        self.send_can_id_input.setFixedWidth(130)
        self.send_data_input = QLineEdit()
        self.send_data_input.setPlaceholderText("例如：16 19 02 00 00 00 31 62")
        self.send_button = QPushButton("发送")
        self.clear_can_button = QPushButton("清空")
        self.clear_can_button.setObjectName("SecondaryButton")

        self.send_form_layout = QGridLayout()
        self.send_form_layout.setContentsMargins(16, 18, 16, 16)
        self.send_form_layout.setHorizontalSpacing(10)
        self.send_form_layout.setVerticalSpacing(8)
        self.send_form_layout.addWidget(QLabel("CAN ID"), 0, 0)
        self.send_form_layout.addWidget(QLabel("Data hex"), 0, 1)
        self.send_form_layout.addWidget(self.send_can_id_input, 1, 0)
        self.send_form_layout.addWidget(self.send_data_input, 1, 1)
        self.send_form_layout.addWidget(self.send_button, 1, 2)
        self.send_form_layout.addWidget(self.clear_can_button, 1, 3)
        self.send_form_layout.setColumnMinimumWidth(0, 120)
        self.send_form_layout.setColumnStretch(0, 0)
        self.send_form_layout.setColumnStretch(1, 1)
        self.send_form_layout.setColumnStretch(2, 0)
        self.send_form_layout.setColumnStretch(3, 0)
        self.send_group = QGroupBox("CAN 发送调试")
        self._set_card_layout(self.send_group, self.send_form_layout)

        self.rx_count_value = QLabel("0 帧")
        self.rx_count_value.setObjectName("SummaryValue")
        self.dll_config_value = QLabel("使用默认 ControlCANFD.dll")
        self.dll_config_value.setObjectName("SummaryValue")
        self.dll_browse_button = QPushButton("选择 DLL")
        self.dll_browse_button.setObjectName("SecondaryButton")
        device_status_layout = QGridLayout()
        device_status_layout.setContentsMargins(16, 18, 16, 16)
        device_status_layout.setHorizontalSpacing(12)
        device_status_layout.setVerticalSpacing(12)
        device_status_layout.addWidget(self._summary_item("固定设备", self.fixed_device_label), 0, 0)
        device_status_layout.addWidget(self._summary_item("接收帧数", self.rx_count_value), 0, 1)
        device_status_layout.addWidget(self._summary_item("当前 DLL", self.dll_path_label), 1, 0)
        device_status_layout.addWidget(self._dll_config_item(), 1, 1)
        self.device_status_group = QGroupBox("设备状态")
        self._set_card_layout(self.device_status_group, device_status_layout)

        self.can_table = QTableWidget(0, 5)
        self.can_table.setHorizontalHeaderLabels(["方向", "时间", "CANID", "Len", "Data"])
        self.can_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.can_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.can_table.setAlternatingRowColors(True)
        self.can_table.verticalHeader().setVisible(False)
        self.can_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.can_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.can_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.can_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.can_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.can_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.can_filter_input = QLineEdit()
        self.can_filter_input.setPlaceholderText("过滤 CAN ID：0x41, 0x7FF")
        self.can_filter_input.setObjectName("FilterInput")
        self.apply_filter_button = QPushButton("应用")
        self.pause_can_display_button = QPushButton("暂停显示")
        self.pause_can_display_button.setObjectName("SecondaryButton")
        self.save_rx_button = QPushButton("导出 CSV")
        self.save_rx_button.setObjectName("SecondaryButton")
        self.can_filter_status_label = QLabel("显示 0 / 0 帧")
        self.can_filter_status_label.setObjectName("MutedLabel")
        self.filter_chip_bar = QWidget()
        self.filter_chip_bar.setObjectName("FilterChipBar")
        self.filter_chip_layout = QHBoxLayout()
        self.filter_chip_layout.setContentsMargins(0, 0, 0, 0)
        self.filter_chip_layout.setSpacing(6)
        self.filter_chip_bar.setLayout(self.filter_chip_layout)

        filter_layout = QGridLayout()
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setHorizontalSpacing(8)
        filter_layout.setVerticalSpacing(6)
        filter_layout.addWidget(self.can_filter_input, 1, 0)
        filter_layout.addWidget(self.apply_filter_button, 1, 1)
        filter_layout.addWidget(self.pause_can_display_button, 1, 2)
        filter_layout.addWidget(self.can_filter_status_label, 1, 3)
        filter_layout.addWidget(self.save_rx_button, 1, 4)
        filter_layout.addWidget(self.filter_chip_bar, 2, 0, 1, 5)
        filter_layout.setColumnStretch(0, 1)
        self.can_filter_panel = QWidget()
        self.can_filter_panel.setObjectName("CanFilterPanel")
        self.can_filter_panel.setLayout(filter_layout)

        tabs_layout = QHBoxLayout()
        tabs_layout.setContentsMargins(0, 0, 0, 0)
        tabs_layout.setSpacing(8)
        for index, text in enumerate(("接收数据", "发送数据", "错误帧")):
            tab = QLabel(text)
            tab.setObjectName("TabActive" if index == 0 else "Tab")
            tabs_layout.addWidget(tab)
        tabs_layout.addStretch(1)

        can_group_layout = QVBoxLayout()
        can_group_layout.setContentsMargins(16, 18, 16, 16)
        can_group_layout.setSpacing(12)
        can_group_layout.addLayout(tabs_layout)
        can_group_layout.addWidget(self.can_filter_panel)
        can_group_layout.addWidget(self.can_table, 1)
        self.can_group = QGroupBox("CAN 监控")
        self._set_card_layout(self.can_group, can_group_layout)

        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(16)
        right_layout.addWidget(self.device_status_group)
        right_layout.addWidget(self.send_group)
        right_layout.addWidget(self.can_group, 1)
        self.right_panel = QWidget()
        self.right_panel.setObjectName("RightPanel")
        self.right_panel.setLayout(right_layout)

        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(18)
        main_layout.addWidget(self.left_panel, 1)
        main_layout.addWidget(self.right_panel, 1)

        content = QWidget()
        content.setObjectName("Content")
        content.setMinimumSize(1050, 900)
        content_layout = QVBoxLayout()
        content_layout.setContentsMargins(22, 20, 22, 22)
        content_layout.setSpacing(18)
        content_layout.addWidget(self.stepper)
        content_layout.addLayout(main_layout, 1)
        content_layout.addWidget(self.system_log_group)
        content.setLayout(content_layout)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("MainScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(content)

        self.motor_page = MotorTestPage(
            send_frame=self._send_motor_frame,
            send_periodic_frame=self._send_motor_periodic_frame,
            open_can_fd=self.open_motor_device,
            close_can=self.close_device,
            default_canfd_dll=str(DEFAULT_CANFD_DLL_PATH),
        )
        self.motor_page.setMinimumSize(1050, 900)
        self.motor_scroll_area = QScrollArea()
        self.motor_scroll_area.setObjectName("MainScrollArea")
        self.motor_scroll_area.setWidgetResizable(True)
        self.motor_scroll_area.setWidget(self.motor_page)
        self.can_debug_page = CanDebugPage(
            send_frame=self._send_debug_frame,
            open_can=self.open_debug_device,
            close_can=self.close_device,
            default_canfd_dll=str(DEFAULT_CANFD_DLL_PATH),
        )
        self.can_debug_scroll_area = QScrollArea()
        self.can_debug_scroll_area.setObjectName("MainScrollArea")
        self.can_debug_scroll_area.setWidgetResizable(True)
        self.can_debug_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.can_debug_scroll_area.setWidget(self.can_debug_page)
        self.machine_info_page = MachineInfoPage(
            send_frame=self._send_debug_frame,
            open_can=self.open_machine_info_device,
            close_can=self.close_device,
            default_canfd_dll=str(DEFAULT_CANFD_DLL_PATH),
        )
        self.machine_info_scroll_area = QScrollArea()
        self.machine_info_scroll_area.setObjectName("MainScrollArea")
        self.machine_info_scroll_area.setWidgetResizable(True)
        self.machine_info_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.machine_info_scroll_area.setWidget(self.machine_info_page)
        self.page_stack = QStackedWidget()
        self.page_stack.setObjectName("PageStack")
        self.page_stack.addWidget(self.scroll_area)
        self.page_stack.addWidget(self.can_debug_scroll_area)
        self.page_stack.addWidget(self.motor_scroll_area)
        self.page_stack.addWidget(self.machine_info_scroll_area)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header_bar)
        layout.addWidget(self.page_stack, 1)

    def _connect_signals(self) -> None:
        self.device_profile_input.currentIndexChanged.connect(self._handle_device_profile_changed)
        self.bin_browse_button.clicked.connect(self._choose_bin)
        self.open_button.clicked.connect(self.open_device)
        self.close_button.clicked.connect(self.close_device)
        self.query_role_button.clicked.connect(self.query_role)
        self.query_version_button.clicked.connect(self.query_software_version)
        self.start_upgrade_button.clicked.connect(self.start_upgrade)
        self.simulate_battery_button.clicked.connect(self.simulate_battery_upgrade)
        self.stop_button.clicked.connect(self.stop_upgrade)
        self.dll_browse_button.clicked.connect(self._choose_dll)
        self.send_button.clicked.connect(self.send_can_frame)
        self.clear_can_button.clicked.connect(self.clear_can_frames)
        self.apply_filter_button.clicked.connect(self.apply_can_filter)
        self.pause_can_display_button.clicked.connect(self.toggle_can_display_pause)
        self.save_rx_button.clicked.connect(self.save_can_records)
        self.save_log_button.clicked.connect(self.save_system_log)
        self.iap_nav_button.clicked.connect(lambda: self._switch_page(0))
        self.can_debug_nav_button.clicked.connect(lambda: self._switch_page(1))
        self.motor_nav_button.clicked.connect(lambda: self._switch_page(2))
        self.machine_info_nav_button.clicked.connect(lambda: self._switch_page(3))

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                background: #f4f7fb;
                color: #111827;
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 13px;
            }
            QWidget#Content {
                background: #f4f7fb;
            }
            QWidget#HeaderBar {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0f172a, stop:1 #0b3b4a);
            }
            QLabel#AppIcon {
                min-width: 40px;
                min-height: 40px;
                max-width: 40px;
                max-height: 40px;
                border-radius: 11px;
                border: 1px solid rgba(255, 255, 255, 64);
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2f6df6, stop:0.45 #15a8df, stop:1 #10b981);
                color: #ffffff;
                font-weight: 900;
                qproperty-alignment: AlignCenter;
            }
            #TitleLabel {
                color: #f4fbf8;
                font-size: 23px;
                font-weight: 800;
                background: transparent;
            }
            #SubtitleLabel {
                color: #cbd5e1;
                font-size: 12px;
                background: transparent;
            }
            QWidget#StatusPill {
                background: rgba(255, 255, 255, 26);
                border: 1px solid rgba(255, 255, 255, 42);
                border-radius: 16px;
            }
            QLabel#StatusLabel {
                color: #f8fafc;
                background: transparent;
                font-weight: 700;
            }
            QLabel#StatusDot {
                border-radius: 5px;
                background: #22c55e;
            }
            QWidget#Stepper {
                background: #ffffff;
                border: 1px solid #e5e7eb;
                border-radius: 14px;
            }
            QWidget#StepPending, QWidget#StepDone, QWidget#StepActive {
                border-radius: 12px;
                border: 1px solid #edf0f4;
                background: #f9fafb;
            }
            QWidget#StepDone {
                background: #ecfdf5;
                border-color: #bbf7d0;
            }
            QWidget#StepActive {
                background: #fef2f2;
                border-color: #fecaca;
            }
            QLabel#StepNumberPending, QLabel#StepNumberDone, QLabel#StepNumberActive {
                min-width: 28px;
                min-height: 28px;
                max-width: 28px;
                max-height: 28px;
                border-radius: 14px;
                color: #ffffff;
                background: #9ca3af;
                font-weight: 800;
                qproperty-alignment: AlignCenter;
            }
            QLabel#StepNumberDone {
                background: #16a34a;
            }
            QLabel#StepNumberActive {
                background: #dc2626;
            }
            QLabel#StepTitle {
                background: transparent;
                color: #111827;
                font-size: 13px;
                font-weight: 800;
            }
            QLabel#StepDesc {
                background: transparent;
                color: #6b7280;
                font-size: 12px;
            }
            QGroupBox {
                border: 1px solid #e5e7eb;
                border-radius: 14px;
                margin-top: 0;
                background: #ffffff;
            }
            QGroupBox::title {
                color: transparent;
                height: 0;
                padding: 0;
                margin: 0;
            }
            QWidget#CardHeader {
                background: transparent;
                border-bottom: 1px solid #f1f5f9;
            }
            QLabel#CardMark {
                min-width: 4px;
                max-width: 4px;
                min-height: 18px;
                max-height: 18px;
                border-radius: 2px;
                background: #2563eb;
            }
            QLabel#CardTitle {
                color: #111827;
                background: transparent;
                font-size: 16px;
                font-weight: 900;
            }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                min-height: 32px;
                border: 1px solid #d1d5db;
                border-radius: 9px;
                background: #ffffff;
                padding: 3px 10px;
                color: #1f2937;
                font-weight: 600;
                selection-background-color: #2563eb;
            }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
                border-color: #2563eb;
            }
            QComboBox {
                min-width: 88px;
                padding-left: 12px;
                padding-right: 34px;
                font-weight: 800;
            }
            QComboBox:hover {
                border-color: #bfdbfe;
                background: #ffffff;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 30px;
                margin: 1px;
                border: 0;
                border-top-right-radius: 8px;
                border-bottom-right-radius: 8px;
                background: #f8fafc;
            }
            QComboBox::drop-down:hover {
                background: #eff6ff;
            }
            QComboBox::down-arrow {
                image: url(assets/chevron-down.svg);
                width: 12px;
                height: 12px;
            }
            QComboBox QAbstractItemView {
                border: 1px solid #d1d5db;
                border-radius: 8px;
                background: #ffffff;
                color: #111827;
                selection-background-color: #eff6ff;
                selection-color: #2563eb;
                padding: 4px;
                outline: 0;
            }
            QPushButton {
                min-height: 32px;
                border: 1px solid #2563eb;
                border-radius: 9px;
                background: #2563eb;
                color: #ffffff;
                padding: 4px 12px;
                font-weight: 800;
            }
            QPushButton:disabled {
                background: #e5e7eb;
                border-color: #e5e7eb;
                color: #9ca3af;
            }
            QPushButton#SecondaryButton {
                background: #f8fafc;
                color: #334155;
                border-color: #d1d5db;
            }
            QPushButton#DangerButton {
                background: #ffffff;
                color: #dc2626;
                border-color: #fecaca;
            }
            QPushButton#EmergencyButton {
                background: #b91c1c;
                color: #ffffff;
                border-color: #991b1b;
                font-weight: 900;
            }
            QPushButton#HeaderNavButton {
                min-height: 30px;
                border-radius: 16px;
                border: 1px solid rgba(255, 255, 255, 48);
                background: rgba(255, 255, 255, 18);
                color: #cbd5e1;
                padding: 2px 14px;
            }
            QPushButton#HeaderNavButton:checked {
                background: #ffffff;
                border-color: #ffffff;
                color: #0f3b4a;
            }
            QPushButton#FilterChip {
                min-height: 25px;
                border: 1px solid #d1d5db;
                border-radius: 12px;
                background: #ffffff;
                color: #334155;
                padding: 2px 9px;
                font-family: "Cascadia Mono", Consolas;
                font-weight: 800;
            }
            QPushButton#FilterChip:checked {
                border-color: #bfdbfe;
                background: #eff6ff;
                color: #2563eb;
            }
            QWidget#CanFilterPanel {
                background: transparent;
            }
            QWidget#FilterChipBar {
                background: transparent;
            }
            QLabel#MutedLabel, QLabel#SummaryLabel {
                color: #6b7280;
                background: transparent;
                font-size: 12px;
                font-weight: 700;
            }
            QLabel#SectionLabel {
                color: #111827;
                background: transparent;
                font-weight: 800;
            }
            QLabel#SummaryValue {
                color: #0f172a;
                background: transparent;
                font-weight: 900;
            }
            QLabel#MetricValue {
                font-family: "Cascadia Mono", Consolas;
                font-size: 14px;
                color: #0f172a;
                background: transparent;
                font-weight: 800;
            }
            QWidget#Metric, QWidget#SummaryItem {
                background: #f8fafc;
                border: 1px solid #edf2f7;
                border-radius: 11px;
            }
            QWidget#RoleBand {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #eff6ff, stop:1 #f8fafc);
                border: 1px solid #dbeafe;
                border-radius: 13px;
            }
            QWidget#VersionPanel {
                border-left: 1px solid #dbeafe;
            }
            QLabel#RoleValue {
                color: #1e3a8a;
                background: transparent;
                font-size: 24px;
                font-weight: 900;
            }
            QWidget#ErrorBox {
                background: #fef2f2;
                border: 1px solid #fecaca;
                border-radius: 12px;
            }
            QLabel#ErrorTitle {
                color: #dc2626;
                background: transparent;
                font-weight: 900;
            }
            QLabel#ErrorText {
                color: #7f1d1d;
                background: transparent;
                font-size: 13px;
            }
            QLabel#Tab, QLabel#TabActive {
                min-height: 30px;
                border-radius: 15px;
                padding: 0 12px;
                background: #ffffff;
                border: 1px solid #e5e7eb;
                color: #64748b;
                font-size: 12px;
                font-weight: 800;
                qproperty-alignment: AlignCenter;
            }
            QLabel#TabActive {
                background: #eff6ff;
                border-color: #bfdbfe;
                color: #2563eb;
            }
            QProgressBar {
                height: 14px;
                border: 0;
                border-radius: 7px;
                background: #e5e7eb;
                text-align: center;
                color: #0f172a;
                font-weight: 800;
            }
            QProgressBar::chunk {
                border-radius: 7px;
                background: #16a34a;
            }
            QTableWidget {
                background: #ffffff;
                alternate-background-color: #f8fafc;
                border: 1px solid #e5e7eb;
                border-radius: 12px;
                gridline-color: #f1f5f9;
                font-family: "Cascadia Mono", Consolas;
            }
            QHeaderView::section {
                background: #f8fafc;
                color: #475569;
                border: 0;
                border-bottom: 1px solid #e5e7eb;
                padding: 7px;
                font-weight: 900;
            }
            QPlainTextEdit#LogOutput {
                min-height: 110px;
                max-height: 150px;
                background: #0b1220;
                color: #dbeafe;
                border: 0;
                border-radius: 10px;
                font-family: "Cascadia Mono", Consolas;
            }
            QWidget#MotorHero {
                border: 1px solid #bae6fd;
                border-radius: 14px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ecfeff, stop:1 #eff6ff);
            }
            QLabel#MotorHeroTitle {
                color: #0f172a;
                background: transparent;
                font-size: 22px;
                font-weight: 900;
            }
            QLabel#MotorHeroText {
                color: #527083;
                background: transparent;
                font-size: 12px;
                font-weight: 700;
            }
            QLabel#MotorConnectionBadge {
                min-height: 30px;
                border-radius: 15px;
                border: 1px solid #cbd5e1;
                background: #f8fafc;
                color: #64748b;
                padding: 0 13px;
                font-weight: 900;
            }
            QLabel#MotorConnectionBadge[connected="true"] {
                border-color: #86efac;
                background: #f0fdf4;
                color: #15803d;
            }
            QWidget#PositionPanel {
                border-radius: 13px;
                border: 1px solid #bfdbfe;
                background: #eff6ff;
            }
            QLabel#MotorPositionValue {
                color: #0f3b70;
                background: transparent;
                font-family: "Cascadia Mono", Consolas;
                font-size: 30px;
                font-weight: 900;
            }
            QLabel#MotorSecondaryValue {
                color: #47708c;
                background: transparent;
                font-family: "Cascadia Mono", Consolas;
                font-size: 15px;
                font-weight: 800;
            }
            QLabel#MotorMonoValue, QLabel#MotorStatusValue {
                color: #0f172a;
                background: transparent;
                font-family: "Cascadia Mono", Consolas;
                font-weight: 800;
            }
            QWidget#MotorMetric {
                border: 1px solid #e2e8f0;
                border-radius: 10px;
                background: #f8fafc;
            }
            QPlainTextEdit#MotorEventLog {
                background: #0b1f2a;
                color: #bae6fd;
                border: 0;
                border-radius: 9px;
                font-family: "Cascadia Mono", Consolas;
            }
            QWidget#CanDebugConnectionBar {
                border: 1px solid #b8d8d2;
                border-radius: 12px;
                background: #edf8f5;
            }
            QLabel#CanDebugTitle {
                color: #123c45;
                background: transparent;
                font-size: 20px;
                font-weight: 900;
            }
            QLabel#CanDebugConnectionBadge {
                min-height: 30px;
                border-radius: 15px;
                border: 1px solid #cbd5e1;
                background: #ffffff;
                color: #64748b;
                padding: 0 13px;
                font-weight: 900;
            }
            QLabel#CanDebugConnectionBadge[connected="true"] {
                border-color: #70c1a9;
                background: #e7f8f0;
                color: #08745a;
            }
            QPushButton#ColorSwatchButton {
                min-width: 90px;
                font-family: "Cascadia Mono", Consolas;
            }
            QStackedWidget#PageStack {
                background: #f4f7fb;
            }
            QScrollArea#MainScrollArea {
                border: 0;
                background: #f4f7fb;
            }
            QScrollArea#MainScrollArea > QWidget > QWidget {
                background: #f4f7fb;
            }
            """
        )

    @staticmethod
    def _add_labeled_widget(layout: QGridLayout, row: int, column: int, label_text: str, widget: QWidget) -> None:
        label = QLabel(label_text)
        label.setObjectName("MutedLabel")
        layout.addWidget(label, row, column)
        layout.addWidget(widget, row + 1, column)

    def _set_card_layout(self, group: QGroupBox, body_layout) -> None:
        header = self._card_header(group.title())
        body = QWidget()
        body.setObjectName("CardBody")
        body.setLayout(body_layout)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(body)
        group.setLayout(layout)

    @staticmethod
    def _card_header(title: str) -> QWidget:
        mark = QLabel()
        mark.setObjectName("CardMark")
        title_label = QLabel(title)
        title_label.setObjectName("CardTitle")

        layout = QHBoxLayout()
        layout.setContentsMargins(16, 12, 16, 10)
        layout.setSpacing(9)
        layout.addWidget(mark)
        layout.addWidget(title_label)
        layout.addStretch(1)

        header = QWidget()
        header.setObjectName("CardHeader")
        header.setLayout(layout)
        return header

    @staticmethod
    def _metric(title: str, value: QLabel, title_store: list[QLabel] | None = None) -> QWidget:
        title_label = QLabel(title)
        title_label.setObjectName("MutedLabel")
        if title_store is not None:
            title_store.append(title_label)
        metric_layout = QVBoxLayout()
        metric_layout.setContentsMargins(8, 6, 8, 6)
        metric_layout.addWidget(title_label)
        metric_layout.addWidget(value)
        metric = QWidget()
        metric.setLayout(metric_layout)
        metric.setObjectName("Metric")
        return metric

    def _make_step(self, number: str, title: str, desc: str, state: str) -> QWidget:
        suffix = state.replace("Step", "")
        number_label = QLabel(number)
        number_label.setObjectName(f"StepNumber{suffix}")
        title_label = QLabel(title)
        title_label.setObjectName("StepTitle")
        desc_label = QLabel(desc)
        desc_label.setObjectName("StepDesc")

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        text_layout.addWidget(title_label)
        text_layout.addWidget(desc_label)

        layout = QHBoxLayout()
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(10)
        layout.addWidget(number_label)
        layout.addLayout(text_layout)

        step = QWidget()
        step.setObjectName(state)
        step.setLayout(layout)
        self.step_title_labels.append(title_label)
        self.step_desc_labels.append(desc_label)
        self.step_number_labels.append(number_label)
        self.step_widgets.append(step)
        return step

    def _set_step_state(self, index: int, state: str, desc: str | None = None) -> None:
        if not 0 <= index < len(self.step_widgets):
            return
        suffix = state.replace("Step", "")
        self.step_widgets[index].setObjectName(state)
        self.step_number_labels[index].setObjectName(f"StepNumber{suffix}")
        if desc is not None:
            self.step_desc_labels[index].setText(desc)
        for widget in (self.step_widgets[index], self.step_number_labels[index]):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()

    def _set_device_role(self, role: str) -> None:
        self.role_value_label.setText(role)
        self._set_step_state(2, "StepDone", f"当前角色 {role}")

    @Slot(str)
    def _handle_upgrade_log(self, message: str) -> None:
        self._log(message)
        prefix = "当前角色 "
        if message.startswith(prefix):
            self._set_device_role(message.removeprefix(prefix).strip())

    def _start_upgrade_timer(self) -> None:
        self._upgrade_started_at = self._monotonic()
        self.upgrade_elapsed_value.setText("计时中")

    def _finish_upgrade_elapsed(self) -> str:
        if self._upgrade_started_at is None:
            elapsed = "00:00:00.000"
        else:
            elapsed = self._format_elapsed_time(max(0.0, self._monotonic() - self._upgrade_started_at))
        self.upgrade_elapsed_value.setText(elapsed)
        self._upgrade_started_at = None
        return elapsed

    @staticmethod
    def _format_elapsed_time(elapsed_seconds: float) -> str:
        total_milliseconds = int(round(elapsed_seconds * 1000))
        milliseconds = total_milliseconds % 1000
        total_seconds = total_milliseconds // 1000
        seconds = total_seconds % 60
        total_minutes = total_seconds // 60
        minutes = total_minutes % 60
        hours = total_minutes // 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"

    @staticmethod
    def _summary_item(title: str, value: QLabel) -> QWidget:
        title_label = QLabel(title)
        title_label.setObjectName("SummaryLabel")
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(5)
        layout.addWidget(title_label)
        layout.addWidget(value)
        item = QWidget()
        item.setObjectName("SummaryItem")
        item.setLayout(layout)
        return item

    def _dll_config_item(self) -> QWidget:
        title_label = QLabel("DLL 配置")
        title_label.setObjectName("SummaryLabel")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.dll_config_value, 1)
        row.addWidget(self.dll_browse_button)

        layout = QVBoxLayout()
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(5)
        layout.addWidget(title_label)
        layout.addLayout(row)
        item = QWidget()
        item.setObjectName("SummaryItem")
        item.setLayout(layout)
        return item

    @Slot()
    def _choose_bin(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 APP bin", "", "Binary (*.bin);;All files (*.*)")
        if path:
            self.bin_path_input.setText(path)
            self._load_firmware_for_log(path)

    @Slot()
    def _choose_dll(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 64 位 ControlCANFD.dll",
            str(self.selected_dll_path.parent),
            "ControlCANFD.dll (ControlCANFD.dll);;DLL (*.dll);;All files (*.*)",
        )
        if path:
            self._set_dll_path(Path(path))

    def _set_dll_path(self, path: str | Path) -> None:
        self.selected_dll_path = Path(path)
        self.dll_path_label.setText(str(self.selected_dll_path))
        self.dll_config_value.setText(
            "使用默认 ControlCANFD.dll"
            if self.selected_dll_path == DEFAULT_DLL_PATH
            else "已选择本地 DLL"
        )

    @Slot(int)
    def _switch_page(self, index: int) -> None:
        self.page_stack.setCurrentIndex(index)
        self.iap_nav_button.setChecked(index == 0)
        self.can_debug_nav_button.setChecked(index == 1)
        self.motor_nav_button.setChecked(index == 2)
        self.machine_info_nav_button.setChecked(index == 3)
        self.device_profile_input.setVisible(index == 0)
        self.app_icon_label.setText(("IAP", "CAN", "M", "MI")[index])

    @Slot()
    def open_device(self) -> None:
        try:
            if _is_zcanpro_running():
                raise RuntimeError("ZCanPro 正在运行，请先关闭 ZCanPro 后再打开设备")
            if self.driver:
                self.close_device()
            driver = self._make_driver()
            options = self._make_options()
            if not driver.open(
                options.device_type,
                options.device_index,
                options.channel,
                options.baudrate,
                can_fd=options.can_fd,
                data_baudrate=options.data_baudrate,
            ):
                raise RuntimeError(driver.last_error)
            self.driver = driver
            self._connection_mode = IAP_CONNECTION_MODE
            message = (
                f"CAN FD 已打开：CH{driver.channel}，"
                f"{options.baudrate / 1_000_000:g}/{options.data_baudrate / 1_000_000:g} Mbps"
            )
            self._set_connection_status(True, message)
            self._log(f"IAP {message}")
        except Exception as exc:
            self._set_connection_status(False, f"打开失败：{exc}", failed=True)
            self._show_error(str(exc))

    def open_motor_device(self, config: MotorCanFdConfig) -> None:
        if _is_zcanpro_running():
            raise RuntimeError("ZCanPro 正在运行，请先关闭 ZCanPro 后再打开设备")
        if self.driver:
            self.close_device()
        driver = ZlgVciCanDriver(config.dll_path, change_working_directory=False)
        if not driver.open(
            config.device_type,
            config.device_index,
            config.channel,
            config.arbitration_baudrate,
            can_fd=True,
            data_baudrate=config.data_baudrate,
        ):
            raise RuntimeError(driver.last_error)
        self.driver = driver
        self._connection_mode = "canfd"
        message = (
            f"CAN FD 已打开：CH{driver.channel}，"
            f"{config.arbitration_baudrate / 1_000_000:g}/{config.data_baudrate / 1_000_000:g} Mbps"
        )
        self._set_connection_status(True, message)
        self._log(message)

    def open_debug_device(self, config: CanDebugConfig) -> None:
        self._open_canfd_tool_device(config, "CAN 调试设备")

    def open_machine_info_device(self, config: MachineInfoCanConfig) -> None:
        self._open_canfd_tool_device(config, "MachineInfo 设备")

    def _open_canfd_tool_device(
        self,
        config: CanDebugConfig | MachineInfoCanConfig,
        source: str,
    ) -> None:
        if _is_zcanpro_running():
            raise RuntimeError("ZCanPro 正在运行，请先关闭 ZCanPro 后再打开设备")
        if self.driver:
            self.close_device()
        driver = ZlgVciCanDriver(config.dll_path, change_working_directory=False)
        if not driver.open(
            config.device_type,
            DEFAULT_DEVICE_INDEX,
            config.channel,
            config.arbitration_baudrate,
            can_fd=True,
            data_baudrate=config.data_baudrate,
        ):
            raise RuntimeError(driver.last_error)
        self.driver = driver
        self._connection_mode = "canfd_tools"
        message = (
            f"CAN FD 盒子已打开：CH{driver.channel}，"
            f"{config.arbitration_baudrate / 1_000_000:g}/"
            f"{config.data_baudrate / 1_000_000:g} Mbps"
        )
        self._set_connection_status(True, message)
        self._log(f"{source}{message}")

    def _send_debug_frame(self, frame: CanFrame) -> None:
        if not self._can_connected or self._connection_mode != "canfd_tools":
            raise RuntimeError("请在 CAN 调试或 MachineInfo 页打开 CAN FD 盒子")
        driver = self._ensure_driver_open()
        if not driver.send(frame):
            raise RuntimeError(driver.last_error)
        self._append_can_frame("TX", frame)

    def _send_motor_frame(self, frame: CanFrame) -> None:
        if not self._can_connected or self._connection_mode != "canfd":
            raise RuntimeError("当前不是 CAN FD 连接，请在电机测试页打开 CAN FD")
        driver = self._ensure_driver_open()
        if not driver.send(frame):
            raise RuntimeError(driver.last_error)
        self._append_can_frame("TX", frame)

    def _send_motor_periodic_frame(self, frame: CanFrame) -> None:
        if not self._can_connected or self._connection_mode != "canfd" or not self.driver:
            raise RuntimeError("CAN FD 连接已断开")
        if not self.driver.send(frame):
            raise RuntimeError(self.driver.last_error)
        self.motor_page.record_periodic_tx(frame)

    @Slot()
    def close_device(self) -> None:
        self.can_poll_timer.stop()
        if hasattr(self, "motor_page"):
            self.motor_page.emergency_stop(silent=True)
        if self.driver:
            self.driver.close()
            if hasattr(self.driver, "shutdown"):
                self.driver.shutdown()
            self.driver = None
        self._connection_mode = None
        self._set_connection_status(False, "未连接")
        self._log("CAN 设备已关闭")

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.can_poll_timer.stop()
        if hasattr(self, "can_debug_page"):
            self.can_debug_page.stop_battery_simulation()
        if hasattr(self, "machine_info_page"):
            self.machine_info_page.shutdown()
        if hasattr(self, "motor_page"):
            self.motor_page.shutdown()
        if self.driver:
            if hasattr(self.driver, "shutdown"):
                self.driver.shutdown()
            else:
                self.driver.close()
            self.driver = None
        super().closeEvent(event)

    @Slot()
    def query_role(self) -> None:
        was_polling = self.can_poll_timer.isActive()
        self.can_poll_timer.stop()
        try:
            driver = self._ensure_driver_open()
            controller = IapUpgradeController(driver, self._make_protocol(), on_log=self._handle_upgrade_log, on_frame=self._append_can_frame)
            role = controller.query_role()
            self._set_device_role(role)
        except Exception as exc:
            self._show_error(str(exc))
        finally:
            if was_polling and self._can_connected:
                self.can_poll_timer.start()

    @Slot()
    def query_software_version(self) -> None:
        was_polling = self.can_poll_timer.isActive()
        self.can_poll_timer.stop()
        try:
            driver = self._ensure_driver_open()
            controller = IapUpgradeController(
                driver,
                self._make_protocol(),
                on_log=self._handle_upgrade_log,
                on_frame=self._append_can_frame,
            )
            self.version_value_label.setText(controller.query_software_version())
        except Exception as exc:
            self._show_error(str(exc))
        finally:
            if was_polling and self._can_connected:
                self.can_poll_timer.start()

    @Slot()
    def start_upgrade(self) -> None:
        try:
            self.error_box.hide()
            self._start_upgrade_timer()
            self._set_step_state(3, "StepActive", "写入中")
            image = self._load_firmware()
            driver = self._ensure_driver_open()
            protocol = self._make_protocol()
            controller = IapUpgradeController(driver, protocol, on_log=lambda _: None)
            self.upgrade_thread = QThread(self)
            self.upgrade_worker = UpgradeWorker(controller, image, self._make_options())
            controller.on_log = self.upgrade_worker.log.emit
            controller.on_progress = self.upgrade_worker.progress.emit
            controller.on_frame = self.upgrade_worker.handle_can_frame
            self.upgrade_worker.moveToThread(self.upgrade_thread)
            self.upgrade_thread.started.connect(self.upgrade_worker.run)
            self.upgrade_worker.log.connect(self._handle_upgrade_log)
            self.upgrade_worker.progress.connect(self.progress_bar.setValue)
            self.upgrade_worker.can_frame.connect(self._append_can_frame)
            self.upgrade_worker.succeeded.connect(self._upgrade_succeeded)
            self.upgrade_worker.failed.connect(self._upgrade_failed)
            self.upgrade_worker.finished.connect(self._upgrade_finished)
            self.upgrade_worker.finished.connect(self.upgrade_thread.quit)
            self.upgrade_worker.finished.connect(self.upgrade_worker.deleteLater)
            self.upgrade_thread.finished.connect(self.upgrade_thread.deleteLater)
            self._set_busy(True)
            self.upgrade_thread.start()
        except Exception as exc:
            self._upgrade_failed(str(exc))

    @Slot()
    def simulate_battery_upgrade(self) -> None:
        try:
            self.error_box.hide()
            image = self._load_firmware()
            profile = self._battery_device_profile()
            protocol = IapProtocol(target_id=profile.target_id, can_id=profile.can_id)
            options = UpgradeOptions(
                pre_upgrade_wakeup_ms=profile.pre_upgrade_wakeup_ms,
                disable_target_can_messages=profile.disable_target_can_messages,
                wait_data_frame_ack=profile.wait_data_frame_ack,
                ignore_validate_ack_failure=profile.ignore_validate_ack_failure,
            )
            preview_frames = build_upgrade_preview_frames(protocol, image, options, max_data_frames=10)
            data_frame_count = sum(
                1 for _, frame in preview_frames if frame.data[2] == CMD_FILL_SEGMENT_DATA
            )

            self._log("模拟电池升级：忽略 ACK，仅打印将发送的 CAN 帧")
            self._log(
                f"模拟配置：{profile.name}, CANID=0x{profile.can_id:X}, "
                f"目标ID=0x{profile.target_id:02X}, 固件大小={image.size} bytes"
            )
            for index, (label, frame) in enumerate(preview_frames, start=1):
                self._log(
                    f"模拟TX[{index:02d}] {label}：ID=0x{frame.id:X}, "
                    f"Len={frame.dlc}, Data={self._format_frame_data(frame)}"
                )
            self._log(f"模拟电池升级：已打印首段自升级数据包前 {data_frame_count} 包")
        except Exception as exc:
            self._show_error(str(exc))

    @Slot()
    def stop_upgrade(self) -> None:
        if self.upgrade_worker:
            self.upgrade_worker.cancel()
            self._log("已请求停止升级")

    @Slot()
    def _upgrade_finished(self) -> None:
        self._set_busy(False)
        self.upgrade_worker = None
        self.upgrade_thread = None

    @Slot()
    def _upgrade_succeeded(self) -> None:
        self.error_box.hide()
        self._set_step_state(3, "StepDone", "写入完成")
        self._set_step_state(4, "StepDone", "升级成功")
        elapsed = self._finish_upgrade_elapsed()
        self._log(f"升级成功，耗时：{elapsed}")
        self._show_information("升级成功", f"升级成功，设备已跳转到 APP。\n升级耗时：{elapsed}")

    @Slot(str)
    def _upgrade_failed(self, message: str) -> None:
        elapsed = self._finish_upgrade_elapsed()
        self._log(f"升级耗时：{elapsed}")
        self._show_error(f"{message}\n升级耗时：{elapsed}")

    @Slot()
    def send_can_frame(self) -> None:
        try:
            driver = self._ensure_driver_open()
            frame = CanFrame(id=self._parse_int(self.send_can_id_input.text()), data=self._parse_data_bytes(self.send_data_input.text()))
            if not driver.send(frame):
                raise RuntimeError(driver.last_error)
            self._append_can_frame("TX", frame)
        except Exception as exc:
            self._show_error(str(exc))

    @Slot()
    def _poll_can_frames(self) -> None:
        if not self._can_connected or not self.driver or not self.driver.is_open():
            return
        if self.upgrade_thread and self.upgrade_thread.isRunning():
            return

        for _ in range(100):
            frame = self.driver.receive(0)
            if frame is None:
                return
            self._append_can_frame("RX", frame)

    def _make_driver(self) -> ZlgVciCanDriver:
        return ZlgVciCanDriver(str(self.selected_dll_path), change_working_directory=False)

    @Slot(int)
    def _handle_device_profile_changed(self, _index: int) -> None:
        self._apply_device_profile(self._selected_device_profile())

    def _selected_device_profile(self) -> DeviceProfile:
        profile = self.device_profile_input.currentData()
        if isinstance(profile, DeviceProfile):
            return profile
        return next(profile for profile in DEVICE_PROFILES if profile.name == DEFAULT_DEVICE_PROFILE_NAME)

    @staticmethod
    def _battery_device_profile() -> DeviceProfile:
        return next(profile for profile in DEVICE_PROFILES if "电池" in profile.name)

    def _apply_device_profile(self, profile: DeviceProfile) -> None:
        self.target_id_input.setText(self._format_can_id(profile.target_id))
        self.can_id_input.setText(self._format_can_id(profile.can_id))
        if hasattr(self, "send_can_id_input"):
            self.send_can_id_input.setText(self._format_can_id(profile.can_id))

    @staticmethod
    def _format_can_id(value: int) -> str:
        return f"0x{value:x}"

    def _ensure_driver_open(self) -> CanDriver:
        if self.driver and self.driver.is_open():
            return self.driver
        if _is_zcanpro_running():
            raise RuntimeError("ZCanPro 正在运行，请先关闭 ZCanPro 后再打开设备")
        driver = self._make_driver()
        options = self._make_options()
        if not driver.open(
            options.device_type,
            options.device_index,
            options.channel,
            options.baudrate,
            can_fd=options.can_fd,
            data_baudrate=options.data_baudrate,
        ):
            raise RuntimeError(driver.last_error)
        self.driver = driver
        self._connection_mode = IAP_CONNECTION_MODE
        message = (
            f"CAN FD 已打开：CH{driver.channel}，"
            f"{options.baudrate / 1_000_000:g}/{options.data_baudrate / 1_000_000:g} Mbps"
        )
        self._set_connection_status(True, message)
        self._log(f"IAP {message}")
        return driver

    def _make_protocol(self) -> IapProtocol:
        return IapProtocol(target_id=self._parse_int(self.target_id_input.text()), can_id=self._parse_int(self.can_id_input.text()))

    def _make_options(self) -> UpgradeOptions:
        return UpgradeOptions(
            device_type=DEFAULT_DEVICE_TYPE,
            device_index=DEFAULT_DEVICE_INDEX,
            channel=self.channel_input.currentData(),
            baudrate=self.baudrate_input.currentData(), 
            can_fd=True,
            data_baudrate=DEFAULT_DATA_BAUDRATE,
            pre_upgrade_wakeup_ms=self._selected_device_profile().pre_upgrade_wakeup_ms,
            disable_target_can_messages=self._selected_device_profile().disable_target_can_messages,
            wait_data_frame_ack=self._selected_device_profile().wait_data_frame_ack,
            ignore_validate_ack_failure=self._selected_device_profile().ignore_validate_ack_failure,
            app_start_wait_ms=self._selected_device_profile().app_start_wait_ms,
            app_total_wait_ms=self._selected_device_profile().app_total_wait_ms,
        )

    def _load_firmware(self) -> FirmwareImage:
        path = self.bin_path_input.text().strip()
        if not path:
            raise ValueError("请选择 APP bin")
        if not Path(path).exists():
            raise ValueError(f"bin 不存在：{path}")
        image = FirmwareImage.from_file(path)
        self._log_firmware_info(image)
        return image

    def _load_firmware_for_log(self, path: str) -> None:
        try:
            self._log_firmware_info(FirmwareImage.from_file(path))
        except Exception as exc:
            self._log(f"固件检查失败：{exc}")

    def _log_firmware_info(self, image: FirmwareImage) -> None:
        self.firmware_size_value.setText(f"{image.size:,} bytes")
        self.firmware_sp_value.setText(f"0x{image.initial_sp:08X}")
        self.firmware_reset_value.setText(f"0x{image.reset_vector:08X}")
        self._set_step_state(1, "StepDone", "APP 固件已读取")
        self._log(f"固件大小：{image.size} bytes")
        self._log(f"Initial SP：0x{image.initial_sp:08X}")
        self._log(f"ResetVector：0x{image.reset_vector:08X}")
        for warning in image.warnings:
            self._log(f"警告：{warning}")

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self.can_poll_timer.stop()
        elif self._can_connected:
            self.can_poll_timer.start()

        iap_connected = self._can_connected and self._connection_mode == IAP_CONNECTION_MODE
        self.start_upgrade_button.setEnabled(not busy and iap_connected)
        self.simulate_battery_button.setEnabled(not busy)
        self.open_button.setEnabled(not busy and not self._can_connected)
        self.close_button.setEnabled(not busy and self._can_connected)
        self.query_role_button.setEnabled(not busy and iap_connected)
        self.query_version_button.setEnabled(not busy and iap_connected)
        self.send_button.setEnabled(not busy and self._can_connected)
        self.stop_button.setEnabled(busy)
        for config_widget in (
            self.device_profile_input,
            self.channel_input,
            self.baudrate_input,
            self.target_id_input,
            self.can_id_input,
        ):
            config_widget.setEnabled(not busy)

    def _set_connection_status(self, connected: bool, message: str, failed: bool = False) -> None:
        self._can_connected = connected
        self.can_status_label.setText(message)
        color = "#22c55e" if connected else "#dc2626" if failed else "#94a3b8"
        self.can_status_dot.setStyleSheet(f"border-radius: 5px; background: {color};")
        if hasattr(self, "motor_page"):
            motor_connected = connected and self._connection_mode == "canfd"
            motor_message = message if motor_connected else "CAN FD 未连接"
            self.motor_page.set_connected(motor_connected, motor_message)
        if hasattr(self, "can_debug_page"):
            debug_connected = connected and self._connection_mode == "canfd_tools"
            debug_message = message if debug_connected else "CAN 未连接"
            self.can_debug_page.set_connected(debug_connected, debug_message)
        if hasattr(self, "machine_info_page"):
            machine_info_connected = connected and self._connection_mode == "canfd_tools"
            machine_info_message = message if machine_info_connected else "CAN 未连接"
            self.machine_info_page.set_connected(machine_info_connected, machine_info_message)
        if connected:
            self._set_step_state(0, "StepDone", "USBCANFD-200U 已打开")
        elif failed:
            self._set_step_state(0, "StepActive", "打开失败")
        else:
            self._set_step_state(0, "StepPending", "等待打开")
        if connected:
            self.send_can_id_input.setText(self.can_id_input.text())
        self._set_busy(False)
        if not connected:
            self.can_poll_timer.stop()

    def _append_can_frame(self, direction: str, frame: CanFrame) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if hasattr(self, "can_debug_page"):
            self.can_debug_page.handle_frame(direction, frame)
        if hasattr(self, "machine_info_page"):
            self.machine_info_page.handle_frame(direction, frame)
        if hasattr(self, "motor_page"):
            self.motor_page.handle_frame(direction, frame)
        if direction == "TX":
            self._log(
                f"[{timestamp}] {direction} ID=0x{frame.id:X}, "
                f"Len={frame.dlc}, Data={self._format_frame_data(frame)}"
            )
        popped_record: tuple[str, str, CanFrame] | None = None
        if direction == "RX":
            self._rx_save_records.append((timestamp, frame))
        self._can_save_records.append((direction, timestamp, frame))
        self._can_records.append((direction, timestamp, frame))
        if len(self._can_records) > MAX_CAN_ROWS:
            popped_record = self._can_records.pop(0)
        self._remember_can_id(frame.id)
        if not self._can_display_paused:
            if popped_record and self._frame_passes_filter(popped_record[2]) and self.can_table.rowCount() > 0:
                self.can_table.removeRow(0)
            if self._frame_passes_filter(frame):
                self._add_can_table_row(direction, timestamp, frame)
        self._update_filter_status(self.can_table.rowCount())
        self.rx_count_value.setText(f"{len(self._rx_save_records)} 帧")

    def _remember_can_id(self, can_id: int) -> None:
        if can_id in self.known_can_ids:
            return
        self.known_can_ids.add(can_id)
        self._refresh_filter_chips()

    def _refresh_filter_chips(self) -> None:
        while self.filter_chip_layout.count():
            item = self.filter_chip_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.can_filter_buttons.clear()
        for can_id in sorted(self.known_can_ids):
            button = QPushButton(f"0x{can_id:X}")
            button.setObjectName("FilterChip")
            button.setCheckable(True)
            button.setChecked(can_id in self.filter_can_ids)
            button.clicked.connect(lambda checked, value=can_id: self._toggle_filter_can_id(value, checked))
            self.can_filter_buttons[can_id] = button
            self.filter_chip_layout.addWidget(button)
        self.filter_chip_layout.addStretch(1)

    def _toggle_filter_can_id(self, can_id: int, checked: bool) -> None:
        if checked:
            self.filter_can_ids.add(can_id)
        else:
            self.filter_can_ids.discard(can_id)
        self._sync_filter_input()
        self._render_can_table()

    @Slot()
    def apply_can_filter(self) -> None:
        try:
            self.filter_can_ids = self._parse_filter_ids(self.can_filter_input.text())
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self._sync_filter_input()
        self._refresh_filter_chips()
        if self._can_display_paused:
            self._update_filter_status(self.can_table.rowCount())
        else:
            self._render_can_table()

    @Slot()
    def clear_can_filter(self) -> None:
        self.filter_can_ids.clear()
        self.can_filter_input.clear()
        self._refresh_filter_chips()
        if self._can_display_paused:
            self._update_filter_status(self.can_table.rowCount())
        else:
            self._render_can_table()

    @Slot()
    def toggle_can_display_pause(self) -> None:
        self._can_display_paused = not self._can_display_paused
        self.pause_can_display_button.setText("继续显示" if self._can_display_paused else "暂停显示")
        if self._can_display_paused:
            self._update_filter_status(self.can_table.rowCount())
        else:
            self._render_can_table()

    @Slot()
    def clear_can_frames(self) -> None:
        self._can_records.clear()
        self._can_save_records.clear()
        self._rx_save_records.clear()
        self.can_table.setRowCount(0)
        self._update_filter_status(0)
        self.rx_count_value.setText("0 帧")

    @Slot()
    def save_can_records(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "保存 CAN 数据", "can.csv", "CSV (*.csv);;All files (*.*)")
        if not path:
            return
        try:
            saved_count = self._save_can_records(Path(path))
        except Exception as exc:
            self._show_error(str(exc))
            return
        self._log(f"已保存 CAN 数据：{saved_count} 帧，{path}")

    @Slot()
    def save_system_log(self) -> None:
        text = self.log_output.toPlainText()
        if not text:
            self._show_information("导出日志", "系统日志为空，无可导出内容。")
            return
        default_name = f"system_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(self, "导出系统日志", default_name, "文本 (*.txt);;All files (*.*)")
        if not path:
            return
        try:
            Path(path).write_text(text, encoding="utf-8")
        except Exception as exc:
            self._show_error(str(exc))
            return
        self._log(f"已导出系统日志：{path}")

    def save_rx_can_records(self) -> None:
        self.save_can_records()

    def _save_can_records(self, path: str | Path) -> int:
        with Path(path).open("w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output)
            writer.writerow(["direction", "time", "canid", "len", "data"])
            for direction, timestamp, frame in self._can_save_records:
                writer.writerow([direction, timestamp, f"0x{frame.id:X}", frame.dlc, self._format_frame_data(frame)])
        return len(self._can_save_records)

    def _save_rx_can_records(self, path: str | Path) -> int:
        return self._save_can_records(path)

    def _sync_filter_input(self) -> None:
        self.can_filter_input.setText(", ".join(f"0x{can_id:X}" for can_id in sorted(self.filter_can_ids)))

    def _render_can_table(self) -> None:
        self.can_table.setRowCount(0)
        visible_records = [
            (direction, timestamp, frame)
            for direction, timestamp, frame in self._can_records
            if self._frame_passes_filter(frame)
        ]
        for direction, timestamp, frame in visible_records:
            self._add_can_table_row(direction, timestamp, frame)
        self._update_filter_status(len(visible_records))

    def _frame_passes_filter(self, frame: CanFrame) -> bool:
        return not self.filter_can_ids or frame.id in self.filter_can_ids

    def _update_filter_status(self, visible_count: int) -> None:
        total_count = len(self._can_records)
        prefix = "暂停显示" if self._can_display_paused else "显示"
        if self.filter_can_ids:
            self.can_filter_status_label.setText(f"{prefix} {visible_count} / {total_count} 帧")
        else:
            self.can_filter_status_label.setText(f"{prefix} {visible_count} / {total_count} 帧")

    def _add_can_table_row(self, direction: str, timestamp: str, frame: CanFrame) -> None:
        row = self.can_table.rowCount()
        self.can_table.insertRow(row)
        values = [
            direction,
            timestamp,
            f"0x{frame.id:X}",
            str(frame.dlc),
            self._format_frame_data(frame),
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column in (0, 2, 3):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if column == 0:
                item.setForeground(Qt.GlobalColor.blue if direction == "RX" else Qt.GlobalColor.darkYellow)
            self.can_table.setItem(row, column, item)
        self.can_table.scrollToBottom()

    @staticmethod
    def _parse_filter_ids(text: str) -> set[int]:
        normalized = text.replace(",", " ").replace(";", " ").strip()
        if not normalized:
            return set()
        result = set()
        for token in normalized.split():
            try:
                result.add(int(token, 0) if token.lower().startswith("0x") else int(token, 16))
            except ValueError as exc:
                raise ValueError(f"CANID 格式无效：{token}") from exc
        return result

    @staticmethod
    def _format_frame_data(frame: CanFrame) -> str:
        return bytes(frame.data[: frame.dlc]).hex(" ").upper()

    @staticmethod
    def _parse_data_bytes(text: str) -> bytes:
        normalized = text.replace(",", " ").replace(";", " ").strip()
        if not normalized:
            return b""

        parts = normalized.split()
        if len(parts) == 1 and parts[0].lower().startswith("0x") and len(parts[0]) > 4:
            raw = parts[0][2:]
            if len(raw) % 2:
                raise ValueError("Data hex 长度必须是偶数")
            data = bytes.fromhex(raw)
        elif len(parts) == 1 and len(parts[0]) > 2:
            raw = parts[0]
            if len(raw) % 2:
                raise ValueError("Data hex 长度必须是偶数")
            data = bytes.fromhex(raw)
        else:
            data = bytes(int(part, 16) for part in parts)

        if len(data) > 8:
            raise ValueError("CAN 2.0 数据长度不能超过 8 bytes")
        return data

    def _log(self, message: str) -> None:
        self.log_output.appendPlainText(message)

    def _show_error(self, message: str) -> None:
        self.error_title_label.setText("操作失败")
        self.error_text_label.setText(message)
        self.error_box.show()
        self._set_step_state(3, "StepActive", "需要处理")
        self._log(f"错误：{message}")
        QMessageBox.warning(self, "错误", message)

    def _show_information(self, title: str, message: str) -> None:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setWindowTitle(title)
        dialog.setText(message)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.adjustSize()
        dialog.move(self._dialog_position_for_size(dialog.width(), dialog.height()))
        dialog.exec()

    def _dialog_position_for_size(self, dialog_width: int, dialog_height: int) -> QPoint:
        window_rect = self.frameGeometry() if self.isVisible() else self.geometry()
        target_x = window_rect.x() + window_rect.width() // 2
        target_y = window_rect.y() + window_rect.height() // 3
        x = target_x - dialog_width // 2
        y = target_y - dialog_height // 2
        max_x = window_rect.right() - dialog_width
        max_y = window_rect.bottom() - dialog_height
        return QPoint(
            max(window_rect.left(), min(x, max_x)),
            max(window_rect.top(), min(y, max_y)),
        )

    @staticmethod
    def _parse_int(text: str) -> int:
        return int(text.strip(), 0)


def _is_zcanpro_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ZCANPRO.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return False
    return "ZCANPRO.exe" in result.stdout


if __name__ == "__main__":
    app = QApplication([])
    window = Widget()
    window.show()
    sys.exit(app.exec())
