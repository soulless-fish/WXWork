import argparse
import csv
import datetime as dt
import importlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path


DEFAULT_DB_NAMES = ("message.db", "session.db", "user.db")
SQLITE_HEADER = b"SQLite format 3\x00"
SQLCIPHER_MODULE_CANDIDATES = (
    "sqlcipher3",
    "pysqlcipher3.dbapi2",
    "pysqlcipher.dbapi2",
)
SQLCIPHER_ATTEMPT_SETTINGS = (
    {"compatibility": None, "page_size": None},
    {"compatibility": 4, "page_size": None},
    {"compatibility": 4, "page_size": 4096},
    {"compatibility": 3, "page_size": None},
    {"compatibility": 3, "page_size": 4096},
    {"compatibility": 2, "page_size": None},
    {"compatibility": 1, "page_size": None},
)
SQLCIPHER_EXTERNAL_TIMEOUT_SECONDS = 60
MEMORY_HEX_KEY_RE = re.compile(rb"(?<![0-9a-fA-F])(?:x'[0-9a-fA-F]{64,128}'|[0-9a-fA-F]{128}|[0-9a-fA-F]{96}|[0-9a-fA-F]{80}|[0-9a-fA-F]{64})(?![0-9a-fA-F])")
MEMORY_CONTEXT_TOKENS = (
    (b"message.db", 8),
    (b"session.db", 5),
    (b"user.db", 5),
    (b"dbkey", 8),
    (b"key", 3),
    (b"cipher", 6),
    (b"wcdb", 6),
    (b"sqlite", 4),
    (b"data", 2),
)


def detect_docs_dir() -> Path:
    """
    自动定位当前用户的企业微信文档目录，避免把账号目录写死在某台机器上。
    """
    userprofile = os.environ.get("USERPROFILE")
    candidates = []
    if userprofile:
        candidates.append(Path(userprofile) / "Documents" / "WXWork")
    candidates.append(Path.home() / "Documents" / "WXWork")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def default_output_dir() -> Path:
    """
    加密业务库读取结果默认放到测试输出目录，避免把复制出的业务库散落在项目根目录。
    """
    return Path(__file__).resolve().parent / "测试文件" / "测试输出" / "加密业务库读取"


def locate_account_dir(corp_id: str, docs_dir=None) -> Path:
    """
    根据企业账号 ID 定位 `Documents\\WXWork\\<corp_id>` 账号目录。
    """
    if not corp_id:
        raise ValueError("corp_id 不能为空。")
    docs_root = Path(docs_dir) if docs_dir else detect_docs_dir()
    account_dir = docs_root / str(corp_id)
    if not account_dir.exists():
        raise FileNotFoundError(f"未找到企业微信账号目录：{account_dir}")
    return account_dir


def read_file_prefix(path: Path, size: int = 64) -> bytes:
    """
    读取文件头，用于判断是否是普通 SQLite 或加密态业务库。
    """
    with path.open("rb") as handle:
        return handle.read(size)


def database_header_info(path: Path):
    """
    汇总业务库文件头信息，便于判断当前库是否能直接 sqlite 打开。
    """
    prefix = read_file_prefix(path)
    return {
        "path": str(path),
        "size": path.stat().st_size if path.exists() else 0,
        "first_16_hex": prefix[:16].hex(),
        "first_64_hex": prefix[:64].hex(),
        "is_plain_sqlite_header": prefix.startswith(SQLITE_HEADER),
    }


def copy_database_snapshot(db_path: Path, snapshot_dir: Path):
    """
    复制 db、wal、shm 三类文件，避免直接读取正在被企业微信占用的源文件。
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for suffix in ("", "-wal", "-shm"):
        source = db_path.with_name(db_path.name + suffix)
        if not source.exists():
            continue
        target = snapshot_dir / source.name
        shutil.copy2(source, target)
        copied.append(str(target))
    return copied


def open_plain_sqlite(path: Path):
    """
    尝试按普通 SQLite 打开；成功说明该库不需要走解密链路。
    """
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        return conn
    except Exception:
        conn.close()
        raise


def sqlite_schema_summary(conn):
    """
    读取已打开数据库的表清单和每张表的字段信息。
    """
    tables = []
    for row in conn.execute("SELECT name, type, sql FROM sqlite_master WHERE type IN ('table', 'index', 'view') ORDER BY type, name"):
        item = {"name": row["name"], "type": row["type"], "sql": row["sql"] or ""}
        if row["type"] == "table":
            try:
                columns = []
                for col in conn.execute(f"PRAGMA table_info({quote_identifier(row['name'])})"):
                    columns.append({"name": col["name"], "type": col["type"], "notnull": col["notnull"], "pk": col["pk"]})
                item["columns"] = columns
            except Exception as exc:
                item["columns_error"] = str(exc)
        tables.append(item)
    return tables


def quote_identifier(value: str):
    """
    为 SQLite 标识符加双引号，避免表名里出现特殊字符时 SQL 拼接失败。
    """
    return '"' + str(value).replace('"', '""') + '"'


def export_table_csv(conn, table_name: str, output_dir: Path, max_rows: int = 0):
    """
    把已解密或普通 SQLite 表导出为 CSV；max_rows 为 0 时导出全量。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{safe_filename(table_name)}.csv"
    limit_sql = "" if not max_rows else f" LIMIT {int(max_rows)}"
    query = f"SELECT * FROM {quote_identifier(table_name)}{limit_sql}"
    cur = conn.execute(query)
    columns = [item[0] for item in cur.description]
    row_count = 0
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in cur:
            writer.writerow([row[column] for column in columns])
            row_count += 1
    return {"table": table_name, "path": str(target), "row_count": row_count}


def safe_filename(value: str):
    """
    把表名转换成可用文件名，避免 Windows 路径非法字符。
    """
    text = "".join("_" if ch in '<>:"/\\|?*\x00\r\n\t' else ch for ch in str(value))
    return text[:120] or "table"


def setting_label(setting: dict):
    """
    把 SQLCipher 兼容参数转换成短标签，用于报告和导出文件名。
    """
    compatibility = setting.get("compatibility")
    page_size = setting.get("page_size")
    parts = [f"compat{compatibility}" if compatibility is not None else "default"]
    if page_size:
        parts.append(f"page{page_size}")
    return "_".join(parts)


def setting_public_info(setting: dict):
    """
    只保留可写入报告的 SQLCipher 参数，不包含任何密钥内容。
    """
    return {
        "compatibility": setting.get("compatibility"),
        "page_size": setting.get("page_size"),
    }


def sqlcipher_setting_sql(setting: dict):
    """
    生成 SQLCipher 兼容参数脚本；必须在第一次读取 sqlite_master 前执行。
    """
    lines = []
    compatibility = setting.get("compatibility")
    page_size = setting.get("page_size")
    if compatibility is not None:
        lines.append(f"PRAGMA cipher_compatibility = {int(compatibility)};")
    if page_size:
        lines.append(f"PRAGMA cipher_page_size = {int(page_size)};")
    return "\n".join(lines)


def apply_sqlcipher_settings(cursor, setting: dict):
    """
    给 Python SQLCipher 连接应用兼容参数；顺序要早于第一次读取数据库内容。
    """
    compatibility = setting.get("compatibility")
    page_size = setting.get("page_size")
    if compatibility is not None:
        cursor.execute(f"PRAGMA cipher_compatibility = {int(compatibility)}")
    if page_size:
        cursor.execute(f"PRAGMA cipher_page_size = {int(page_size)}")


def quote_sql_literal(value: str):
    """
    为 SQL 字符串字面量加单引号，避免路径或口令里出现单引号导致 SQL 脚本失败。
    """
    return "'" + str(value).replace("'", "''") + "'"


def load_sqlcipher_module():
    """
    按常见模块名加载 SQLCipher Python 绑定；没有安装时返回 None。
    """
    errors = {}
    for module_name in SQLCIPHER_MODULE_CANDIDATES:
        try:
            return module_name, importlib.import_module(module_name)
        except Exception as exc:
            errors[module_name] = str(exc)
    return None, errors


def find_sqlcipher_exe(sqlcipher_exe=""):
    """
    定位外部 sqlcipher.exe；优先使用命令行参数，其次使用环境变量和 PATH。
    """
    candidates = []
    if sqlcipher_exe:
        candidates.append(str(sqlcipher_exe))
    env_sqlcipher = os.environ.get("SQLCIPHER_EXE")
    if env_sqlcipher:
        candidates.append(env_sqlcipher)
    path_sqlcipher = shutil.which("sqlcipher")
    if path_sqlcipher:
        candidates.append(path_sqlcipher)
    path_sqlcipher_exe = shutil.which("sqlcipher.exe")
    if path_sqlcipher_exe:
        candidates.append(path_sqlcipher_exe)
    candidates.extend(
        [
            r"C:\msys64\ucrt64\bin\sqlcipher.exe",
            r"C:\msys64\mingw64\bin\sqlcipher.exe",
            r"C:\msys64\clang64\bin\sqlcipher.exe",
        ]
    )
    chocolatey_install = os.environ.get("ChocolateyInstall")
    if chocolatey_install:
        candidates.append(str(Path(chocolatey_install) / "bin" / "sqlcipher.exe"))

    checked = set()
    for candidate in candidates:
        if not candidate:
            continue
        text = str(candidate).strip().strip('"')
        if not text or text.lower() in checked:
            continue
        checked.add(text.lower())
        command_path = shutil.which(text)
        if command_path:
            return command_path
        path = Path(text)
        if path.exists() and path.is_file():
            return str(path)
    return ""


def sqlcipher_key_expression(raw_key: str):
    """
    生成 SQLCipher PRAGMA key 表达式；支持 `hex:<key>`、裸 64 位十六进制和普通口令。
    """
    key = str(raw_key or "").strip()
    if not key:
        return ""
    lowered = key.lower()
    if lowered.startswith("hex:"):
        hex_key = key[4:].strip()
        return quote_sql_literal(f"x'{hex_key}'")
    if len(key) in (64, 96, 128) and all(ch in "0123456789abcdefABCDEF" for ch in key):
        return quote_sql_literal(f"x'{key}'")
    return quote_sql_literal(key)


def load_key_candidates(cli_key="", key_file=""):
    """
    从命令行、文件和环境变量收集候选解密密钥。
    """
    candidates = []
    sources = []

    def add_candidate(value, source):
        text = str(value or "").strip()
        if not text:
            return
        if text in candidates:
            return
        candidates.append(text)
        sources.append(source)

    add_candidate(cli_key, "cli_key")
    env_key = os.environ.get("WXWORK_DB_KEY")
    add_candidate(env_key, "WXWORK_DB_KEY")

    file_candidates = []
    if key_file:
        file_candidates.append(Path(key_file))
    env_key_file = os.environ.get("WXWORK_DB_KEY_FILE")
    if env_key_file:
        file_candidates.append(Path(env_key_file))

    for path in file_candidates:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            add_candidate(line, f"key_file:{path}")

    return [{"key": key, "source": source} for key, source in zip(candidates, sources)]


def normalize_memory_hex_key(raw_value: bytes):
    """
    从内存命中的十六进制片段里提取可交给 SQLCipher 的 raw key 文本。
    """
    text = raw_value.decode("ascii", errors="ignore").strip()
    lowered = text.lower()
    if lowered.startswith("x'") and text.endswith("'"):
        text = text[2:-1]
    if len(text) not in (64, 80, 96, 128):
        return ""
    if not all(ch in "0123456789abcdefABCDEF" for ch in text):
        return ""
    return text.lower()


def hex_bytes_entropy(hex_text: str):
    """
    计算十六进制候选解码后的熵，用来剔除明显不是密钥的低质量片段。
    """
    try:
        data = bytes.fromhex(hex_text)
    except Exception:
        return 0.0
    if not data:
        return 0.0
    counts = {}
    for value in data:
        counts[value] = counts.get(value, 0) + 1
    total = len(data)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def looks_like_sequential_bytes(hex_text: str):
    """
    过滤 00010203 这类测试表或编码表片段，它们熵不低但不是数据库密钥。
    """
    try:
        data = bytes.fromhex(hex_text)
    except Exception:
        return False
    if len(data) < 16:
        return False
    increasing = 0
    for index in range(1, len(data)):
        if ((data[index - 1] + 1) & 0xFF) == data[index]:
            increasing += 1
    return increasing >= len(data) - 2


def score_memory_key_candidate(hex_text: str, context: bytes, corp_id: str):
    """
    给内存中的十六进制候选打分，优先验证和当前账号、数据库、密钥上下文更近的片段。
    """
    entropy = hex_bytes_entropy(hex_text)
    if entropy < 3.2 or looks_like_sequential_bytes(hex_text):
        return None
    score = entropy + (len(hex_text) / 32.0)
    lowered_context = context.lower()
    if corp_id:
        corp_bytes = str(corp_id).encode("ascii", errors="ignore").lower()
        if corp_bytes and corp_bytes in lowered_context:
            score += 6
    for token, weight in MEMORY_CONTEXT_TOKENS:
        if token in lowered_context:
            score += weight
    return round(score, 6)


def scan_memory_key_candidates(corp_id: str, pid: int = 0, max_candidates: int = 200):
    """
    只读扫描目标 WXWork.exe 内存里的 raw SQLCipher key 候选；不把候选密钥写入报告。
    """
    started_at = time.time()
    summary = {
        "enabled": True,
        "pid": int(pid or 0),
        "matched_processes": [],
        "scanned_regions": 0,
        "scanned_bytes": 0,
        "raw_hit_count": 0,
        "candidate_count": 0,
        "selected_count": 0,
        "duration_seconds": 0,
        "error": "",
    }
    try:
        import pymem
        import recover_wxwork_partial_messages as recovery_memory
    except Exception as exc:
        summary["error"] = f"内存扫描依赖不可用：{exc}"
        summary["duration_seconds"] = round(time.time() - started_at, 3)
        return [], summary

    process_infos = []
    if pid:
        process_infos.append({"pid": int(pid)})
    else:
        try:
            process_infos = recovery_memory.find_processes_with_target_path(corp_id)
        except Exception as exc:
            summary["error"] = f"定位 WXWork.exe 失败：{exc}"
            summary["duration_seconds"] = round(time.time() - started_at, 3)
            return [], summary
    summary["matched_processes"] = [
        {"pid": item.get("pid"), "count": item.get("count"), "wow64": item.get("wow64")}
        for item in process_infos
    ]

    scored = {}
    for proc_info in process_infos:
        current_pid = int(proc_info.get("pid") or 0)
        if not current_pid:
            continue
        pm = pymem.Pymem()
        try:
            pm.open_process_from_id(current_pid)
            wow64 = recovery_memory.is_wow64_process(pm.process_handle)
            upper_bound = 0x7FFFFFFF if wow64 else 0x7FFFFFFF0000
            for base, size in recovery_memory.region_iter(pm, upper_bound):
                try:
                    data = pm.read_bytes(base, size)
                except Exception:
                    continue
                summary["scanned_regions"] += 1
                summary["scanned_bytes"] += len(data)
                for match in MEMORY_HEX_KEY_RE.finditer(data):
                    summary["raw_hit_count"] += 1
                    key_text = normalize_memory_hex_key(match.group(0))
                    if not key_text:
                        continue
                    context = data[max(0, match.start() - 512) : min(len(data), match.end() + 512)]
                    score = score_memory_key_candidate(key_text, context, corp_id)
                    if score is None:
                        continue
                    source = f"memory_hex:pid={current_pid}:addr=0x{base + match.start():x}:score={score}"
                    previous = scored.get(key_text)
                    if previous is None or score > previous["score"]:
                        scored[key_text] = {"key": key_text, "source": source, "score": score}
        except Exception as exc:
            summary["error"] = str(exc)
        finally:
            try:
                pm.close_process()
            except Exception:
                pass

    selected = sorted(scored.values(), key=lambda item: item["score"], reverse=True)
    summary["candidate_count"] = len(selected)
    selected = selected[: max(0, int(max_candidates or 0))]
    summary["selected_count"] = len(selected)
    summary["duration_seconds"] = round(time.time() - started_at, 3)
    return [{"key": item["key"], "source": item["source"]} for item in selected], summary


def run_sqlcipher_exe_script(sqlcipher_exe: str, database_path: Path, script: str):
    """
    调用外部 sqlcipher.exe 执行脚本；脚本通过标准输入传入，避免密钥出现在命令行参数里。
    """
    return subprocess.run(
        [str(sqlcipher_exe), str(database_path)],
        input=script,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=SQLCIPHER_EXTERNAL_TIMEOUT_SECONDS,
        check=False,
    )


def export_plaintext_with_sqlcipher_exe(path: Path, key_expr: str, setting: dict, sqlcipher_exe: str, plain_export_path: Path):
    """
    使用外部 sqlcipher.exe 打开加密库，并导出一份明文 SQLite 供 Python sqlite3 继续读取。
    """
    for suffix in ("", "-wal", "-shm"):
        stale_path = plain_export_path.with_name(plain_export_path.name + suffix)
        if stale_path.exists():
            stale_path.unlink()
    plain_export_path.parent.mkdir(parents=True, exist_ok=True)
    setting_sql = sqlcipher_setting_sql(setting)
    if setting_sql:
        setting_sql += "\n"
    script = (
        ".bail on\n"
        ".timeout 5000\n"
        f"PRAGMA key = {key_expr};\n"
        f"{setting_sql}"
        "SELECT count(*) FROM sqlite_master;\n"
        f"ATTACH DATABASE {quote_sql_literal(str(plain_export_path))} AS plaintext KEY '';\n"
        "SELECT sqlcipher_export('plaintext');\n"
        "DETACH DATABASE plaintext;\n"
        ".exit\n"
    )
    completed = run_sqlcipher_exe_script(sqlcipher_exe, path, script)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or f"sqlcipher.exe 退出码 {completed.returncode}").strip())
    if not plain_export_path.exists():
        raise RuntimeError("sqlcipher.exe 未生成明文导出库。")
    return plain_export_path


def batch_validate_sqlcipher_exe_keys(path: Path, key_candidates, sqlcipher_exe: str):
    """
    用一个 sqlcipher.exe 进程批量验证候选密钥，避免大量候选时反复启动进程。
    """
    if not key_candidates:
        return None, None, {
            "ok": False,
            "reason": "key_missing",
            "attempt_count": 0,
        }

    lines = [".bail off", ".timeout 5000"]
    attempt_count = 0
    db_path = Path(path).resolve().as_posix()
    for key_index, item in enumerate(key_candidates):
        key_expr = sqlcipher_key_expression(item["key"])
        if not key_expr:
            continue
        for setting_index, setting in enumerate(SQLCIPHER_ATTEMPT_SETTINGS):
            setting_sql = sqlcipher_setting_sql(setting)
            lines.append(f".open {quote_sql_literal(db_path)}")
            lines.append(f"PRAGMA key = {key_expr};")
            if setting_sql:
                lines.append(setting_sql)
            lines.append(f"SELECT 'CANDIDATE_OK|{key_index}|{setting_index}|' || count(*) FROM sqlite_master;")
            attempt_count += 1
    lines.append(".exit")
    script = "\n".join(lines) + "\n"

    started_at = time.time()
    completed = run_sqlcipher_exe_script(sqlcipher_exe, path, script)
    duration = round(time.time() - started_at, 3)
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("CANDIDATE_OK|"):
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        try:
            key_index = int(parts[1])
            setting_index = int(parts[2])
        except Exception:
            continue
        if 0 <= key_index < len(key_candidates) and 0 <= setting_index < len(SQLCIPHER_ATTEMPT_SETTINGS):
            return key_candidates[key_index], SQLCIPHER_ATTEMPT_SETTINGS[setting_index], {
                "ok": True,
                "attempt_count": attempt_count,
                "duration_seconds": duration,
                "returncode": completed.returncode,
            }

    return None, None, {
        "ok": False,
        "reason": "all_key_attempts_failed",
        "attempt_count": attempt_count,
        "duration_seconds": duration,
        "returncode": completed.returncode,
        "stderr_sample": (completed.stderr or "")[:4000],
    }


def try_open_sqlcipher_with_module(path: Path, key_candidates):
    """
    使用 Python SQLCipher 绑定尝试打开数据库。
    """
    module_name, module_or_errors = load_sqlcipher_module()
    if not module_name:
        return None, {
            "ok": False,
            "reason": "sqlcipher_module_missing",
            "module_errors": module_or_errors,
        }
    if not key_candidates:
        return None, {
            "ok": False,
            "reason": "key_missing",
            "module": module_name,
        }

    attempts = []
    for item in key_candidates:
        key_expr = sqlcipher_key_expression(item["key"])
        if not key_expr:
            continue
        for setting in SQLCIPHER_ATTEMPT_SETTINGS:
            conn = None
            try:
                conn = module_or_errors.connect(str(path))
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(f"PRAGMA key = {key_expr}")
                apply_sqlcipher_settings(cur, setting)
                cur.execute("SELECT count(*) FROM sqlite_master")
                cur.fetchall()
                return conn, {
                    "ok": True,
                    "module": module_name,
                    "key_source": item["source"],
                    **setting_public_info(setting),
                }
            except Exception as exc:
                attempts.append(
                    {
                        "key_source": item["source"],
                        **setting_public_info(setting),
                        "error": str(exc),
                    }
                )
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
    return None, {
        "ok": False,
        "reason": "all_key_attempts_failed",
        "module": module_name,
        "attempts": attempts[:20],
    }


def try_open_sqlcipher_with_exe(path: Path, key_candidates, sqlcipher_exe="", plain_export_dir=None):
    """
    使用外部 sqlcipher.exe 尝试解密，并返回导出的明文 SQLite 连接。
    """
    detected_exe = find_sqlcipher_exe(sqlcipher_exe=sqlcipher_exe)
    if not detected_exe:
        return None, {
            "ok": False,
            "reason": "sqlcipher_exe_missing",
            "requested_exe": str(sqlcipher_exe or ""),
            "env_SQLCIPHER_EXE": os.environ.get("SQLCIPHER_EXE", ""),
        }

    if not key_candidates:
        return None, {
            "ok": False,
            "reason": "key_missing",
            "exe": detected_exe,
        }

    matched_key, matched_setting, validation_info = batch_validate_sqlcipher_exe_keys(path, key_candidates, detected_exe)
    if matched_key is None:
        return None, {
            "ok": False,
            "reason": validation_info.get("reason", "all_key_attempts_failed"),
            "exe": detected_exe,
            "validation": validation_info,
        }

    export_dir = Path(plain_export_dir) if plain_export_dir else path.parent / "_sqlcipher_plain_exports"
    key_expr = sqlcipher_key_expression(matched_key["key"])
    plain_export_path = export_dir / f"{path.stem}_sqlcipher_plain_{safe_filename(str(matched_key['source']))}_{setting_label(matched_setting)}.sqlite"
    conn = None
    export_error = ""
    try:
        exported_path = export_plaintext_with_sqlcipher_exe(path, key_expr, matched_setting, detected_exe, plain_export_path)
        conn = open_plain_sqlite(exported_path)
        return conn, {
            "ok": True,
            "method": "external_exe",
            "exe": detected_exe,
            "key_source": matched_key["source"],
            **setting_public_info(matched_setting),
            "plain_export_path": str(exported_path),
            "validation": validation_info,
        }
    except Exception as exc:
        export_error = str(exc)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return None, {
        "ok": False,
        "reason": "validated_key_export_failed",
        "exe": detected_exe,
        "key_source": matched_key["source"],
        **setting_public_info(matched_setting),
        "validation": validation_info,
        "error": export_error,
    }


def try_open_sqlcipher(path: Path, key_candidates, sqlcipher_exe="", plain_export_dir=None):
    """
    依次使用 Python SQLCipher 绑定和外部 sqlcipher.exe 尝试打开数据库。
    """
    if not key_candidates:
        detected_exe = find_sqlcipher_exe(sqlcipher_exe=sqlcipher_exe)
        module_name, module_or_errors = load_sqlcipher_module()
        reason = "key_missing"
        if not module_name and not detected_exe:
            reason = "key_and_sqlcipher_runtime_missing"
        return None, {
            "ok": False,
            "reason": reason,
            "module": module_name or "",
            "module_errors": {} if module_name else module_or_errors,
            "exe": detected_exe,
        }

    module_conn, module_info = try_open_sqlcipher_with_module(path, key_candidates)
    if module_conn is not None:
        module_info["method"] = "python_module"
        return module_conn, module_info

    exe_conn, exe_info = try_open_sqlcipher_with_exe(path, key_candidates, sqlcipher_exe=sqlcipher_exe, plain_export_dir=plain_export_dir)
    if exe_conn is not None:
        exe_info["python_module"] = module_info
        return exe_conn, exe_info

    reason = exe_info.get("reason") or module_info.get("reason") or "all_sqlcipher_methods_failed"
    if module_info.get("reason") == "sqlcipher_module_missing" and exe_info.get("reason") == "sqlcipher_exe_missing":
        reason = "sqlcipher_runtime_missing"
    return None, {
        "ok": False,
        "reason": reason,
        "python_module": module_info,
        "external_exe": exe_info,
    }


def inspect_database(snapshot_path: Path, output_dir: Path, key_candidates, max_export_rows: int = 0, sqlcipher_exe=""):
    """
    检查单个业务库：普通 SQLite 优先，失败后尝试 SQLCipher。
    """
    result = {
        "database": snapshot_path.name,
        "snapshot_path": str(snapshot_path),
        "header": database_header_info(snapshot_path),
        "plain_sqlite": {"ok": False},
        "sqlcipher": {"ok": False},
        "status": "unknown",
        "schema_json": "",
        "exported_tables": [],
    }

    conn = None
    try:
        conn = open_plain_sqlite(snapshot_path)
        result["plain_sqlite"] = {"ok": True}
        result["status"] = "plain_sqlite_ok"
    except Exception as exc:
        result["plain_sqlite"] = {"ok": False, "error": str(exc)}
    if conn is None:
        conn, cipher_info = try_open_sqlcipher(
            snapshot_path,
            key_candidates,
            sqlcipher_exe=sqlcipher_exe,
            plain_export_dir=output_dir / "sqlcipher_plain_exports",
        )
        result["sqlcipher"] = cipher_info
        if conn is not None:
            result["status"] = "sqlcipher_ok"
        elif cipher_info.get("reason") == "key_missing":
            result["status"] = "encrypted_key_missing"
        elif cipher_info.get("reason") == "key_and_sqlcipher_runtime_missing":
            result["status"] = "encrypted_key_and_sqlcipher_runtime_missing"
        elif cipher_info.get("reason") == "sqlcipher_module_missing":
            result["status"] = "encrypted_sqlcipher_module_missing"
        elif cipher_info.get("reason") in {"sqlcipher_exe_missing", "sqlcipher_runtime_missing"}:
            result["status"] = "encrypted_sqlcipher_runtime_missing"
        else:
            result["status"] = "encrypted_key_failed"

    if conn is not None:
        try:
            schema = sqlite_schema_summary(conn)
            schema_path = output_dir / f"{snapshot_path.stem}_schema.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
            result["schema_json"] = str(schema_path)
            table_dir = output_dir / f"{snapshot_path.stem}_tables"
            for item in schema:
                if item.get("type") != "table" or item.get("name", "").startswith("sqlite_"):
                    continue
                result["exported_tables"].append(export_table_csv(conn, item["name"], table_dir, max_rows=max_export_rows))
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return result


def build_markdown_report(report: dict):
    """
    生成便于人工查看的加密业务库读取报告。
    """
    lines = [
        "# 企业微信加密业务库读取报告",
        "",
        f"- 企业账号ID: `{report.get('corp_id', '')}`",
        f"- 账号目录: `{report.get('account_dir', '')}`",
        f"- 输出目录: `{report.get('output_dir', '')}`",
        f"- 生成时间: `{report.get('generated_at', '')}`",
        f"- 候选密钥数量: `{report.get('key_candidate_count', 0)}`",
        f"- 外部 SQLCipher: `{report.get('sqlcipher_exe', '')}`",
        f"- 内存密钥扫描: `{report.get('memory_key_scan', {}).get('enabled', False)}`",
        "",
    ]
    memory_scan = report.get("memory_key_scan") or {}
    if memory_scan.get("enabled"):
        lines.extend(
            [
                "## 内存密钥扫描",
                "",
                f"- 匹配进程: `{memory_scan.get('matched_processes', [])}`",
                f"- 扫描区域数: `{memory_scan.get('scanned_regions', 0)}`",
                f"- 扫描字节数: `{memory_scan.get('scanned_bytes', 0)}`",
                f"- 原始命中数: `{memory_scan.get('raw_hit_count', 0)}`",
                f"- 候选数量: `{memory_scan.get('candidate_count', 0)}`",
                f"- 参与验证数量: `{memory_scan.get('selected_count', 0)}`",
                f"- 耗时秒数: `{memory_scan.get('duration_seconds', 0)}`",
                f"- 错误: `{memory_scan.get('error', '')}`",
                "",
            ]
        )
    lines.extend(
        [
            "## 数据库检查结果",
            "",
        ]
    )
    for item in report.get("databases", []):
        lines.extend(
            [
                f"### {item.get('database', '')}",
                "",
                f"- 状态: `{item.get('status', '')}`",
                f"- 快照: `{item.get('snapshot_path', '')}`",
                f"- 文件头前16字节: `{item.get('header', {}).get('first_16_hex', '')}`",
                f"- 普通 SQLite: `{item.get('plain_sqlite', {}).get('ok', False)}`",
                f"- SQLCipher: `{item.get('sqlcipher', {}).get('ok', False)}`",
                f"- schema: `{item.get('schema_json', '')}`",
                f"- 导出表数量: `{len(item.get('exported_tables', []))}`",
                "",
            ]
        )
        if item.get("plain_sqlite", {}).get("error"):
            lines.append(f"- 普通 SQLite 错误: `{item['plain_sqlite']['error']}`")
        if item.get("sqlcipher", {}).get("reason"):
            lines.append(f"- SQLCipher 状态: `{item['sqlcipher']['reason']}`")
        validation = item.get("sqlcipher", {}).get("validation") or item.get("sqlcipher", {}).get("external_exe", {}).get("validation") or {}
        if validation:
            lines.append(f"- SQLCipher 验证次数: `{validation.get('attempt_count', 0)}`")
            lines.append(f"- SQLCipher 验证耗时秒数: `{validation.get('duration_seconds', 0)}`")
        lines.append("")
    lines.extend(
        [
            "## 说明",
            "",
            "1. 如果状态是 `encrypted_key_missing`，说明读取链路已经走到 SQLCipher 尝试阶段，但还缺少数据库密钥。",
            "2. 如果状态是 `encrypted_key_and_sqlcipher_runtime_missing`，说明同时缺少候选密钥和 SQLCipher 运行时。",
            "3. 如果状态是 `encrypted_sqlcipher_runtime_missing`，说明当前环境没有可用 Python SQLCipher 绑定，也没有找到外部 sqlcipher.exe。",
            "4. 如果已经安装 SQLCipher 命令行工具，可通过 `--sqlcipher-exe` 或 `SQLCIPHER_EXE` 指定工具路径。",
            "5. 如果后续拿到密钥，可通过 `--key`、`--key-file`、`WXWORK_DB_KEY` 或 `WXWORK_DB_KEY_FILE` 重新运行。",
        ]
    )
    return "\n".join(lines)


def run_read_encrypted_databases(
    corp_id: str,
    docs_dir=None,
    output_dir=None,
    db_names=None,
    key="",
    key_file="",
    sqlcipher_exe="",
    scan_memory_keys=False,
    memory_key_pid=0,
    max_memory_key_candidates=200,
    max_export_rows: int = 0,
):
    """
    加密业务库读取主入口：定位账号目录、复制快照、尝试打开、导出报告。
    """
    account_dir = locate_account_dir(corp_id, docs_dir=docs_dir)
    output_root = Path(output_dir) if output_dir else default_output_dir()
    generated_at = dt.datetime.now().astimezone().replace(microsecond=0).isoformat()
    run_dir = output_root / f"{corp_id}_{generated_at.replace(':', '').replace('+', '_')}"
    snapshot_dir = run_dir / "snapshots"
    run_dir.mkdir(parents=True, exist_ok=True)
    key_candidates = load_key_candidates(cli_key=key, key_file=key_file)
    memory_key_scan = {"enabled": False}
    if scan_memory_keys:
        memory_candidates, memory_key_scan = scan_memory_key_candidates(
            corp_id,
            pid=memory_key_pid,
            max_candidates=max_memory_key_candidates,
        )
        known_keys = {item["key"] for item in key_candidates}
        for item in memory_candidates:
            if item["key"] in known_keys:
                continue
            key_candidates.append(item)
            known_keys.add(item["key"])
    detected_sqlcipher_exe = find_sqlcipher_exe(sqlcipher_exe=sqlcipher_exe)

    databases = []
    for db_name in db_names or DEFAULT_DB_NAMES:
        db_path = account_dir / "Data" / db_name
        item = {"database": db_name, "source_path": str(db_path), "status": "missing"}
        if not db_path.exists():
            databases.append(item)
            continue
        copied = copy_database_snapshot(db_path, snapshot_dir / db_name)
        item["copied_files"] = copied
        snapshot_path = Path(copied[0]) if copied else None
        if snapshot_path:
            item.update(
                inspect_database(
                    snapshot_path,
                    run_dir,
                    key_candidates,
                    max_export_rows=max_export_rows,
                    sqlcipher_exe=detected_sqlcipher_exe,
                )
            )
        databases.append(item)

    report = {
        "corp_id": str(corp_id),
        "account_dir": str(account_dir),
        "output_dir": str(run_dir),
        "generated_at": generated_at,
        "key_candidate_count": len(key_candidates),
        "sqlcipher_exe": detected_sqlcipher_exe,
        "memory_key_scan": memory_key_scan,
        "databases": databases,
    }
    report_json = run_dir / "encrypted_database_read_report.json"
    report_md = run_dir / "encrypted_database_read_report.md"
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report_md.write_text(build_markdown_report(report), encoding="utf-8")
    report["report_json"] = str(report_json)
    report["report_md"] = str(report_md)
    return report


def main():
    parser = argparse.ArgumentParser(description="读取企业微信本地加密业务库的诊断和解密入口。")
    parser.add_argument("--corp-id", required=True, help="企业微信账号目录 ID，例如 <CorpId>")
    parser.add_argument("--docs-dir", default="", help="企业微信 Documents\\WXWork 根目录，留空自动识别")
    parser.add_argument("--output-dir", default="", help="输出目录，留空写入测试文件\\测试输出\\加密业务库读取")
    parser.add_argument("--db-name", action="append", default=[], help="只检查指定数据库，可重复传入")
    parser.add_argument("--key", default="", help="候选数据库密钥；支持普通口令、hex:<key> 或裸十六进制")
    parser.add_argument("--key-file", default="", help="候选密钥文件，每行一个密钥")
    parser.add_argument("--sqlcipher-exe", default="", help="外部 sqlcipher.exe 路径；也可用 SQLCIPHER_EXE 环境变量指定")
    parser.add_argument("--scan-memory-keys", action="store_true", help="只读扫描目标 WXWork.exe 内存中的十六进制 SQLCipher 候选密钥")
    parser.add_argument("--memory-key-pid", type=int, default=0, help="指定内存密钥扫描的 WXWork.exe PID，0 表示按企业账号自动识别")
    parser.add_argument("--max-memory-key-candidates", type=int, default=200, help="最多拿多少个内存候选密钥参与验证")
    parser.add_argument("--max-export-rows", type=int, default=0, help="每张表最多导出的行数，0 表示全量")
    args = parser.parse_args()

    report = run_read_encrypted_databases(
        args.corp_id,
        docs_dir=args.docs_dir or None,
        output_dir=args.output_dir or None,
        db_names=args.db_name or None,
        key=args.key,
        key_file=args.key_file,
        sqlcipher_exe=args.sqlcipher_exe,
        scan_memory_keys=args.scan_memory_keys,
        memory_key_pid=args.memory_key_pid,
        max_memory_key_candidates=args.max_memory_key_candidates,
        max_export_rows=args.max_export_rows,
    )
    print(f"报告 JSON: {report['report_json']}")
    print(f"报告 Markdown: {report['report_md']}")
    for item in report["databases"]:
        print(f"{item.get('database')}: {item.get('status')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
