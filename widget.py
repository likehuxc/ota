# This Python file uses the following encoding: utf-8
from __future__ import annotations

import sys
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from d7_pmu_iap_tool.can.broker_can_driver import ZlgCanBrokerDriver
from d7_pmu_iap_tool.iap.firmware_image import FirmwareImage
from d7_pmu_iap_tool.iap.iap_protocol import IapProtocol
from d7_pmu_iap_tool.iap.iap_upgrade_controller import IapUpgradeController, UpgradeOptions

# ZLG's 32-bit zlgcan.dll (loaded by the 32-bit broker subprocess). It pulls
# device backends from the sibling kerneldlls\ folder.
DEFAULT_DLL_PATH = Path(r"C:\Program Files (x86)\ZCANPRO\zlgcan.dll")


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
        self.resize(920, 640)

        self.driver: ZlgCanBrokerDriver | None = None
        self.upgrade_thread: QThread | None = None
        self.upgrade_worker: UpgradeWorker | None = None
        self._can_connected = False

        self._build_ui()
        self._connect_signals()
        self._set_connection_status(False, "未连接")

    def _build_ui(self) -> None:
        style = self.style()

        self.dll_path_label = QLabel(str(DEFAULT_DLL_PATH))
        self.dll_path_label.setWordWrap(True)

        self.bin_path_input = QLineEdit()
        self.bin_path_input.setPlaceholderText("APP bin")
        self.bin_browse_button = QPushButton()
        self.bin_browse_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        self.bin_browse_button.setToolTip("选择 APP bin")

        self.device_type_input = QSpinBox()
        self.device_type_input.setRange(0, 255)
        self.device_type_input.setValue(3)  # ZCAN_USBCAN1 (USBCAN-I)

        self.device_index_input = QSpinBox()
        self.device_index_input.setRange(0, 32)
        self.device_index_input.setValue(0)

        self.channel_input = QComboBox()
        self.channel_input.addItem("0", 0)
        self.channel_input.addItem("1", 1)

        self.baudrate_input = QComboBox()
        for value in (1_000_000, 500_000, 250_000, 125_000, 100_000):
            self.baudrate_input.addItem(str(value), value)

        self.target_id_input = QLineEdit("0x19")
        self.can_id_input = QLineEdit("0x7ff")
        self.can_status_dot = QLabel("●")
        self.can_status_dot.setFixedWidth(18)
        self.can_status_label = QLabel()

        status_layout = QHBoxLayout()
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.addWidget(self.can_status_dot)
        status_layout.addWidget(self.can_status_label, 1)
        status_widget = QWidget()
        status_widget.setLayout(status_layout)

        file_layout = QGridLayout()
        file_layout.addWidget(QLabel("默认 DLL"), 0, 0)
        file_layout.addWidget(self.dll_path_label, 0, 1, 1, 2)
        file_layout.addWidget(QLabel("Bin"), 1, 0)
        file_layout.addWidget(self.bin_path_input, 1, 1)
        file_layout.addWidget(self.bin_browse_button, 1, 2)
        file_group = QGroupBox("文件")
        file_group.setLayout(file_layout)

        conn_layout = QFormLayout()
        conn_layout.addRow("设备类型", self.device_type_input)
        conn_layout.addRow("设备索引", self.device_index_input)
        conn_layout.addRow("通道", self.channel_input)
        conn_layout.addRow("波特率", self.baudrate_input)
        conn_layout.addRow("目标 ID", self.target_id_input)
        conn_layout.addRow("发送 CAN ID", self.can_id_input)
        conn_layout.addRow("CAN 状态", status_widget)
        conn_group = QGroupBox("连接")
        conn_group.setLayout(conn_layout)

        self.open_button = QPushButton("打开")
        self.open_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.close_button = QPushButton("关闭")
        self.query_role_button = QPushButton("查询角色")
        self.start_upgrade_button = QPushButton("开始升级")
        self.start_upgrade_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.stop_button = QPushButton("停止")
        self.stop_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_BrowserStop))

        button_layout = QHBoxLayout()
        for button in (
            self.open_button,
            self.close_button,
            self.query_role_button,
            self.start_upgrade_button,
            self.stop_button,
        ):
            button_layout.addWidget(button)
        button_layout.addStretch(1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)

        top_layout = QHBoxLayout()
        top_layout.addWidget(file_group, 2)
        top_layout.addWidget(conn_group, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(top_layout)
        layout.addLayout(button_layout)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.log_output, 1)

    def _connect_signals(self) -> None:
        self.bin_browse_button.clicked.connect(self._choose_bin)
        self.open_button.clicked.connect(self.open_device)
        self.close_button.clicked.connect(self.close_device)
        self.query_role_button.clicked.connect(self.query_role)
        self.start_upgrade_button.clicked.connect(self.start_upgrade)
        self.stop_button.clicked.connect(self.stop_upgrade)

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
            self.device_type_input.setValue(driver.device_type)
            options = UpgradeOptions(
                device_type=driver.device_type,
                device_index=driver.device_index,
                channel=driver.channel,
                baudrate=options.baudrate,
            )
            message = (
                f"已打开：设备 {options.device_type}，索引 {options.device_index}，"
                f"通道 {options.channel}，{options.baudrate} bps"
            )
            self._set_connection_status(True, message)
            self._log(f"CAN 设备{message}")
        except Exception as exc:
            self._set_connection_status(False, f"打开失败：{exc}", failed=True)
            self._show_error(str(exc))

    @Slot()
    def close_device(self) -> None:
        if self.driver:
            self.driver.close()
            self.driver.shutdown()
            self.driver = None
            self._set_connection_status(False, "未连接")
            self._log("CAN 设备已关闭")

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self.driver:
            self.driver.shutdown()
            self.driver = None
        super().closeEvent(event)

    @Slot()
    def query_role(self) -> None:
        try:
            driver = self._ensure_driver_open()
            controller = IapUpgradeController(driver, self._make_protocol(), on_log=self._log)
            controller.query_role()
        except Exception as exc:
            self._show_error(str(exc))

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
        self.device_type_input.setValue(driver.device_type)
        options = UpgradeOptions(
            device_type=driver.device_type,
            device_index=driver.device_index,
            channel=driver.channel,
            baudrate=options.baudrate,
        )
        message = (
            f"已打开：设备 {options.device_type}，索引 {options.device_index}，"
            f"通道 {options.channel}，{options.baudrate} bps"
        )
        self._set_connection_status(True, message)
        self._log(f"CAN 设备{message}")
        return driver

    def _make_protocol(self) -> IapProtocol:
        return IapProtocol(target_id=self._parse_int(self.target_id_input.text()), can_id=self._parse_int(self.can_id_input.text()))

    def _make_options(self) -> UpgradeOptions:
        return UpgradeOptions(
            device_type=self.device_type_input.value(),
            device_index=self.device_index_input.value(),
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
        self._log(f"固件大小：{image.size} bytes")
        self._log(f"Initial SP：0x{image.initial_sp:08X}")
        self._log(f"ResetVector：0x{image.reset_vector:08X}")
        for warning in image.warnings:
            self._log(f"警告：{warning}")

    def _set_busy(self, busy: bool) -> None:
        self.start_upgrade_button.setEnabled(not busy and self._can_connected)
        self.open_button.setEnabled(not busy and not self._can_connected)
        self.close_button.setEnabled(not busy and self._can_connected)
        self.query_role_button.setEnabled(not busy and self._can_connected)
        self.stop_button.setEnabled(busy)

    def _set_connection_status(self, connected: bool, message: str, failed: bool = False) -> None:
        self._can_connected = connected
        self.can_status_label.setText(message)
        color = "#188038" if connected else "#d93025" if failed else "#80868b"
        self.can_status_dot.setStyleSheet(f"color: {color}; font-size: 16px; font-weight: bold;")
        self._set_busy(False)

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
