# D7 PMU CAN IAP Tool

PySide6/Qt 上位机，用 ZLG/兼容 USBCAN-II 通过 CAN 2.0 标准帧给 D7 PMU 升级 APP 固件。

## 运行

```powershell
pip install -r requirements.txt
python widget.py
```

## 使用

1. 选择 `ControlCANFD.dll`。
   推荐优先使用 ZCanPro 目录下与当前 Python/Qt 位数一致的 DLL：
   - 64 位：`...\ZCanPro\bin\x64\ControlCANFD.dll`
   - 32 位：`...\ZCanPro\bin\Win32\ControlCANFD.dll`
2. 设备类型默认 `4`，对应 `VCI_USBCAN2`。
3. 设备索引默认 `0`，通道选择 `0` 或 `1`。
4. 波特率默认 `1000000`。
5. 目标设备 ID 默认 `0x19`，发送 CAN ID 默认 `0x7ff`。
6. 选择 APP bin 后，工具会检查文件大小、初始 SP、ResetVector。
7. 点击“打开”后可先“查询角色”，确认设备在 `APP` 或 `BOOT`。
8. 点击“开始升级”会自动执行：
   - 查询角色
   - 如果在 APP，发送重启进 BOOT
   - 设置固件大小并擦除 APP 区
   - 按 1024 bytes 分段发送
   - 每段发送 0x08 校验写入并检查 ACK byte3
   - 发送跳转 APP

## 协议要点

- 协议头：`0x16`
- 目标设备 ID：`0x19`
- 普通命令 checksum：`sum(byte0..byte6) & 0xff`
- `0x07` 数据帧每帧携带 5 bytes 固件数据
- `0x08` 帧 `byte3` 固定为 `0x00`，且参与段 CRC
- CRC32 使用板端非 reflected 算法，不使用 zlib CRC32

## 常见错误

- `DLL 不存在`：确认选择的是实际 `ControlCANFD.dll` 文件。
- `DLL 加载失败`：确认 Python/Qt 位数与 DLL 位数一致。
- `DLL 缺少函数`：当前 DLL 不包含经典 `VCI_*` 接口，需要换 ZCanPro DLL 或后续接入 `ZCAN_*`。
- `VCI_OpenDevice failed`：检查驱动、USBCAN-II 连接、设备是否被 ZCanPro 或其它程序占用。
- `等待 ACK 超时`：检查 CAN 通道、波特率、接线、终端电阻、目标 ID。
- `ACK checksum mismatch`：检查总线是否有其它设备回包，或目标 ID/协议版本是否一致。
- `段写入失败，ACK byte3 != 1`：优先检查 `0x08` CRC、bin 内容和 flash 写入状态。
- `疑似按 0x00000000 链接`：当前 bin 的 ResetVector 不像 `0x20000` APP 镜像，升级后 boot 可能无法跳转。

## 联调建议

第一阶段先只打开 CAN 并查询角色，确认能稳定收到 `0x16 0x19 0x02 ...`。确认 APP/BOOT 切换稳定后，再发送小文件或真实 APP 做完整升级。
