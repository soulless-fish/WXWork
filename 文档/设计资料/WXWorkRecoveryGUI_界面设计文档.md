# WXWorkRecoveryGUI 界面设计文档

## 一、说明

本文档用于说明当前项目图形界面的实际设计。

需要先说明一个关键事实：

1. 当前项目的 GUI **不是 PyQt5 实现**。
2. 当前项目实际使用的是 **Tkinter + ttk**，入口文件是 [wxwork_recovery_gui.py](./wxwork_recovery_gui.py)。
3. 因此，下文描述的是“当前项目实际 GUI 设计”，不是 PyQt5 版本设计。

## 二、界面实现总览

当前 GUI 的核心入口和初始化链路如下：

1. `main()`
   - 位置：`wxwork_recovery_gui.py` 第 `2283` 行附近
   - 作用：创建 `tk.Tk()` 主窗口，实例化 `RecoveryApp`，进入 `root.mainloop()`
2. `RecoveryApp.__init__()`
   - 位置：`wxwork_recovery_gui.py` 第 `199` 行附近
   - 作用：初始化状态变量、路径、运行环境、界面绑定变量，并依次调用：
     - `configure_root()`
     - `build_ui()`
     - `refresh_environment_status()`
     - `refresh_corp_dirs()`
     - `refresh_results()`
     - `refresh_realtime_controls()`
     - `process_ui_queue()`

当前界面是一个**单主窗口设计**，没有额外的独立子窗口；辅助交互主要通过：

1. 菜单栏
2. `messagebox` 提示框
3. `filedialog.askdirectory()` 目录选择框
4. 系统默认文件/目录打开动作 `os.startfile()`

## 三、技术栈与界面支撑代码

### 1. GUI 框架

由以下代码实现：

1. `import tkinter as tk`
   - 位置：`wxwork_recovery_gui.py` 第 `19` 行
2. `from tkinter import filedialog, messagebox, ttk`
   - 位置：`wxwork_recovery_gui.py` 第 `18` 行

对应作用：

1. `tk` 负责 `Tk`、`PanedWindow`、`Text`、`Menu`、`Canvas` 等基础控件
2. `ttk` 负责 `Frame`、`LabelFrame`、`Button`、`Treeview`、`Combobox`、`Scrollbar` 等主题控件
3. `filedialog` 负责输出目录选择
4. `messagebox` 负责警告、提示、错误弹窗

### 2. 图片预览支持

由以下代码实现：

1. `from PIL import Image, ImageOps, ImageTk`
   - 位置：`wxwork_recovery_gui.py` 第 `21` 行
2. `insert_preview_image()`
   - 位置：`wxwork_recovery_gui.py` 第 `1716` 行附近
3. `append_media_preview()`
   - 位置：`wxwork_recovery_gui.py` 第 `1737` 行附近

对应作用：

1. 打开并缩放本机缓存图片
2. 将缩略图插入聊天内容浏览区
3. 在图片加载失败时输出友好提示

### 3. 异步任务与 UI 回写

由以下代码实现：

1. `self.ui_queue = queue.Queue()`
   - 位置：`wxwork_recovery_gui.py` 第 `207` 行附近
2. `threading.Thread(...)`
   - 用于进程扫描、恢复、会话扫描、聊天预览、聊天整理
3. `process_ui_queue()`
   - 位置：`wxwork_recovery_gui.py` 第 `2182` 行附近

对应作用：

1. 后台任务在线程中执行，避免主窗口卡死
2. 后台线程把结果写入 `ui_queue`
3. 主线程定时轮询 `ui_queue`，再更新界面控件

## 四、当前主窗口整体布局设计

### 1. 主窗口

由以下代码实现：

1. `configure_root()`
   - 位置：`wxwork_recovery_gui.py` 第 `263` 行附近

主要设计：

1. 窗口标题：`企业微信聊天记录浏览工具`
2. 默认尺寸：`1280x820`
3. 最小尺寸：`980x650`
4. 主配色：
   - 页面底色：`#edf2f7`
   - 信息卡白底：`#ffffff`
   - 日志区深色底：`#0f1720`
5. 样式通过 `ttk.Style()` 统一设置

### 2. 顶部说明区

由以下代码实现：

1. `build_ui()`
   - 位置：`wxwork_recovery_gui.py` 第 `306` 行附近

界面内容：

1. 标题：`企业微信聊天记录浏览`
2. 副标题：说明当前界面支持恢复目标设置、实时恢复、会话整理与完整聊天浏览
3. 右上角状态文本：绑定 `self.status_var`

设计作用：

1. 明确当前工具定位
2. 让用户随时看到当前运行状态

### 3. 主分栏结构

由以下代码实现：

1. `build_ui()`
2. `initialize_responsive_layout()`
   - 位置：`wxwork_recovery_gui.py` 第 `992` 行附近
3. `refresh_responsive_layout()`
   - 位置：`wxwork_recovery_gui.py` 第 `1033` 行附近
4. `resize_tree_columns()`
   - 位置：`wxwork_recovery_gui.py` 第 `1044` 行附近

当前布局是**嵌套 PanedWindow 结构**：

1. 第一层：左右分栏
   - `self.main_pane`
   - 左栏和右栏都可拖拽调整宽度
2. 左栏：上下三段
   - 会话摘要区
   - 会话列表区
   - 运行日志区
3. 右栏：上下两段
   - 整理与恢复控制区
   - 聊天内容浏览区

## 五、菜单栏设计

由以下代码实现：

1. `build_menu()`
   - 位置：`wxwork_recovery_gui.py` 第 `390` 行附近

当前菜单栏分为三组：

### 1. 文件

功能项：

1. 打开输出目录
   - 代码：`open_output_dir()`
2. 打开整理目录
   - 代码：`open_organized_dir()`
3. 刷新结果文件
   - 代码：`refresh_results()`
4. 退出
   - 代码：`on_close()`

### 2. 聊天整理

功能项：

1. 刷新会话列表
   - 代码：`start_conversation_scan()`
2. 刷新当前预览
   - 代码：`refresh_selected_conversation_preview(force=True)`
3. 立即恢复一次
   - 代码：`start_recovery()`
4. 开启实时恢复
   - 代码：`start_realtime_recovery()`
5. 停止实时恢复
   - 代码：`stop_realtime_recovery_by_user()`
6. 整理所选会话
   - 代码：`start_organize_selected()`
7. 整理当前筛选结果
   - 代码：`start_organize_filtered()`
8. 打开最近导出索引
   - 代码：`open_organized_index()`
9. 打开全部会话总表
   - 代码：`open_all_conversations_index()`
10. 打开最近整理时间线
   - 代码：`open_latest_organized_timeline()`

### 3. 帮助

功能项：

1. 打开 GUI 使用说明
2. 打开按钮功能详解
3. 打开操作文档
4. 打开交接文档
5. 打开链路说明
6. 打开企业微信目录

实现代码：

1. `open_named_file(name)`
   - 位置：`wxwork_recovery_gui.py` 第 `2145` 行附近
2. `open_docs_dir()`
   - 位置：`wxwork_recovery_gui.py` 第 `2171` 行附近

## 六、左栏窗口区域设计

## 1. 会话摘要区

由以下代码实现：

1. `build_preview_summary_frame()`
   - 位置：`wxwork_recovery_gui.py` 第 `651` 行附近

该区域分成两张信息卡：

### 1. 顶部摘要卡 `summary_card`

显示内容：

1. 整理输出目录
   - 绑定变量：`self.organize_output_dir_var`
2. 最新 sqlite
   - 绑定变量：`self.latest_sqlite_var`
3. 实时恢复状态
   - 绑定变量：`self.realtime_status_var`
4. 最近执行记录
   - 绑定变量：`self.realtime_last_run_var`

按钮功能：

1. 打开目录
   - 调用：`open_organized_dir()`
2. 最近导出索引
   - 调用：`open_organized_index()`
3. 全部会话总表
   - 调用：`open_all_conversations_index()`
4. 最近整理时间线
   - 调用：`open_latest_organized_timeline()`

### 2. 会话信息卡 `info_card`

显示内容：

1. 会话标题
   - 变量：`self.preview_title_var`
2. 会话副标题
   - 变量：`self.preview_subtitle_var`
3. 会话类型
   - 变量：`self.preview_kind_var`
4. 会话 ID
   - 变量：`self.preview_id_var`
5. 对方 ID
   - 变量：`self.preview_counterpart_var`
6. 消息数 / 发送人数
   - 变量：`self.preview_stats_var`
7. 时间范围
   - 变量：`self.preview_range_var`
8. 高频发送人
   - 变量：`self.preview_participants_var`
9. 命名依据
   - 变量：`self.preview_evidence_var`

按钮功能：

1. 整理当前会话
   - 调用：`start_organize_selected()`
2. 最近时间线
   - 调用：`open_latest_organized_timeline()`

信息刷新链路：

1. 选中左侧会话
2. `on_conversation_selected()`
3. `update_preview_card_from_conversation()`
4. `request_conversation_preview()`
5. `render_conversation_preview()`

## 2. 会话列表区

由以下代码实现：

1. `build_conversation_list_frame()`
   - 位置：`wxwork_recovery_gui.py` 第 `735` 行附近

当前内容：

1. 顶部说明文字
2. `Treeview` 会话表
3. 纵向/横向滚动条
4. 底部提示文字
   - 绑定变量：`self.conversation_hint_var`

表格展示列：

1. 会话 / 客户
2. 最近时间
3. 消息数
4. 类型

隐藏但仍作为数据存储的列：

1. `conversation_id`
2. `counterpart_id`
3. `sender_count`

交互设计：

1. 单击会话
   - 事件：`<<TreeviewSelect>>`
   - 代码：`on_conversation_selected()`
   - 作用：在右侧加载完整聊天内容
2. 双击会话
   - 事件：`<Double-1>`
   - 代码：`start_organize_selected()`
   - 作用：直接整理当前会话

数据来源链路：

1. `start_conversation_scan()`
2. `conversation_scan_worker()`
3. `organizer.list_conversations(...)`
4. `process_ui_queue()` 中处理 `conversation_list`
5. `apply_conversation_filter()`
6. `populate_conversation_tree()`

## 3. 运行日志区

由以下代码实现：

1. `build_log_frame()`
   - 位置：`wxwork_recovery_gui.py` 第 `928` 行附近
2. `log(message)`
   - 位置：`wxwork_recovery_gui.py` 第 `1197` 行附近

当前内容：

1. 深色背景文本框
2. 横向 / 纵向滚动条
3. 初始文本：`图形界面已初始化。`

设计作用：

1. 显示恢复、扫描、整理、预览加载等后台动作日志
2. 作为错误诊断主窗口

## 七、右栏窗口区域设计

## 1. 整理与恢复控制区

由以下代码实现：

1. `build_organize_controls_frame()`
   - 位置：`wxwork_recovery_gui.py` 第 `784` 行附近

该区域是当前项目右栏的核心操作区，包含四组内容。

### 1. 恢复目标区 `target_frame`

显示内容：

1. 企业账号 ID 输入框
   - 变量：`self.corp_id_var`
2. 可选 PID 输入框
   - 变量：`self.pid_var`
3. 输出根目录输入框
   - 变量：`self.output_dir_var`
4. 提示文本
   - 变量：`self.selection_hint_var`

按钮功能与实现代码：

1. 刷新账号列表
   - `refresh_corp_dirs()`
   - 位置：`wxwork_recovery_gui.py` 第 `1410` 行附近
2. 扫描匹配进程
   - `start_process_scan()`
   - 位置：`wxwork_recovery_gui.py` 第 `1571` 行附近
3. 清空 PID
   - `lambda: self.pid_var.set("")`
4. 立即恢复一次
   - `start_recovery()`
   - 位置：`wxwork_recovery_gui.py` 第 `1594` 行附近
5. 浏览输出目录
   - `choose_output_dir()`
   - 位置：`wxwork_recovery_gui.py` 第 `2153` 行附近
6. 打开输出目录
   - `open_output_dir()`
   - 位置：`wxwork_recovery_gui.py` 第 `2167` 行附近

对应设计意图：

1. 用户在当前主界面里就能完成恢复目标设置
2. 不再依赖旧版单独“恢复操作”页

### 2. 实时恢复区 `realtime_frame`

显示内容：

1. 间隔秒数输入框
   - 变量：`self.realtime_interval_var`
2. 开启实时恢复按钮
3. 停止实时恢复按钮
4. 当前实时恢复状态文本
   - 变量：`self.realtime_status_var`
5. 最近一次执行结果文本
   - 变量：`self.realtime_last_run_var`

实现代码：

1. `start_realtime_recovery()`
   - 位置：`wxwork_recovery_gui.py` 第 `1344` 行附近
2. `stop_realtime_recovery()`
   - 位置：`wxwork_recovery_gui.py` 第 `1367` 行附近
3. `stop_realtime_recovery_by_user()`
   - 位置：`wxwork_recovery_gui.py` 第 `1382` 行附近
4. `schedule_realtime_recovery()`
5. `run_scheduled_realtime_recovery()`
6. `update_realtime_status_after_cycle()`
7. `refresh_realtime_controls()`

设计特点：

1. 实时恢复本质上是按间隔自动重跑恢复
2. 如果上一轮执行超时，会在本轮结束后立即补跑下一轮
3. 当当前有其他任务占用时，会延迟重试，避免并发冲突

### 3. 会话筛选区 `filter_row`

显示内容：

1. `Combobox` 下拉筛选框
   - 变量：`self.conversation_filter_var`

实现代码：

1. `CONVERSATION_FILTERS`
   - 位置：`wxwork_recovery_gui.py` 第 `33` 行附近
2. `on_conversation_filter_changed()`
3. `apply_conversation_filter()`
4. `filtered_conversation_rows()`

支持筛选：

1. 聊天会话（内/外部）
2. 全部会话
3. 外部群
4. 内部群/部门群
5. 单聊
6. 助手/应用
7. F 类
8. M 类
9. 审批
10. 邮件
11. 其他

### 4. 整理操作按钮区 `button_grid`

按钮功能：

1. 刷新会话列表
   - `start_conversation_scan()`
2. 刷新当前预览
   - `refresh_selected_conversation_preview(force=True)`
3. 整理所选会话
   - `start_organize_selected()`
4. 整理当前筛选结果
   - `start_organize_filtered()`
5. 打开整理目录
   - `open_organized_dir()`
6. 最近导出索引
   - `open_organized_index()`

整理功能的后台实现：

1. `start_organize()`
2. `organize_worker()`
3. `organizer.run_organize(...)`
4. `process_ui_queue()` 处理 `organize_result`

## 2. 聊天内容浏览区

由以下代码实现：

1. `build_chat_browser_frame()`
   - 位置：`wxwork_recovery_gui.py` 第 `893` 行附近
2. `request_conversation_preview()`
   - 位置：`wxwork_recovery_gui.py` 第 `1834` 行附近
3. `conversation_preview_worker()`
4. `render_conversation_preview()`
   - 位置：`wxwork_recovery_gui.py` 第 `1771` 行附近
5. `insert_preview_image()`
6. `append_media_preview()`

显示内容：

1. 大型 `Text` 文本浏览区
2. 横向/纵向滚动条
3. 底部状态文本
   - 变量：`self.preview_status_var`

聊天消息渲染设计：

1. 每条消息显示元信息行
   - 序号
   - 时间
   - flag
   - 媒体类型
2. 显示发送人
3. 显示内容类型
4. 显示消息预览文本
5. 如果命中图片，则插入缩略图
6. 以分隔线区分消息

文本样式标签：

1. `meta`
2. `sender`
3. `type`
4. `body`
5. `separator`

图片显示链路：

1. 用户选中会话
2. `request_conversation_preview()`
3. `organizer.get_conversation_preview(...)`
4. 返回图片文件路径列表
5. `append_media_preview()`
6. `insert_preview_image()`
7. 使用 `ImageTk.PhotoImage` 插入到 `Text` 组件中

## 八、弹窗与辅助交互设计

### 1. 警告 / 提示 / 错误弹窗

由 `messagebox` 实现，常见场景包括：

1. 企业账号 ID 为空或格式错误
   - `validate_corp_id()`
2. PID 非法
   - `parsed_pid()`
3. 实时恢复间隔非法
   - `get_realtime_interval_seconds()`
4. 当前任务进行中
5. 未选择会话
6. 没有可整理的会话
7. 未找到目录 / 文件
8. 任务失败时显示错误摘要
   - `process_ui_queue()` 处理中 `task_failed`

### 2. 输出目录选择框

由以下代码实现：

1. `choose_output_dir()`
   - 位置：`wxwork_recovery_gui.py` 第 `2153` 行附近
2. `filedialog.askdirectory(...)`

设计作用：

1. 允许用户切换恢复输出根目录
2. 切换后会重置当前会话缓存并刷新结果文件列表

## 九、后台任务与窗口行为链路

当前 GUI 使用“前台主线程 + 后台工作线程 + UI 队列”的模式。

### 1. 进程扫描

链路：

1. 按钮：扫描匹配进程
2. `start_process_scan()`
3. `process_scan_worker()`
4. `engine.find_processes_with_target_path(corp_id)`
5. `process_ui_queue()` 处理 `process_hits`
6. `populate_process_tree()` / 自动回填 PID

### 2. 手动恢复 / 实时恢复

链路：

1. 手动恢复按钮或菜单
2. `start_recovery()`
3. `recovery_worker()`
4. `engine.run_recovery(...)`
5. `process_ui_queue()` 处理：
   - `log`
   - `recovery_result`
   - `task_done`
   - `task_failed`

### 3. 会话扫描

链路：

1. 刷新会话列表
2. `start_conversation_scan()`
3. `conversation_scan_worker()`
4. `organizer.list_conversations(...)`
5. `process_ui_queue()` 处理 `conversation_list`
6. `apply_conversation_filter()`
7. `populate_conversation_tree()`

### 4. 聊天预览加载

链路：

1. 选中会话
2. `request_conversation_preview()`
3. `conversation_preview_worker()`
4. `organizer.get_conversation_preview(...)`
5. `process_ui_queue()` 处理 `conversation_preview`
6. `render_conversation_preview()`

### 5. 聊天整理导出

链路：

1. 整理所选会话 / 整理当前筛选结果
2. `start_organize()`
3. `organize_worker()`
4. `organizer.run_organize(...)`
5. `process_ui_queue()` 处理 `organize_result`
6. 自动更新日志、提示信息，必要时自动打开时间线或会话目录

## 十、代码中保留但当前未挂载到主界面的旧窗口/区域

当前 `build_ui()` 里实际挂载的只有：

1. `build_preview_summary_frame()`
2. `build_conversation_list_frame()`
3. `build_log_frame()`
4. `build_organize_controls_frame()`
5. `build_chat_browser_frame()`

下面这些方法仍保留在代码里，但**当前主界面没有挂载显示**：

1. `build_simple_frame()`
   - 旧版“三步式”简化恢复页
2. `build_environment_frame()`
   - 旧版运行环境说明区
3. `build_target_frame()`
   - 旧版目标设置区
4. `build_corp_frame()`
   - 旧版企业账号目录表格
5. `build_process_frame()`
   - 旧版候选进程表格
6. `build_actions_frame()`
   - 旧版执行操作区
7. `build_results_frame()`
   - 旧版输出文件区

这部分代码的意义更接近：

1. 历史保留
2. 备用布局
3. 后续可能复用的功能块

不能把这些旧方法误认为当前窗口正在显示的内容。

## 十一、界面设计结论

当前项目 GUI 的设计重点不是“多窗口”，而是“单主窗口下的恢复、整理、浏览一体化”。

当前实际设计思路是：

1. 顶部做统一状态说明
2. 左栏负责会话概览与日志
3. 右栏负责恢复控制与聊天浏览
4. 通过后台线程 + UI 队列避免卡界面
5. 通过 `Text + 图片插入` 的方式实现聊天内容浏览，而不是独立聊天子窗口

如果后续真的要改成 PyQt5，那么这份文档可作为**当前 Tkinter 版本的界面基线**，用于后续迁移设计参考。
