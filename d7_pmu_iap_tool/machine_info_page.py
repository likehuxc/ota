from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
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
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from d7_pmu_iap_tool.can.can_frame import CanFrame
from d7_pmu_iap_tool.machine_info_protocol import (
    MACHINE_INFO_FIELDS,
    MACHINE_INFO_SUCCESS,
    MachineInfoField,
    build_machine_info_request,
    format_machine_info_value,
    parse_machine_info_response,
)


@dataclass(frozen=True)
class MachineInfoCanConfig:
    dll_path: str
    device_type: int
    channel: int
    arbitration_baudrate: int
    data_baudrate: int


class MachineInfoPage(QWidget):
    def __init__(
        self,
        send_frame: Callable[[CanFrame], None],
        open_can: Callable[[MachineInfoCanConfig], None],
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
        self._rows_by_slot: dict[int, list[int]] = {}
        self._fields_by_row: dict[int, MachineInfoField] = {}
        self._requested_slots: set[int] = set()
        self._received_slots: set[int] = set()
        self._failed_slots: set[int] = set()
        self._response_can_id = 0x08

        self._build_ui()
        self._connect_signals()
        self.set_connected(False, "CAN 未连接")

    def _build_ui(self) -> None:
        title = QLabel("MachineInfo 读取测试")
        title.setObjectName("CanDebugTitle")
        subtitle = QLabel("按字段选择读取 · 00 53 请求 · 53 响应解析")
        subtitle.setObjectName("MutedLabel")
        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(2)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)

        self.connection_badge = QLabel("CAN 未连接")
        self.connection_badge.setObjectName("CanDebugConnectionBadge")
        self.connection_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.addLayout(title_box)
        heading.addStretch(1)
        heading.addWidget(self.connection_badge)

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

        connection_grid = QGridLayout()
        connection_grid.setContentsMargins(0, 0, 0, 0)
        connection_grid.setHorizontalSpacing(10)
        connection_grid.setVerticalSpacing(8)
        connection_grid.addWidget(QLabel("设备"), 0, 0)
        connection_grid.addWidget(QLabel("USBCANFD-200U · Index 0"), 0, 1, 1, 3)
        connection_grid.addWidget(QLabel("通道"), 1, 0)
        connection_grid.addWidget(self.channel_input, 1, 1)
        connection_grid.addWidget(QLabel("仲裁域"), 1, 2)
        connection_grid.addWidget(self.baudrate_input, 1, 3)
        connection_grid.addWidget(QLabel("数据域"), 2, 0)
        connection_grid.addWidget(self.data_baudrate_input, 2, 1)
        connection_actions = QHBoxLayout()
        connection_actions.setContentsMargins(0, 0, 0, 0)
        connection_actions.addWidget(self.dll_button)
        connection_actions.addWidget(self.open_button)
        connection_actions.addWidget(self.close_button)
        connection_actions.addStretch(1)
        connection_grid.addLayout(connection_actions, 2, 2, 1, 2)
        connection_grid.setColumnStretch(1, 1)
        connection_grid.setColumnStretch(3, 1)

        connection = QVBoxLayout()
        connection.setContentsMargins(18, 12, 18, 12)
        connection.setSpacing(10)
        connection.addLayout(heading)
        connection.addLayout(connection_grid)
        connection_bar = QWidget()
        connection_bar.setObjectName("CanDebugConnectionBar")
        connection_bar.setLayout(connection)

        self.request_id_input = QLineEdit("0x00")
        self.request_id_input.setMaximumWidth(100)
        self.response_id_input = QLineEdit("0x08")
        self.response_id_input.setMaximumWidth(100)
        self.select_all_button = QPushButton("全选")
        self.select_all_button.setObjectName("SecondaryButton")
        self.clear_selection_button = QPushButton("清空选择")
        self.clear_selection_button.setObjectName("SecondaryButton")
        self.read_button = QPushButton("读取选中值")
        self.status_label = QLabel("请选择要读取的值")
        self.status_label.setObjectName("MutedLabel")
        self.status_label.setWordWrap(True)

        actions_grid = QGridLayout()
        actions_grid.setContentsMargins(0, 0, 0, 0)
        actions_grid.setHorizontalSpacing(10)
        actions_grid.setVerticalSpacing(8)
        actions_grid.addWidget(QLabel("请求 CAN ID"), 0, 0)
        actions_grid.addWidget(self.request_id_input, 0, 1)
        actions_grid.addWidget(QLabel("响应 CAN ID"), 0, 2)
        actions_grid.addWidget(self.response_id_input, 0, 3)
        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.addWidget(self.select_all_button)
        button_row.addWidget(self.clear_selection_button)
        button_row.addWidget(self.read_button)
        button_row.addStretch(1)
        actions_grid.addLayout(button_row, 1, 0, 1, 4)
        actions_grid.addWidget(self.status_label, 2, 0, 1, 4)
        actions_grid.setColumnStretch(3, 1)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["选择", "Slot", "名称", "类型", "值", "原始数据", "状态"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.setMinimumHeight(200)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        header.resizeSection(0, 48)
        header.resizeSection(1, 64)
        header.resizeSection(3, 96)
        header.resizeSection(4, 170)
        header.resizeSection(5, 140)

        for field in MACHINE_INFO_FIELDS:
            row = self.table.rowCount()
            self.table.insertRow(row)
            select_item = QTableWidgetItem()
            select_item.setFlags(select_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            select_item.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, select_item)
            values = (f"0x{field.slot:02X}", field.key, field.type_label, "—", "—", "未读取")
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                if column in (1, 3, 6):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)
            self._rows_by_slot.setdefault(field.slot, []).append(row)
            self._fields_by_row[row] = field

        machine_info_layout = QVBoxLayout()
        machine_info_layout.setContentsMargins(16, 18, 16, 16)
        machine_info_layout.setSpacing(10)
        machine_info_layout.addLayout(actions_grid)
        machine_info_layout.addWidget(self.table, 1)
        machine_info_card = self._card("MachineInfo 参数", machine_info_layout)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 22)
        layout.setSpacing(14)
        layout.addWidget(connection_bar)
        layout.addWidget(machine_info_card, 1)

    def _connect_signals(self) -> None:
        self.open_button.clicked.connect(self.open_can)
        self.dll_button.clicked.connect(self.choose_canfd_dll)
        self.close_button.clicked.connect(self._close_can_callback)
        self.select_all_button.clicked.connect(self.select_all)
        self.clear_selection_button.clicked.connect(self.clear_selection)
        self.read_button.clicked.connect(self.read_selected)

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

    def set_connected(self, connected: bool, message: str) -> None:
        self._connected = connected
        self.connection_badge.setText(message)
        self.connection_badge.setProperty("connected", connected)
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        self.open_button.setEnabled(not connected)
        self.close_button.setEnabled(connected)
        self.read_button.setEnabled(connected)
        for widget in (self.channel_input, self.baudrate_input, self.data_baudrate_input, self.dll_button):
            widget.setEnabled(not connected)
        if not connected:
            self._requested_slots.clear()
            self._received_slots.clear()
            self._failed_slots.clear()
            self.status_label.setText("CAN 未连接 · 保留上次读取结果")

    @Slot()
    def open_can(self) -> None:
        try:
            if not self._canfd_dll_path:
                raise RuntimeError("请选择 64 位 ControlCANFD.dll")
            self._open_can_callback(MachineInfoCanConfig(
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
    def select_all(self) -> None:
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(Qt.CheckState.Checked)

    @Slot()
    def clear_selection(self) -> None:
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)

    @Slot()
    def read_selected(self) -> None:
        if not self._connected:
            self._show_error("请先打开 CAN 设备")
            return
        selected_slots = {
            field.slot
            for row, field in self._fields_by_row.items()
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked
        }
        if not selected_slots:
            self._show_error("请至少选择一个 MachineInfo 值")
            return
        try:
            request_can_id = self._parse_standard_can_id(self.request_id_input.text(), "MachineInfo 请求 CAN ID")
            self._response_can_id = self._parse_standard_can_id(
                self.response_id_input.text(), "MachineInfo 响应 CAN ID"
            )
            self._requested_slots.clear()
            self._received_slots.clear()
            self._failed_slots.clear()
            for slot in sorted(selected_slots):
                rows = self._rows_by_slot[slot]
                for row in rows:
                    self.table.item(row, 4).setText("—")
                    self.table.item(row, 5).setText("—")
                    self.table.item(row, 6).setText("等待读取")
                try:
                    self._send_frame_callback(build_machine_info_request(request_can_id, slot))
                except Exception:
                    for row in rows:
                        self.table.item(row, 6).setText("发送失败")
                    self.status_label.setText(
                        f"发送中断 · 已发送 {len(self._requested_slots)}/{len(selected_slots)} slot"
                    )
                    raise
                self._requested_slots.add(slot)
            self.status_label.setText(f"已发送 {len(selected_slots)} 个 slot，等待响应")
        except Exception as exc:
            self._show_error(str(exc))

    def handle_frame(self, direction: str, frame: CanFrame) -> None:
        if direction != "RX" or not self._connected:
            return
        response = parse_machine_info_response(frame, self._response_can_id)
        if response is None:
            return
        rows = self._rows_by_slot.get(response.slot)
        if not rows:
            return
        raw_text = response.raw.hex(" ").upper()
        success = response.status == MACHINE_INFO_SUCCESS
        status_text = "已读取" if success else f"状态 0x{response.status:02X}"
        for row in rows:
            field = self._fields_by_row[row]
            value = format_machine_info_value(field, response.raw) if success else "—"
            self.table.item(row, 4).setText(value)
            self.table.item(row, 5).setText(raw_text)
            self.table.item(row, 6).setText(status_text)
        if response.slot not in self._requested_slots:
            if success:
                self.status_label.setText(f"收到 slot 0x{response.slot:02X}")
            else:
                self.status_label.setText(
                    f"收到失败响应 · slot 0x{response.slot:02X} · 状态 0x{response.status:02X}"
                )
            return
        if success:
            self._received_slots.add(response.slot)
            self._failed_slots.discard(response.slot)
        else:
            self._failed_slots.add(response.slot)
            self._received_slots.discard(response.slot)
        requested = len(self._requested_slots)
        completed = len(self._received_slots) + len(self._failed_slots)
        if completed == requested:
            if self._failed_slots:
                self.status_label.setText(
                    f"读取结束 · 成功 {len(self._received_slots)} / 失败 {len(self._failed_slots)} slot"
                )
            else:
                self.status_label.setText(f"读取完成 · {completed}/{requested} slot")
        elif self._failed_slots:
            self.status_label.setText(
                f"读取中 · 成功 {len(self._received_slots)} / 失败 {len(self._failed_slots)} / {requested} slot"
            )
        else:
            self.status_label.setText(f"已读取 {completed}/{requested} slot")

    def shutdown(self) -> None:
        self._connected = False
        self._requested_slots.clear()
        self._received_slots.clear()
        self._failed_slots.clear()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        compact = event.size().width() < 900
        self.table.setColumnHidden(3, compact)
        self.table.setColumnHidden(5, compact)
        super().resizeEvent(event)

    @staticmethod
    def _parse_standard_can_id(text: str, label: str) -> int:
        try:
            can_id = int(text.strip(), 0)
        except ValueError as exc:
            raise ValueError(f"{label} 格式无效") from exc
        if not 0 <= can_id <= 0x7FF:
            raise ValueError(f"{label} 必须在 0x000 到 0x7FF 之间")
        return can_id

    @staticmethod
    def _show_error(message: str) -> None:
        QMessageBox.warning(None, "MachineInfo", message)
