import argparse
import binascii
import csv
import ctypes
import datetime as dt
import json
import os
import re
import sqlite3
import struct
import sys
from ctypes import wintypes
from pathlib import Path

import psutil
import pymem


PAGE_SIZE = 4096
SQLITE_HEADER = b"SQLite format 3\x00"
BTREE_TYPES = {0x02, 0x05, 0x0A, 0x0D}
MESSAGE_COLUMNS = [
    "message_id",
    "server_id",
    "sequence",
    "sender_id",
    "conversation_id",
    "content_type",
    "send_time",
    "flag",
    "content",
    "devinfo",
    "from_app_id",
    "msg_from_devinfo",
    "extra_content",
    "local_extra_content",
    "client_id",
    "local_extra_content_translate_info",
    "local_extra_content_time_nlp",
    "local_extra_content_approval_nlp",
]
INTEGER_MESSAGE_COLUMNS = {
    "message_id",
    "sequence",
    "content_type",
    "send_time",
    "flag",
}
TEXT_PREVIEW_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9_:/\\.@#\-\(\)（）,，。！？；、“”‘’\[\]{}+=]{4,}")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
LONG_HEX_RE = re.compile(r"[0-9a-fA-F]{32,}")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]+")
WHITESPACE_RE = re.compile(r"\s+")
CORP_ID_MESSAGE_DB_PATH_RE = re.compile(rb"(?P<corp_id>\d{8,20})[\\/]+Data[\\/]+message\.db", re.IGNORECASE)
IMAGE_CONTENT_TYPES = {14, 103}
LEGACY_OUTPUT_ROOT = None

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


MEM_COMMIT = 0x1000
PAGE_READABLE = {0x02, 0x04, 0x06, 0x20, 0x40, 0x60, 0x80}


def get_runtime_base_dir() -> Path:
    """
    统一获取当前脚本或打包后 EXE 的所在目录，避免默认输出目录继续依赖固定项目路径。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_output_root() -> Path:
    """
    优先兼容当前仓库目录；如果换到新电脑运行，则回退到当前用户文档目录或程序所在目录。
    """
    if LEGACY_OUTPUT_ROOT and LEGACY_OUTPUT_ROOT.exists():
        return LEGACY_OUTPUT_ROOT

    candidates = []
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        candidates.append(Path(userprofile) / "Documents" / "WXWorkRecovered")
    candidates.append(Path.home() / "Documents" / "WXWorkRecovered")

    seen = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        parent = candidate.parent
        if parent.exists():
            return candidate
    return get_runtime_base_dir() / "WXWorkRecovered"


def is_wow64_process(handle) -> bool:
    wow64 = ctypes.c_int()
    if not kernel32.IsWow64Process(handle, ctypes.byref(wow64)):
        raise OSError("IsWow64Process failed")
    return bool(wow64.value)


def region_iter(pm, upper_bound: int):
    address = 0
    while address < upper_bound:
        mbi = MEMORY_BASIC_INFORMATION()
        result = kernel32.VirtualQueryEx(
            pm.process_handle,
            ctypes.c_void_p(address),
            ctypes.byref(mbi),
            ctypes.sizeof(mbi),
        )
        if result == 0:
            address += 0x10000
            continue
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize or 0x10000
        if mbi.State == MEM_COMMIT and (mbi.Protect & 0xFF) in PAGE_READABLE:
            yield base, size
        address = base + size


def extract_corp_ids_from_memory_block(data: bytes):
    """
    从进程内存块里的 message.db 路径片段反推出企业微信账号目录 ID。
    """
    counts = {}
    for match in CORP_ID_MESSAGE_DB_PATH_RE.finditer(data or b""):
        corp_id = match.group("corp_id").decode("ascii", errors="ignore")
        if not corp_id:
            continue
        counts[corp_id] = counts.get(corp_id, 0) + 1
    return counts


def discover_running_wxwork_corp_ids():
    """
    在没有手动填写企业账号 ID 时，从正在运行的 WXWork.exe 内存里扫描 `数字ID\\Data\\message.db` 路径。
    """
    discovered = {}
    for proc in psutil.process_iter(["pid", "name"]):
        if proc.info["name"] != "WXWork.exe":
            continue
        pm = pymem.Pymem()
        try:
            pm.open_process_from_id(proc.info["pid"])
        except Exception:
            continue
        try:
            wow64 = is_wow64_process(pm.process_handle)
            upper_bound = 0x7FFFFFFF if wow64 else 0x7FFFFFFF0000
            per_process_counts = {}
            sample_hits = {}
            for base, size in region_iter(pm, upper_bound):
                if size < len(b"\\Data\\message.db"):
                    continue
                try:
                    data = pm.read_bytes(base, size)
                except Exception:
                    continue
                for match in CORP_ID_MESSAGE_DB_PATH_RE.finditer(data):
                    corp_id = match.group("corp_id").decode("ascii", errors="ignore")
                    if not corp_id:
                        continue
                    per_process_counts[corp_id] = per_process_counts.get(corp_id, 0) + 1
                    sample_hits.setdefault(corp_id, [])
                    if len(sample_hits[corp_id]) < 8:
                        sample_hits[corp_id].append(base + match.start("corp_id"))
            for corp_id, count in per_process_counts.items():
                item = discovered.setdefault(
                    corp_id,
                    {
                        "corp_id": corp_id,
                        "total_hits": 0,
                        "pid_count": 0,
                        "processes": [],
                    },
                )
                item["total_hits"] += count
                item["pid_count"] += 1
                item["processes"].append(
                    {
                        "pid": proc.info["pid"],
                        "wow64": wow64,
                        "count": count,
                        "sample_hits": sample_hits.get(corp_id, []),
                    }
                )
        finally:
            try:
                pm.close_process()
            except Exception:
                pass

    result = list(discovered.values())
    result.sort(key=lambda item: (item["total_hits"], item["pid_count"], item["corp_id"]), reverse=True)
    return result


def find_processes_with_target_path(corp_id: str):
    hits = []
    needle = f"{corp_id}\\Data\\message.db".encode("ascii")
    for proc in psutil.process_iter(["pid", "name"]):
        if proc.info["name"] != "WXWork.exe":
            continue
        pm = pymem.Pymem()
        try:
            pm.open_process_from_id(proc.info["pid"])
        except Exception:
            continue
        try:
            wow64 = is_wow64_process(pm.process_handle)
            upper_bound = 0x7FFFFFFF if wow64 else 0x7FFFFFFF0000
            count = 0
            sample_hits = []
            for base, size in region_iter(pm, upper_bound):
                if size < len(needle):
                    continue
                try:
                    data = pm.read_bytes(base, size)
                except Exception:
                    continue
                pos = 0
                while True:
                    idx = data.find(needle, pos)
                    if idx == -1:
                        break
                    count += 1
                    if len(sample_hits) < 8:
                        sample_hits.append(base + idx)
                    pos = idx + 1
            if count:
                hits.append(
                    {
                        "pid": proc.info["pid"],
                        "wow64": wow64,
                        "count": count,
                        "sample_hits": sample_hits,
                    }
                )
        finally:
            try:
                pm.close_process()
            except Exception:
                pass
    hits.sort(key=lambda item: item["count"], reverse=True)
    return hits


def content_type_label(content_type):
    labels = {
        0: "富文本/通知",
        2: "文本",
        14: "图片",
        29: "动画表情",
        101: "媒体下载链接",
        103: "图片",
        501: "引用/组合消息",
        561: "语音/音频",
        565: "视频",
        573: "系统提示",
        1001: "位置/门店",
        1002: "成员信息",
        1011: "文件/卡片",
        1022: "链接/卡片",
    }
    return labels.get(content_type, f"类型{content_type}")


def decode_hex_text(value):
    if value is None:
        return ""
    text = str(value)
    if len(text) >= 2 and len(text) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in text[: min(len(text), 600)]):
        try:
            return binascii.unhexlify(text).decode("utf-8", errors="ignore").replace("\x00", " ")
        except Exception:
            return text
    return text


def collect_decoded_texts(value):
    texts = []
    primary = decode_hex_text(value)
    if not primary:
        return texts
    texts.append(primary)
    seen = {primary}
    for match in LONG_HEX_RE.findall(primary):
        if len(match) % 2 != 0:
            continue
        try:
            nested = binascii.unhexlify(match).decode("utf-8", errors="ignore").replace("\x00", " ").strip()
        except Exception:
            continue
        if not nested or nested in seen:
            continue
        seen.add(nested)
        texts.append(nested)
    return texts


def extract_media_clues(row):
    texts = []
    for field_name in ("content", "extra_content", "local_extra_content"):
        texts.extend(collect_decoded_texts(row.get(field_name)))

    uuids = []
    urls = []
    seen_uuid = set()
    seen_url = set()
    for text in texts:
        for match in UUID_RE.findall(text):
            lowered = match.lower()
            if lowered not in seen_uuid:
                seen_uuid.add(lowered)
                uuids.append(lowered)
        for match in URL_RE.findall(text):
            cleaned = match.rstrip(").,，。；;")
            if cleaned not in seen_url:
                seen_url.add(cleaned)
                urls.append(cleaned)

    media_kind = ""
    content_type = row.get("content_type")
    if content_type in IMAGE_CONTENT_TYPES:
        media_kind = "图片"
    elif content_type == 29:
        media_kind = "动画表情"
    elif content_type == 101:
        media_kind = "媒体下载链接"
    elif uuids or urls:
        media_kind = "媒体线索"

    return {
        "content_type_label": content_type_label(content_type),
        "media_kind": media_kind,
        "media_uuid": " | ".join(uuids),
        "media_url": " | ".join(urls[:4]),
    }


def find_page1_candidates(pm, upper_bound: int):
    candidates = []
    seen = set()
    pattern = b"message_table"
    for base, size in region_iter(pm, upper_bound):
        if size < PAGE_SIZE:
            continue
        try:
            data = pm.read_bytes(base, size)
        except Exception:
            continue
        pos = 0
        while True:
            idx = data.find(pattern, pos)
            if idx == -1:
                break
            abs_hit = base + idx
            for back in range(0, PAGE_SIZE, 8):
                candidate_addr = abs_hit - back
                if candidate_addr in seen or candidate_addr < 0x10000:
                    continue
                try:
                    page = pm.read_bytes(candidate_addr, PAGE_SIZE)
                except Exception:
                    continue
                if page[:16] == SQLITE_HEADER and page[100] in BTREE_TYPES and pattern in page:
                    seen.add(candidate_addr)
                    candidates.append(candidate_addr)
                    break
            pos = idx + len(pattern)
    return candidates


def pointer_bytes(value: int, pointer_size: int) -> bytes:
    if pointer_size == 4:
        return struct.pack("<I", value & 0xFFFFFFFF)
    return struct.pack("<Q", value)


def u32(data: bytes, offset: int) -> int:
    return struct.unpack("<I", data[offset : offset + 4])[0]


def u64(data: bytes, offset: int) -> int:
    return struct.unpack("<Q", data[offset : offset + 8])[0]


def find_pghdr_candidates(pm, upper_bound: int, page_addr: int, pointer_size: int):
    if pointer_size == 4:
        p_data_offset = 4
        pgno_offsets = [20, 24, 28, 32, 36, 40, 44, 48]
        cache_offsets = [12, 16, 20, 24, 28, 32]
        unpack_ptr = u32
        pre_window = 32
        struct_read = 96
    else:
        p_data_offset = 8
        pgno_offsets = [40, 44, 48, 52, 56, 60, 64, 68, 72, 76, 80]
        cache_offsets = [24, 32, 40, 48]
        unpack_ptr = u64
        pre_window = 64
        struct_read = 160

    needle = pointer_bytes(page_addr, pointer_size)
    results = []
    seen = set()
    for base, size in region_iter(pm, upper_bound):
        if size < pointer_size:
            continue
        try:
            data = pm.read_bytes(base, size)
        except Exception:
            continue
        pos = 0
        while True:
            idx = data.find(needle, pos)
            if idx == -1:
                break
            loc = base + idx
            pos = idx + pointer_size
            try:
                raw = pm.read_bytes(loc - pre_window, pre_window + struct_read)
            except Exception:
                continue
            pghdr_addr = loc - p_data_offset
            for pgno_offset in pgno_offsets:
                raw_offset = pgno_offset + pre_window - p_data_offset
                if raw_offset + 4 > len(raw):
                    continue
                pgno = struct.unpack("<I", raw[raw_offset : raw_offset + 4])[0]
                if pgno != 1:
                    continue
                try:
                    struct_data = pm.read_bytes(pghdr_addr, struct_read)
                except Exception:
                    continue
                caches = []
                for cache_offset in cache_offsets:
                    ptr = unpack_ptr(struct_data, cache_offset)
                    if 0x10000 < ptr < upper_bound:
                        caches.append((cache_offset, ptr))
                key = (pghdr_addr, pgno_offset, tuple(caches))
                if key not in seen:
                    seen.add(key)
                    results.append(
                        {
                            "pghdr_addr": pghdr_addr,
                            "pgno_offset": pgno_offset,
                            "cache_candidates": caches,
                            "p_data_offset": p_data_offset,
                        }
                    )
                break
    return results


def extract_pages_for_cache(pm, upper_bound: int, pointer_size: int, p_data_offset: int, pgno_offset: int, cache_offset: int, cache_ptr: int):
    needle = pointer_bytes(cache_ptr, pointer_size)
    found = {}
    for base, size in region_iter(pm, upper_bound):
        if size < pointer_size:
            continue
        try:
            data = pm.read_bytes(base, size)
        except Exception:
            continue
        pos = 0
        while True:
            idx = data.find(needle, pos)
            if idx == -1:
                break
            pos = idx + pointer_size
            candidate_pghdr = base + idx - cache_offset
            try:
                hdr = pm.read_bytes(candidate_pghdr, max(pgno_offset + 4, p_data_offset + pointer_size))
            except Exception:
                continue
            pdata = u32(hdr, p_data_offset) if pointer_size == 4 else u64(hdr, p_data_offset)
            pgno = struct.unpack("<I", hdr[pgno_offset : pgno_offset + 4])[0]
            if not (1 <= pgno <= 100000) or not (0x10000 < pdata < upper_bound):
                continue
            try:
                page = pm.read_bytes(pdata, PAGE_SIZE)
            except Exception:
                continue
            if pgno == 1:
                ok = page[:16] == SQLITE_HEADER and page[100] in BTREE_TYPES
            else:
                ok = page[0] in BTREE_TYPES
            if ok:
                found.setdefault(pgno, page)
    return found


def choose_best_cache(pm, upper_bound: int, pointer_size: int, page1_candidates):
    best = None
    for page1_addr in page1_candidates:
        pghdrs = find_pghdr_candidates(pm, upper_bound, page1_addr, pointer_size)
        for pghdr in pghdrs:
            for cache_offset, cache_ptr in pghdr["cache_candidates"]:
                pages = extract_pages_for_cache(
                    pm,
                    upper_bound,
                    pointer_size,
                    pghdr["p_data_offset"],
                    pghdr["pgno_offset"],
                    cache_offset,
                    cache_ptr,
                )
                if 1 not in pages:
                    continue
                rootpage, _master_tree, _master_rows = find_message_table_rootpage(pages)
                # 企业微信进程里可能同时缓存多个 SQLite 库，优先选择真正包含 message_table 的缓存。
                score = (1 if rootpage is not None else 0, len(pages))
                if best is None or score > best["cache_score"]:
                    best = {
                        "page1_addr": page1_addr,
                        "pghdr_addr": pghdr["pghdr_addr"],
                        "p_data_offset": pghdr["p_data_offset"],
                        "pgno_offset": pghdr["pgno_offset"],
                        "cache_offset": cache_offset,
                        "cache_ptr": cache_ptr,
                        "pages": pages,
                        "page_count": len(pages),
                        "cache_score": score,
                        "message_table_rootpage": rootpage,
                    }
    return best


def read_varint(buf: bytes, off: int):
    value = 0
    for i in range(9):
        byte = buf[off + i]
        if i == 8:
            return (value << 8) | byte, off + 9
        value = (value << 7) | (byte & 0x7F)
        if not (byte & 0x80):
            return value, off + i + 1
    raise ValueError("bad varint")


def parse_serial(buf: bytes, off: int, serial_type: int):
    if serial_type == 0:
        return None, off
    if serial_type == 1:
        return struct.unpack(">b", buf[off : off + 1])[0], off + 1
    if serial_type == 2:
        return struct.unpack(">h", buf[off : off + 2])[0], off + 2
    if serial_type == 3:
        value = int.from_bytes(buf[off : off + 3], "big", signed=False)
        if value & 0x800000:
            value -= 1 << 24
        return value, off + 3
    if serial_type == 4:
        return struct.unpack(">i", buf[off : off + 4])[0], off + 4
    if serial_type == 5:
        value = int.from_bytes(buf[off : off + 6], "big", signed=False)
        if value & (1 << 47):
            value -= 1 << 48
        return value, off + 6
    if serial_type == 6:
        return struct.unpack(">q", buf[off : off + 8])[0], off + 8
    if serial_type == 7:
        return struct.unpack(">d", buf[off : off + 8])[0], off + 8
    if serial_type == 8:
        return 0, off
    if serial_type == 9:
        return 1, off
    if serial_type >= 12:
        size = (serial_type - 12) // 2
        blob = buf[off : off + size]
        if serial_type % 2 == 0:
            return blob.hex(), off + size
        return blob.decode("utf-8", errors="replace"), off + size
    raise ValueError(f"unsupported serial type: {serial_type}")


def parse_record(payload: bytes):
    header_size, pos = read_varint(payload, 0)
    serials = []
    while pos < header_size:
        serial_type, pos = read_varint(payload, pos)
        serials.append(serial_type)
    data_pos = header_size
    values = []
    for serial_type in serials:
        value, data_pos = parse_serial(payload, data_pos, serial_type)
        values.append(value)
    return values


def read_be_u16(buf: bytes, off: int):
    """
    从 sqlite 页里安全读取 2 字节大端整数；内存坏页或半截页不够长时返回 None。
    """
    if off < 0 or off + 2 > len(buf):
        return None
    return struct.unpack(">H", buf[off : off + 2])[0]


def read_be_u32(buf: bytes, off: int):
    """
    从 sqlite 页里安全读取 4 字节大端整数；坏 cell 指针越界时不抛异常。
    """
    if off < 0 or off + 4 > len(buf):
        return None
    return struct.unpack(">I", buf[off : off + 4])[0]


def normalize_integer_message_fields(record: dict):
    """
    把 message_table 里可能被 sqlite 以文本形式存储的数字字段转成 int，坏文本保留原值供后续过滤。
    """
    for column in INTEGER_MESSAGE_COLUMNS:
        value = record.get(column)
        if isinstance(value, bool):
            record[column] = int(value)
        elif isinstance(value, int):
            continue
        elif isinstance(value, float) and value.is_integer():
            record[column] = int(value)
        elif isinstance(value, bytes):
            text = value.decode("utf-8", errors="ignore").strip()
            if re.fullmatch(r"[+-]?\d+", text or ""):
                record[column] = int(text)
        elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip() or ""):
            record[column] = int(value.strip())
    return record


def recovered_row_sort_key(row):
    """
    给恢复行生成稳定排序键；脏页里混入字符串时按 0 兜底，避免排序阶段直接中断。
    """
    return (
        safe_int(row.get("send_time"), 0),
        safe_int(row.get("message_id"), 0),
        safe_int(row.get("_rowid"), 0),
    )


def walk_table_btree(pages: dict, root_pgno: int):
    reachable = set()
    missing = set()
    corrupt_pages = []
    skipped_cells = 0
    leaf_pages = []
    interior_pages = []
    stack = [root_pgno]
    while stack:
        pgno = stack.pop()
        if pgno in reachable:
            continue
        page = pages.get(pgno)
        if page is None:
            missing.add(pgno)
            continue
        reachable.add(pgno)
        offset = 100 if pgno == 1 else 0
        if len(page) <= offset + 5:
            corrupt_pages.append(pgno)
            continue
        page_type = page[offset]
        cell_count = read_be_u16(page, offset + 3)
        if cell_count is None:
            corrupt_pages.append(pgno)
            continue
        if page_type == 0x05:
            interior_pages.append(pgno)
            rightmost = read_be_u32(page, offset + 8)
            if rightmost is not None:
                stack.append(rightmost)
            else:
                corrupt_pages.append(pgno)
            ptr_base = offset + 12
            for idx in range(cell_count):
                ptr_offset = ptr_base + idx * 2
                ptr = read_be_u16(page, ptr_offset)
                if ptr is None:
                    skipped_cells += cell_count - idx
                    break
                left_child = read_be_u32(page, ptr)
                if left_child is None:
                    skipped_cells += 1
                    continue
                stack.append(left_child)
        elif page_type == 0x0D:
            leaf_pages.append(pgno)
        else:
            corrupt_pages.append(pgno)
    return {
        "reachable": sorted(reachable),
        "missing": sorted(missing),
        "leaf_pages": sorted(leaf_pages),
        "interior_pages": sorted(interior_pages),
        "corrupt_pages": sorted(set(corrupt_pages)),
        "skipped_cells": skipped_cells,
    }


def read_table_rows(pages: dict, leaf_pages, columns):
    rows = []
    for pgno in sorted(leaf_pages):
        page = pages.get(pgno)
        if not page or len(page) < 5:
            continue
        cell_count = read_be_u16(page, 3)
        if cell_count is None:
            continue
        for idx in range(cell_count):
            try:
                ptr = read_be_u16(page, 8 + idx * 2)
                if ptr is None:
                    break
                if ptr <= 0 or ptr >= len(page):
                    continue
                payload_size, pos = read_varint(page, ptr)
                rowid, pos = read_varint(page, pos)
                payload = page[pos : pos + payload_size]
                if len(payload) < payload_size:
                    continue
                values = parse_record(payload)
            except Exception:
                # 内存页缓存可能混入半截旧页或坏 cell，跳过坏行比整轮恢复失败更有价值。
                continue
            record = {"_rowid": rowid, "_page": pgno}
            for col_idx, column in enumerate(columns):
                record[column] = values[col_idx] if col_idx < len(values) else None
            if record.get("message_id") is None:
                record["message_id"] = rowid
            normalize_integer_message_fields(record)
            rows.append(record)
    rows.sort(key=recovered_row_sort_key)
    return rows


def looks_like_message_table_row(row):
    """
    判断从游离叶子页解析出的记录是否像 message_table 行，避免把其它表的叶子页误当聊天消息。
    """
    conversation_id = str(row.get("conversation_id") or "").strip()
    if not conversation_id:
        return False

    message_id = row.get("message_id")
    send_time = row.get("send_time")
    content_type = row.get("content_type")
    if not isinstance(message_id, int) or message_id <= 0:
        return False
    if not isinstance(send_time, int) or not (1262304000 <= send_time <= 2208988800):
        return False
    if not isinstance(content_type, int) or not (-1 <= content_type <= 20000):
        return False
    if not (":" in conversation_id or conversation_id in {"APPROVAL", "MAIL"}):
        return False
    return True


def has_valid_message_core_fields(row):
    """
    校验可导出聊天行的核心字段，避免坏缓存页解析出的表碎片进入后续导出和整理流程。
    """
    conversation_id = str(row.get("conversation_id") or "").strip()
    if not conversation_id:
        return False

    message_id = row.get("message_id")
    send_time = row.get("send_time")
    content_type = row.get("content_type")
    if not isinstance(message_id, int) or message_id <= 0:
        return False
    if not isinstance(send_time, int) or not (1262304000 <= send_time <= 2208988800):
        return False
    if not isinstance(content_type, int) or not (-1 <= content_type <= 20000):
        return False
    return True


def filter_recoverable_message_rows(rows):
    """
    只保留核心字段可信的聊天行，并返回被跳过的脏行数量，便于在日志里解释恢复结果。
    """
    valid_rows = []
    skipped = 0
    for row in rows or []:
        if has_valid_message_core_fields(row):
            valid_rows.append(row)
        else:
            skipped += 1
    return valid_rows, skipped


def read_orphan_message_table_rows(pages: dict, reachable_leaf_pages):
    """
    从当前缓存里补捞未被 B-tree 路径连上的叶子页。

    企业微信内存里经常只缓存部分索引页，如果中间页缺失，某些真实 message_table 叶子页会留在 pCache，
    但无法从 rootpage 走到。这里只读取看起来像 message_table 的游离叶子页，用来降低最近图片消息漏恢复的概率。
    """
    reachable_leaf_pages = set(reachable_leaf_pages or [])
    orphan_rows = []
    orphan_pages = []
    for pgno, page in sorted(pages.items()):
        if pgno in reachable_leaf_pages or pgno == 1:
            continue
        if not page or page[0] != 0x0D:
            continue
        rows = [row for row in read_table_rows({pgno: page}, [pgno], MESSAGE_COLUMNS) if looks_like_message_table_row(row)]
        if not rows:
            continue
        orphan_pages.append(pgno)
        for row in rows:
            row["_orphan_leaf_page"] = True
            orphan_rows.append(row)
    return orphan_rows, orphan_pages


def find_message_table_rootpage(pages: dict):
    """
    从候选缓存页里解析 sqlite_master，返回 message_table 的 rootpage。
    """
    sqlite_master_tree = walk_table_btree(pages, 1)
    sqlite_master_rows = read_table_rows(pages, sqlite_master_tree["leaf_pages"], ["type", "name", "tbl_name", "rootpage", "sql"])
    rootpage = None
    for row in sqlite_master_rows:
        if row.get("type") == "table" and row.get("name") == "message_table":
            rootpage = row.get("rootpage")
            break
    if rootpage is None:
        rootpage = heuristic_message_table_rootpage(pages)
    return rootpage, sqlite_master_tree, sqlite_master_rows


def heuristic_message_table_rootpage(pages: dict):
    """
    当前 WXWork 5.0.8 场景下，sqlite_master 的叶子页可能没进缓存，但 page1 里仍残留建表记录。
    这里只在标准 sqlite_master 解析失败时，从 `table + message_table + message_table + rootpage + CREATE TABLE` 模式里兜底提取 rootpage。
    """
    for page in pages.values():
        marker = b"tablemessage_tablemessage_table"
        search_pos = 0
        while True:
            idx = page.find(marker, search_pos)
            if idx < 0:
                break
            root_start = idx + len(marker)
            create_idx = page.find(b"CREATE TABLE message_table", root_start, root_start + 64)
            if create_idx > root_start:
                root_bytes = page[root_start:create_idx]
                if 1 <= len(root_bytes) <= 8:
                    rootpage = int.from_bytes(root_bytes, "big", signed=False)
                    if 1 <= rootpage <= 100000:
                        return rootpage
            search_pos = idx + 1
    return None


def clean_preview_text(value: str):
    """
    预览文本只用于给用户快速查看，先统一移除控制字符和多余空白。
    """
    text = str(value or "")
    text = CONTROL_CHAR_RE.sub(" ", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def strip_visible_length_prefix_artifact(text: str, preserve_sender_marker=False):
    """
    protobuf 长度字节偶尔会以 T、F、5 这类首字符显示出来；优先按长度判断，历史文本再兜底清掉单字母中文前缀。
    """
    text = str(text or "").strip()
    if preserve_sender_marker or len(text) < 2:
        return text
    first = text[0]
    rest = text[1:].lstrip()
    if first in {"A", "a"} or not rest or not CJK_RE.search(rest):
        return text
    if not (first.isascii() and (first.isalpha() or first.isdigit())):
        return text
    rest_length = len(rest.encode("utf-8", errors="ignore"))
    if abs(ord(first) - rest_length) <= 2:
        return rest
    if first.isalpha() and CJK_RE.match(rest) and len(CJK_RE.findall(rest)) >= 4:
        return rest
    return text


def normalize_preview_segment(segment: str):
    """
    把单个预览片段规整为可读文本，避免原始 CSV 和报告里继续残留控制字节。
    """
    raw_text = clean_preview_text(segment)
    preserve_sender_marker = raw_text.lstrip().startswith((",", "，", "@"))
    text = raw_text.strip(" |,:;{}[]")
    if not text:
        return ""
    text = text.replace("\\/", "/")
    text = re.sub(r"^[\]})>]+", "", text)
    text = re.sub(r"^[A-Za-z]{1,4}(?=https?://)", "", text)
    return clean_preview_text(strip_visible_length_prefix_artifact(text, preserve_sender_marker=preserve_sender_marker))


def unique_preview_hits(hits):
    """
    保持预览片段原顺序去重，避免同一段正文重复写进 readable.csv。
    """
    unique_hits = []
    seen = set()
    for hit in hits:
        normalized = normalize_preview_segment(hit)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_hits.append(normalized)
    return unique_hits


def build_preview_text(text: str):
    """
    从解码后的原始文本里提取适合展示的片段；短中文回复没有命中正则时走整体兜底。
    """
    cleaned_text = clean_preview_text(text)
    preserve_sender_marker = cleaned_text.lstrip().startswith((",", "，", "@"))
    cleaned_text = strip_visible_length_prefix_artifact(cleaned_text, preserve_sender_marker=preserve_sender_marker)
    hits = unique_preview_hits(TEXT_PREVIEW_RE.findall(cleaned_text or ""))
    if hits:
        return " | ".join(hits[:8])[:500]
    return normalize_preview_segment(cleaned_text)[:500]


def decode_preview(value):
    if value is None:
        return ""
    text = str(value)
    raw = None
    if len(text) >= 2 and len(text) % 2 == 0 and all(ch in "0123456789abcdefABCDEF" for ch in text[: min(len(text), 600)]):
        try:
            raw = binascii.unhexlify(text)
        except Exception:
            raw = None
    if raw is not None:
        decoded = raw.decode("utf-8", errors="ignore").replace("\x00", " ")
        return build_preview_text(decoded)
    return build_preview_text(text)


def recovered_row_signature_text(row):
    """
    为恢复结果生成尽量稳定的文本签名，方便把历史导出和本次导出做去重合并。
    """
    preview = decode_preview(row.get("content"))
    if preview:
        return preview
    raw = row.get("content")
    if raw is None:
        return ""
    return " ".join(str(raw).split())[:240]


def recovered_row_dedupe_key(row):
    """
    先按媒体线索和可读预览去重，避免旧导出的历史消息因为缺少 message_id 而无法和新结果合并。
    """
    conversation_id = str(row.get("conversation_id") or "").strip()
    sender_id = str(row.get("sender_id") or "").strip()
    send_time = safe_int(row.get("send_time"), 0)
    content_type = safe_int(row.get("content_type"), 0)
    media = extract_media_clues(row)
    media_uuid = str(media.get("media_uuid") or "").strip().lower()
    media_url = str(media.get("media_url") or "").strip()
    if conversation_id and (media_uuid or media_url):
        return ("media", conversation_id, send_time, sender_id, content_type, media_uuid, media_url)

    preview = recovered_row_signature_text(row)
    if conversation_id and preview:
        return ("preview", conversation_id, send_time, sender_id, content_type, preview)

    message_id = safe_int(row.get("message_id"), 0)
    if conversation_id and message_id:
        return ("message_id", conversation_id, message_id)

    server_id = str(row.get("server_id") or "").strip()
    if conversation_id and server_id:
        return ("server_id", conversation_id, server_id)

    client_id = str(row.get("client_id") or "").strip()
    if conversation_id and client_id:
        return ("client_id", conversation_id, client_id)

    sequence = row.get("sequence")
    return ("fallback", conversation_id, send_time, sender_id, content_type, preview, sequence)


def recovered_row_priority(row):
    """
    重复消息保留信息更完整的一条，避免历史行把新恢复出的字段覆盖掉。
    """
    score = 0
    for key in (
        "_rowid",
        "_page",
        "message_id",
        "server_id",
        "sequence",
        "sender_id",
        "conversation_id",
        "content",
        "devinfo",
        "from_app_id",
        "msg_from_devinfo",
        "extra_content",
        "local_extra_content",
        "client_id",
        "local_extra_content_translate_info",
        "local_extra_content_time_nlp",
        "local_extra_content_approval_nlp",
    ):
        value = row.get(key)
        if value not in (None, ""):
            score += 1
    score += len(recovered_row_signature_text(row))
    return (
        score,
        safe_int(row.get("send_time"), 0),
        safe_int(row.get("message_id"), 0),
        safe_int(row.get("_rowid"), 0),
    )


def load_existing_exported_rows(sqlite_path: Path):
    """
    读取同名历史 sqlite，给重复恢复场景做增量合并，避免消息条数越恢复越少。
    """
    if not sqlite_path.exists():
        return [], {}

    conn = None
    try:
        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        tables = {row["name"] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "message_table_partial" not in tables:
            return [], {}

        rows = []
        for row in cur.execute(
            """
            SELECT
              recovered_rowid,
              recovered_page,
              message_id,
              server_id,
              sequence,
              sender_id,
              conversation_id,
              content_type,
              send_time,
              flag,
              content,
              devinfo,
              from_app_id,
              msg_from_devinfo,
              extra_content,
              local_extra_content,
              client_id,
              local_extra_content_translate_info,
              local_extra_content_time_nlp,
              local_extra_content_approval_nlp
            FROM message_table_partial
            ORDER BY send_time, message_id, recovered_rowid
            """
        ):
            item = dict(row)
            item["_rowid"] = item.pop("recovered_rowid", None)
            item["_page"] = item.pop("recovered_page", None)
            rows.append(item)

        metadata = {}
        if "metadata" in tables:
            for item in cur.execute("SELECT key, value FROM metadata"):
                metadata[item["key"]] = item["value"]
        return rows, metadata
    except sqlite3.DatabaseError:
        return [], {}
    finally:
        if conn is not None:
            conn.close()


def merge_recovered_rows(existing_rows, new_rows):
    """
    合并历史导出和本次导出；同一条消息保留内容更完整的一份。
    """
    merged = {}
    for row in list(existing_rows) + list(new_rows):
        item = dict(row)
        key = recovered_row_dedupe_key(item)
        previous = merged.get(key)
        if previous is None or recovered_row_priority(item) >= recovered_row_priority(previous):
            merged[key] = item
    rows = list(merged.values())
    rows.sort(key=recovered_row_sort_key)
    return rows


def export_outputs(output_dir: Path, corp_id: str, target_pid: int, metadata: dict, rows):
    stem = f"wxwork_{corp_id}_partial_{target_pid}"
    json_path = output_dir / f"{stem}.json"
    sqlite_path = output_dir / f"{stem}.sqlite"
    csv_path = output_dir / f"{stem}_readable.csv"
    md_path = output_dir / f"{stem}_report.md"

    current_run_row_count = len(rows)
    previous_rows, previous_metadata = load_existing_exported_rows(sqlite_path)
    merged_rows = merge_recovered_rows(previous_rows, rows)
    merged_metadata = dict(previous_metadata)
    merged_metadata.update(metadata)
    merged_metadata["current_run_row_count"] = current_run_row_count
    merged_metadata["merged_row_count"] = len(merged_rows)
    merged_metadata["previous_export_row_count"] = len(previous_rows)

    json_doc = dict(merged_metadata)
    json_doc["rows"] = merged_rows
    json_path.write_text(json.dumps(json_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    if sqlite_path.exists():
        sqlite_path.unlink()
    conn = sqlite3.connect(sqlite_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE message_table_partial (
          recovered_rowid INTEGER,
          recovered_page INTEGER,
          message_id INTEGER,
          server_id INTEGER,
          sequence INTEGER,
          sender_id TEXT,
          conversation_id TEXT,
          content_type INTEGER,
          send_time INTEGER,
          flag INTEGER,
          content TEXT,
          devinfo TEXT,
          from_app_id TEXT,
          msg_from_devinfo TEXT,
          extra_content TEXT,
          local_extra_content TEXT,
          client_id TEXT,
          local_extra_content_translate_info TEXT,
          local_extra_content_time_nlp TEXT,
          local_extra_content_approval_nlp TEXT
        )
        """
    )
    cur.execute("CREATE INDEX idx_send_time ON message_table_partial(send_time)")
    cur.execute("CREATE INDEX idx_conversation_id ON message_table_partial(conversation_id)")
    cur.execute("CREATE INDEX idx_message_id ON message_table_partial(message_id)")
    for row in merged_rows:
        cur.execute(
            """
            INSERT INTO message_table_partial VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row.get("_rowid"),
                row.get("_page"),
                row.get("message_id"),
                row.get("server_id"),
                row.get("sequence"),
                str(row.get("sender_id")) if row.get("sender_id") is not None else None,
                str(row.get("conversation_id")) if row.get("conversation_id") is not None else None,
                row.get("content_type"),
                row.get("send_time"),
                row.get("flag"),
                str(row.get("content")) if row.get("content") is not None else None,
                str(row.get("devinfo")) if row.get("devinfo") is not None else None,
                str(row.get("from_app_id")) if row.get("from_app_id") is not None else None,
                str(row.get("msg_from_devinfo")) if row.get("msg_from_devinfo") is not None else None,
                str(row.get("extra_content")) if row.get("extra_content") is not None else None,
                str(row.get("local_extra_content")) if row.get("local_extra_content") is not None else None,
                str(row.get("client_id")) if row.get("client_id") is not None else None,
                str(row.get("local_extra_content_translate_info")) if row.get("local_extra_content_translate_info") is not None else None,
                str(row.get("local_extra_content_time_nlp")) if row.get("local_extra_content_time_nlp") is not None else None,
                str(row.get("local_extra_content_approval_nlp")) if row.get("local_extra_content_approval_nlp") is not None else None,
            ),
    )
    cur.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
    for key, value in merged_metadata.items():
        if key == "rows":
            continue
        stored = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        cur.execute("INSERT INTO metadata VALUES (?, ?)", (key, stored))
    conn.commit()
    conn.close()

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "message_id",
                "send_time",
                "send_time_iso",
                "conversation_id",
                "sender_id",
                "content_type",
                "content_type_label",
                "flag",
                "media_kind",
                "media_uuid",
                "media_url",
                "preview",
                "content_raw_head",
            ]
        )
        for row in merged_rows:
            send_time = safe_int(row.get("send_time"), 0)
            send_time_iso = format_unix_time(send_time)
            raw = row.get("content")
            media = extract_media_clues(row)
            writer.writerow(
                [
                    row.get("message_id"),
                    send_time,
                    send_time_iso,
                    row.get("conversation_id"),
                    row.get("sender_id"),
                    row.get("content_type"),
                    media["content_type_label"],
                    row.get("flag"),
                    media["media_kind"],
                    media["media_uuid"],
                    media["media_url"],
                    decode_preview(raw),
                    str(raw)[:120] if raw is not None else "",
                ]
            )

    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    conv_rows = list(
        cur.execute(
            """
            SELECT conversation_id, COUNT(*) AS cnt, MIN(send_time) AS min_ts, MAX(send_time) AS max_ts
            FROM message_table_partial
            GROUP BY conversation_id
            ORDER BY cnt DESC, max_ts DESC
            """
        )
    )
    report = []
    report.append("# Recovered WXWork Messages")
    report.append("")
    report.append(f"Corp ID: {corp_id}")
    report.append(f"Target PID: {target_pid}")
    report.append(f"Rows: {len(merged_rows)}")
    if previous_rows:
        report.append(f"Previous Rows: {len(previous_rows)}")
        report.append(f"Current Run Rows: {current_run_row_count}")
    report.append("")
    report.append("## Top Conversations")
    report.append("")
    for item in conv_rows[:20]:
        min_iso = format_unix_time(item["min_ts"])
        max_iso = format_unix_time(item["max_ts"])
        report.append(f"- {item['conversation_id']}: {item['cnt']} rows, {min_iso} -> {max_iso}")
    report.append("")
    report.append("## Recent 50")
    report.append("")
    for row in cur.execute(
        """
        SELECT *
        FROM message_table_partial
        ORDER BY send_time DESC, message_id DESC
        LIMIT 50
        """
    ):
        iso = format_unix_time(row["send_time"])
        media = extract_media_clues(dict(row))
        report.append(
            f"- [{iso}] mid={row['message_id']} conv={row['conversation_id']} sender={row['sender_id']} "
            f"type={row['content_type']}({media['content_type_label']}) :: {decode_preview(row['content'])}"
        )
    report.append("")
    report.append("## Top Conversation Details")
    report.append("")
    for item in conv_rows[:8]:
        report.append(f"### {item['conversation_id']}")
        report.append("")
        for row in cur.execute(
            """
            SELECT *
            FROM message_table_partial
            WHERE conversation_id = ?
            ORDER BY send_time DESC, message_id DESC
            LIMIT 20
            """,
            (item["conversation_id"],),
        ):
            iso = format_unix_time(row["send_time"])
            media = extract_media_clues(dict(row))
            report.append(
                f"- [{iso}] mid={row['message_id']} sender={row['sender_id']} "
                f"type={row['content_type']}({media['content_type_label']}) :: {decode_preview(row['content'])}"
            )
        report.append("")
    md_path.write_text("\n".join(report), encoding="utf-8")
    conn.close()

    return {
        "json": str(json_path),
        "sqlite": str(sqlite_path),
        "csv": str(csv_path),
        "report": str(md_path),
    }


def emit(logger, message=""):
    if logger:
        logger(message)


def prefixed_logger(logger, prefix: str):
    """
    给同一轮恢复里的不同候选进程打上前缀，避免多 PID 日志混在一起后难以判断是哪一个进程的结果。
    """
    if logger is None:
        return None

    def wrapped(message=""):
        text = str(message or "")
        if text:
            logger(f"{prefix}{text}")
        else:
            logger("")

    return wrapped


def safe_int(value, default: int = 0):
    """
    把 metadata 里的字符串数字安全转回整数，避免多进程结果比较时因为类型不一致出错。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_unix_time(value):
    """
    把恢复出的 Unix 时间戳格式化为可读时间；坏值返回空字符串，避免报表生成中断。
    """
    timestamp = safe_int(value, 0)
    if not timestamp:
        return ""
    try:
        return dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def latest_send_time_from_rows(rows):
    """
    取当前恢复结果里最新一条消息的时间，供多候选进程比较“谁更接近当前最新缓存”。
    """
    latest = 0
    for row in rows or []:
        latest = max(latest, safe_int(row.get("send_time"), 0))
    return latest


def recovery_snapshot_priority(snapshot):
    """
    未锁定 PID 时，优先选择“最新消息时间更靠后”的候选进程；再按条数、页数和路径命中数兜底。
    """
    metadata = snapshot.get("metadata", {}) or {}
    return (
        latest_send_time_from_rows(snapshot.get("rows", [])),
        len(snapshot.get("rows", [])),
        safe_int(metadata.get("page_count"), 0),
        safe_int(snapshot.get("path_hit_count"), -1),
    )


def recover_process_snapshot(corp_id: str, target_pid: int, logger=None):
    """
    从单个 WXWork 进程里提取一份可用于导出的内存 sqlite 快照。
    """
    current_logger = prefixed_logger(logger, f"[PID {target_pid}] ")
    pm = pymem.Pymem()
    pm.open_process_from_id(target_pid)
    try:
        wow64 = is_wow64_process(pm.process_handle)
        pointer_size = 4 if wow64 else 8
        upper_bound = 0x7FFFFFFF if pointer_size == 4 else 0x7FFFFFFF0000
        emit(current_logger, f"Pointer size: {pointer_size} bytes")

        page1_candidates = find_page1_candidates(pm, upper_bound)
        if not page1_candidates:
            emit(current_logger, "No page1 candidates found. Open a target chat and scroll more history, then retry.")
            return {
                "ok": False,
                "exit_code": 2,
                "corp_id": corp_id,
                "target_pid": target_pid,
                "rows": [],
                "metadata": {},
            }
        emit(current_logger, f"Page1 candidates: {[hex(item) for item in page1_candidates[:20]]}")

        best = choose_best_cache(pm, upper_bound, pointer_size, page1_candidates)
        if not best:
            emit(current_logger, "Failed to resolve a usable PCache from the page1 candidates.")
            return {
                "ok": False,
                "exit_code": 3,
                "corp_id": corp_id,
                "target_pid": target_pid,
                "rows": [],
                "metadata": {},
            }

        emit(current_logger, f"Chosen page1: {hex(best['page1_addr'])}")
        emit(current_logger, f"Chosen PgHdr: {hex(best['pghdr_addr'])}")
        emit(current_logger, f"Chosen pCache: {hex(best['cache_ptr'])}")
        emit(current_logger, f"Cached pages: {best['page_count']}")

        pages = best["pages"]
        rootpage, sqlite_master_tree, _sqlite_master_rows = find_message_table_rootpage(pages)
        if rootpage is None:
            emit(current_logger, "message_table rootpage not found in sqlite_master.")
            return {
                "ok": False,
                "exit_code": 4,
                "corp_id": corp_id,
                "target_pid": target_pid,
                "rows": [],
                "metadata": {},
            }
        emit(current_logger, f"message_table rootpage: {rootpage}")

        table_tree = walk_table_btree(pages, rootpage)
        raw_rows = read_table_rows(pages, table_tree["leaf_pages"], MESSAGE_COLUMNS)
        rows, skipped_invalid_rows = filter_recoverable_message_rows(raw_rows)
        if skipped_invalid_rows:
            emit(current_logger, f"Skipped invalid message rows: {skipped_invalid_rows}")
        orphan_rows, orphan_pages = read_orphan_message_table_rows(pages, table_tree["leaf_pages"])
        if orphan_rows:
            rows = merge_recovered_rows(rows, orphan_rows)
            emit(current_logger, f"Recovered orphan leaf rows: {len(orphan_rows)} from pages {orphan_pages[:20]}")
        emit(current_logger, f"Recovered rows: {len(rows)}")
        first_time = format_unix_time(rows[0].get("send_time")) if rows else ""
        last_time = format_unix_time(rows[-1].get("send_time")) if rows else ""
        if first_time and last_time:
            emit(
                current_logger,
                "Time range: "
                f"{first_time} -> "
                f"{last_time}",
            )

        metadata = {
            "corp_id": corp_id,
            "pid": str(target_pid),
            "pointer_size": str(pointer_size),
            "page1_addr": hex(best["page1_addr"]),
            "pghdr_addr": hex(best["pghdr_addr"]),
            "cache_ptr": hex(best["cache_ptr"]),
            "page_count": str(best["page_count"]),
            "sqlite_master_reachable_pages": sqlite_master_tree["reachable"],
            "sqlite_master_missing_pages": sqlite_master_tree["missing"],
            "message_table_rootpage": str(rootpage),
            "message_table_reachable_pages": table_tree["reachable"],
            "message_table_missing_pages": table_tree["missing"],
            "message_table_leaf_pages": table_tree["leaf_pages"],
            "message_table_interior_pages": table_tree["interior_pages"],
            "message_table_orphan_leaf_pages": orphan_pages,
            "message_table_orphan_row_count": str(len(orphan_rows)),
            "message_table_invalid_row_count": str(skipped_invalid_rows),
            "row_count": str(len(rows)),
        }
        return {
            "ok": True,
            "exit_code": 0,
            "corp_id": corp_id,
            "target_pid": target_pid,
            "rows": rows,
            "metadata": metadata,
        }
    finally:
        try:
            pm.close_process()
        except Exception:
            pass


def run_recovery(corp_id: str, output_dir, pid: int = 0, logger=print):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    process_hits = [{"pid": pid, "wow64": True, "count": -1, "sample_hits": []}] if pid else find_processes_with_target_path(corp_id)
    if not process_hits:
        emit(logger, "No WXWork.exe process contains the target corp_id path in memory.")
        emit(logger, "Open the target account in WXWork and retry.")
        return {
            "ok": False,
            "exit_code": 1,
            "corp_id": corp_id,
            "target_pid": None,
            "outputs": {},
        }

    if pid:
        emit(logger, f"Selected PID: {pid}")
    else:
        emit(logger, f"Detected candidate WXWork.exe processes: {len(process_hits)}")
        emit(logger, "PID 未锁定，本轮会依次尝试所有候选进程，并优先采用最新消息时间更靠后的结果。")

    successful_snapshots = []
    failed_snapshots = []
    for index, target in enumerate(process_hits, start=1):
        target_pid = target["pid"]
        if target.get("count", -1) >= 0:
            emit(logger, f"Trying candidate PID {target_pid} ({index}/{len(process_hits)})")
            emit(logger, f"Path hits: {target['count']}")
            emit(logger, f"Sample hits: {[hex(item) for item in target.get('sample_hits', [])]}")
        else:
            emit(logger, f"Trying specified PID {target_pid}")

        snapshot = recover_process_snapshot(corp_id, target_pid, logger=logger)
        snapshot["path_hit_count"] = target.get("count", -1)
        snapshot["candidate_index"] = index
        if snapshot.get("ok"):
            successful_snapshots.append(snapshot)
            latest_send_time = latest_send_time_from_rows(snapshot.get("rows", []))
            latest_iso = format_unix_time(latest_send_time) or "未知"
            emit(
                logger,
                f"Candidate PID {target_pid} recovered {len(snapshot.get('rows', []))} rows, latest message time: {latest_iso}",
            )
        else:
            failed_snapshots.append(snapshot)
            emit(logger, f"Candidate PID {target_pid} failed with exit code {snapshot.get('exit_code')}")

        if pid:
            break

    if not successful_snapshots:
        target_pid = process_hits[0]["pid"] if process_hits else None
        exit_code = failed_snapshots[0]["exit_code"] if failed_snapshots else 1
        return {
            "ok": False,
            "exit_code": exit_code,
            "corp_id": corp_id,
            "target_pid": target_pid,
            "outputs": {},
        }

    primary_snapshot = max(successful_snapshots, key=recovery_snapshot_priority)
    target_pid = primary_snapshot["target_pid"]
    merged_rows = []
    for snapshot in successful_snapshots:
        merged_rows = merge_recovered_rows(merged_rows, snapshot.get("rows", []))

    metadata = dict(primary_snapshot.get("metadata", {}) or {})
    metadata["pid"] = str(target_pid)
    metadata["primary_pid"] = str(target_pid)
    metadata["candidate_attempt_count"] = str(len(process_hits))
    metadata["successful_candidate_count"] = str(len(successful_snapshots))
    metadata["merged_candidate_pids"] = [str(item["target_pid"]) for item in successful_snapshots]
    metadata["merged_candidate_row_counts"] = {
        str(item["target_pid"]): len(item.get("rows", [])) for item in successful_snapshots
    }
    metadata["merged_candidate_latest_send_times"] = {
        str(item["target_pid"]): latest_send_time_from_rows(item.get("rows", [])) for item in successful_snapshots
    }
    if failed_snapshots:
        metadata["failed_candidate_pids"] = [str(item["target_pid"]) for item in failed_snapshots]

    if len(successful_snapshots) > 1:
        emit(
            logger,
            f"Successfully recovered {len(successful_snapshots)} candidate processes; exporting merged rows via primary PID {target_pid}.",
        )
        emit(logger, f"Merged current-run rows: {len(merged_rows)}")

    outputs = export_outputs(output_dir, corp_id, target_pid, metadata, merged_rows)
    emit(logger, "Outputs:")
    for key, value in outputs.items():
        emit(logger, f"  {key}: {value}")
    emit(logger, "")
    emit(logger, "Fast tips:")
    emit(logger, "  1. Keep WXWork open.")
    emit(logger, "  2. Open the target chat and scroll more history before rerunning.")
    emit(logger, "  3. This method recovers cached pages only, so the result is partial by design.")
    return {
        "ok": True,
        "exit_code": 0,
        "corp_id": corp_id,
        "target_pid": target_pid,
        "outputs": outputs,
        "metadata": metadata,
    }


def main():
    default_output_dir = default_output_root()
    parser = argparse.ArgumentParser(description="Recover partial WXWork message_table rows from in-memory SQLite page cache.")
    parser.add_argument("--corp-id", required=True, help="Target corp/account folder id under Documents\\WXWork, e.g. <CorpId>")
    parser.add_argument("--output-dir", default=str(default_output_dir), help="Directory for generated artifacts")
    parser.add_argument("--pid", type=int, default=0, help="Optional WXWork.exe PID to force")
    args = parser.parse_args()
    result = run_recovery(args.corp_id, args.output_dir, pid=args.pid, logger=print)
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
