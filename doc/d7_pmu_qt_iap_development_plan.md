# D7 PMU Qt CAN IAP 上位机开发计划

## 1. 目标

开发一个 Windows Qt 上位机，用 ZLG/兼容 USBCAN-II 盒子通过 CAN 给 D7 PMU 板升级 APP 固件。

范围包括：

```text
1. 打开/关闭 CAN 设备
2. 选择 CAN 通道、波特率、设备类型
3. 读取 APP bin
4. 按 0x16 IAP 协议升级
5. 显示升级进度、日志、错误
6. 支持升级完成后跳转 APP
```

## 2. 已确认条件

板端协议信息：

```text
CAN 类型：CAN 2.0 标准帧
DLC：8
波特率：1Mbps
设备协议头：0x16
目标设备 ID：0x19
设备响应 CAN ID：0x7ff
APP 写入地址：0x20000
APP 信息地址：0x7e000
每段大小：最大 1024 bytes
每个 0x07 数据帧携带：5 bytes 固件数据
```

ZLG/兼容盒子信息：

```text
设备：USBCAN-II
推荐 DLL：ZCanPro 目录下的 ControlCANFD.dll
推荐优先接口：VCI_* 老接口
可选备用接口：ZCAN_* 新接口
```

推荐使用这个 DLL 目录：

```text
C:\Users\huxiaocheng1\Desktop\绿色软件\CANFD分析仪资料20250213(固件V2.11以上)\调试工具\CAN(FD)-bus综合应用软件ZCanPro\bin\x64\ControlCANFD.dll
```

如果 Qt 使用 32 位，则改用：

```text
C:\Users\huxiaocheng1\Desktop\绿色软件\CANFD分析仪资料20250213(固件V2.11以上)\调试工具\CAN(FD)-bus综合应用软件ZCanPro\bin\Win32\ControlCANFD.dll
```

## 3. Qt、SDK、DLL 的关系

Qt 本身主要负责界面、线程、文件读取、日志和状态机。Qt 不能凭空直接控制 USB-CAN 盒子，真正的 CAN 设备打开、初始化、发送、接收，需要调用第三方设备厂商 SDK/DLL。

推荐分层：

```text
Qt 程序
  -> CAN 抽象类，例如 CanDriver
  -> ZLG/兼容盒子 DLL，例如 ControlCANFD.dll
  -> Windows 驱动
  -> USB-CAN 盒子
  -> CAN 总线
  -> D7 PMU 板
```

ZCanPro 能正常识别并打开设备，说明驱动、硬件和兼容层已经可用。我们的程序不调用 ZCanPro 软件本身，只调用它目录中的 DLL。

## 4. 为什么推荐 ZCanPro 目录

对比之前 MYACTUATOR 目录：

```text
MYACTUATOR 那套：
  zlgcan.dll / kerneldlls/usbcan.dll
  主要是给它自己的电机调试软件用
  DLL 全是 32 位
  目录结构更像应用程序运行环境

ZCanPro 那套：
  bin/Win32/ControlCANFD.dll
  bin/x64/ControlCANFD.dll
  同时提供 32 位和 64 位
  更像通用 CAN 工具/SDK 运行库
  设备识别已被实测验证
```

因此推荐固定使用 ZCanPro 的 `ControlCANFD.dll`，避免混用多个软件目录中的 DLL。

## 5. VCI 与 ZCAN 的选择

`VCI_*` 是经典 ControlCAN 接口，常见函数包括：

```text
VCI_OpenDevice
VCI_InitCAN
VCI_StartCAN
VCI_Transmit
VCI_Receive
VCI_CloseDevice
```

`ZCAN_*` 是 ZLG 新接口，更适合 CANFD、新设备和复杂配置，常见函数包括：

```text
ZCAN_OpenDevice
ZCAN_InitCAN
ZCAN_StartCAN
ZCAN_Transmit
ZCAN_Receive
ZCAN_CloseDevice
```

本项目只需要普通 CAN 2.0 标准帧、DLC 8、1Mbps，不需要 CANFD。因此第一版优先使用 `VCI_*`，实现简单、资料多、兼容 USBCAN-II。若 `VCI_*` 在某些 DLL 或 64 位环境下出现兼容问题，再切换到 `ZCAN_*`。

## 6. 总体架构

建议分 4 层：

```text
Qt UI
  -> IapUpgradeController
  -> IapProtocol
  -> CanDriver
  -> ZLG ControlCANFD.dll
```

模块职责：

```text
CanDriver：
  只负责 CAN 设备打开、关闭、发送帧、接收帧。

ZlgVciCanDriver：
  使用 QLibrary 动态加载 ControlCANFD.dll。
  封装 VCI_OpenDevice / VCI_InitCAN / VCI_StartCAN / VCI_Transmit / VCI_Receive。

IapProtocol：
  只负责 0x16 协议组包、解析 ACK、CRC、分段。

IapUpgradeController：
  升级状态机。
  管理查询角色、重启进 boot、擦除、分段发送、校验、跳转 APP。

Qt UI：
  选择 DLL、bin 文件、设备参数。
  显示日志、进度、按钮状态。
```

## 7. 推荐目录结构

如果新建 Qt 工程，可以这样组织：

```text
d7_pmu_iap_tool/
  CMakeLists.txt
  main.cpp

  src/
    mainwindow.h
    mainwindow.cpp
    mainwindow.ui

    can/
      can_frame.h
      can_driver.h
      zlg_vci_can_driver.h
      zlg_vci_can_driver.cpp
      zlg_vci_defs.h

    iap/
      d7_crc32.h
      d7_crc32.cpp
      firmware_image.h
      firmware_image.cpp
      iap_protocol.h
      iap_protocol.cpp
      iap_upgrade_controller.h
      iap_upgrade_controller.cpp
```

## 8. CAN Driver 设计

定义通用接口：

```cpp
struct CanFrame {
    uint32_t id;
    uint8_t dlc;
    uint8_t data[8];
    bool extended;
    bool remote;
};

class CanDriver {
public:
    virtual ~CanDriver() = default;

    virtual bool open(int deviceType, int deviceIndex, int channel, int baudrate) = 0;
    virtual void close() = 0;
    virtual bool isOpen() const = 0;

    virtual bool send(const CanFrame& frame) = 0;
    virtual bool receive(CanFrame& frame, int timeoutMs) = 0;

    virtual QString lastError() const = 0;
};
```

ZLG VCI 参数建议：

```text
DeviceType = 4       // VCI_USBCAN2
DeviceIndex = 0
CANIndex = 0 或 1
Baudrate = 1000000
Mode = 0             // normal
Filter = 0
AccCode = 0x00000000
AccMask = 0xFFFFFFFF
```

典型调用顺序：

```text
VCI_OpenDevice(4, 0, 0)
VCI_InitCAN(4, 0, channel, &config)
VCI_StartCAN(4, 0, channel)
VCI_Transmit(4, 0, channel, &frame, 1)
VCI_Receive(4, 0, channel, &frame, count, waitTime)
VCI_CloseDevice(4, 0)
```

注意：ZLG 不同 DLL 对 1Mbps 的配置方式可能不同。优先查 `ControlCANFD.dll` 对应文档或示例。若使用经典 `VCI_INIT_CONFIG`，常见 1Mbps 参数可能是：

```text
Timing0 = 0x00
Timing1 = 0x14
```

如果该 DLL 支持 `VCI_SetReference` 直接设置 `1000000`，优先使用数值配置，减少 Timing 表差异。

## 9. 0x16 IAP 协议

所有协议数据帧长度 8。

通用格式：

```text
byte0 = 0x16
byte1 = target_id，默认 0x19
byte2 = cmd
byte3~byte6 = 参数
byte7 = checksum
```

checksum：

```text
除 0x07 和 0x08 外：
byte7 = sum(byte0..byte6) & 0xff
```

设备 ACK：

```text
byte0 = 0x16
byte1 = 0x19
byte2 = cmd
byte3~byte6 = 返回参数
byte7 = sum(byte0..byte6) & 0xff
CAN ID 通常为 0x7ff
```

命令列表：

```text
0x01  重启进 boot
0x02  查询运行角色
0x03  获取软件版本
0x04  打开/关闭 CAN 消息发送，当前板端基本空实现
0x05  设置固件大小，并擦除 APP 区
0x06  设置段信息
0x07  填充段数据
0x08  校验并写入段
0x09  跳转 APP
0x10  获取板卡和芯片类型
```

## 10. 升级流程

完整状态机：

```text
1. 打开 CAN 设备
2. 查询角色 0x02
3. 如果当前在 APP：
   发送 0x01
   等待设备复位
   循环查询 0x02，直到返回 boot
4. 发送 0x05 设置固件总大小
5. 按 1024 bytes 分段
6. 对每段：
   6.1 发送 0x06 段号和段长度
   6.2 按 5 bytes 一帧发送 0x07
   6.3 发送 0x08 段 CRC
   6.4 等待 ACK byte3 == 1
7. 所有段完成后发送 0x09
8. 可选：等待复位后再次查询角色，确认进入 APP
```

## 11. 命令组包细节

### 11.1 查询角色 0x02

```text
TX: 16 19 02 00 00 00 00 checksum
RX: byte3 = 0 表示 APP
RX: byte3 = 1 表示 BOOT
```

### 11.2 设置固件大小 0x05

`total_size` 为 24-bit 大端：

```text
byte3 = size >> 16
byte4 = size >> 8
byte5 = size
byte6 = 0
byte7 = checksum
```

### 11.3 设置段信息 0x06

```text
section_size 为 16-bit 大端
section_num  为 16-bit 大端

byte3 = section_size >> 8
byte4 = section_size
byte5 = section_num >> 8
byte6 = section_num
byte7 = checksum
```

### 11.4 段数据 0x07

```text
byte0 = 0x16
byte1 = 0x19
byte2 = 0x07
byte3~byte7 = 5 bytes 固件数据
```

最后不足 5 bytes 的帧也发 8 字节，`byte3` 开始放剩余数据，其它补 0 即可。板端会根据 `section.size` 只取需要的字节。

### 11.5 段 CRC 0x08

```text
byte0 = 0x16
byte1 = 0x19
byte2 = 0x08
byte3 = crc_head_extra，建议先固定 0x00
byte4 = crc >> 24
byte5 = crc >> 16
byte6 = crc >> 8
byte7 = crc
```

重要：`0x08` 的 `byte3` 参与段 CRC 计算，不能改了帧后忘记同步 CRC。

段 CRC 输入：

```text
head[0] = section_num >> 8
head[1] = section_num
head[2] = 0x16
head[3] = 0x19
head[4] = 0x08
head[5] = byte3_of_0x08_frame

crc32(head[0..5] + section_data)
```

## 12. CRC32 算法

不能用 zlib CRC32。板端算法是非 reflected CRC32：

```text
初值：0xffffffff
多项式表：0x04C11DB7
每个字节：
  val ^= byte
  循环 4 次：
    tmp = table[(val >> 24) & 0xff]
    val <<= 8
    val ^= tmp
无 final xor
```

建议直接把 `components/cryp/crc32.c` 的算法移植到 Qt 工程中，函数名可叫：

```cpp
uint32_t d7Crc32(const QByteArray& data);
uint32_t d7Crc32Append(uint32_t current, const uint8_t* data, uint32_t len);
```

注意：板端 `crc32_calc()` 的 `len` 应使用 `uint32_t`。如果仍为 `uint16_t`，80KB APP 会在 boot 校验时发生长度截断。

## 13. 固件文件校验

读取 bin 后先做本地检查：

```text
1. 文件不能为空
2. 文件大小 < 376KB
3. 前 4 字节初始 SP 应在 SRAM 范围：
   > SRAM_BASE
   <= SRAM_BASE + 192KB
4. ResetVector 应看起来像 APP 区地址
```

当前工程 boot 期望 APP 从 `0x20000` 运行，所以正常 APP bin 的 ResetVector 应类似：

```text
0x00020xxx
```

如果读到：

```text
0x00000xxx
```

要在 UI 给出强警告：

```text
当前 bin 疑似按 0x00000000 链接，不是 0x20000 APP 镜像。升级后 boot 可能无法跳转。
```

## 14. UI 设计

建议最小可用界面包含：

```text
DLL 路径选择
设备类型下拉：USBCAN-II，默认 4
设备索引：默认 0
通道：0 / 1
波特率：默认 1000000
目标设备 ID：默认 0x19
发送 CAN ID：默认 0x7ff
固件 bin 选择
打开设备按钮
查询角色按钮
开始升级按钮
停止按钮
进度条
日志窗口
```

日志必须记录：

```text
打开设备结果
当前角色 APP/BOOT
固件大小
每段发送进度
每段 CRC
每段 ACK 结果
错误帧/超时
最终跳转 APP 结果
```

## 15. 线程模型

不要在 UI 线程里跑升级。

推荐：

```text
MainWindow 在主线程
IapUpgradeController 放到 QThread
CAN receive 可用阻塞 receive + timeout
通过 signal/slot 更新 UI
```

需要支持停止：

```text
std::atomic_bool cancelRequested
每发送一帧/每等待一次 ACK 都检查一次
```

## 16. 超时和重试

建议策略：

```text
普通命令 ACK 超时：500ms
重启进 boot 等待：3000ms，总等待 10s
0x05 擦除等待：3000ms，可重试 1 次
0x06 ACK 等待：500ms，可重试 2 次
0x08 写入 ACK 等待：3000ms，可重试 2 次
0x07 数据帧：默认不等 ACK
```

发送节奏：

```text
0x07 连续发帧时建议每帧间隔 1~3ms
每 1024 bytes 一段约 205 帧
```

后续实测如果稳定，可以缩短间隔。

## 17. 错误处理

必须明确报错：

```text
DLL 加载失败
函数解析失败
打开设备失败
初始化 CAN 失败
启动 CAN 失败
发送失败
接收超时
ACK checksum 错误
ACK cmd 不匹配
段 CRC 写入失败，ACK byte3 != 1
用户选择的 bin 疑似链接地址错误
```

## 18. 联调步骤

第一阶段，只验证 CAN：

```text
1. 打开设备
2. 发送 0x02 查询角色
3. 观察是否收到 0x16 0x19 0x02 ...
4. 能稳定查询后再做升级
```

第二阶段，验证 boot 切换：

```text
1. APP 下查询角色，应返回 APP
2. 发送 0x01
3. 等 2~3 秒
4. 查询角色，应返回 BOOT
```

第三阶段，小文件或真实 APP 升级：

```text
1. 发送 0x05
2. 只发第 0 段
3. 看 0x08 ACK 是否 byte3 == 1
4. 再跑完整升级
```

第四阶段，完整升级后确认：

```text
1. 发送 0x09
2. 设备复位
3. 查询角色或观察 APP 行为
```

## 19. 关键风险

### 19.1 APP bin 链接地址

当前看到的 `d7_ct02_app.bin` ResetVector 像 `0x000002C9`，疑似不是 `0x20000` APP 镜像。这要优先修，否则上位机成功写入也可能无法启动。

### 19.2 0x08 byte3

段 CRC 计算包含 `0x08` 帧的 `byte3`。计划里先固定 `byte3=0x00`，CRC 也按 0x00 算。

### 19.3 DLL/API 差异

优先 `ControlCANFD.dll + VCI_*`。如果 VCI 初始化 1Mbps 不通，再切 `ZCAN_*` 或参考 ZCanPro 示例。

### 19.4 Qt 位数

如果用 `bin/x64/ControlCANFD.dll`，Qt 必须 64 位。

如果用 `bin/Win32/ControlCANFD.dll`，Qt 必须 32 位。

## 20. 交付物

最终应交付：

```text
1. Qt 工程源码
2. README.md
3. 可运行 exe
4. DLL 放置说明
5. 升级协议说明文档
6. 一份联调日志示例
```

README 至少写：

```text
如何选择 DLL
如何选择通道和波特率
如何判断设备在 APP/BOOT
如何选择 bin
常见错误及处理
```

## 21. 推荐实现顺序

```text
1. 写 d7_crc32，和板端算法对齐。
2. 写 CanDriver 抽象接口。
3. 写 ZlgVciCanDriver，只完成打开、发送、接收。
4. 写一个控制台/按钮测试：发送 0x02 查询角色。
5. 写 IapProtocol 组包和 ACK 解析。
6. 写 FirmwareImage 分段和 bin 检查。
7. 写 IapUpgradeController 状态机。
8. 接 UI。
9. 实机联调。
10. 根据总线稳定性调发送间隔、超时、重试。
```

## 22. 最小验收标准

能完成以下流程就算第一版可用：

```text
1. 选择 ControlCANFD.dll
2. 打开 USBCAN-II
3. 查询设备角色
4. APP 自动重启进 BOOT
5. 发送完整 APP bin
6. 每段都收到写入成功 ACK
7. 发送跳转 APP
8. 设备成功运行 APP
```

## 23. 给实现 AI 的核心提醒

```text
1. CAN 底层只做 VCI 收发，IAP 协议独立写。
2. 升级前必须检查 bin 是否真的是 0x20000 APP 镜像。
3. 不能使用 zlib CRC32，必须使用板端自定义 CRC32。
4. 0x08 帧的 byte3 参与段 CRC，建议固定为 0x00。
5. Qt 程序位数必须和 ControlCANFD.dll 位数一致。
```
