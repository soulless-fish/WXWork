# WXWork Chat Recovery Toolkit

企业微信聊天记录恢复整理工具。这个项目面向 Windows 本地取证、数据迁移和内部合规留档场景，提供企业微信本机缓存消息的恢复、整理、预览和导出能力。

## 功能概览

- 从运行中的 `WXWork.exe` 进程内存里提取已解密的 SQLite page cache。
- 解析企业微信 `message_table`，导出部分可读聊天记录。
- 提供 Tkinter 桌面 GUI，支持账号识别、PID 匹配、手动恢复、定时恢复、会话列表、聊天内容预览和媒体缩略图。
- 将恢复结果整理成 `CSV`、`JSONL`、Markdown 时间线、发送人映射、媒体索引和批次清单。
- 支持 PyInstaller 打包为 CLI / GUI 单文件 EXE。

## 适用环境

- Windows。
- 已登录目标企业微信账号的本机。
- 目标 `WXWork.exe` 正在运行。
- Python 3.10+。

本项目恢复能力依赖目标机器本地缓存。没有登录过目标账号、没有本机缓存、目标聊天未加载时，恢复结果会明显减少。

## 安全与合规说明

本仓库只包含工具源码、说明文档和打包脚本，不包含真实聊天记录、企业账号数据、SQLite 数据库、CSV 导出、图片、视频、EXE 产物、测试输出或操作日志。

请只在你拥有合法授权的设备、账号和数据范围内使用。企业微信聊天记录可能包含个人信息、客户资料和商业数据，导出、存储、上传和共享前需要确认内部授权与合规要求。

## 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## GUI 启动

```powershell
python .\wxwork_recovery_gui.py
```

标准流程：

1. 打开企业微信并切到目标企业账号。
2. 打开目标聊天，向上滚动加载更多历史消息。
3. 启动 GUI。
4. 选择或输入企业账号 ID。
5. 扫描匹配进程。
6. 点击立即恢复。
7. 在会话列表中查看并整理需要导出的会话。

## CLI 恢复

```powershell
python .\recover_wxwork_partial_messages.py --corp-id <CorpId> --output-dir .\WXWorkRecovered
```

已知 PID 时：

```powershell
python .\recover_wxwork_partial_messages.py --corp-id <CorpId> --pid <PID> --output-dir .\WXWorkRecovered
```

## 整理导出

```powershell
python .\organize_wxwork_recovered_messages.py --source-dir .\WXWorkRecovered --output-dir .\organized_external_groups
```

整理后的典型文件：

- `时间线.csv`
- `发送人映射.csv`
- `媒体索引.csv`
- `会话时间线.md`
- `会话摘要.json`
- `recovery_manifest.json`

## 打包

GUI：

```powershell
powershell -ExecutionPolicy Bypass -File .\打包配置\构建脚本\Build_WXWork_Recovery_GUI_Exe.ps1
```

CLI：

```powershell
powershell -ExecutionPolicy Bypass -File .\打包配置\构建脚本\Build_WXWork_Recovery_Exe.ps1
```

## 目录结构

```text
.
├─ wxwork_recovery_gui.py
├─ recover_wxwork_partial_messages.py
├─ organize_wxwork_recovered_messages.py
├─ read_wxwork_encrypted_databases.py
├─ 文档
│  ├─ 使用说明
│  ├─ 交接资料
│  └─ 设计资料
├─ 运行脚本
└─ 打包配置
   └─ 构建脚本
```

## 项目名称

本项目公开名称定为：

```text
WXWork Chat Recovery Toolkit
企业微信聊天记录恢复整理工具
```
