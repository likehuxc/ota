# This Python file uses the following encoding: utf-8
from __future__ import annotations

import csv
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
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
    QSizePolicy,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from d7_pmu_iap_tool.can.broker_can_driver import ZlgCanBrokerDriver
from d7_pmu_iap_tool.can.can_frame import CanFrame
from d7_pmu_iap_tool.iap.firmware_image import FirmwareImage
from d7_pmu_iap_tool.iap.iap_protocol import IapProtocol
from d7_pmu_iap_tool.iap.iap_upgrade_controller import IapUpgradeController, UpgradeOptions

# ZLG's 32-bit zlgcan.dll (loaded by the 32-bit broker subprocess). It pulls
# device backends from the sibling kerneldlls\ folder.
DEFAULT_DLL_PATH = Path(r"C:\Program Files (x86)\ZCANPRO\zlgcan.dll")
DEFAULT_DEVICE_TYPE = 3  # ZCAN_USBCAN1 (USBCAN-I)
DEFAULT_DEVICE_INDEX = 0
MAX_CAN_ROWS = 500


class UpgradeWorker(QObject):
    log = Signal(str)
    progress = Signal(int)
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
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    @Slot()
    def cancel(self) -> None:
        self.controller.cancel()


class Widget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("D7 PMU CAN IAP")
        self.resize(1120, 720)

        self.driver: ZlgCanBrokerDriver | None = None
        self.upgrade_thread: QThread | None = None
        self.upgrade_worker: UpgradeWorker | None = None
        self._can_connected = False
        self.known_can_ids: set[int] = set()
        self.filter_can_ids: set[int] = set()
        self.can_filter_buttons: dict[int, QPushButton] = {}
        self._can_records: list[tuple[str, str, CanFrame]] = []
        self._rx_save_records: list[tuple[str, CanFrame]] = []

        self.can_poll_timer = QTimer(self)
        self.can_poll_timer.setInterval(80)
        self.can_poll_timer.timeout.connect(self._poll_can_frames)

        self._build_ui()
        self._connect_signals()
        self._apply_style()
        self._set_connection_status(False, "未连接")

    def _build_ui(self) -> None:
        style = self.style()

        self.title_label = QLabel("D7 PMU CAN IAP")
        self.title_label.setObjectName("TitleLabel")
        self.fixed_device_label = QLabel("固定设备：USBCAN-I · Index 0")
        self.fixed_device_label.setObjectName("FixedDeviceLabel")
        self.can_status_dot = QLabel("●")
        self.can_status_dot.setFixedWidth(18)
        self.can_status_label = QLabel()
        self.can_status_label.setObjectName("StatusLabel")

        title_layout = QHBoxLayout()
        title_layout.setContentsMargins(16, 12, 16, 12)
        title_layout.addWidget(self.title_label)
        title_layout.addWidget(self.fixed_device_label)
        title_layout.addStretch(1)
        title_layout.addWidget(self.can_status_dot)
        title_layout.addWidget(self.can_status_label)
        self.header_bar = QWidget()
        self.header_bar.setObjectName("HeaderBar")
        self.header_bar.setLayout(title_layout)

        self.dll_path_label = QLabel(str(DEFAULT_DLL_PATH))
        self.dll_path_label.setWordWrap(True)
        self.dll_path_label.setObjectName("MutedLabel")

        self.channel_input = QComboBox()
        self.channel_input.addItem("CH0", 0)
        self.channel_input.addItem("CH1", 1)

        self.baudrate_input = QComboBox()
        for value in (1_000_000, 500_000, 250_000, 125_000, 100_000):
            self.baudrate_input.addItem(f"{value:,}", value)

        self.target_id_input = QLineEdit("0x19")
        self.can_id_input = QLineEdit("0x7ff")

        conn_layout = QGridLayout()
        conn_layout.setHorizontalSpacing(10)
        conn_layout.setVerticalSpacing(7)
        self._add_labeled_widget(conn_layout, 0, "通道", self.channel_input)
        self._add_labeled_widget(conn_layout, 1, "波特率", self.baudrate_input)
        self._add_labeled_widget(conn_layout, 2, "目标 ID", self.target_id_input)
        self._add_labeled_widget(conn_layout, 3, "IAP CAN ID", self.can_id_input)

        self.open_button = QPushButton("打开")
        self.open_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.close_button = QPushButton("关闭")
        self.close_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogCancelButton))
        conn_layout.addWidget(self.open_button, 0, 8)
        conn_layout.addWidget(self.close_button, 1, 8)
        conn_layout.setColumnStretch(7, 1)

        self.conn_group = QGroupBox("连接")
        self.conn_group.setLayout(conn_layout)

        self.bin_path_input = QLineEdit()
        self.bin_path_input.setPlaceholderText("请选择 APP bin 固件")
        self.bin_browse_button = QPushButton("选择")
        self.bin_browse_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        self.bin_browse_button.setToolTip("选择 APP bin")

        file_layout = QGridLayout()
        file_layout.setHorizontalSpacing(10)
        file_layout.addWidget(QLabel("APP 固件"), 0, 0)
        file_layout.addWidget(self.bin_path_input, 0, 1)
        file_layout.addWidget(self.bin_browse_button, 0, 2)
        file_layout.addWidget(QLabel("默认 DLL"), 1, 0)
        file_layout.addWidget(self.dll_path_label, 1, 1, 1, 2)

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

        file_layout.addWidget(self._metric("Size", self.firmware_size_value), 2, 0)
        file_layout.addWidget(self._metric("Image", self.firmware_image_value), 2, 1)
        file_layout.addWidget(self._metric("Initial SP", self.firmware_sp_value), 3, 0)
        file_layout.addWidget(self._metric("Reset Vector", self.firmware_reset_value), 3, 1)

        file_group = QGroupBox("APP 固件")
        file_group.setLayout(file_layout)

        self.query_role_button = QPushButton("查询角色")
        self.start_upgrade_button = QPushButton("开始升级")
        self.start_upgrade_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.stop_button = QPushButton("停止")
        self.stop_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_BrowserStop))

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        upgrade_actions = QHBoxLayout()
        upgrade_actions.addWidget(self.query_role_button)
        upgrade_actions.addWidget(self.start_upgrade_button)
        upgrade_actions.addWidget(self.stop_button)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setObjectName("LogOutput")
        self.system_log_group = QGroupBox("系统输出")
        system_log_layout = QVBoxLayout()
        system_log_layout.addWidget(self.log_output)
        self.system_log_group.setLayout(system_log_layout)

        left_layout = QVBoxLayout()
        left_layout.addWidget(file_group)
        left_layout.addWidget(self.progress_bar)
        left_layout.addLayout(upgrade_actions)
        left_layout.addWidget(self.system_log_group, 1)
        self.left_panel = QWidget()
        self.left_panel.setObjectName("LeftPanel")
        self.left_panel.setLayout(left_layout)
        self.left_panel.setMinimumWidth(360)
        self.left_panel.setMaximumWidth(430)

        self.send_can_id_input = QLineEdit("0x7ff")
        self.send_can_id_input.setFixedWidth(130)
        self.send_data_input = QLineEdit()
        self.send_data_input.setPlaceholderText("例如：16 19 02 00 00 00 31 62")
        self.send_button = QPushButton("发送")
        self.clear_can_button = QPushButton("清空")
        self.clear_can_button.setObjectName("SecondaryButton")

        send_layout = QGridLayout()
        send_layout.setHorizontalSpacing(10)
        send_layout.addWidget(QLabel("CAN ID"), 0, 0)
        send_layout.addWidget(QLabel("Data hex"), 0, 1)
        send_layout.addWidget(self.send_can_id_input, 1, 0)
        send_layout.addWidget(self.send_data_input, 1, 1)
        send_layout.addWidget(self.send_button, 1, 2)
        send_layout.addWidget(self.clear_can_button, 1, 3)
        send_layout.setColumnMinimumWidth(0, 120)
        send_layout.setColumnStretch(0, 0)
        send_layout.setColumnStretch(1, 1)
        send_layout.setColumnStretch(2, 0)
        send_layout.setColumnStretch(3, 0)
        self.send_group = QGroupBox("CAN 发送")
        self.send_group.setLayout(send_layout)
        conn_layout.addWidget(self.send_group, 0, 7, 2, 1)

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
        self.can_filter_input.setPlaceholderText("输入 CANID，例如：0x41, 0x7FF")
        self.can_filter_input.setObjectName("FilterInput")
        self.apply_filter_button = QPushButton("应用")
        self.clear_filter_button = QPushButton("全部")
        self.clear_filter_button.setObjectName("SecondaryButton")
        self.save_rx_button = QPushButton("保存接收")
        self.save_rx_button.setObjectName("SecondaryButton")
        self.can_filter_status_label = QLabel("全部显示")
        self.can_filter_status_label.setObjectName("MutedLabel")
        self.filter_chip_bar = QWidget()
        self.filter_chip_bar.setObjectName("FilterChipBar")
        self.filter_chip_layout = QHBoxLayout()
        self.filter_chip_layout.setContentsMargins(0, 0, 0, 0)
        self.filter_chip_layout.setSpacing(6)
        self.filter_chip_bar.setLayout(self.filter_chip_layout)

        filter_layout = QGridLayout()
        filter_layout.setHorizontalSpacing(8)
        filter_layout.setVerticalSpacing(6)
        filter_layout.addWidget(QLabel("过滤 CANID"), 0, 0)
        filter_layout.addWidget(self.can_filter_input, 1, 0)
        filter_layout.addWidget(self.apply_filter_button, 1, 1)
        filter_layout.addWidget(self.clear_filter_button, 1, 2)
        filter_layout.addWidget(self.can_filter_status_label, 1, 3)
        filter_layout.addWidget(self.save_rx_button, 1, 4)
        filter_layout.addWidget(self.filter_chip_bar, 2, 0, 1, 5)
        filter_layout.setColumnStretch(0, 1)
        self.can_filter_panel = QWidget()
        self.can_filter_panel.setObjectName("CanFilterPanel")
        self.can_filter_panel.setLayout(filter_layout)

        self.can_group = QGroupBox("CAN 数据")
        self.can_group.setLayout(QVBoxLayout())
        self.can_group.layout().addWidget(self.can_filter_panel)
        self.can_group.layout().addWidget(self.can_table)

        right_layout = QVBoxLayout()
        right_layout.addWidget(self.can_group, 1)
        self.right_panel = QWidget()
        self.right_panel.setObjectName("RightPanel")
        self.right_panel.setLayout(right_layout)

        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(14, 14, 14, 14)
        content_layout.setSpacing(14)
        content_layout.addWidget(self.left_panel)
        content_layout.addWidget(self.right_panel, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header_bar)
        layout.addWidget(self.conn_group)
        layout.addLayout(content_layout, 1)

    def _connect_signals(self) -> None:
        self.bin_browse_button.clicked.connect(self._choose_bin)
        self.open_button.clicked.connect(self.open_device)
        self.close_button.clicked.connect(self.close_device)
        self.query_role_button.clicked.connect(self.query_role)
        self.start_upgrade_button.clicked.connect(self.start_upgrade)
        self.stop_button.clicked.connect(self.stop_upgrade)
        self.send_button.clicked.connect(self.send_can_frame)
        self.clear_can_button.clicked.connect(self.clear_can_frames)
        self.apply_filter_button.clicked.connect(self.apply_can_filter)
        self.clear_filter_button.clicked.connect(self.clear_can_filter)
        self.save_rx_button.clicked.connect(self.save_rx_can_records)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                background: #eef3f1;
                color: #15231f;
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 13px;
            }
            #TitleLabel {
                color: #f4fbf8;
                font-size: 20px;
                font-weight: 700;
                background: transparent;
            }
            #FixedDeviceLabel, #StatusLabel {
                color: #c7d8d3;
                background: transparent;
            }
            QWidget#HeaderBar {
                background: #14211e;
            }
            QGroupBox {
                border: 1px solid #d2dfdb;
                margin-top: 12px;
                padding: 12px;
                background: #ffffff;
                font-weight: 700;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #263733;
            }
            QLineEdit, QComboBox {
                min-height: 30px;
                border: 1px solid #cbd8d4;
                background: #ffffff;
                padding: 4px 8px;
                selection-background-color: #1f6feb;
            }
            QLineEdit:focus, QComboBox:focus {
                border-color: #1f6feb;
            }
            QPushButton {
                min-height: 31px;
                border: 1px solid #0b704d;
                background: #0b704d;
                color: #ffffff;
                padding: 4px 12px;
                font-weight: 700;
            }
            QPushButton:disabled {
                background: #d7e0dd;
                border-color: #c4d0cc;
                color: #7b8b86;
            }
            QPushButton#SecondaryButton {
                background: #ffffff;
                color: #20312d;
                border-color: #cbd8d4;
            }
            QPushButton#FilterChip {
                min-height: 25px;
                border: 1px solid #cbd8d4;
                background: #ffffff;
                color: #20312d;
                padding: 2px 9px;
                font-family: "Cascadia Mono", Consolas;
                font-weight: 700;
            }
            QPushButton#FilterChip:checked {
                border-color: #18a56f;
                background: #dff3eb;
                color: #0b704d;
            }
            QWidget#CanFilterPanel {
                background: #f7faf9;
                border: 1px solid #dce5e2;
            }
            QWidget#FilterChipBar {
                background: transparent;
            }
            QLabel#MutedLabel {
                color: #65746f;
            }
            QLabel#MetricValue {
                font-family: "Cascadia Mono", Consolas;
                font-size: 14px;
                color: #13201d;
            }
            QWidget#Metric {
                background: #f7faf9;
                border: 1px solid #dce5e2;
            }
            QProgressBar {
                height: 12px;
                border: 0;
                background: #dce6e2;
                text-align: center;
            }
            QProgressBar::chunk {
                background: #18a56f;
            }
            QTableWidget {
                background: #ffffff;
                alternate-background-color: #f7faf9;
                border: 1px solid #d2dfdb;
                gridline-color: #edf2f0;
                font-family: "Cascadia Mono", Consolas;
            }
            QHeaderView::section {
                background: #eaf1ef;
                color: #596965;
                border: 0;
                border-bottom: 1px solid #d2dfdb;
                padding: 7px;
                font-weight: 700;
            }
            QPlainTextEdit#LogOutput {
                min-height: 110px;
                max-height: 150px;
                background: #10201c;
                color: #c7f4df;
                border: 1px solid #cbd8d4;
                font-family: "Cascadia Mono", Consolas;
            }
            """
        )

    @staticmethod
    def _add_labeled_widget(layout: QGridLayout, column: int, label_text: str, widget: QWidget) -> None:
        label = QLabel(label_text)
        label.setObjectName("MutedLabel")
        layout.addWidget(label, 0, column * 2)
        layout.addWidget(widget, 1, column * 2)

    @staticmethod
    def _metric(title: str, value: QLabel) -> QWidget:
        title_label = QLabel(title)
        title_label.setObjectName("MutedLabel")
        metric_layout = QVBoxLayout()
        metric_layout.setContentsMargins(8, 6, 8, 6)
        metric_layout.addWidget(title_label)
        metric_layout.addWidget(value)
        metric = QWidget()
        metric.setLayout(metric_layout)
        metric.setObjectName("Metric")
        return metric

    @Slot()
    def _choose_bin(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 APP bin", "", "Binary (*.bin);;All files (*.*)")
        if path:
            self.bin_path_input.setText(path)
            self._load_firmware_for_log(path)

    @Slot()
    def open_device(self) -> None:
        try:
            if _is_zcanpro_running():
                raise RuntimeError("ZCanPro 正在运行，请先关闭 ZCanPro 后再打开设备")
            driver = self._make_driver()
            options = self._make_options()
            if not driver.open(options.device_type, options.device_index, options.channel, options.baudrate):
                raise RuntimeError(driver.last_error)
            self.driver = driver
            message = f"已打开：通道 {driver.channel}，{options.baudrate} bps"
            self._set_connection_status(True, message)
            self._log(f"CAN 设备{message}")
        except Exception as exc:
            self._set_connection_status(False, f"打开失败：{exc}", failed=True)
            self._show_error(str(exc))

    @Slot()
    def close_device(self) -> None:
        self.can_poll_timer.stop()
        if self.driver:
            self.driver.close()
            self.driver.shutdown()
            self.driver = None
        self._set_connection_status(False, "未连接")
        self._log("CAN 设备已关闭")

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.can_poll_timer.stop()
        if self.driver:
            self.driver.shutdown()
            self.driver = None
        super().closeEvent(event)

    @Slot()
    def query_role(self) -> None:
        was_polling = self.can_poll_timer.isActive()
        self.can_poll_timer.stop()
        try:
            driver = self._ensure_driver_open()
            controller = IapUpgradeController(driver, self._make_protocol(), on_log=self._log)
            controller.query_role()
        except Exception as exc:
            self._show_error(str(exc))
        finally:
            if was_polling and self._can_connected:
                self.can_poll_timer.start()

    @Slot()
    def start_upgrade(self) -> None:
        try:
            image = self._load_firmware()
            driver = self._ensure_driver_open()
            protocol = self._make_protocol()
            controller = IapUpgradeController(driver, protocol, on_log=lambda _: None)
            controller.on_log = lambda message: self.upgrade_worker.log.emit(message) if self.upgrade_worker else self._log(message)
            controller.on_progress = lambda value: self.upgrade_worker.progress.emit(value) if self.upgrade_worker else self.progress_bar.setValue(value)

            self.upgrade_thread = QThread(self)
            self.upgrade_worker = UpgradeWorker(controller, image, self._make_options())
            self.upgrade_worker.moveToThread(self.upgrade_thread)
            self.upgrade_thread.started.connect(self.upgrade_worker.run)
            self.upgrade_worker.log.connect(self._log)
            self.upgrade_worker.progress.connect(self.progress_bar.setValue)
            self.upgrade_worker.failed.connect(self._show_error)
            self.upgrade_worker.finished.connect(self._upgrade_finished)
            self.upgrade_worker.finished.connect(self.upgrade_thread.quit)
            self.upgrade_worker.finished.connect(self.upgrade_worker.deleteLater)
            self.upgrade_thread.finished.connect(self.upgrade_thread.deleteLater)
            self._set_busy(True)
            self.upgrade_thread.start()
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
    def send_can_frame(self) -> None:
        try:
            driver = self._ensure_driver_open()
            frame = CanFrame(id=self._parse_int(self.send_can_id_input.text()), data=self._parse_data_bytes(self.send_data_input.text()))
            if not driver.send(frame):
                raise RuntimeError(driver.last_error)
            self._append_can_frame("TX", frame)
            self._log(f"发送 CAN：ID=0x{frame.id:X}, Len={frame.dlc}, Data={self._format_frame_data(frame)}")
        except Exception as exc:
            self._show_error(str(exc))

    @Slot()
    def _poll_can_frames(self) -> None:
        if not self._can_connected or not self.driver or not self.driver.is_open():
            return
        if self.upgrade_thread and self.upgrade_thread.isRunning():
            return

        for _ in range(20):
            frame = self.driver.receive(0)
            if frame is None:
                return
            self._append_can_frame("RX", frame)

    def _make_driver(self) -> ZlgCanBrokerDriver:
        return ZlgCanBrokerDriver(DEFAULT_DLL_PATH)

    def _ensure_driver_open(self) -> ZlgCanBrokerDriver:
        if self.driver and self.driver.is_open():
            return self.driver
        if _is_zcanpro_running():
            raise RuntimeError("ZCanPro 正在运行，请先关闭 ZCanPro 后再打开设备")
        driver = self._make_driver()
        options = self._make_options()
        if not driver.open(options.device_type, options.device_index, options.channel, options.baudrate):
            raise RuntimeError(driver.last_error)
        self.driver = driver
        message = f"已打开：通道 {driver.channel}，{options.baudrate} bps"
        self._set_connection_status(True, message)
        self._log(f"CAN 设备{message}")
        return driver

    def _make_protocol(self) -> IapProtocol:
        return IapProtocol(target_id=self._parse_int(self.target_id_input.text()), can_id=self._parse_int(self.can_id_input.text()))

    def _make_options(self) -> UpgradeOptions:
        return UpgradeOptions(
            device_type=DEFAULT_DEVICE_TYPE,
            device_index=DEFAULT_DEVICE_INDEX,
            channel=self.channel_input.currentData(),
            baudrate=self.baudrate_input.currentData(),
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

        self.start_upgrade_button.setEnabled(not busy and self._can_connected)
        self.open_button.setEnabled(not busy and not self._can_connected)
        self.close_button.setEnabled(not busy and self._can_connected)
        self.query_role_button.setEnabled(not busy and self._can_connected)
        self.send_button.setEnabled(not busy and self._can_connected)
        self.stop_button.setEnabled(busy)

    def _set_connection_status(self, connected: bool, message: str, failed: bool = False) -> None:
        self._can_connected = connected
        self.can_status_label.setText(message)
        color = "#18a56f" if connected else "#c7392f" if failed else "#8a9692"
        self.can_status_dot.setStyleSheet(f"color: {color}; font-size: 16px; font-weight: bold; background: transparent;")
        if connected:
            self.send_can_id_input.setText(self.can_id_input.text())
        self._set_busy(False)
        if not connected:
            self.can_poll_timer.stop()

    def _append_can_frame(self, direction: str, frame: CanFrame) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        popped_record: tuple[str, str, CanFrame] | None = None
        if direction == "RX":
            self._rx_save_records.append((timestamp, frame))
        self._can_records.append((direction, timestamp, frame))
        if len(self._can_records) > MAX_CAN_ROWS:
            popped_record = self._can_records.pop(0)
        if popped_record and self._frame_passes_filter(popped_record[2]) and self.can_table.rowCount() > 0:
            self.can_table.removeRow(0)
        self._remember_can_id(frame.id)
        if self._frame_passes_filter(frame):
            self._add_can_table_row(direction, timestamp, frame)
        self._update_filter_status(self.can_table.rowCount())

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
        self._render_can_table()

    @Slot()
    def clear_can_filter(self) -> None:
        self.filter_can_ids.clear()
        self.can_filter_input.clear()
        self._refresh_filter_chips()
        self._render_can_table()

    @Slot()
    def clear_can_frames(self) -> None:
        self._can_records.clear()
        self._rx_save_records.clear()
        self.can_table.setRowCount(0)
        self._update_filter_status(0)

    @Slot()
    def save_rx_can_records(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "保存 CAN 接收数据", "can_rx.csv", "CSV (*.csv);;All files (*.*)")
        if not path:
            return
        try:
            saved_count = self._save_rx_can_records(Path(path))
        except Exception as exc:
            self._show_error(str(exc))
            return
        self._log(f"已保存 CAN 接收数据：{saved_count} 帧，{path}")

    def _save_rx_can_records(self, path: str | Path) -> int:
        with Path(path).open("w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output)
            writer.writerow(["direction", "time", "canid", "len", "data"])
            for timestamp, frame in self._rx_save_records:
                writer.writerow(["RX", timestamp, f"0x{frame.id:X}", frame.dlc, self._format_frame_data(frame)])
        return len(self._rx_save_records)

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
        if self.filter_can_ids:
            self.can_filter_status_label.setText(f"显示 {visible_count} / {total_count} 帧")
        else:
            self.can_filter_status_label.setText(f"全部显示 {total_count} 帧")

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
        self._log(f"错误：{message}")
        QMessageBox.warning(self, "错误", message)

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
