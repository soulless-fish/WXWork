# WXWork Recovery Codex Handoff

把下面这段直接交给另一台机器上的 Codex：

```text
先阅读 <PROJECT_DIR>\文档\交接资料\WXWork_Chat_Recovery_Playbook.md。

目标：
从企业微信 WXWork 5.0.7.6005 的运行中进程内存里恢复 target corp/account 的 message_table 聊天记录，不优先走磁盘 message.db 解密。

强制要求：
1. 优先使用 <PROJECT_DIR>\recover_wxwork_partial_messages.py。
2. 所有输出文件都写到 <PROJECT_DIR>。
3. 不要先从 SQLCipher、Frida、整库重建开始，除非内存页缓存方案失败。
4. 必须先确认目标 CorpId，并确认对应 WXWork.exe 进程内存在 "<CorpId>\\Data\\message.db" 路径。
5. 如果恢复结果偏少，先要求我打开目标聊天并滚动更多历史，再重跑脚本。

执行步骤：
1. 确认 Python 依赖：
   pip install pymem psutil
2. 让我打开企业微信目标账号，并打开目标聊天窗口，向上滚动更多历史。
3. 运行：
   python <PROJECT_DIR>\recover_wxwork_partial_messages.py --corp-id <CorpId> --output-dir <PROJECT_DIR>
4. 检查输出：
   - wxwork_<CorpId>_partial_<PID>.json
   - wxwork_<CorpId>_partial_<PID>.sqlite
   - wxwork_<CorpId>_partial_<PID>_readable.csv
   - wxwork_<CorpId>_partial_<PID>_report.md
5. 优先阅读：
   - *_readable.csv
   - *_report.md
6. 如果结果不足，回到第 2 步，让我继续滚动聊天，再重跑。

你需要知道的关键事实：
1. 原始磁盘文件 C:\Users\<User>\Documents\WXWork\<CorpId>\Data\message.db 是加密的，直接 sqlite3 打不开。
2. 成功路径是：从运行中的 WXWork.exe 内存里提取已解密的 SQLite page cache，然后手工解析 sqlite_master 和 message_table。
3. 这个方法天然是“部分恢复”，因为只依赖当前缓存页。
4. 在本机已验证的关键点：
   - WXWork.exe 是 32 位 WOW64 进程
   - 有效 PgHdr 偏移是：
     - pData = +4
     - pCache = +20（+12 也可能可用）
     - pgno = +24
5. 恢复出的 content 字段很多不是纯文本，脚本已经做了 best-effort 的可读字符串提取。

如果脚本失败：
1. 先检查是否选错 CorpId。
2. 再检查目标账号是否真的处于打开状态。
3. 再检查是否已经打开目标聊天并滚动过历史。
4. 只有在内存法确认失败后，才考虑继续做更重的逆向或解密尝试。
```

建议同时把这两个文件也发给另一台机器的 Codex：

- `<PROJECT_DIR>\文档\交接资料\WXWork_Chat_Recovery_Playbook.md`
- `<PROJECT_DIR>\recover_wxwork_partial_messages.py`
