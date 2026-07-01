# WXWork Chat Recovery Playbook

## Purpose

This document records the exact method that successfully recovered readable WXWork chat records on this machine.

This is the fast path that another Codex should follow on another Windows machine running the same WXWork build.

Validated target:

- WXWork `5.0.7.6005`
- Windows
- `WXWork.exe` running as 32-bit under WOW64

## Core Idea

The readable data did **not** come from directly opening the on-disk file:

`C:\Users\<User>\Documents\WXWork\<CorpId>\Data\message.db`

That file is encrypted on disk.

The readable data came from:

1. finding the correct running `WXWork.exe` process for the target corp/account
2. locating decrypted SQLite pages for that account's `message.db` in process memory
3. extracting the in-memory SQLite page cache
4. parsing `sqlite_master`
5. traversing `message_table` directly from cached pages
6. exporting partial readable results

## Why This Worked Better Than Disk Decryption

What was observed:

- direct `sqlite3` open on `message.db` returned `file is not a database`
- common SQLCipher-style attempts were not enough to open the raw file
- rebuilding a full SQLite file from incomplete cached pages usually produced `database disk image is malformed`

What actually worked:

- skip trying to fully open the whole DB first
- directly parse the reachable b-tree pages already decrypted in memory

This is faster and more reliable for partial recovery.

## Important Limits

This method is **partial by design**.

It only sees pages currently cached in memory, so results depend on what WXWork recently opened.

To improve recovery:

1. open the target account in WXWork
2. open the target chat
3. scroll more history
4. rerun extraction

## Required Python Packages

Install:

```powershell
pip install pymem psutil
```

`frida` and `sqlcipher3` were explored during debugging but are **not required** for the fast path that actually worked.

## Files

Reusable script:

`<PROJECT_DIR>\recover_wxwork_partial_messages.py`

Default output directory:

`<PROJECT_DIR>`

## Fast Execution

### Step 1. Identify the target Corp ID

Find the account folder under:

`C:\Users\<User>\Documents\WXWork`

Example:

`<CorpId>`

Use that folder name as `--corp-id`.

### Step 2. Warm the cache

Before extraction:

1. open WXWork
2. switch to the target corp/account
3. open the target chat or several important chats
4. scroll older messages

### Step 3. Run the script

Example:

```powershell
python <PROJECT_DIR>\recover_wxwork_partial_messages.py --corp-id <CorpId> --output-dir <PROJECT_DIR>
```

If the correct `WXWork.exe` PID is already known:

```powershell
python <PROJECT_DIR>\recover_wxwork_partial_messages.py --corp-id <CorpId> --pid 15256 --output-dir <PROJECT_DIR>
```

## What The Script Does

### 1. Finds the correct `WXWork.exe`

It scans every `WXWork.exe` process for the in-memory path fragment:

`<CorpId>\Data\message.db`

The process with the strongest hit count is treated as the target process.

This matters because multiple `WXWork.exe` processes may exist, and some belong to other corp accounts.

### 2. Detects pointer width

For this WXWork build, the successful path used a **32-bit** memory layout.

That was a critical discovery.

The working offsets were:

- `pData = +4`
- `pCache = +20` and `+12` were both usable
- `pgno = +24`

If another build changes these offsets, the script may need adjustment.

### 3. Finds candidate page 1 pages

The script scans readable memory for `message_table`, then walks backward inside a 4 KB window looking for a page that:

- starts with `SQLite format 3\0`
- has a valid b-tree header at offset `100`
- contains `message_table`

These are treated as candidate `page 1` pages for `message.db`.

### 4. Resolves the SQLite page cache

For each page 1 candidate, the script:

1. searches for pointers to that page data address
2. interprets those hits as possible `PgHdr` structures
3. extracts candidate `pCache` pointers
4. scans memory again for all pages attached to that `pCache`
5. validates pages by SQLite b-tree signatures

The cache producing the most valid pages is selected.

### 5. Parses `sqlite_master`

The script walks SQLite root page `1`, reads `sqlite_master`, and finds the root page for:

`message_table`

This avoids hardcoding the root page.

### 6. Traverses `message_table`

The script recursively walks the `message_table` b-tree:

- interior table pages: `0x05`
- leaf table pages: `0x0d`

Missing pages are recorded, but reachable pages are still parsed.

### 7. Parses rows manually

The script manually decodes SQLite records using:

- varints
- SQLite serial types
- the known column order for WXWork `5.0.7.6005`

Column order used:

1. `message_id`
2. `server_id`
3. `sequence`
4. `sender_id`
5. `conversation_id`
6. `content_type`
7. `send_time`
8. `flag`
9. `content`
10. `devinfo`
11. `from_app_id`
12. `msg_from_devinfo`
13. `extra_content`
14. `local_extra_content`
15. `client_id`
16. `local_extra_content_translate_info`
17. `local_extra_content_time_nlp`
18. `local_extra_content_approval_nlp`

Important note:

- `message_id` may be `NULL` in the payload because of SQLite integer primary key behavior
- in that case, the script falls back to the SQLite `rowid`

## Output Files

For each run, the script writes four files to `<PROJECT_DIR>`:

- `wxwork_<CorpId>_partial_<PID>.json`
- `wxwork_<CorpId>_partial_<PID>.sqlite`
- `wxwork_<CorpId>_partial_<PID>_readable.csv`
- `wxwork_<CorpId>_partial_<PID>_report.md`

Recommended files to inspect first:
  
- `*_readable.csv`
- `*_report.md`

## Data Interpretation

Observed `conversation_id` patterns:

- `R:<id>`: likely group or room chats
- `S:<a>_<b>`: likely single chats
- `Y:<id>`: system or business notification channels
- `MAIL`, `APPROVAL`: special system channels

`content` is often not plain text. It may contain:

- protobuf-like payloads
- JSON-like cards
- hex-encoded binary blobs

The script builds a preview by:

1. detecting hex-like payloads
2. decoding bytes best effort as UTF-8
3. extracting readable Chinese / ASCII substrings

This preview is heuristic, not a full protocol decoder.

## How To Get Better Results Faster

Use this order:

1. do **not** start with SQLCipher or Frida
2. first identify the correct `WXWork.exe` via the corp path in memory
3. use the in-memory cache extraction script immediately
4. open and scroll the target chat before rerunning
5. inspect the CSV / report first

In practice this is much faster than trying to fully decrypt the on-disk database.

## Troubleshooting

### No matching process

Likely causes:

- target account is not open
- wrong corp id
- WXWork not running

Fix:

1. open the target account
2. verify the folder under `Documents\WXWork`
3. rerun

### No page 1 candidate found

Likely cause:

- the target chat cache is cold

Fix:

1. open a target chat
2. scroll history
3. rerun

### Too few messages

Likely cause:

- not enough target pages are cached

Fix:

1. open more relevant chats
2. scroll more history
3. rerun

### Wrong account data

Likely cause:

- another `WXWork.exe` process belongs to another corp/account

Fix:

- verify the chosen process contains `<CorpId>\Data\message.db`

## Recommended Prompt For Another Codex

```text
Read <PROJECT_DIR>\文档\交接资料\WXWork_Chat_Recovery_Playbook.md first.
Use <PROJECT_DIR>\recover_wxwork_partial_messages.py as the primary extraction path.
Do not begin with SQLCipher or disk-level decryption unless the in-memory cache path fails.
Write all generated artifacts to <PROJECT_DIR>.
If recovery is sparse, ask me to open the target chat and scroll more history, then rerun.
```

## Short Version

For WXWork `5.0.7.6005`, the fastest practical workflow is:

1. warm the chat cache
2. locate the correct `WXWork.exe`
3. extract cached SQLite pages from memory
4. parse `sqlite_master`
5. traverse `message_table`
6. export partial readable results

This is the exact recovery path that successfully produced readable chat outputs on the current machine.
