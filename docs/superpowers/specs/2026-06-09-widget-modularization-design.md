# widget.py 模块化设计（保守抽取）

日期：2026-06-09
状态：已批准设计，待实现

## 背景与目标

`widget.py` 当前约 1540 行，是一个 `Widget` "上帝类"，把界面布局、设备连接、升级编排、CAN 监控四件事揉在一起，外加两大块非逻辑内容（一段 ~340 行 QSS 样式表、一个 375 行的 `_build_ui`）。业务逻辑（CAN 驱动 / IAP 协议 / 控制器 / 固件）已经干净地放在 `d7_pmu_iap_tool/` 里，臃肿只在 UI 层。

目标：**在不改变任何运行行为、不改动 `tests/test_widget.py` 的前提下**，把"不属于 Widget 行为"的部分抽到独立模块，并把巨型 `_build_ui` 拆成可读的分段方法，从而降低维护成本、提升可读性。

非目标：不做 MVVM/组件化重构；不抽取 CAN 监控为独立控件；不改业务逻辑。

## 硬约束：测试必须零改动通过

`tests/test_widget.py` 深度依赖 `widget` 模块的公共面，重构必须全部保留：

- 导入：`from widget import DEFAULT_DLL_PATH, MAX_CAN_ROWS, Widget`
- patch 目标：`patch("widget.IapUpgradeController")`、`patch("widget.QMessageBox.information")`
  → `Widget` 类与 `IapUpgradeController`、`QMessageBox` 必须仍位于 `widget` 命名空间
- 当作 `Widget` 方法调用的纯函数：`widget._parse_filter_ids(...)`、`widget._format_elapsed_time(...)`
  → 在 `Widget` 上保留薄方法（委托给抽出的模块函数）
- 大量属性断言：`widget.channel_input`、`widget.step_title_labels`、`widget.can_table`、`widget.firmware_metric_title_labels` 等
  → 这些属性仍由 `Widget` 自身在构建期创建

## 目标结构

```
ui/
  __init__.py
  config.py          # DeviceProfile, DEVICE_PROFILES, DEFAULT_DEVICE_PROFILE_NAME,
                     #   DEFAULT_DLL_PATH, DEFAULT_DEVICE_TYPE, DEFAULT_DEVICE_INDEX, MAX_CAN_ROWS
  styles.py          # APP_STYLESHEET = """ ...搬来的 340 行 QSS... """
  factory.py         # add_labeled_widget / card_header / set_card_layout / metric /
                     #   summary_item / make_step（返回控件 + 标签，由 Widget 存入列表）
  upgrade_worker.py  # UpgradeWorker(QObject)
  can_format.py      # parse_filter_ids / parse_data_bytes / format_frame_data /
                     #   format_elapsed_time / parse_int（纯函数，可独立单测）
widget.py            # 只剩 Widget 类 + 顶部向后兼容的 re-export
```

### 各模块职责

- **ui/config.py** — 纯配置数据，无 Qt 依赖。`widget.py` 顶部 `from ui.config import *`（或具名导入）再 re-export，使 `from widget import DEFAULT_DLL_PATH, MAX_CAN_ROWS` 继续可用。
- **ui/styles.py** — 仅 `APP_STYLESHEET` 字符串常量。`Widget._apply_style` 改为 `self.setStyleSheet(APP_STYLESHEET)`。
- **ui/factory.py** — 现有的静态 UI 工厂方法转为模块级自由函数。`make_step` 返回 `(step_widget, title_label, desc_label, number_label)`，由 `Widget` 负责把标签塞进 `self.step_*` 列表，保证属性不变。
- **ui/upgrade_worker.py** — `UpgradeWorker` 原样搬出；`widget.py` 导入它供 `start_upgrade` 使用。无测试 patch 它，安全。
- **ui/can_format.py** — 5 个纯解析/格式化函数。`Widget` 上保留同名薄方法委托调用，测试照常。

### widget.py 内部：拆分 `_build_ui`

`_build_ui` 拆成多个分段方法，全部仍是 `Widget` 的方法（属性仍挂在 `self` 上，测试不受影响）：

- `_build_header_bar()` — 标题栏 / 设备下拉 / 状态药丸
- `_build_stepper()` — 五步进度条
- `_build_connection_card()` — 设备连接卡片
- `_build_file_card()` — 固件文件卡片
- `_build_upgrade_card()` — 升级控制卡片（角色带 / 进度 / 错误框）
- `_build_left_panel()` — 组装左栏
- `_build_send_card()` — CAN 发送调试卡片
- `_build_device_status_card()` — 设备状态卡片
- `_build_can_monitor_card()` — CAN 监控卡片（表格 / 过滤 / 标签页）
- `_build_right_panel()` — 组装右栏
- `_assemble_main_layout()` — 滚动区 + 顶层布局拼装

`_build_ui` 退化为按顺序调用上述方法的协调器。每个方法约 30–50 行。

## 预期结果

- 抽出约 530 行到 5 个聚焦、可独立阅读/单测的模块。
- `widget.py` 约 950–1000 行，但**无任何方法超过 ~50 行**——可读性问题直接解决。
- `pure` 函数（解析/格式化）首次可在不构造 Qt 窗口的情况下单测。

## 实施步骤（每步后跑全量测试）

基线：先跑一次 `python -m pytest tests/ -q` 确认当前全绿。

1. 建 `ui/` 包与 `ui/__init__.py`。
2. 抽 `ui/config.py`，在 `widget.py` re-export，跑测试。
3. 抽 `ui/styles.py`，`_apply_style` 改用 `APP_STYLESHEET`，跑测试。
4. 抽 `ui/can_format.py`，`Widget` 留薄方法委托，跑测试。
5. 抽 `ui/upgrade_worker.py`，`widget.py` 导入，跑测试。
6. 抽 `ui/factory.py`，`Widget` 改调用自由函数，跑测试。
7. 在 `widget.py` 内把 `_build_ui` 拆成分段方法，跑测试。
8. 全量测试 + 手动启动 `python widget.py` 目视确认界面与原先一致。

每一步都是独立小改动，测试是安全网；任一步变红立即定位。

## 验证

- `python -m pytest tests/ -q` 全绿，且 `tests/test_widget.py` 内容零改动。
- `python widget.py` 能正常启动、界面外观与交互与重构前一致。
