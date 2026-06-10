# D7 CAN IAP 上位机 UI 改版 · 设计文档

- 日期：2026-06-09
- 目标文件：`widget.py`（PySide6 单文件，样式集中在 `_apply_style()`，布局在 `_build_ui()`）
- 视觉参考：`docs/ui_mockup.html`（已与用户多轮迭代定稿）

## 1. 背景与目标

现有界面已是卡片化的现代布局，但存在几处影响体验和观感的问题，本次按已定稿的 HTML mockup 一次性落地：

1. 强制滚动：`content.setMinimumSize(1050, 900)` 高于默认窗口高度，导致升级时进度与日志无法同屏。
2. 步骤条「进行中」用红色，与「错误/危险」语义冲突。
3. 字重几乎全是 800/900，视觉层级被压平。
4. 固件卡片只显示 Size，SP/ResetVector 已算出但只进日志。
5. CAN 监控的三个 Tab 是假的（点击不切换）。
6. 按钮用 Qt 原生拟物图标，与扁平风格不协调。
7. 系统日志占整宽底栏；用户希望日志移到左列、CAN 监控在右列，两者撑高且底部对齐。

## 2. 已确认的取舍（与用户对齐）

| 项 | 决定 |
|---|---|
| 落地范围 | 全部一次到位 |
| 步骤条连接线 | 做（用 QFrame 画横线连接）|
| Tab 切换 | 接上真实切换（RX/TX 真实过滤；错误帧暂无数据源，恒为空）|
| 按钮图标 | 新建描边 SVG 图标替换 Qt 原生图标 |

## 3. 详细设计

### 3.1 布局重排（`_build_ui`）

目标版式：

```text
[ 步骤条（整宽，1─2─3─4─5 带连接线）          ]
┌──────────────────────┬──────────────────────┐
│ 设备连接              │ 设备状态              │
│ 固件文件              │ CAN 发送调试          │
│ 升级控制              │                      │
│ 系统日志（flex 撑满）  │ CAN 监控（flex 撑满）  │
└──────────────────────┴──────────────────────┘
```

改动：

- 将 `system_log_group` 从 `content_layout` 移入 `left_layout`，置于 `upgrade_group` 之后。
- `left_layout` 顺序：`conn_group`、`file_group`、`upgrade_group`、`system_log_group`（`stretch=1`）。移除左列末尾的 `addStretch(1)`。
- `right_layout`：`device_status_group`、`send_group`、`can_group`（`stretch=1`）。
- `content_layout`：`stepper` + `main_layout`（`stretch=1`），不再包含日志。
- 两列均为严格的 `QVBoxLayout`；末位卡片用 `stretch=1` 撑满，使两列底部对齐。
- 将 `content.setMinimumSize(1050, 900)` 改为仅约束宽度 `content.setMinimumWidth(1050)`（去掉 900 的最小高度，这是恒显滚动条的根因）；默认窗口 `resize(1200, 980)`。
- `QScrollArea` 保留作为小屏兜底（不再因 minimum 过高而恒显滚动条）。

### 3.2 配色 token 与字重（`_apply_style`）

- 收敛色板（与 mockup 一致）：
  - primary `#2563eb` / primary-weak `#eff6ff` / primary-line `#bfdbfe`
  - success `#16a34a` / `#22c55e`，danger `#dc2626` / danger-line `#fecaca`
  - 中性：bg `#f4f7fb`、card `#fff`、line `#e5e7eb`、field `#f8fafc`、ink `#0f172a`、muted `#6b7280`
  - header 渐变 navy `#0f172a` → teal `#0b3b4a`
- 字重降档：卡片标题 600、关键数值（角色/进度/指标）700、标签/正文 400–500。去掉大面积 800/900。
- 等宽字体（hex/时间/指标）沿用 `Cascadia Mono`/`Consolas`。

### 3.3 步骤条连接线 + 蓝色进行中

- 步骤项重构为「圆点 + 标题 + 描述」，相邻步骤之间插入 `QFrame`（细横线）作为连接线，与圆心垂直对齐。
- **状态模型（关键修正）**：
  - `Pending`：灰圆（`#9ca3af`）
  - `Active`：蓝圆（`#2563eb`）+ 浅蓝描述色 —— 表示「进行中」（原为红色，纠正）
  - `Done`：绿圆（`#16a34a`）+ 勾
  - `Error`：红圆（`#dc2626`）—— 仅用于真实失败
- 连接线上色：已完成段为绿，当前进行段为蓝，未到为灰。
- `_set_step_state` 扩展：除圆点/卡片外，同步刷新对应连接线的 objectName 并 unpolish/polish。
- 调用点修正：
  - `open_device` 失败 → 步骤 0 用 `Error`（而非现在的红色 Active）。
  - `start_upgrade` 写入中 → 步骤 3 用 `Active`（蓝）。
  - 维护 `self.step_connectors: list[QFrame]`。

### 3.4 固件四宫格（`_metric`）

- 在「固件文件」卡片中，将 Size 单格扩为一行四格：**Size / 镜像类型 / Initial SP / Reset Vector**。
- 复用已存在的 `firmware_size_value`、`firmware_image_value`(=APP)、`firmware_sp_value`、`firmware_reset_value`。
- `_log_firmware_info` 已经计算 SP/ResetVector，确保同时 `setText` 到对应指标标签（目前仅 size 上界面）。

### 3.5 Tab 真实切换

- 数据模型：新增 `self._can_tab: str`（取值 `"RX"` / `"TX"` / `"ERR"`，默认 `"RX"`）。
- Tab 由 `QLabel` 改为 `QPushButton`（`setCheckable(True)`，互斥分组），保留圆角胶囊样式 + 数量徽标。点击切换 `_can_tab` 并重渲染。
- 过滤规则（与现有 CAN ID 过滤叠加）：
  - 接收数据：`direction == "RX"`
  - 发送数据：`direction == "TX"`
  - 错误帧：当前无数据源 → 始终为空（计数 0），保留 UI 与扩展点。
- `_frame_passes_filter` / `_render_can_table` / `_append_can_frame` 接入 tab 维度的过滤；live append 时若帧不属于当前 tab 则不插入但仍计数。
- 徽标计数：接收=RX 帧数、发送=TX 帧数、错误=0。计数随帧更新。
- 切换 tab 时高亮当前 tab 样式（`TabActive` / `Tab`）。

### 3.6 SVG 描边图标

- 在 `assets/` 新建一套 12×12～16×16 描边 SVG（沿用 `chevron-down.svg` 的 stroke 风格，`stroke-width≈1.6–2`）：
  - `play`（开始升级）、`stop`（停止升级）、`folder`（选择固件/DLL）、`refresh`（重新查询）、`send`（发送）、`check`（打开设备）、`x`（关闭设备）、`download`（导出 CSV）
- 用 `QIcon(str(path))` 替换所有 `style.standardIcon(QStyle.StandardPixmap.*)` 调用。

### 3.7 暂不做

- 进度条流光动画：Qt `QProgressBar::chunk` 不支持 CSS 动画，保留绿色渐变 chunk。
- mockup 中的伪标题栏 / 窗口控制按钮 / 水印：浏览器演示专用，真实应用使用系统标题栏。

## 4. 测试

- `tests/test_widget.py` 已存在。需要：
  - 为 Tab 切换新增测试：构造 RX/TX 帧，切换 `_can_tab`，断言表格仅显示对应方向的行、徽标计数正确、错误帧 tab 为空。
  - 校验固件信息加载后 SP/ResetVector 指标标签被正确 `setText`。
  - 步骤状态切换（Pending/Active/Done/Error）不抛异常、连接线随之更新。
- 落地后运行现有全部测试（`tests/test_widget.py`、`tests/test_upgrade_controller.py`）确保通过。

## 5. 影响面与风险

- 纯前端/交互改动；不触碰 CAN 协议、升级控制器逻辑。
- 唯一的逻辑改动是 CAN 表格的 tab 过滤，回归风险集中在 `_append_can_frame` / `_render_can_table`，由单测覆盖。
- `_apply_style` QSS 字符串较大，重写时保持 objectName 选择器与控件一致，避免样式丢失。

## 6. 验收标准

1. 默认窗口下无需滚动即可看到：步骤条、左右两列、左下日志、右下 CAN 监控。
2. 系统日志在左列底部、CAN 监控在右列底部，两者底边对齐。
3. 步骤「进行中」为蓝色，步骤之间有连接线，已完成段为绿。
4. 固件卡片显示 Size/镜像类型/Initial SP/Reset Vector 四格。
5. 点击 CAN 监控的接收/发送 Tab 能真实切换显示内容，徽标计数正确。
6. 按钮图标为描边 SVG 风格，无 Qt 原生拟物图标。
7. 所有现有测试通过。
