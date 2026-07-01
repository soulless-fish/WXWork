import argparse
import binascii
import csv
import datetime as dt
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path


# 缓存媒体索引，避免每次切换会话都重复扫描整棵图片目录。
_MEDIA_CACHE_INDEX_CACHE = {}
_RELATED_RAW_SQLITE_CACHE = {}
_RAW_SQLITE_METADATA_CACHE = {}
_RAW_SQLITE_CONVERSATION_ID_CACHE = {}
_RAW_SQLITE_GROUP_TITLE_CACHE = {}
LEGACY_OUTPUT_ROOT = None
RAW_SQLITE_SKIP_DIR_NAMES = {"dist", "build", "build-gui", "__pycache__"}
RAW_MESSAGE_TEXT_FIELDS = (
    "content",
    "extra_content",
    "local_extra_content",
    "devinfo",
    "msg_from_devinfo",
    "local_extra_content_translate_info",
    "local_extra_content_time_nlp",
    "local_extra_content_approval_nlp",
)
STRONG_DECODE_ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be", "gb18030")


TEXT_PREVIEW_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9_:/\\.@#\-\(\)（）,，。！？；、“”‘’\[\]{}+=]{4,}")
INTRO_NAME_RE = re.compile(r"(?:大家好[！!。 ]*我是|我是)([\u4e00-\u9fffA-Za-z0-9_\-·()（）]{2,30})[,，！!。 ]{0,4}我来了")
JOIN_APPLY_NAME_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9_\-·()（）]{2,30})申请加入")
PIPE_PREFIX_RE = re.compile(r"^([\u4e00-\u9fffA-Za-z0-9_\-·()（）]{2,30})\s*\|")
SAFE_NAME_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9_\-·()（）]{2,30}$")
SENDER_ALIAS_PREFIX_RE = re.compile(r"^[,，@]?([\u4e00-\u9fffA-Za-z0-9_.\-·()（）]{2,40})\s*\|")
SENDER_ALIAS_INLINE_RE = re.compile(r"^[,，@]?([\u4e00-\u9fffA-Za-z0-9_.\-·()（）]{2,40})$")
PURE_NUMBERISH_RE = re.compile(r"^[0-9][0-9.]{0,15}$")
PHONE_NUMBER_RE = re.compile(r"1\d{10}")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
LONG_HEX_RE = re.compile(r"[0-9a-fA-F]{32,}")
ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_:.@#\-]{6,}")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]+")
COLOR_VALUE_RE = re.compile(r"^#[0-9A-Fa-f]{6,8}$")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".m4a", ".amr", ".silk", ".opus"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
IMAGE_CONTENT_TYPES = {14, 103}
MULTI_MEDIA_CONTENT_TYPES = {501, 1011}
QUOTE_REPLY_FLAG = 512
MEDIA_SUFFIX_PATTERN = "jpg|jpeg|png|gif|webp|bmp|mp4|mov|avi|mkv|wmv|m4v|webm|mp3|wav|aac|m4a|amr|silk|opus"
MEDIA_FILE_PATH_RE = re.compile(
    rf"([A-Za-z]:(?:\\|/(?!/))[^\r\n<>:\"|?*]+?\.(?:{MEDIA_SUFFIX_PATTERN}))",
    re.IGNORECASE,
)
EMOTION_MEDIA_PATH_RE = re.compile(
    r"([A-Za-z]:(?:\\|/(?!/))[^\r\n<>:\"|?*]*?(?:\\|/)Emotion(?:\\|/)\d{4}-\d{2}(?:\\|/)[0-9A-Za-z_-]{8,})",
    re.IGNORECASE,
)
MEDIA_FILE_NAME_RE = re.compile(
    rf"(?<![A-Za-z0-9_./\\-])([^\r\n<>:\"/\\\\|?*]{{1,180}}\.(?:{MEDIA_SUFFIX_PATTERN}))(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
MEDIA_BASENAME_RE = re.compile(
    rf"([\u4e00-\u9fffA-Za-z0-9_@#\-\(\)（）]{{1,180}}\.(?:{MEDIA_SUFFIX_PATTERN}))",
    re.IGNORECASE,
)
WINDOWS_ILLEGAL_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE_RE = re.compile(r"\s+")
QUOTED_TITLE_RE = re.compile(r'[“"]([^”"]{2,40})[”"]')
QUOTE_REPLY_RE = re.compile(r'[“"](?P<quoted>[^”"]{2,1200})[”"]\s*-{4,}\s*(?P<reply>.+)', re.S)
QUOTE_SENDER_SPLIT_RE = re.compile(r"^(.{1,120}?)[：:]\s*(.+)$", re.S)
GROUP_LIKE_TITLE_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9_\-·()（）]{2,40}(?:群|俱乐部|部|门店))")
SAFE_TITLE_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9_+\-·()（）【】《》、&·\s]{2,40}$")
STORE_GROUP_TITLE_RE = re.compile(r"(?:[A-Z][+-]?)?极修匠[^\r\n]{0,48}")
LOCAL_INDEX_REPEAT_RE = re.compile(r"((?:[\u4e00-\u9fffA-Za-z0-9()（）+\-Vv心]{2,40}?))(?:\1){1,}")
LOCAL_STORE_TITLE_CAPTURE_RE = re.compile(
    r"((?:[A-Z][+\-]?)?极修匠[A-Za-z0-9+\-·()（）\u4e00-\u9fff]{0,40}?"
    r"(?:街道店|大厦店|区店|门店|店|街|广场|城)[)）]?)"
)
LOCAL_GENERIC_TITLE_CAPTURE_RE = re.compile(
    r"((?:[A-Z][+\-]?)?极修匠[\u4e00-\u9fffA-Za-z0-9+\-·()（）]{2,28})"
)
NUMERIC_ID_RE = re.compile(r"\b\d{8,18}\b")
ORGANIZED_HISTORY_SUMMARY_FILE_NAMES = ("会话摘要.json", "summary.json")
ORGANIZED_HISTORY_TIMELINE_FILE_NAMES = ("时间线.csv",)
ORGANIZED_HISTORY_CHAT_RECORD_PATTERN = "*_聊天记录.csv"

GENERIC_NAME_BLOCKLIST = {
    "大家好",
    "各位同事",
    "各位老板",
    "各位门店老板",
    "各位门店伙伴",
    "收到",
    "确认",
    "已登记",
    "已解决",
    "务必参加",
    "务必准时参加",
    "上架了的",
    "打开",
    "浏览",
    "通知",
    "老板",
    "门店",
    "图片",
    "截图",
    "链接",
    "二维码",
    "极修匠",
    "企业微信",
}

GENERIC_NAME_PARTS = [
    "各位",
    "门店",
    "老板",
    "同事",
    "通知",
    "链接",
    "二维码",
    "会议",
    "企业",
    "视频号",
    "视频",
    "截图",
]

TITLE_BLOCKLIST = {
    "header",
    "title",
    "sub_title",
    "main_title",
    "card_title",
    "detail_list",
    "data_list",
    "button_list",
    "button_text",
    "logo_image",
    "icon",
    "image",
    "desc",
    "aspect_ratio",
    "color",
    "is_bold",
    "title_span",
    "sub_title_span",
    "info_list",
    "action",
    "utf8",
    "none",
}

EXTERNAL_GROUP_FALSE_TITLE_PARTS = {
    "发群",
    "发到群",
    "群上",
    "群里",
    "群内",
    "群主",
    "在群",
    "回群",
    "拉群",
    "进群",
    "退群",
    "咨询群",
    "一对一群",
    "哪个群",
    "参照群",
    "截图",
    "核销",
    "上翻",
    "回传",
    "传至群",
    "请联系",
}

EXTERNAL_GROUP_STRONG_HINTS = (
    "群",
    "俱乐部",
    "小组",
    "项目组",
    "战队",
    "车友会",
)

EXTERNAL_GROUP_MEDIUM_HINTS = (
    "运营部",
    "客服部",
    "财务部",
    "市场部",
    "人事部",
    "行政部",
    "直播部",
    "商务部",
    "销售部",
    "技术部",
    "售后部",
)

EXTERNAL_GROUP_TITLE_HINT_PARTS = (
    "极修匠",
    "客服",
    "售后",
    "运营",
    "商务",
    "流程",
    "测试",
    "项目",
    "门店",
    "支持",
    "案例",
    "接待",
)

EXTERNAL_GROUP_MESSAGE_PREFIXES = (
    "把",
    "怎么",
    "哪个",
    "为什么",
    "请问",
    "您好",
    "你好",
    "有人",
    "这里",
    "这个",
    "那个",
    "是否",
    "麻烦",
    "加入了",
    "能不能",
    "都是",
)

EXTERNAL_GROUP_MESSAGE_PARTS = {
    "加入了群",
    "加入群聊",
    "发到群",
    "哪个群",
    "参照群",
    "邀请其企业微信身份",
    "有一些",
    "不是很",
    "明显",
    "门头",
    "都是",
    "如有疑问",
    "便宜一点点",
    "可以吗",
    "看一下",
    "看下",
    "回复一下",
    "回复下",
    "截图",
    "核销",
}

EXTERNAL_GROUP_NOTICE_SEGMENT_PREFIXES = (
    "重要通知",
    "紧急通知",
    "正式启动",
    "各位",
    "为帮助",
    "主讲导师",
    "另外提醒",
    "请自行",
    "请及时",
    "阅读完毕",
    "务必",
    "全员必看",
)

EXTERNAL_GROUP_NOTICE_PREVIEW_PARTS = {
    "正式启动",
    "主讲导师",
    "负责人",
    "资深主播",
    "另外提醒",
    "请自行",
    "阅读完毕",
    "务必",
    "二维码",
    "直播间",
    "视频号",
    "避免客诉",
    "确认一下",
}

SENDER_ALIAS_BLOCKLIST_PARTS = {
    "群",
    "通知",
    "直播",
    "培训",
    "巡讲",
    "课程",
    "年会",
    "提醒",
    "统计",
    "助手",
    "邮件",
    "审批",
    "问卷",
    "负责人:您好",
    "家人们",
    ".pdf",
    ".zip",
    ".doc",
    ".xls",
}

SENDER_ALIAS_NEGATIVE_PARTS = {
    "发票",
    "开票",
    "链接",
    "教程",
    "二维码",
    "会议",
    "考核",
    "冲锋日",
    "注意事项",
    "视频号",
    "截图",
    "复制打开",
    "打开抖音",
    "各门店",
    "门店老板",
    "公司名称",
    "服务费",
    "现代服务",
    "开年启市",
    "营业执照",
    "银行卡",
    "手机号",
    "验证码",
    "活动",
    "拍一下",
    "看一下",
    "看看",
}

SENDER_ALIAS_NEGATIVE_PREFIXES = (
    "您",
    "各",
    "这边",
    "这里",
    "这个",
    "那个",
    "如何",
    "关于",
)

SENDER_ALIAS_NEGATIVE_SUFFIXES = ("吗", "呢", "呀", "哦", "哈", "吧", "了")

STORE_GROUP_BLOCKLIST_PARTS = {
    "重要通知",
    "线上年会",
    "培训",
    "巡讲",
    "运营部",
    "官方商务",
    "商务小",
    "客服一对一",
    "视频号直播",
    "直播间",
    "抖音",
    "问卷",
}

JOIN_LIKE_PREVIEWS = {
    "加入了群聊",
    "加入了群",
}

SENDER_ROLE_LABELS = {
    "staff": "商务/企业成员",
    "external_contact": "外部联系人",
    "unknown": "未知",
}

STAFF_NAME_HINT_PARTS = (
    "商务",
    "官方",
    "客服",
    "运营",
    "售后",
    "售中",
)


def get_runtime_base_dir() -> Path:
    """
    统一获取当前脚本或打包后 EXE 的所在目录，避免默认目录继续依赖固定盘符。
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


def default_source_dir() -> Path:
    """
    默认从恢复结果所在根目录寻找最新 sqlite，供 CLI 或新电脑上的独立整理使用。
    """
    return default_output_root()


def default_organized_output_dir(source_dir=None) -> Path:
    """
    默认把整理结果放在恢复根目录下的 organized_external_groups，保持 GUI 和 CLI 目录一致。
    """
    base_dir = Path(source_dir) if source_dir else default_source_dir()
    return base_dir / "organized_external_groups"


def decode_preview(value):
    if value is None:
        return ""
    text = str(value)
    raw = None
    if len(text) >= 2 and len(text) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in text[: min(len(text), 600)]):
        try:
            raw = binascii.unhexlify(text)
        except Exception:
            raw = None
    if raw is not None:
        decoded = raw.decode("utf-8", errors="ignore").replace("\x00", " ")
        preview = build_preview_text(decoded)
        if preview:
            return preview[:500]
        fallback = clean_preview_text(decoded)
        return fallback[:500]
    preview = build_preview_text(text)
    if preview:
        return preview[:500]
    return clean_preview_text(text)[:500]


def latest_sqlite_in_dir(directory: Path):
    candidates = []
    for pattern in ("wxwork_*_partial_*.sqlite", "message_table_partial_*.sqlite"):
        candidates.extend(directory.glob(pattern))
    if not candidates:
        raise FileNotFoundError(f"没有在 {directory} 找到恢复结果 sqlite 文件。")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def corp_id_from_sqlite_name(sqlite_path: Path):
    """
    从标准恢复 sqlite 文件名里提取企业账号 ID，供跨 PID 合并原始恢复结果时使用。
    """
    match = re.match(r"wxwork_(.+?)_partial_", sqlite_path.name)
    return match.group(1) if match else ""


def raw_sqlite_patterns():
    """
    项目历史上既有标准命名的恢复库，也有旧版 `message_table_partial_*.sqlite`。
    这里统一维护可识别的原始恢复库命名模式。
    """
    return ("wxwork_*_partial_*.sqlite", "message_table_partial_*.sqlite")


def raw_sqlite_search_root(source_dir):
    return Path(source_dir) if source_dir else default_source_dir()


def should_skip_raw_sqlite_candidate(path: Path, search_root: Path):
    """
    过滤掉打包目录等明显不应参与原始恢复合并的位置，避免无关产物混入。
    """
    try:
        relative_parts = path.resolve().relative_to(search_root.resolve()).parts[:-1]
    except Exception:
        relative_parts = path.parts[:-1]
    return any(part in RAW_SQLITE_SKIP_DIR_NAMES for part in relative_parts)


def load_metadata_from_sqlite(sqlite_path: Path):
    """
    读取单个恢复 sqlite 的 metadata，并做缓存，避免跨批次合并时重复打开同一文件。
    """
    resolved = Path(sqlite_path).resolve()
    cache_key = str(resolved).lower()
    if cache_key in _RAW_SQLITE_METADATA_CACHE:
        return dict(_RAW_SQLITE_METADATA_CACHE[cache_key])

    metadata = {}
    conn = None
    try:
        conn = sqlite3.connect(resolved)
        cur = conn.cursor()
        metadata = load_metadata_from_cursor(cur)
    except Exception:
        metadata = {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    _RAW_SQLITE_METADATA_CACHE[cache_key] = dict(metadata)
    return dict(metadata)


def load_sqlite_conversation_ids(sqlite_path: Path):
    """
    读取单个恢复 sqlite 内出现过的会话 ID 集合，供旧格式恢复库做相关性判断。
    """
    resolved = Path(sqlite_path).resolve()
    cache_key = str(resolved).lower()
    if cache_key in _RAW_SQLITE_CONVERSATION_ID_CACHE:
        return set(_RAW_SQLITE_CONVERSATION_ID_CACHE[cache_key])

    conversation_ids = set()
    conn = None
    try:
        conn = sqlite3.connect(resolved)
        cur = conn.cursor()
        for row in cur.execute("SELECT DISTINCT conversation_id FROM message_table_partial"):
            current_id = str((row[0] if row else "") or "").strip()
            if current_id:
                conversation_ids.add(current_id)
    except Exception:
        conversation_ids = set()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    _RAW_SQLITE_CONVERSATION_ID_CACHE[cache_key] = tuple(sorted(conversation_ids))
    return set(conversation_ids)


def related_conversation_ids_for_matching(conversation_ids):
    """
    做跨文件相关性判断时，只保留更能代表真实聊天会话的 ID，避免 `APPROVAL`、`MAIL` 这类公共项造成误判。
    """
    selected = set()
    for conversation_id in conversation_ids:
        current_id = str(conversation_id or "").strip()
        if current_id.startswith(("R:", "S:")):
            selected.add(current_id)
    return selected


def raw_sqlite_matches_primary(primary_sqlite: Path, candidate: Path, corp_id: str, primary_conversation_ids=None):
    """
    判断一个候选恢复 sqlite 是否和当前主恢复库属于同一批真实聊天来源。
    优先按 `corp_id` 判断；旧格式文件没有 `corp_id` 时，再退回到会话 ID 重叠判断。
    """
    candidate_metadata = load_metadata_from_sqlite(candidate)
    candidate_corp_id = str(candidate_metadata.get("corp_id") or corp_id_from_sqlite_name(candidate) or "").strip()
    if candidate_corp_id:
        return candidate_corp_id == corp_id

    current_primary_ids = primary_conversation_ids
    if current_primary_ids is None:
        current_primary_ids = related_conversation_ids_for_matching(load_sqlite_conversation_ids(primary_sqlite))
    current_candidate_ids = related_conversation_ids_for_matching(load_sqlite_conversation_ids(candidate))
    if not current_primary_ids or not current_candidate_ids:
        return False
    return bool(current_primary_ids & current_candidate_ids)


def preferred_raw_group_titles_map(sqlite_paths):
    """
    从标准命名的原始恢复 sqlite 中提取外部群标题。
    这些标题直接来自原始恢复内容，可信度高于旧整理目录里的历史标题。
    """
    resolved_paths = [Path(path).resolve() for path in (sqlite_paths or []) if Path(path).name.startswith("wxwork_")]
    cache_key = tuple(sorted(str(path).lower() for path in resolved_paths))
    if cache_key in _RAW_SQLITE_GROUP_TITLE_CACHE:
        return dict(_RAW_SQLITE_GROUP_TITLE_CACHE[cache_key])

    title_map = {}
    for sqlite_path in resolved_paths:
        try:
            rows, _metadata = load_rows_from_single_sqlite(sqlite_path)
        except Exception:
            continue
        grouped = defaultdict(list)
        for row in rows:
            conversation_id = str(row.get("conversation_id") or "").strip()
            if conversation_id.startswith("R:"):
                grouped[conversation_id].append(row)

        for conversation_id, conv_rows in grouped.items():
            title_scores, _title_evidence = collect_conversation_title_candidates(conv_rows, "external_group")
            ranked_titles = sorted(title_scores.items(), key=lambda item: (item[1],) + title_priority(item[0]), reverse=True)
            for title, score in ranked_titles:
                if not looks_like_store_group_title(title):
                    continue
                if not external_group_title_confident(title, score):
                    continue
                candidate_rank = (score,) + title_priority(title)
                previous = title_map.get(conversation_id)
                if previous is None or candidate_rank > previous[1]:
                    title_map[conversation_id] = (title, candidate_rank)
                break

    normalized_map = {conversation_id: item[0] for conversation_id, item in title_map.items() if item and item[0]}
    _RAW_SQLITE_GROUP_TITLE_CACHE[cache_key] = dict(normalized_map)
    return dict(normalized_map)


def discover_related_raw_sqlites(primary_sqlite: Path, source_dir=None, metadata=None):
    """
    递归寻找同一企业账号的历史原始恢复 sqlite。
    这样即使企业微信重启导致 PID 变了，也能把不同批次的原始恢复结果继续拼回同一个会话里。
    """
    primary_sqlite = Path(primary_sqlite)
    corp_id = str((metadata or {}).get("corp_id") or corp_id_from_sqlite_name(primary_sqlite) or "").strip()
    search_root = raw_sqlite_search_root(source_dir)
    if not corp_id or not search_root.exists():
        return [primary_sqlite]

    cache_key = (str(search_root.resolve()).lower(), str(primary_sqlite.resolve()).lower(), corp_id)
    if cache_key in _RELATED_RAW_SQLITE_CACHE:
        return list(_RELATED_RAW_SQLITE_CACHE[cache_key])

    candidates = []
    seen = set()
    primary_conversation_ids = related_conversation_ids_for_matching(load_sqlite_conversation_ids(primary_sqlite))
    for pattern in raw_sqlite_patterns():
        for path in search_root.rglob(pattern):
            if should_skip_raw_sqlite_candidate(path, search_root):
                continue
            resolved = path.resolve()
            key = str(resolved).lower()
            if key in seen:
                continue
            if not raw_sqlite_matches_primary(
                primary_sqlite,
                resolved,
                corp_id,
                primary_conversation_ids=primary_conversation_ids,
            ):
                continue
            seen.add(key)
            candidates.append(resolved)

    primary_resolved = primary_sqlite.resolve()
    if str(primary_resolved).lower() not in seen:
        candidates.append(primary_resolved)

    candidates.sort(key=lambda item: (item.stat().st_mtime, item.name))
    _RELATED_RAW_SQLITE_CACHE[cache_key] = tuple(candidates)
    return list(candidates)


def slugify(text: str):
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", text).strip("_") or "conversation"


def safe_path_component(text: str, fallback="未命名", max_len=72):
    text = str(text or "")
    text = text.replace("\r", " ").replace("\n", " ")
    text = WINDOWS_ILLEGAL_FILENAME_RE.sub(" ", text)
    text = WHITESPACE_RE.sub(" ", text).strip(" .")
    if len(text) > max_len:
        text = text[:max_len].rstrip(" .")
    return text or fallback


def conversation_chat_record_filename(display_name: str, suffix: str = ".csv"):
    """
    按用户可读的会话名称生成聊天记录文件名，方便商务人员直接从文件名识别内容。
    """
    safe_name = safe_path_component(display_name, fallback="会话", max_len=56)
    return f"{safe_name}_聊天记录{suffix}"


def conversation_tail(conversation_id: str):
    conversation_id = str(conversation_id or "")
    if ":" in conversation_id:
        return conversation_id.split(":", 1)[1]
    return conversation_id


def preview_segments(preview: str):
    if not preview:
        return []
    parts = []
    seen = set()
    for segment in re.split(r"\s*\|\s*", str(preview)):
        segment = segment.strip()
        if not segment:
            continue
        if segment not in seen:
            seen.add(segment)
            parts.append(segment)
    return parts


def clean_title_candidate(candidate: str):
    candidate = WHITESPACE_RE.sub(" ", str(candidate or "")).strip(" .,:：;-")
    candidate = candidate.replace(" | ", " ").replace("|", " ")
    candidate = WHITESPACE_RE.sub(" ", candidate).strip(" .,:：;-")
    return candidate


def looks_like_hex_noise_title(candidate: str):
    candidate = clean_title_candidate(candidate)
    if not candidate or CJK_RE.search(candidate):
        return False
    compact = re.sub(r"[^A-Za-z0-9]", "", candidate)
    if len(compact) < 24:
        return False
    hex_chars = sum(ch in "0123456789abcdefABCDEF" for ch in compact)
    non_hex_chars = len(compact) - hex_chars
    if hex_chars >= 24 and non_hex_chars <= 2 and hex_chars >= int(len(compact) * 0.9):
        return True
    return False


def looks_like_store_group_title(candidate: str):
    candidate = clean_title_candidate(candidate)
    if not candidate or "极修匠" not in candidate:
        return False
    if len(candidate) < 6 or len(candidate) > 48:
        return False
    if candidate[:1].isdigit():
        return False
    if looks_like_external_group_chat_sentence(candidate):
        return False
    if candidate.count("(") + candidate.count("（") != candidate.count(")") + candidate.count("）"):
        return False
    if any(part in candidate for part in STORE_GROUP_BLOCKLIST_PARTS):
        return False
    if any(part in candidate for part in EXTERNAL_GROUP_FALSE_TITLE_PARTS):
        return False
    store_hints = ("店", "门店", "街道", "大厦", "广场", "城", "区店")
    if any(hint in candidate for hint in store_hints):
        return True
    return False


def extract_store_like_titles(preview: str):
    if not preview:
        return []

    raw_segments = [segment.strip() for segment in re.split(r"\s*\|\s*", str(preview)) if segment.strip()]
    candidates = []
    seen = set()

    for idx, segment in enumerate(raw_segments):
        possible = [segment]
        if "极修匠" in segment and idx + 1 < len(raw_segments):
            combined = clean_title_candidate(segment + " " + raw_segments[idx + 1])
            possible.append(combined)
            if idx + 2 < len(raw_segments):
                possible.append(clean_title_candidate(combined + " " + raw_segments[idx + 2]))

        for item in possible:
            item = clean_title_candidate(item)
            if looks_like_store_group_title(item) and item not in seen:
                seen.add(item)
                candidates.append(item)

    normalized_preview = clean_title_candidate(" ".join(raw_segments))
    for match in STORE_GROUP_TITLE_RE.findall(normalized_preview):
        item = clean_title_candidate(match)
        if looks_like_store_group_title(item) and item not in seen:
            seen.add(item)
            candidates.append(item)

    return candidates


def plausible_conversation_title(candidate: str, kind=""):
    candidate = clean_title_candidate(candidate)
    if not candidate or len(candidate) < 2 or len(candidate) > 40:
        return False
    if candidate.isdigit():
        return False
    if looks_like_hex_noise_title(candidate):
        return False
    if preview_segment_is_noisy(candidate):
        return False
    lowered = candidate.lower()
    if lowered in TITLE_BLOCKLIST:
        return False
    if candidate in GENERIC_NAME_BLOCKLIST and kind != "assistant":
        return False
    if "http://" in lowered or "https://" in lowered or "www." in lowered:
        return False
    if not SAFE_TITLE_RE.match(candidate):
        return False
    if kind == "external_group" and looks_like_store_group_title(candidate):
        return True
    if kind == "external_group":
        if looks_like_external_group_chat_sentence(candidate):
            return False
        generic_parts = ["各位", "门店", "老板", "同事", "通知", "链接", "二维码", "会议"]
        if any(part in candidate for part in generic_parts) and "群" not in candidate:
            return False
        if any(part in candidate for part in EXTERNAL_GROUP_FALSE_TITLE_PARTS):
            return False
        if "群" not in candidate and not looks_like_external_group_plain_title(candidate):
            return False
    return True


def title_priority(candidate: str):
    candidate = clean_title_candidate(candidate)
    priority = 0
    if looks_like_store_group_title(candidate):
        if re.match(r"^[A-Z][+-]极修匠", candidate):
            priority += 4
        elif re.match(r"^[A-Z]极修匠", candidate):
            priority += 2
        if "(" in candidate or "（" in candidate:
            priority += 1
    if "群" in candidate:
        priority += 1
    return priority, len(candidate)


def external_group_title_confident(candidate: str, score: int):
    candidate = clean_title_candidate(candidate)
    if not candidate:
        return False
    if looks_like_store_group_title(candidate):
        return True
    if looks_like_external_group_plain_title(candidate) and score >= 18:
        return True
    if any(part in candidate for part in EXTERNAL_GROUP_FALSE_TITLE_PARTS):
        return False
    if candidate.startswith(("关于", "请", "可", "您", "如果", "之后", "上翻")):
        return False
    if any(hint in candidate for hint in EXTERNAL_GROUP_STRONG_HINTS):
        return True
    if score >= 14 and any(hint in candidate for hint in EXTERNAL_GROUP_MEDIUM_HINTS):
        return True
    return False


def choose_best_title(score_counter, evidence_map):
    if not score_counter:
        return "", ""
    title, _score = max(score_counter.items(), key=lambda item: (item[1],) + title_priority(item[0]))
    return title, evidence_map.get(title, "")


def preview_looks_like_external_group_notice(preview: str, row=None):
    """
    某些外部群会把长通知、直播宣讲或运营公告解码成多段 `|` 分隔文本。
    这些内容的首段经常像标题，但它实际上只是消息主题，不是真实群名。
    """
    segments = preview_segments(preview)
    if len(segments) < 3:
        return False

    content_type = 0
    flag = 0
    try:
        content_type = int((row or {}).get("content_type") or 0)
    except (TypeError, ValueError, AttributeError):
        content_type = 0
    try:
        flag = int((row or {}).get("flag") or 0)
    except (TypeError, ValueError, AttributeError):
        flag = 0

    joined = " | ".join(segments[:8])
    notice_hits = sum(1 for part in EXTERNAL_GROUP_NOTICE_PREVIEW_PARTS if part in joined)
    if any(segment.startswith(EXTERNAL_GROUP_NOTICE_SEGMENT_PREFIXES) for segment in segments[1:]):
        return True
    if notice_hits >= 2:
        return True
    if len(segments) >= 5 and notice_hits >= 1 and (content_type in {0, 573} or flag >= 16777216):
        return True
    return False


def collect_conversation_title_candidates(rows, kind: str):
    title_scores = Counter()
    title_evidence = {}

    for row_index, row in enumerate(rows[:80]):
        preview = row.get("preview", "")
        if not preview:
            continue
        if kind == "external_group" and preview_looks_like_external_group_notice(preview, row=row):
            continue

        for idx, segment in enumerate(preview_segments(preview)[:3]):
            if not plausible_conversation_title(segment, kind=kind):
                continue
            score = 10 if idx == 0 else 4
            if kind == "assistant":
                score += 5
            if kind == "external_group" and "群" in segment:
                score += 6
            if kind == "external_group" and idx == 0 and row_index < 12 and looks_like_external_group_plain_title(segment):
                score += 14
            title_scores[segment] += score
            if kind == "external_group" and idx == 0 and row_index < 12 and looks_like_external_group_plain_title(segment):
                title_evidence.setdefault(segment, f"早期群名候选: {preview[:90]}")
            else:
                title_evidence.setdefault(segment, f"消息片段: {preview[:90]}")

        if kind == "external_group":
            for idx, title in enumerate(extract_store_like_titles(preview)[:3]):
                score = 26 if idx == 0 else 18
                title_scores[title] += score
                title_evidence.setdefault(title, f"门店群样式命中: {preview[:90]}")

        for match in QUOTED_TITLE_RE.findall(preview):
            title = match.strip()
            if not plausible_conversation_title(title, kind=kind):
                continue
            score = 14
            if kind == "external_group":
                score += 4
            title_scores[title] += score
            title_evidence.setdefault(title, f"引号命中: {preview[:90]}")

        for match in GROUP_LIKE_TITLE_RE.findall(preview):
            title = match.strip()
            if not plausible_conversation_title(title, kind=kind):
                continue
            title_scores[title] += 12
            title_evidence.setdefault(title, f"群名样式命中: {preview[:90]}")

    return title_scores, title_evidence


def build_conversation_folder_name(conversation_id: str, display_name: str):
    safe_display = safe_path_component(display_name, fallback="会话", max_len=48)
    safe_id = safe_path_component(str(conversation_id or "").replace(":", "_"), fallback="conversation", max_len=28)
    return f"{safe_display}__{safe_id}"


def looks_like_external_group_chat_sentence(candidate: str):
    candidate = clean_title_candidate(candidate)
    if not candidate:
        return False
    if any(mark in candidate for mark in "，。！？；：,.!?;:"):
        return True
    if any(candidate.startswith(prefix) for prefix in EXTERNAL_GROUP_MESSAGE_PREFIXES):
        return True
    if candidate.startswith(("操作流程", "流程参照")) and "群" in candidate:
        return True
    if candidate.startswith(("如有疑问", "如果有疑问", "有疑问")):
        return True
    if any(part in candidate for part in ("加入了群", "加入群聊", "发到群", "哪个群", "参照群", "群跑")):
        return True
    if candidate[:1].isdigit() and any(part in candidate for part in ("有一些", "不是很", "明显", "老板", "可以")):
        return True
    if candidate.endswith(("吗", "呢", "呀", "吧", "嘛", "么")):
        return True
    if any(part in candidate for part in EXTERNAL_GROUP_FALSE_TITLE_PARTS):
        return True
    if any(part in candidate for part in EXTERNAL_GROUP_MESSAGE_PARTS):
        return True
    if len(candidate) >= 8 and any(part in candidate for part in ("可以", "回复", "联系", "跑", "看一下", "看下")):
        return True
    return False


def looks_like_personal_display_name(candidate: str):
    candidate = clean_title_candidate(candidate)
    if not candidate:
        return False
    compact = re.sub(r"[\s()（）]", "", candidate)
    if "@" in compact or "＠" in compact:
        return True
    if any(ch.isdigit() for ch in compact):
        return False
    if any(part in candidate for part in EXTERNAL_GROUP_TITLE_HINT_PARTS):
        return False
    if re.fullmatch(r"[\u4e00-\u9fff·]{2,6}", compact):
        return True
    if len(compact) <= 8 and plausible_name(candidate):
        return True
    return False


def looks_like_external_group_plain_title(candidate: str):
    candidate = clean_title_candidate(candidate)
    if not candidate or len(candidate) < 4 or len(candidate) > 40:
        return False
    if not SAFE_TITLE_RE.match(candidate):
        return False
    if looks_like_hex_noise_title(candidate):
        return False
    if preview_segment_is_noisy(candidate):
        return False
    if looks_like_external_group_chat_sentence(candidate):
        return False
    if any(part in candidate for part in EXTERNAL_GROUP_FALSE_TITLE_PARTS):
        return False
    if looks_like_store_group_title(candidate):
        return True
    if looks_like_personal_display_name(candidate):
        return False
    if "群" in candidate or any(hint in candidate for hint in EXTERNAL_GROUP_STRONG_HINTS):
        return True
    if any(hint in candidate for hint in EXTERNAL_GROUP_MEDIUM_HINTS):
        return True
    if any(hint in candidate for hint in EXTERNAL_GROUP_TITLE_HINT_PARTS):
        return True
    if ASCII_LETTER_RE.search(candidate) and any(ch.isdigit() for ch in candidate):
        return True
    return False


CONVERSATION_KIND_LABELS = {
    "external_group": "外部群",
    "internal_group": "内部群/部门群",
    "single_chat": "单聊",
    "assistant": "助手/应用",
    "f_session": "F 类会话",
    "m_session": "M 类会话",
    "approval": "审批",
    "mail": "邮件",
    "other": "其他/无前缀",
}


def conversation_prefix(conversation_id: str):
    conversation_id = str(conversation_id or "")
    if conversation_id in {"APPROVAL", "MAIL"}:
        return conversation_id
    if ":" in conversation_id:
        return conversation_id.split(":", 1)[0] + ":"
    return "(none)"


def conversation_kind(conversation_id: str):
    prefix = conversation_prefix(conversation_id)
    if prefix == "R:":
        return "external_group"
    if prefix == "S:":
        return "single_chat"
    if prefix == "Y:":
        return "assistant"
    if prefix == "F:":
        return "f_session"
    if prefix == "M:":
        return "m_session"
    if prefix == "APPROVAL":
        return "approval"
    if prefix == "MAIL":
        return "mail"
    return "other"


def infer_r_conversation_kind(rows):
    previews = [str(row.get("preview", "") or "") for row in rows if row.get("preview")]
    if not previews:
        return "external_group", ""

    if any("此群为部门群" in preview for preview in previews):
        return "internal_group", "命中部门群提示"

    if any(extract_store_like_titles(preview) for preview in previews):
        return "external_group", "命中门店群样式"

    join_like_count = sum(1 for preview in previews if preview in JOIN_LIKE_PREVIEWS or preview.startswith("加入了群"))
    office_hints = ("办公区域", "公司办公", "上下班考勤")
    if join_like_count >= max(5, len(previews) // 4):
        if any("极修匠的家人们" in preview for preview in previews):
            return "internal_group", "入群提示占比较高，且命中内部群标题"
        if any("各位同事" in preview for preview in previews):
            return "internal_group", "入群提示占比较高，且命中内部办公群提示"
    if any(any(keyword in preview for keyword in office_hints) for preview in previews):
        return "internal_group", "命中内部办公群提示"

    return "external_group", ""


def infer_conversation_kind(conversation_id: str, rows):
    base_kind = conversation_kind(conversation_id)
    if base_kind != "external_group":
        return base_kind, ""
    return infer_r_conversation_kind(rows)


def conversation_kind_label(kind: str):
    return CONVERSATION_KIND_LABELS.get(kind, kind)


def conversation_matches_prefix(conversation_id: str, prefix: str):
    if not prefix:
        return True
    if prefix.endswith(":"):
        return str(conversation_id or "").startswith(prefix)
    return str(conversation_id or "") == prefix


def resolve_selected_conversation_ids(grouped, conversation_index, conversation_id="", conversation_ids=None, prefix="R:"):
    if conversation_ids:
        selected_ids = []
        missing = []
        seen = set()
        for current_id in conversation_ids:
            if current_id in seen:
                continue
            seen.add(current_id)
            if current_id not in grouped:
                missing.append(current_id)
                continue
            selected_ids.append(current_id)
        if missing:
            raise ValueError(f"未在数据中找到指定会话: {', '.join(missing[:5])}")
        return selected_ids

    if conversation_id:
        if conversation_id not in grouped:
            raise ValueError(f"未在数据中找到指定会话: {conversation_id}")
        return [conversation_id]

    if prefix == "R:":
        return [item["conversation_id"] for item in conversation_index if item["conversation_kind"] == "external_group"]

    return [item["conversation_id"] for item in conversation_index if conversation_matches_prefix(item["conversation_id"], prefix)]


def choose_index_filename(conversation_index, selected_ids, conversation_id="", display_name=""):
    if conversation_id:
        label = safe_path_component(display_name or conversation_id, fallback="已选会话", max_len=36)
        return f"已选会话索引_{label}.csv"

    selected_set = set(selected_ids)
    kinds = {item["conversation_kind"] for item in conversation_index if item["conversation_id"] in selected_set}
    if kinds == {"external_group", "internal_group"}:
        return "聊天会话索引.csv"
    if len(kinds) == 1:
        kind = next(iter(kinds))
        kind_filenames = {
            "external_group": "外部群索引.csv",
            "internal_group": "内部群索引.csv",
            "single_chat": "单聊索引.csv",
            "assistant": "助手应用索引.csv",
            "f_session": "F类会话索引.csv",
            "m_session": "M类会话索引.csv",
            "approval": "审批会话索引.csv",
            "mail": "邮件会话索引.csv",
            "other": "其他会话索引.csv",
        }
        return kind_filenames.get(kind, f"{safe_path_component(conversation_kind_label(kind), max_len=20)}索引.csv")

    return "当前筛选会话索引.csv"


def content_type_label(content_type):
    labels = {
        -1: "疑似缺失消息",
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
        1023: "会话状态/同步标记",
    }
    return labels.get(content_type, f"类型{content_type}")


def iso_from_ts(ts):
    ts = safe_int_value(ts)
    if not ts:
        return ""
    try:
        return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


MAX_RECOVERY_GAP_MARKER_SIZE = 5
MAX_RECOVERY_GAP_MARKER_SECONDS = 600


def safe_int_value(value, default=0):
    """
    把 sqlite 或历史 CSV 里读到的数字字段转成 int；无法转换时给调用方一个稳定默认值。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def row_is_recovery_gap_marker(row):
    """
    判断当前行是否为导出阶段补充的“疑似缺失消息”提示行。
    """
    return bool((row or {}).get("_recovery_gap_marker"))


def build_recovery_gap_marker(previous_row, next_row, missing_message_id: int, missing_index: int, missing_count: int):
    """
    生成聊天记录里的缺口提示行，用来标明内存缓存中没有恢复出的连续 message_id。
    """
    previous_ts = safe_int_value((previous_row or {}).get("send_time"))
    next_ts = safe_int_value((next_row or {}).get("send_time"))
    if previous_ts and next_ts and next_ts >= previous_ts:
        marker_ts = previous_ts + max(1, round((next_ts - previous_ts) * missing_index / (missing_count + 1)))
    else:
        marker_ts = previous_ts or next_ts

    marker_preview = (
        f"疑似缺失消息：message_id {missing_message_id} 当前缓存页未恢复，"
        "可能是图片、文件或其它媒体消息。请在企业微信打开该群并停留在缺失消息附近后重新恢复。"
    )
    return {
        "_recovery_gap_marker": True,
        "_rowid": "",
        "_page": "",
        "message_id": missing_message_id,
        "server_id": "",
        "sequence": "",
        "sender_id": "",
        "conversation_id": (previous_row or {}).get("conversation_id") or (next_row or {}).get("conversation_id", ""),
        "content_type": -1,
        "send_time": marker_ts,
        "send_time_iso": iso_from_ts(marker_ts),
        "flag": "",
        "content": "",
        "devinfo": "",
        "from_app_id": "",
        "msg_from_devinfo": "",
        "extra_content": "",
        "local_extra_content": "",
        "client_id": "",
        "local_extra_content_translate_info": "",
        "local_extra_content_time_nlp": "",
        "local_extra_content_approval_nlp": "",
        "preview": marker_preview,
    }


def rows_with_recovery_gap_markers(rows):
    """
    在会话展示用时间线里插入小跨度 message_id 缺口提示，避免疑似图片消息漏恢复时没有任何痕迹。
    """
    if not rows:
        return []

    display_rows = []
    ordered_rows = sorted(
        list(rows),
        key=lambda item: (
            safe_int_value(item.get("send_time")),
            safe_int_value(item.get("message_id")),
        ),
    )
    previous_row = None
    for row in ordered_rows:
        if previous_row:
            previous_id = safe_int_value(previous_row.get("message_id"))
            current_id = safe_int_value(row.get("message_id"))
            previous_ts = safe_int_value(previous_row.get("send_time"))
            current_ts = safe_int_value(row.get("send_time"))
            same_conversation = str(previous_row.get("conversation_id") or "") == str(row.get("conversation_id") or "")
            missing_count = current_id - previous_id - 1
            nearby_time = previous_ts and current_ts and 0 <= current_ts - previous_ts <= MAX_RECOVERY_GAP_MARKER_SECONDS
            if same_conversation and 0 < missing_count <= MAX_RECOVERY_GAP_MARKER_SIZE and nearby_time:
                for missing_index, missing_message_id in enumerate(range(previous_id + 1, current_id), start=1):
                    display_rows.append(
                        build_recovery_gap_marker(previous_row, row, missing_message_id, missing_index, missing_count)
                    )
        display_rows.append(row)
        previous_row = row
    return display_rows


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
            nested = binascii.unhexlify(match).decode("utf-8", errors="ignore").replace("\x00", " ")
        except Exception:
            continue
        nested = nested.strip()
        if not nested or nested in seen:
            continue
        seen.add(nested)
        texts.append(nested)
    return texts


def decoded_text_score(text: str):
    """
    给不同编码尝试后的文本打一个粗略分数，优先保留中文、英文和可阅读符号比例更高的结果。
    """
    text = str(text or "")
    if not text:
        return 0
    visible = sum(1 for char in text if char.isprintable() and char not in "\ufffd")
    cjk = len(CJK_RE.findall(text))
    ascii_letters = len(ASCII_LETTER_RE.findall(text))
    punctuation = sum(1 for char in text if char in "，。！？；：、,.!?;:()（）[]【】<>《》@#_-+=/\\|")
    control_penalty = len(text) - visible
    replacement_penalty = text.count("\ufffd") * 5
    return visible + cjk * 3 + ascii_letters + punctuation - control_penalty * 3 - replacement_penalty


def clean_strong_decoded_text(text: str):
    """
    强解码结果用于完整聊天记录留档，保留的信息比预览更完整，只做控制字符和空白规整。
    """
    text = str(text or "").replace("\x00", " ")
    text = CONTROL_CHAR_RE.sub(" ", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def unique_decoded_texts(texts):
    """
    去掉重复和明显无效的解码结果，避免同一段内容在完整记录里反复出现。
    """
    unique_values = []
    seen = set()
    for item in texts:
        cleaned = clean_strong_decoded_text(item)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_values.append(cleaned)
    return unique_values


def decode_bytes_with_multiple_encodings(raw_bytes: bytes):
    """
    对原始字节做多编码尝试，解决部分字段不是 UTF-8 或被嵌在十六进制里的情况。
    """
    candidates = []
    for encoding in STRONG_DECODE_ENCODINGS:
        try:
            decoded = raw_bytes.decode(encoding, errors="ignore")
        except Exception:
            continue
        cleaned = clean_strong_decoded_text(decoded)
        if not cleaned:
            continue
        candidates.append((decoded_text_score(cleaned), cleaned))
    candidates.sort(reverse=True, key=lambda item: item[0])
    return [text for _score, text in candidates if _score > 0]


def strong_decode_value(value, max_nested_hex=8):
    """
    尽量把单个原始字段解成可读文本：先识别整段十六进制，再继续识别字段内部嵌套的长十六进制片段。
    """
    if value is None:
        return []
    text = str(value)
    decoded_texts = []
    stripped = text.strip()

    if stripped and len(stripped) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in stripped[: min(len(stripped), 800)]):
        try:
            decoded_texts.extend(decode_bytes_with_multiple_encodings(binascii.unhexlify(stripped)))
        except Exception:
            decoded_texts.append(stripped)
    else:
        decoded_texts.append(stripped)

    for source_text in list(decoded_texts):
        nested_count = 0
        for match in LONG_HEX_RE.findall(source_text):
            if nested_count >= max_nested_hex:
                break
            if len(match) % 2 != 0:
                continue
            try:
                nested_bytes = binascii.unhexlify(match)
            except Exception:
                continue
            decoded_texts.extend(decode_bytes_with_multiple_encodings(nested_bytes))
            nested_count += 1

    return unique_decoded_texts(decoded_texts)


def strong_decoded_field_text(row, field_name: str):
    """
    返回某个原始消息字段的强解码文本，多个候选用换行分隔，便于人工检查。
    """
    return "\n".join(strong_decode_value((row or {}).get(field_name)))


def strong_decoded_message_text(row, media_metadata=None):
    """
    汇总一条消息所有可读内容，作为“完整聊天记录”里的强解码正文。
    """
    media_metadata = media_metadata or extract_media_metadata(row)
    pieces = []
    preview_text = clean_strong_decoded_text(build_message_preview_text(row, media_metadata=media_metadata))
    if preview_text:
        pieces.append(preview_text)
    for field_name in RAW_MESSAGE_TEXT_FIELDS:
        pieces.extend(strong_decode_value((row or {}).get(field_name)))
    return "\n".join(unique_decoded_texts(pieces))


def unhex_value_to_bytes(value):
    """
    将十六进制字段转成字节；不是十六进制时返回 None。
    """
    text = str(value or "").strip()
    if not text or len(text) % 2 != 0:
        return None
    if not all(c in "0123456789abcdefABCDEF" for c in text[: min(len(text), 800)]):
        return None
    try:
        return binascii.unhexlify(text)
    except Exception:
        return None


def read_protobuf_varint(data: bytes, offset: int):
    """
    读取 protobuf varint，供结构化字段里提取 length-delimited token 使用。
    """
    result = 0
    shift = 0
    current = offset
    while current < len(data) and shift < 64:
        byte = data[current]
        result |= (byte & 0x7F) << shift
        current += 1
        if not (byte & 0x80):
            return result, current
        shift += 7
    return None, offset


def printable_ascii_tokens_from_bytes(data: bytes):
    """
    从二进制字段里提取较长 ASCII token，例如文件 ID、base64 片段或数字 ID。
    """
    if not data:
        return []
    text = data.decode("latin1", errors="ignore")
    tokens = []
    for token in ASCII_TOKEN_RE.findall(text):
        token = token.strip("=_-")
        if len(token) < 6:
            continue
        if re.fullmatch(r"[0-9a-fA-F]{32,}", token):
            continue
        tokens.append(token)
    return unique_items(tokens)


def protobuf_length_delimited_tokens(data: bytes, max_segments=32):
    """
    粗略解析 protobuf wire type 2 字段，提取其中可见的文本和 token。
    """
    tokens = []
    offset = 0
    segment_count = 0
    while offset < len(data) and segment_count < max_segments:
        key, next_offset = read_protobuf_varint(data, offset)
        if key is None or next_offset <= offset:
            offset += 1
            continue
        wire_type = key & 0x07
        if wire_type == 0:
            _value, value_end = read_protobuf_varint(data, next_offset)
            offset = value_end if value_end > next_offset else offset + 1
            continue
        if wire_type == 1:
            offset = next_offset + 8
            continue
        if wire_type == 5:
            offset = next_offset + 4
            continue
        if wire_type != 2:
            offset += 1
            continue

        length, payload_offset = read_protobuf_varint(data, next_offset)
        if length is None or length <= 0 or length > 2048 or payload_offset + length > len(data):
            offset += 1
            continue
        payload = data[payload_offset : payload_offset + length]
        tokens.extend(decode_bytes_with_multiple_encodings(payload))
        tokens.extend(printable_ascii_tokens_from_bytes(payload))
        offset = payload_offset + length
        segment_count += 1
    return unique_decoded_texts(tokens)


def structured_tokens_from_value(value):
    """
    从原始字段里提取结构化 token；这类信息不一定是聊天正文，但能说明消息不是完全不可解。
    """
    if value is None:
        return []
    raw_bytes = unhex_value_to_bytes(value)
    if raw_bytes is None:
        text = str(value)
        return unique_items(ASCII_TOKEN_RE.findall(text))
    tokens = []
    tokens.extend(printable_ascii_tokens_from_bytes(raw_bytes))
    tokens.extend(protobuf_length_delimited_tokens(raw_bytes))
    return unique_decoded_texts(tokens)


def structured_tokens_from_row(row):
    """
    汇总一条消息全部原始字段里的结构化 token。
    """
    tokens = []
    for field_name in RAW_MESSAGE_TEXT_FIELDS:
        tokens.extend(structured_tokens_from_value((row or {}).get(field_name)))
    return unique_decoded_texts(tokens)


def clean_preview_text(value: str):
    text = str(value or "")
    text = CONTROL_CHAR_RE.sub(" ", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def strip_visible_length_prefix_artifact(text: str, preserve_sender_marker=False):
    """
    企业微信的正文常包在 protobuf 字段里，长度字节偶尔会被当成可见字符留在正文开头。
    优先按首字符 ASCII 值与后续 UTF-8 字节长度判断；历史预览丢失上下文时，再兜底清掉单字母中文前缀。
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
    把预览片段里常见的 JSON 碎片、控制字符残留和单字母尾巴先做一轮规整，
    减少卡片/通知类消息在预览区出现 protobuf 噪声。
    """
    raw_text = clean_preview_text(segment)
    preserve_sender_marker = raw_text.lstrip().startswith((",", "，", "@"))
    text = raw_text.strip(" |,:;{}[]")
    if not text:
        return ""
    text = text.replace("\\/", "/")
    text = re.sub(r"^[\]})>]+", "", text)
    text = re.sub(r"^[A-Za-z]{1,4}(?=https?://)", "", text)
    text = strip_visible_length_prefix_artifact(text, preserve_sender_marker=preserve_sender_marker)
    if CJK_RE.search(text):
        url_match = URL_RE.search(text)
        if url_match:
            text_prefix = clean_preview_text(text[: url_match.start()])
            text_prefix = re.sub(r"[0-9\-:：]+$", "", text_prefix).rstrip()
            if len(text_prefix) >= 4:
                text = text_prefix
    if CJK_RE.search(text) and re.search(r"[A-Za-z]$", text) and not re.search(r"[A-Za-z]{2,}$", text):
        text = text[:-1].rstrip()
    return clean_preview_text(text)


def normalize_preview_text(preview: str):
    """
    对已经生成过的预览文本再做一次整段清洗；历史导出里常见的长度字节需要先按整段长度判断。
    """
    text = clean_preview_text(preview)
    if not text:
        return ""
    preserve_sender_marker = text.lstrip().startswith((",", "，", "@"))
    text = strip_visible_length_prefix_artifact(text, preserve_sender_marker=preserve_sender_marker)
    segments = [normalize_preview_segment(segment) for segment in re.split(r"\s*\|\s*", text)]
    segments = [segment for segment in segments if segment]
    if not segments:
        return ""
    return " | ".join(unique_items(segments))[:500]


def unique_items(items):
    unique_values = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique_values.append(item)
    return unique_values


def preview_segment_is_layout_meta(segment: str):
    segment = normalize_preview_segment(segment)
    if not segment:
        return True
    lowered = segment.lower()
    if lowered in TITLE_BLOCKLIST:
        return True
    if COLOR_VALUE_RE.fullmatch(segment):
        return True
    if lowered in {"true", "false", "null", "none"}:
        return True
    if lowered.startswith(("title_", "sub_title_", "button_", "card_", "detail_", "info_")):
        return True
    if lowered.endswith(("_list", "_desc", "_span", "_icon", "_image")):
        return True
    if re.fullmatch(r"[a-z0-9_]{3,}", lowered) and "_" in lowered:
        return True
    if lowered in {"interaction", "tips", "button", "type", "style", "selections", "data"}:
        return True
    if segment.startswith(":") and not CJK_RE.search(segment):
        return True
    if "=" in segment and not CJK_RE.search(segment) and not URL_RE.search(segment):
        return True
    if re.fullmatch(r"[:;,\-_=+{}\[\]\"'`]+", segment):
        return True
    return False


def preview_segment_is_url_noise(segment: str):
    """
    卡片消息经常带头像、封面或跳转链接；这类只有 URL 的片段不适合作为知识预览正文。
    """
    segment = normalize_preview_segment(segment)
    if not segment or not URL_RE.search(segment):
        return False
    remainder = clean_preview_text(URL_RE.sub(" ", segment))
    compact = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", remainder)
    if not compact:
        return True
    if CJK_RE.search(compact):
        return False
    return len(compact) <= 4


def preview_segment_is_noisy(segment: str):
    segment = normalize_preview_segment(segment).strip(" |")
    if not segment:
        return True

    lowered = segment.lower()
    if URL_RE.search(segment):
        return False
    if UUID_RE.fullmatch(lowered):
        return False
    if MEDIA_FILE_PATH_RE.search(segment):
        return False

    segment_suffix = Path(segment).suffix.lower()
    if segment_suffix in MEDIA_EXTENSIONS:
        return False

    compact = re.sub(r"[\s|_:/\\.@#\-\(\)（）,，。！？；、“”‘’\[\]{}+=]+", "", segment)
    if compact.isdigit():
        return len(compact) >= 8
    if re.fullmatch(r"[0-9a-fA-F]{24,}", compact):
        return True
    if len(compact) < 20:
        return False
    if CJK_RE.search(compact):
        return False

    hex_count = sum(ch in "0123456789abcdefABCDEF" for ch in compact)
    alpha_count = sum(ch.isalpha() for ch in compact)
    digit_count = sum(ch.isdigit() for ch in compact)
    if hex_count >= max(18, int(len(compact) * 0.72)) and alpha_count <= max(2, int(len(compact) * 0.18)):
        return True
    if len(compact) >= 32 and digit_count >= max(20, int(len(compact) * 0.7)) and alpha_count <= 2:
        return True
    return False


def build_preview_segments(text: str):
    candidates = [normalize_preview_segment(hit) for hit in TEXT_PREVIEW_RE.findall(text or "") if normalize_preview_segment(hit)]
    if not candidates:
        fallback = normalize_preview_segment(text)
        if fallback and not preview_segment_is_layout_meta(fallback):
            return [fallback]
        return []

    filtered = [
        item
        for item in unique_items(candidates)
        if not preview_segment_is_noisy(item)
        and not preview_segment_is_layout_meta(item)
        and not preview_segment_is_url_noise(item)
    ]
    if filtered:
        return filtered[:8]
    return []


def build_preview_text(text: str):
    segments = build_preview_segments(text)
    if not segments:
        return ""
    return " | ".join(segments[:8])[:500]


def detect_docs_dir():
    candidates = []
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        candidates.append(Path(userprofile) / "Documents" / "WXWork")
    candidates.append(Path.home() / "Documents" / "WXWork")

    seen = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else None


def normalize_media_stem(stem: str):
    return re.sub(r"\(\d+\)$", "", stem).lower()


def emotion_cache_path(path: Path):
    return any(part.lower() == "emotion" for part in path.parts)


def cached_media_kind_for_path(path: Path):
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "图片"
    if suffix in VIDEO_EXTENSIONS:
        return "视频"
    if suffix in AUDIO_EXTENSIONS:
        return "语音/音频"
    if emotion_cache_path(path):
        return "动画表情"
    return ""


def media_index_keys(path: Path):
    keys = []
    # 同一份媒体文件同时按“去后缀名称”和“完整文件名”建索引，兼容 UUID 表情和普通文件名两种命中方式。
    stem_key = normalize_media_stem(path.name if not path.suffix else path.stem)
    if stem_key:
        keys.append(stem_key)
    full_name_key = path.name.lower()
    if full_name_key and full_name_key not in keys:
        keys.append(full_name_key)
    return keys


def build_media_cache_index(docs_dir: Path, extra_sources=None):
    sources = []
    if docs_dir and docs_dir.exists():
        sources.append(docs_dir / "Global" / "Image")
        sources.extend(docs_dir.glob("*/Cache/Image"))
        sources.extend(docs_dir.glob("*/Cache/File"))
        # 企业微信本地视频和表情不在图片缓存目录里，这里一起纳入索引，预览时才能命中。
        sources.extend(docs_dir.glob("*/Cache/Video"))
        sources.extend(docs_dir.glob("*/Emotion"))

    # 额外把项目里已经导出的图片目录也纳入索引，这样聊天浏览区能直接复用历史导出的图片。
    for source in extra_sources or []:
        source_path = Path(source)
        if source_path.exists():
            sources.append(source_path)

    if not sources:
        return {}

    index = defaultdict(list)
    seen_paths = set()
    for source in sources:
        if not source.exists():
            continue
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            if not cached_media_kind_for_path(path):
                continue
            key = str(path).lower()
            if key in seen_paths:
                continue
            seen_paths.add(key)
            for media_key in media_index_keys(path):
                index[media_key].append(path)
    for paths in index.values():
        paths.sort(key=lambda item: (item.stat().st_mtime, item.stat().st_size), reverse=True)
    return dict(index)


def preview_media_search_roots(source_dir: Path):
    roots = []
    source_dir = Path(source_dir)
    for candidate in (
        source_dir / "organized_external_groups",
        source_dir / "organized_external_groups_gui_test",
    ):
        if candidate.exists():
            roots.append(candidate)
    return roots


def load_media_cache_index(docs_dir: Path, extra_sources=None):
    normalized_sources = []
    if docs_dir and docs_dir.exists():
        normalized_sources.append(str(docs_dir.resolve()).lower())
    for source in extra_sources or []:
        source_path = Path(source)
        if source_path.exists():
            normalized_sources.append(str(source_path.resolve()).lower())

    cache_key = tuple(sorted(normalized_sources))
    if cache_key in _MEDIA_CACHE_INDEX_CACHE:
        return _MEDIA_CACHE_INDEX_CACHE[cache_key]

    media_cache_index = build_media_cache_index(docs_dir, extra_sources=extra_sources)
    _MEDIA_CACHE_INDEX_CACHE[cache_key] = media_cache_index
    return media_cache_index


def extract_media_file_paths(texts):
    paths = []
    seen = set()
    for text in texts:
        for regex in (MEDIA_FILE_PATH_RE, EMOTION_MEDIA_PATH_RE):
            for match in regex.findall(text or ""):
                candidate = clean_preview_text(match).strip("\"'")
                if not candidate:
                    continue
                lowered = candidate.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                paths.append(candidate)
    return paths


def extract_media_file_names(texts):
    file_names = []
    seen = set()
    for text in texts:
        for match in MEDIA_FILE_NAME_RE.findall(text or ""):
            candidate = clean_preview_text(match).strip("\"'")
            if not candidate:
                continue
            candidate = candidate.split("\\")[-1].split("/")[-1]
            if " " in candidate:
                parts = [item for item in candidate.split() if Path(item).suffix.lower() in MEDIA_EXTENSIONS]
                if parts:
                    candidate = parts[-1]
            basename_hits = MEDIA_BASENAME_RE.findall(candidate)
            if basename_hits:
                candidate = basename_hits[-1]
            if Path(candidate).suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            lowered = candidate.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            file_names.append(candidate)
    normalized = []
    for candidate in file_names:
        if any(candidate != other and candidate in other for other in file_names):
            continue
        normalized.append(candidate)
    return normalized


def extract_media_metadata(row):
    decoded_fields = []
    for field_name in ("content", "extra_content", "local_extra_content"):
        decoded_fields.extend(collect_decoded_texts(row.get(field_name)))
    if row.get("preview"):
        decoded_fields.append(row["preview"])

    joined = "\n".join(decoded_fields)
    uuids = []
    urls = []
    file_paths = extract_media_file_paths(decoded_fields)
    file_names = extract_media_file_names(decoded_fields)
    seen_uuid = set()
    seen_url = set()

    for match in UUID_RE.findall(joined):
        lowered = match.lower()
        if lowered not in seen_uuid:
            seen_uuid.add(lowered)
            uuids.append(lowered)

    for match in URL_RE.findall(joined):
        cleaned = match.rstrip(").,，。；;")
        if cleaned not in seen_url:
            seen_url.add(cleaned)
            urls.append(cleaned)

    path_media_kinds = [cached_media_kind_for_path(Path(path)) for path in file_paths]
    name_media_kinds = [cached_media_kind_for_path(Path(name)) for name in file_names]
    content_type = row.get("content_type")
    media_item_count = max(len(file_paths), len(file_names), len(uuids), len(urls))
    allow_multiple_media_files = content_type in MULTI_MEDIA_CONTENT_TYPES and media_item_count > 1
    media_kind = ""
    has_recoverable_media_clue = bool(uuids or file_paths or file_names)
    if content_type in IMAGE_CONTENT_TYPES:
        media_kind = "图片"
    elif content_type == 29:
        media_kind = "动画表情"
    elif "视频" in path_media_kinds or "视频" in name_media_kinds:
        media_kind = "视频"
    elif content_type == 561 or "语音/音频" in path_media_kinds or "语音/音频" in name_media_kinds:
        media_kind = "语音/音频"
    elif "图片" in path_media_kinds or "图片" in name_media_kinds:
        media_kind = "图片"
    elif content_type == 101:
        media_kind = "媒体下载链接"
    elif has_recoverable_media_clue:
        media_kind = "媒体线索"

    display_label = content_type_label(content_type)
    if display_label.startswith("类型") and media_kind in {"图片", "动画表情", "视频", "语音/音频"}:
        display_label = media_kind

    return {
        "content_type_label": display_label,
        "media_kind": media_kind,
        "uuids": uuids,
        "urls": urls,
        "file_paths": file_paths,
        "file_names": file_names,
        "has_media": bool(media_kind or has_recoverable_media_clue),
        "media_item_count": media_item_count,
        "allow_multiple_media_files": allow_multiple_media_files,
    }


def media_metadata_uses_single_file_limit(media_metadata):
    """
    判断媒体消息是否应只展示一个文件；转发聊天记录或组合卡片可能包含多张图片，需要保留全部命中的文件。
    """
    if media_metadata.get("allow_multiple_media_files"):
        return False
    return media_metadata.get("media_kind") in {"图片", "动画表情", "视频", "语音/音频"}


def resolve_cached_media_files(media_metadata, media_cache_index):
    resolved = []
    seen = set()
    media_kind = media_metadata.get("media_kind", "")
    single_file_limit = media_metadata_uses_single_file_limit(media_metadata)

    # 优先直接使用消息里已经带出来的本机缓存路径；这样即使索引缓存还没刷新，也能先把图片显示出来。
    for raw_path in media_metadata.get("file_paths", []):
        try:
            path = Path(raw_path)
        except Exception:
            continue
        if not path.exists() or not path.is_file():
            continue
        detected_kind = cached_media_kind_for_path(path)
        if not detected_kind and not (media_kind == "动画表情" and emotion_cache_path(path)):
            continue
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        resolved.append(path)
    if resolved:
        if single_file_limit:
            return resolved[:1]
        return resolved

    for file_name in media_metadata.get("file_names", []):
        stem_key = normalize_media_stem(Path(file_name).stem)
        for candidate_key in (stem_key, str(file_name).lower()):
            for path in media_cache_index.get(candidate_key, []):
                key = str(path).lower()
                if key in seen:
                    continue
                seen.add(key)
                resolved.append(path)
        if single_file_limit and resolved:
            return resolved[:1]

    for media_uuid in media_metadata["uuids"]:
        for path in media_cache_index.get(media_uuid, []):
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            resolved.append(path)
    return resolved[:1] if single_file_limit and resolved else resolved


def preview_text_is_informative(preview_text: str):
    preview_text = clean_preview_text(preview_text)
    if not preview_text:
        return False
    if preview_segment_is_layout_meta(preview_text):
        return False
    if preview_segment_is_url_noise(preview_text):
        return False
    if len(preview_text) == 1 and not (CJK_RE.search(preview_text) or ASCII_LETTER_RE.search(preview_text)):
        return False
    return True


def media_preview_prefers_prefix(media_metadata, preview_text: str):
    media_kind = media_metadata.get("media_kind", "")
    if media_kind in {"图片", "动画表情", "视频", "语音/音频", "媒体下载链接"}:
        return True
    if media_metadata.get("file_paths") or media_metadata.get("file_names"):
        return True
    if not preview_text_is_informative(preview_text) and media_metadata.get("has_media"):
        return True
    return False


def build_message_preview_text(row, media_metadata=None):
    media_metadata = media_metadata or extract_media_metadata(row)
    preview_text = normalize_preview_text(row.get("preview", ""))
    if media_metadata.get("has_media") and media_preview_prefers_prefix(media_metadata, preview_text):
        detail_items = []
        detail_limit = 3 if media_metadata.get("allow_multiple_media_files") else 1
        for candidate in media_metadata.get("file_paths", []):
            candidate_name = Path(candidate).name
            if candidate_name and candidate_name not in detail_items:
                detail_items.append(candidate_name)
            if len(detail_items) >= detail_limit:
                break
        for candidate in media_metadata.get("file_names", []):
            if candidate and candidate not in detail_items:
                detail_items.append(candidate)
            if len(detail_items) >= detail_limit:
                break
        if not detail_items:
            for candidate in build_preview_segments(preview_text):
                if UUID_RE.fullmatch(candidate.lower()):
                    continue
                if candidate and candidate not in detail_items:
                    detail_items.append(candidate)
                if len(detail_items) >= detail_limit:
                    break
        if not detail_items and media_metadata.get("urls"):
            detail_items.extend(media_metadata["urls"][:detail_limit])
        if not detail_items and media_metadata.get("uuids"):
            detail_items.extend(media_metadata["uuids"][:detail_limit])
        preview_label = media_metadata.get("media_kind") or media_metadata["content_type_label"]
        prefix = f"[{preview_label}]"
        if detail_items:
            media_item_count = media_metadata.get("media_item_count") or len(detail_items)
            if media_metadata.get("allow_multiple_media_files") and media_item_count > 1:
                return f"{prefix} {media_item_count} 个媒体文件：{' | '.join(detail_items[:3])}"
            return f"{prefix} {' | '.join(detail_items[:3])}"
        return prefix
    if preview_text_is_informative(preview_text):
        return preview_text
    return f"[{content_type_label(row.get('content_type'))}]"


def message_has_quote_reply_flag(row):
    """
    判断消息 flag 是否包含企业微信引用回复标记；当前样本中 512 表示这条消息带被引用内容。
    """
    return bool(safe_int_value((row or {}).get("flag")) & QUOTE_REPLY_FLAG)


def quote_reply_text_score(text: str):
    """
    给引用回复候选文本打分，优先选择中文可读内容，降低 UTF-8 被错误当成 GBK 后的乱码候选权重。
    """
    text = str(text or "")
    mojibake_hints = ("锛", "涓", "鐨", "鏋", "鍖", "杩", "浜", "垫", "睜", "鏈", "殑")
    penalty = sum(text.count(item) for item in mojibake_hints) * 80
    return decoded_text_score(text) - penalty


def clean_quote_reply_part(text: str, max_segments: int = 6):
    """
    把引用回复里的片段清洗成适合 GUI 展示的短文本，去掉 protobuf 长度字节和无意义符号。
    """
    text = clean_strong_decoded_text(text).strip(" '\"“”")
    if not text:
        return ""
    bracket_match = re.fullmatch(r"\[[^\[\]]{1,30}\]", text)
    if bracket_match:
        return bracket_match.group(0)
    segments = build_preview_segments(text)
    if segments:
        return " | ".join(segments[:max_segments])
    normalized = normalize_preview_text(text)
    return normalized[:500]


def clean_quote_sender_text(text: str):
    """
    清洗被引用消息的发送人名称；只取最像姓名或昵称的片段，避免把手机号和控制字节一起展示出来。
    """
    segments = build_preview_segments(clean_strong_decoded_text(text))
    if segments:
        return segments[0]
    return normalize_preview_text(text)[:80]


def split_quoted_message_text(quoted_text: str):
    """
    将企业微信引用块拆成“被引用发送人”和“被引用内容”两部分。
    """
    cleaned = clean_strong_decoded_text(quoted_text).strip(" '\"“”")
    if not cleaned:
        return "", ""

    match = QUOTE_SENDER_SPLIT_RE.match(cleaned)
    if match:
        sender = clean_quote_sender_text(match.group(1))
        quoted = clean_quote_reply_part(match.group(2))
        return sender, quoted

    return "", clean_quote_reply_part(cleaned)


def extract_quote_reply_from_decoded_text(text: str):
    """
    从强解码后的原始字段中识别企业微信引用回复结构：引号包裹的被引用消息 + 分隔线 + 当前回复。
    """
    cleaned = clean_strong_decoded_text(text)
    match = QUOTE_REPLY_RE.search(cleaned)
    if not match:
        return None

    quote_sender, quoted_text = split_quoted_message_text(match.group("quoted"))
    reply_text = clean_quote_reply_part(match.group("reply"))
    if not quoted_text and not reply_text:
        return None

    return {
        "quote_sender": quote_sender,
        "quote_text": quoted_text,
        "quote_reply_text": reply_text,
        "quote_raw_text": cleaned[:1200],
    }


def extract_quote_reply_metadata(row):
    """
    提取引用回复展示信息；无法拆出详细内容时仍保留 is_quote_reply，方便 GUI 标明这条消息是引用回复。
    """
    is_quote_reply = message_has_quote_reply_flag(row)
    result = {
        "is_quote_reply": is_quote_reply,
        "quote_sender": "",
        "quote_text": "",
        "quote_reply_text": "",
        "quote_raw_text": "",
    }
    if not is_quote_reply or row_is_recovery_gap_marker(row):
        return result

    candidates = []
    for field_name in ("content", "extra_content", "local_extra_content"):
        for decoded_text in strong_decode_value((row or {}).get(field_name)):
            parsed = extract_quote_reply_from_decoded_text(decoded_text)
            if parsed:
                candidates.append(parsed)

    if not candidates:
        return result

    best = max(
        candidates,
        key=lambda item: quote_reply_text_score(
            " ".join([item.get("quote_sender", ""), item.get("quote_text", ""), item.get("quote_reply_text", "")])
        ),
    )
    result.update(best)
    return result


def history_content_type_from_row(row):
    """
    兼容不同版本时间线导出格式，把 `content_type_label` 反推回数字类型。
    """
    content_type = row.get("content_type")
    if content_type not in (None, ""):
        try:
            return int(content_type)
        except (TypeError, ValueError):
            pass

    label = str(row.get("content_type_label") or "").strip()
    if not label:
        return 0
    if label.startswith("类型"):
        suffix = label.removeprefix("类型").strip()
        if suffix.isdigit():
            return int(suffix)

    known_types = (0, 2, 14, 29, 101, 103, 501, 561, 565, 573, 1001, 1002, 1011, 1022)
    for known_type in known_types:
        if content_type_label(known_type) == label:
            return known_type
    return 0


def history_row_is_recovery_gap_marker(row):
    """
    判断历史时间线 CSV 中的行是否为缺口提示，避免重复整理时把提示当成真实消息合并。
    """
    label = str((row or {}).get("content_type_label") or "").strip()
    preview = normalize_preview_text((row or {}).get("message_preview") or (row or {}).get("preview") or "")
    return label == "疑似缺失消息" or preview.startswith("疑似缺失消息：message_id")


def history_send_time_from_row(row):
    send_time = row.get("send_time")
    if send_time not in (None, ""):
        try:
            return int(send_time)
        except (TypeError, ValueError):
            pass

    send_time_iso = str(row.get("send_time_iso") or "").strip()
    if not send_time_iso:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return int(dt.datetime.strptime(send_time_iso, fmt).timestamp())
        except ValueError:
            continue
    return 0


def history_sender_id_from_row(row):
    sender_id = str(row.get("sender_id") or "").strip()
    if sender_id:
        return sender_id

    sender_display = str(row.get("sender_display") or "").strip()
    match = re.search(r"<(\d{8,18})>", sender_display)
    if match:
        return match.group(1)
    if re.fullmatch(r"\d{8,18}", sender_display):
        return sender_display
    return ""


def build_organized_history_row(raw_row, conversation_id: str, timeline_path: Path, sequence_index: int, display_name: str = ""):
    """
    把历史时间线 CSV 统一映射成当前预览逻辑可复用的行结构。
    """
    send_time = history_send_time_from_row(raw_row)
    preview = normalize_preview_text(raw_row.get("message_preview") or raw_row.get("preview") or "")
    cached_media = str(raw_row.get("cached_media_file") or "").strip()
    exported_media = str(raw_row.get("exported_media_file") or "").strip()
    media_url = str(raw_row.get("media_url") or "").strip()
    media_uuid = str(raw_row.get("media_uuid") or "").strip()
    raw_content_head = str(raw_row.get("raw_content_head") or "").strip()
    extra_parts = [item for item in (media_uuid, media_url, raw_content_head) if item]
    local_parts = [item for item in (cached_media, exported_media) if item]

    message_id = raw_row.get("message_id")
    if str(message_id or "").strip().isdigit():
        message_id = int(str(message_id).strip())
    else:
        message_id = None

    sequence = raw_row.get("seq")
    if str(sequence or "").strip().isdigit():
        sequence = int(str(sequence).strip())
    else:
        sequence = sequence_index

    return {
        "_history_row_index": sequence_index,
        "_history_source": str(timeline_path),
        "_history_display_name": display_name,
        "message_id": message_id,
        "server_id": "",
        "sequence": sequence,
        "sender_id": history_sender_id_from_row(raw_row),
        "conversation_id": conversation_id,
        "content_type": history_content_type_from_row(raw_row),
        "send_time": send_time,
        "send_time_iso": str(raw_row.get("send_time_iso") or iso_from_ts(send_time)),
        "flag": int(raw_row.get("flag") or 0) if str(raw_row.get("flag") or "").strip() else 0,
        "content": preview or raw_content_head,
        "devinfo": "",
        "from_app_id": "",
        "msg_from_devinfo": "",
        "extra_content": "\n".join(extra_parts),
        "local_extra_content": "\n".join(local_parts),
        "client_id": "",
        "local_extra_content_translate_info": "",
        "local_extra_content_time_nlp": "",
        "local_extra_content_approval_nlp": "",
        "preview": preview,
    }


def history_title_quality(title: str):
    """
    给历史整理目录里的标题做一个简单质量评分，方便优先挑选更像真实群名的那份导出。
    """
    title = clean_title_candidate(title)
    if not title:
        return 0
    if looks_like_store_group_title(title):
        return 4
    if looks_like_external_group_plain_title(title):
        return 3
    if title.startswith("外部群_"):
        return 2
    if title.startswith(("R_", "R:", "S_", "S:")):
        return 1
    if looks_like_external_group_chat_sentence(title):
        return -2
    return 0


def history_summary_message_count(summary):
    """
    历史摘要里的消息条数可能是整数也可能是字符串，这里统一转成整数。
    """
    value = (summary or {}).get("message_count")
    text = str(value or "").strip()
    return int(text) if text.isdigit() else 0


def load_rows_from_history_timeline(timeline_path: Path, conversation_id: str, display_name: str = ""):
    """
    读取单个历史时间线文件，统一映射成当前预览逻辑可复用的行结构。
    """
    history_rows = []
    with timeline_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for sequence_index, raw_row in enumerate(reader, start=1):
            if history_row_is_recovery_gap_marker(raw_row):
                continue
            history_rows.append(
                build_organized_history_row(
                    raw_row,
                    conversation_id=conversation_id,
                    timeline_path=timeline_path,
                    sequence_index=sequence_index,
                    display_name=display_name,
                )
            )
    return history_rows


def load_organized_history_rows(source_dir=None, conversation_id: str = ""):
    """
    读取历史整理出的时间线 CSV，把仍然可用的旧消息重新纳入当前浏览和整理范围。
    """
    history_rows = []
    history_index = load_organized_history_index(source_dir)
    for current_id, entry in history_index.items():
        if conversation_id and current_id != conversation_id:
            continue
        display_name = entry.get("titles", [""])[0] if entry.get("titles") else ""
        for timeline_path in entry.get("timeline_paths", []):
            try:
                with timeline_path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    for sequence_index, raw_row in enumerate(reader, start=1):
                        if history_row_is_recovery_gap_marker(raw_row):
                            continue
                        history_rows.append(
                            build_organized_history_row(
                                raw_row,
                                conversation_id=current_id,
                                timeline_path=timeline_path,
                                sequence_index=sequence_index,
                                display_name=display_name,
                            )
                        )
            except Exception:
                continue
    return history_rows


def conversation_row_dedupe_key(row):
    """
    原始 sqlite 行和历史时间线行字段并不完全一致，这里按“媒体线索/预览文本/关键 ID”统一去重。
    """
    conversation_id = str(row.get("conversation_id") or "").strip()
    sender_id = str(row.get("sender_id") or "").strip()
    send_time = safe_int_value(row.get("send_time"))
    content_type = safe_int_value(row.get("content_type"))
    media = extract_media_metadata(row)
    media_signature = tuple(media.get("uuids", [])[:2] + media.get("urls", [])[:1] + media.get("file_names", [])[:1])
    if conversation_id and media_signature:
        return ("media", conversation_id, send_time, sender_id, content_type, media_signature)

    preview = clean_preview_text(row.get("preview") or "")
    if conversation_id and preview:
        return ("preview", conversation_id, send_time, sender_id, content_type, preview)

    message_id = row.get("message_id")
    if conversation_id and message_id not in (None, ""):
        return ("message_id", conversation_id, str(message_id))

    server_id = str(row.get("server_id") or "").strip()
    if conversation_id and server_id:
        return ("server_id", conversation_id, server_id)

    client_id = str(row.get("client_id") or "").strip()
    if conversation_id and client_id:
        return ("client_id", conversation_id, client_id)

    return ("fallback", conversation_id, send_time, sender_id, content_type)


def conversation_row_priority(row):
    """
    合并重复消息时优先保留原始 sqlite 行；如果只能用历史时间线，也尽量保留字段更多的一条。
    """
    score = 0
    if row.get("_history_source"):
        score -= 20
    for key in (
        "message_id",
        "server_id",
        "client_id",
        "content",
        "extra_content",
        "local_extra_content",
        "preview",
        "send_time",
        "sender_id",
    ):
        if row.get(key) not in (None, ""):
            score += 2
    score += len(clean_preview_text(row.get("preview") or ""))
    return (
        score,
        safe_int_value(row.get("send_time")),
        int(row.get("message_id") or 0) if str(row.get("message_id") or "").isdigit() else 0,
        int(row.get("sequence") or 0) if str(row.get("sequence") or "").isdigit() else 0,
    )


def merge_conversation_rows(rows, history_rows):
    """
    把当前原始恢复结果和历史整理结果合并，避免后一次小样本把之前已经恢复出的消息“看丢”。
    """
    merged = {}
    for row in list(history_rows) + list(rows):
        item = dict(row)
        key = conversation_row_dedupe_key(item)
        previous = merged.get(key)
        if previous is None or conversation_row_priority(item) >= conversation_row_priority(previous):
            merged[key] = item
    merged_rows = list(merged.values())
    merged_rows.sort(
        key=lambda item: (
            item.get("conversation_id") or "",
            safe_int_value(item.get("send_time")),
            int(item.get("message_id") or 0) if str(item.get("message_id") or "").isdigit() else 0,
            safe_int_value(item.get("_history_row_index")),
            safe_int_value(item.get("recovered_rowid")),
        )
    )
    return merged_rows


def copy_media_files(cached_files, images_dir: Path, copied_cache_map):
    exported = []
    images_dir.mkdir(parents=True, exist_ok=True)
    for source in cached_files:
        source_key = str(source).lower()
        if source_key in copied_cache_map:
            exported.append(copied_cache_map[source_key])
            continue

        target_name = source.name
        target = images_dir / target_name
        duplicate_index = 1
        while target.exists() and target.stat().st_size != source.stat().st_size:
            target = images_dir / f"{source.stem}_{duplicate_index}{source.suffix.lower()}"
            duplicate_index += 1
        if not target.exists():
            shutil.copy2(source, target)
        copied_cache_map[source_key] = target
        exported.append(target)
    return exported


def choose_writable_output_path(path: Path):
    candidate = path
    index = 1
    while True:
        try:
            with candidate.open("a+b"):
                pass
            return candidate
        except PermissionError:
            candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
            index += 1


def plausible_name(candidate: str):
    candidate = candidate.strip()
    if not candidate or len(candidate) < 2 or len(candidate) > 30:
        return False
    if candidate.isdigit():
        return False
    if candidate in GENERIC_NAME_BLOCKLIST:
        return False
    if any(part in candidate for part in GENERIC_NAME_PARTS):
        return False
    if "http" in candidate.lower() or "www" in candidate.lower():
        return False
    if not SAFE_NAME_RE.match(candidate):
        return False
    return True


def normalize_sender_alias_candidate(candidate: str):
    candidate = clean_title_candidate(candidate)
    candidate = candidate.lstrip(",，@").strip()
    return candidate


def sender_alias_identity_strength(candidate: str):
    candidate = normalize_sender_alias_candidate(candidate)
    strength = 0
    if PHONE_NUMBER_RE.search(candidate):
        strength += 4
    if any(token in candidate for token in ("商务", "官方", "老师", "总", "经理", "客服")):
        strength += 3
    if "." in candidate or "．" in candidate:
        strength += 2
    if ASCII_LETTER_RE.search(candidate) and CJK_RE.search(candidate):
        strength += 1
    if ASCII_LETTER_RE.search(candidate) and any(ch.isdigit() for ch in candidate):
        strength += 1
    if 2 <= len(candidate) <= 6 and CJK_RE.search(candidate):
        strength += 1
    return strength


def plausible_sender_alias(candidate: str):
    candidate = normalize_sender_alias_candidate(candidate)
    if not candidate or len(candidate) < 2 or len(candidate) > 40:
        return False
    if candidate.isdigit():
        return False
    if PURE_NUMBERISH_RE.match(candidate):
        return False
    lowered = candidate.lower()
    if "http" in lowered or "www" in lowered:
        return False
    if any(part.lower() in lowered for part in SENDER_ALIAS_BLOCKLIST_PARTS):
        return False
    if candidate in GENERIC_NAME_BLOCKLIST:
        return False
    if looks_like_store_group_title(candidate):
        return False
    if not SENDER_ALIAS_INLINE_RE.match(candidate):
        return False
    strength = sender_alias_identity_strength(candidate)
    if any(candidate.startswith(prefix) for prefix in SENDER_ALIAS_NEGATIVE_PREFIXES) and strength < 3:
        return False
    if any(part in candidate for part in SENDER_ALIAS_NEGATIVE_PARTS) and strength < 4:
        return False
    if candidate.endswith(SENDER_ALIAS_NEGATIVE_SUFFIXES) and strength < 3:
        return False
    if len(candidate) > 12 and strength < 3:
        return False
    return True


def sender_alias_priority(candidate: str):
    candidate = normalize_sender_alias_candidate(candidate)
    priority = sender_alias_identity_strength(candidate)
    if any(part in candidate for part in ("群", "通知", "培训", "直播", "年会", "家人们")):
        priority -= 4
    return priority, len(candidate)


def sender_alias_context_bonus(candidate: str, segments):
    if sender_alias_identity_strength(candidate) < 3 or len(segments) < 2:
        return 0
    second_segment = clean_title_candidate(segments[1])
    if second_segment.startswith(("0@", "M@", "@", "[", "------")):
        return 3
    return 0


def collect_sender_aliases(rows):
    sender_scores = defaultdict(Counter)
    sender_evidence = defaultdict(dict)

    for row in rows:
        sender_id = row["sender_id"] or ""
        preview = row["preview"]
        if not sender_id or not preview:
            continue

        for match in INTRO_NAME_RE.findall(preview):
            name = match.strip()
            if plausible_sender_alias(name):
                sender_scores[sender_id][name] += 10
                sender_evidence[sender_id].setdefault(name, f"自我介绍: {preview[:80]}")

        for match in JOIN_APPLY_NAME_RE.findall(preview):
            name = match.strip()
            if plausible_sender_alias(name):
                sender_scores[sender_id][name] += 8
                sender_evidence[sender_id].setdefault(name, f"加入申请: {preview[:80]}")

        match = SENDER_ALIAS_PREFIX_RE.match(preview)
        if match:
            name = normalize_sender_alias_candidate(match.group(1))
            if plausible_sender_alias(name):
                sender_scores[sender_id][name] += 5 + sender_alias_context_bonus(name, preview_segments(preview))
                sender_evidence[sender_id].setdefault(name, f"前缀命中: {preview[:80]}")

        segments = preview_segments(preview)
        if segments:
            first_segment = normalize_sender_alias_candidate(segments[0])
            if plausible_sender_alias(first_segment):
                score = 4 + sender_alias_context_bonus(first_segment, segments)
                if len(segments) >= 2:
                    second_segment_raw = clean_title_candidate(segments[1])
                    if second_segment_raw.startswith(("0@", "M@", "@")) and first_segment in second_segment_raw:
                        score = 11 + sender_alias_context_bonus(first_segment, segments)
                sender_scores[sender_id][first_segment] += score
                sender_evidence[sender_id].setdefault(first_segment, f"首段别名命中: {preview[:80]}")

    by_sender = defaultdict(list)
    for row in rows:
        by_sender[row["sender_id"]].append(row)

    for sender_id, sender_rows in by_sender.items():
        sender_rows.sort(key=lambda item: (item["send_time"] or 0, item["message_id"] or 0))
        previews = [item["preview"] for item in sender_rows]
        for idx, preview in enumerate(previews):
            normalized_preview = normalize_sender_alias_candidate(preview)
            if not preview or not plausible_sender_alias(normalized_preview):
                continue
            nearby = previews[max(0, idx - 2) : min(len(previews), idx + 3)]
            if any(other.startswith(normalized_preview + " |") for other in nearby if other != preview):
                sender_scores[sender_id][normalized_preview] += 7
                sender_evidence[sender_id].setdefault(normalized_preview, f"独立姓名+前缀关联: {normalized_preview}")

    sender_aliases = {}
    for sender_id, score_counter in sender_scores.items():
        if not score_counter:
            continue
        alias, score = max(score_counter.items(), key=lambda item: (item[1],) + sender_alias_priority(item[0]))
        if score >= 6 and (score >= 10 or sender_alias_identity_strength(alias) >= 3):
            sender_aliases[sender_id] = {
                "alias": alias,
                "score": score,
                "evidence": sender_evidence[sender_id].get(alias, ""),
            }
    return sender_aliases


def infer_self_user_id(rows, metadata=None):
    existing = str((metadata or {}).get("self_user_id", "") or "").strip()
    if existing:
        return existing

    docs_dir = detect_docs_dir()
    part_counter = Counter()
    conversation_counter = Counter()
    for row in rows:
        conversation_id = str(row.get("conversation_id") or "")
        if not conversation_id.startswith("S:"):
            continue
        tail = conversation_tail(conversation_id)
        if "_" not in tail:
            continue
        parts = [part for part in tail.split("_") if part]
        for part in parts:
            part_counter[part] += 1
        for part in set(parts):
            conversation_counter[part] += 1

    if not conversation_counter:
        return ""

    # 如果本机企业微信目录里已经存在某个账号文件夹，则优先把它识别为当前登录账号。
    if docs_dir and docs_dir.exists():
        matched_local_accounts = []
        for part in conversation_counter:
            if (docs_dir / part).exists():
                matched_local_accounts.append(part)
        if len(matched_local_accounts) == 1:
            return matched_local_accounts[0]

    self_id, count = max(conversation_counter.items(), key=lambda item: (item[1], part_counter[item[0]], item[0]))
    if count < 2:
        return ""
    return self_id


def decode_local_index_text(raw_value):
    if raw_value is None:
        return ""
    if isinstance(raw_value, str):
        text = raw_value
    else:
        text = bytes(raw_value).decode("utf-8", errors="ignore")
    return text.replace("\x00", " ")


def extract_local_contact_chunks(index_text: str):
    chunks = []
    seen = set()
    for match in LOCAL_INDEX_REPEAT_RE.finditer(index_text or ""):
        candidate = clean_title_candidate(match.group(1))
        if not candidate or not CJK_RE.search(candidate):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        chunks.append(candidate)
    return chunks


def local_contact_primary_priority(candidate: str):
    score = 0
    if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", candidate):
        score += 7
    elif re.fullmatch(r"[\u4e00-\u9fff]{2,6}", candidate):
        score += 5
    if candidate.startswith("小"):
        score -= 1
    if any(token in candidate for token in ("商务", "官方", "客服", "经理", "老师", "美工")):
        score -= 2
    if any(ch.isdigit() for ch in candidate):
        score -= 2
    if any(mark in candidate for mark in ("-", "（", "(")):
        score -= 1
    return score, -abs(len(candidate) - 3), -len(candidate)


def local_contact_secondary_priority(candidate: str, primary: str):
    if candidate == primary:
        return (-99, 0)
    score = 0
    if any(token in candidate for token in ("商务", "官方", "客服", "经理", "老师", "美工")):
        score += 4
    if any(ch.isdigit() for ch in candidate):
        score += 3
    if candidate.startswith("小"):
        score += 2
    if any(mark in candidate for mark in ("-", "（", "(")):
        score += 1
    if len(candidate) > len(primary):
        score += 1
    return score, len(candidate)


def build_local_contact_display_name(index_text: str):
    chunks = extract_local_contact_chunks(index_text)
    if not chunks:
        return ""

    primary = max(chunks, key=local_contact_primary_priority)
    secondary_candidates = [chunk for chunk in chunks if chunk != primary]
    secondary = ""
    if secondary_candidates:
        secondary = max(secondary_candidates, key=lambda item: local_contact_secondary_priority(item, primary))
        if local_contact_secondary_priority(secondary, primary)[0] < 0:
            secondary = ""

    if secondary and secondary != primary:
        return f"{primary}（{secondary}）"
    return primary


def local_contact_index_content_tables():
    """
    当前只接入 `id` 仍然可直接映射回企业微信用户 ID 的联系人索引表。
    其中 `idx_hot_user_content` 覆盖活跃联系人，`idx_user_content` 补足非高频成员。
    """
    return (
        "idx_hot_user_content",
        "idx_user_content",
    )


def usable_local_contact_row_id(row_id):
    """
    只接受像企业微信用户 ID 那样的长数字键，避免把顺序号型 rowid 错当成 sender_id。
    """
    return bool(re.fullmatch(r"\d{5,20}", str(row_id or "").strip()))


def extract_local_group_titles(index_text: str):
    titles = []
    seen = set()

    def append_title(candidate: str):
        title = clean_title_candidate(candidate)
        store_match = LOCAL_STORE_TITLE_CAPTURE_RE.search(title)
        if store_match:
            title = clean_title_candidate(store_match.group(1))
        if not title or title in seen:
            return
        if not (looks_like_store_group_title(title) or looks_like_external_group_plain_title(title)):
            return
        seen.add(title)
        titles.append(title)

    for match in LOCAL_STORE_TITLE_CAPTURE_RE.findall(index_text or ""):
        append_title(match)
    for match in LOCAL_GENERIC_TITLE_CAPTURE_RE.findall(index_text or ""):
        append_title(match)
    return titles


def load_local_wxwork_name_context(self_user_id: str):
    context = {
        "self_user_id": self_user_id or "",
        "local_contact_names": {},
        "local_group_titles": [],
    }
    if not self_user_id:
        return context

    docs_dir = detect_docs_dir()
    if not docs_dir or not docs_dir.exists():
        return context

    # 联系人索引目录跟随当前机器实际的企业微信文档目录，不能写死到某个用户名。
    data_index_db = docs_dir / self_user_id / "Index" / "data_index.db"
    if not data_index_db.exists():
        return context

    conn = None
    try:
        conn = sqlite3.connect(f"file:{data_index_db}?mode=ro", uri=True)
        conn.text_factory = bytes
        cur = conn.cursor()

        for table_name in local_contact_index_content_tables():
            try:
                rows = cur.execute(f"SELECT id, c0 FROM {table_name}")
            except sqlite3.DatabaseError:
                continue
            for row_id, raw_value in rows:
                row_id_text = str(row_id or "").strip()
                if not usable_local_contact_row_id(row_id_text):
                    continue
                if row_id_text in context["local_contact_names"]:
                    continue
                display_name = build_local_contact_display_name(decode_local_index_text(raw_value))
                if display_name:
                    context["local_contact_names"][row_id_text] = display_name

        titles = []
        for _row_id, raw_value in cur.execute("SELECT id, c0 FROM idx_conversation_content"):
            titles.extend(extract_local_group_titles(decode_local_index_text(raw_value)))
        context["local_group_titles"] = list(dict.fromkeys(titles))
    except Exception:
        return context
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return context


def normalize_history_conversation_id(value: str):
    """
    历史整理目录里有 `R_123` 这种旧格式，这里统一还原成当前代码使用的 `R:123`。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if ":" in text:
        prefix, tail = text.split(":", 1)
        return f"{prefix.upper()}:{tail}"
    if re.fullmatch(r"[A-Za-z]_.+", text):
        prefix, tail = text.split("_", 1)
        return f"{prefix.upper()}:{tail}"
    return text


def organized_history_root(source_dir):
    source_dir = Path(source_dir) if source_dir else default_source_dir()
    return source_dir / "organized_external_groups"


def iter_organized_history_timeline_paths(folder: Path):
    """
    兼容旧版固定 `时间线.csv` 和新版 `会话名称_聊天记录.csv`，保证历史补回能继续读取新导出。
    """
    paths = []
    for file_name in ORGANIZED_HISTORY_TIMELINE_FILE_NAMES:
        timeline_path = folder / file_name
        if timeline_path.exists():
            paths.append(timeline_path)
    paths.extend(sorted(folder.glob(ORGANIZED_HISTORY_CHAT_RECORD_PATTERN), key=lambda item: item.name))
    return list(dict.fromkeys(paths))


def load_organized_history_index(source_dir):
    """
    扫描历史整理目录，收集每个会话已有的标题候选和时间线文件，供预览和索引回填使用。
    """
    root = organized_history_root(source_dir)
    if not root.exists():
        return {}

    history_index = {}
    for folder in sorted(root.iterdir(), key=lambda item: item.name):
        if not folder.is_dir():
            continue

        folder_display_name = ""
        conversation_id = ""
        if "__" in folder.name:
            folder_display_name, raw_tail = folder.name.rsplit("__", 1)
            folder_display_name = clean_title_candidate(folder_display_name)
            conversation_id = normalize_history_conversation_id(raw_tail)
        else:
            conversation_id = normalize_history_conversation_id(folder.name)

        summary = {}
        for file_name in ORGANIZED_HISTORY_SUMMARY_FILE_NAMES:
            summary_path = folder / file_name
            if not summary_path.exists():
                continue
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                break
            except Exception:
                continue

        if summary.get("conversation_id"):
            conversation_id = normalize_history_conversation_id(summary.get("conversation_id"))
        if not conversation_id:
            continue

        entry = history_index.setdefault(
            conversation_id,
            {
                "conversation_id": conversation_id,
                "titles": [],
                "folders": [],
                "timeline_paths": [],
                "summaries": [],
                "exports": [],
            },
        )
        entry["folders"].append(folder)
        if summary:
            entry["summaries"].append(summary)

        export_item = {
            "folder": folder,
            "summary": summary,
            "timeline_paths": [],
            "display_title": "",
            "message_count": history_summary_message_count(summary),
        }
        for candidate in (summary.get("conversation_display_name", "") if summary else "", folder_display_name):
            candidate = clean_title_candidate(candidate)
            if candidate and candidate not in entry["titles"]:
                entry["titles"].append(candidate)
            if not export_item["display_title"] or history_title_quality(candidate) > history_title_quality(export_item["display_title"]):
                export_item["display_title"] = candidate

        for timeline_path in iter_organized_history_timeline_paths(folder):
            if timeline_path not in entry["timeline_paths"]:
                entry["timeline_paths"].append(timeline_path)
            if timeline_path not in export_item["timeline_paths"]:
                export_item["timeline_paths"].append(timeline_path)

        if export_item["timeline_paths"] or summary:
            entry["exports"].append(export_item)

    return history_index


def choose_preferred_history_export(entry):
    """
    同一会话可能存在多份历史导出，这里优先选“消息更多、标题质量更高、文件更新”的那一份。
    """
    candidates = []
    for export_item in entry.get("exports", []):
        timeline_paths = list(export_item.get("timeline_paths", []))
        if not timeline_paths:
            continue
        best_timeline_path = max(
            timeline_paths,
            key=lambda item: (
                history_summary_message_count(export_item.get("summary")),
                item.stat().st_size if item.exists() else 0,
                item.stat().st_mtime if item.exists() else 0,
                item.name,
            ),
        )
        candidates.append(
            (
                history_summary_message_count(export_item.get("summary")),
                history_title_quality(export_item.get("display_title") or ""),
                best_timeline_path.stat().st_mtime if best_timeline_path.exists() else 0,
                best_timeline_path.stat().st_size if best_timeline_path.exists() else 0,
                export_item.get("display_title") or "",
                str(best_timeline_path),
                export_item,
                best_timeline_path,
            )
        )
    if not candidates:
        return None, None
    best_item = max(candidates)
    return best_item[-2], best_item[-1]


def load_useful_organized_history_rows(source_dir=None, conversation_ids=None, current_counts=None):
    """
    只在“历史导出明显比当前原始恢复更完整”时，才把该会话的最佳历史时间线补回来。
    这样既能尽量补回外部群旧消息，也避免把所有旧整理结果再次整批混入当前浏览。
    """
    current_counts = current_counts or {}
    history_rows = []
    history_sources = []
    history_index = load_organized_history_index(source_dir)
    if conversation_ids is None:
        conversation_ids = history_index.keys()

    for current_id in conversation_ids:
        entry = history_index.get(current_id)
        if not entry:
            continue
        export_item, timeline_path = choose_preferred_history_export(entry)
        if not export_item or not timeline_path:
            continue

        current_count = int(current_counts.get(current_id, 0) or 0)
        history_count = history_summary_message_count(export_item.get("summary"))
        if history_count and history_count <= current_count:
            continue

        display_name = clean_title_candidate(export_item.get("display_title") or "")
        try:
            candidate_rows = load_rows_from_history_timeline(timeline_path, current_id, display_name=display_name)
        except Exception:
            continue
        if len(candidate_rows) <= current_count:
            continue

        history_rows.extend(candidate_rows)
        history_sources.append(str(timeline_path))

    return history_rows, history_sources


def build_historical_group_titles_map(source_dir):
    """
    只把看起来像真实群名的历史标题留下来，避免把旧版本误判过的聊天句子再次带回来。
    """
    title_map = {}
    for conversation_id, entry in load_organized_history_index(source_dir).items():
        titles = []
        for title in entry.get("titles", []):
            if not title:
                continue
            if not (looks_like_store_group_title(title) or looks_like_external_group_plain_title(title)):
                continue
            if title not in titles:
                titles.append(title)
        if titles:
            title_map[conversation_id] = titles
    return title_map


def build_naming_context(rows, metadata=None, source_dir=None):
    self_user_id = infer_self_user_id(rows, metadata=metadata)
    context = load_local_wxwork_name_context(self_user_id)
    context["self_user_id"] = self_user_id
    context["historical_group_titles_map"] = build_historical_group_titles_map(source_dir)
    context["preferred_raw_group_titles_map"] = dict((metadata or {}).get("preferred_raw_group_titles_map", {}) or {})
    return context


def split_single_chat_participants(conversation_id: str, self_user_id=""):
    parts = []
    tail = conversation_tail(conversation_id)
    if "_" in tail:
        for part in tail.split("_"):
            if part and part not in parts:
                parts.append(part)

    if self_user_id and self_user_id in parts:
        other_ids = [part for part in parts if part != self_user_id]
    elif len(parts) == 2:
        other_ids = [parts[1]]
    else:
        other_ids = list(parts)
    return other_ids


def display_name_primary_part(display_name: str):
    text = str(display_name or "").strip()
    if not text:
        return ""
    return re.split(r"[（(]", text, maxsplit=1)[0].strip()


def guess_single_chat_name(conversation_id: str, rows, metadata, sender_aliases, naming_context=None, counterpart_id=""):
    self_id = str((naming_context or {}).get("self_user_id", "") or "")
    local_contact_names = (naming_context or {}).get("local_contact_names", {})
    other_ids = []
    if counterpart_id:
        other_ids = [counterpart_id]
    else:
        other_ids = split_single_chat_participants(conversation_id, self_user_id=self_id)

    for sender_id in other_ids:
        local_name = local_contact_names.get(sender_id)
        if local_name:
            return local_name, f"本机联系人索引命中（ID {sender_id}）"
        guessed = sender_aliases.get(sender_id, {})
        alias = guessed.get("alias", "")
        if alias and sender_alias_identity_strength(alias) >= 3:
            return alias, f"单聊对方推测名（ID {sender_id}）"

    sender_stats = Counter(row["sender_id"] for row in rows if row.get("sender_id"))
    for sender_id, _count in sender_stats.most_common():
        if sender_id == self_id:
            continue
        local_name = local_contact_names.get(sender_id)
        if local_name:
            return local_name, f"本机联系人索引命中（ID {sender_id}）"
        guessed = sender_aliases.get(sender_id, {})
        alias = guessed.get("alias", "")
        if alias and sender_alias_identity_strength(alias) >= 3:
            return alias, f"消息发送人推测名（ID {sender_id}）"

    if other_ids:
        return f"单聊_{other_ids[0]}", "会话ID中的对方账号"

    title_scores, title_evidence = collect_conversation_title_candidates(rows, "single_chat")
    title, evidence = choose_best_title(title_scores, title_evidence)
    if title and plausible_name(title):
        return title, evidence

    return f"单聊_{safe_path_component(conversation_id, fallback='unknown', max_len=24)}", "未识别出更合适名称"


def external_group_title_match_tokens(title: str):
    tokens = []
    seen = set()
    for segment in re.split(r"[-()（）\s]+", clean_title_candidate(title)):
        segment = segment.strip()
        if not segment or segment == "极修匠":
            continue
        for token in re.findall(r"[A-Z]{1,4}\d{2,8}", segment):
            if token not in seen:
                seen.add(token)
                tokens.append(token)
        chinese_token = re.sub(r"(街道店|大厦店|区店|门店|店|街)$", "", segment)
        chinese_token = chinese_token.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ+-")
        chinese_token = clean_title_candidate(chinese_token)
        if not chinese_token or chinese_token in {"极修匠", "门店", "街道", "大厦"}:
            continue
        if CJK_RE.search(chinese_token) and chinese_token not in seen:
            seen.add(chinese_token)
            tokens.append(chinese_token)
    return tokens


def local_external_group_title_match_score(title: str, rows):
    haystack = " ".join(row.get("preview", "") for row in rows if row.get("preview"))
    score = 0
    for token in external_group_title_match_tokens(title):
        if token and token in haystack:
            score += max(2, min(len(token), 6))
    return score


def extract_known_participant_ids(rows, self_user_id="", known_ids=None):
    known_ids = {str(item) for item in (known_ids or []) if str(item)}
    participant_ids = []
    seen = set()
    for row in rows:
        candidates = [str(row.get("sender_id") or "")]
        candidates.extend(NUMERIC_ID_RE.findall(row.get("preview", "") or ""))
        for candidate in candidates:
            if not candidate or candidate == self_user_id or candidate in seen:
                continue
            if known_ids and candidate not in known_ids:
                continue
            seen.add(candidate)
            participant_ids.append(candidate)
    return participant_ids


def guess_external_group_name(conversation_id: str, rows, sender_aliases, naming_context=None):
    title_scores, title_evidence = collect_conversation_title_candidates(rows, "external_group")
    ranked_titles = sorted(title_scores.items(), key=lambda item: (item[1],) + title_priority(item[0]), reverse=True)
    preferred_raw_title = ((naming_context or {}).get("preferred_raw_group_titles_map", {}) or {}).get(conversation_id, "")
    historical_group_titles = ((naming_context or {}).get("historical_group_titles_map", {}) or {}).get(conversation_id, [])
    best_historical_title = ("", 0)
    if historical_group_titles:
        scored_titles = []
        for title in historical_group_titles:
            if not (looks_like_store_group_title(title) or looks_like_external_group_plain_title(title)):
                continue
            score = local_external_group_title_match_score(title, rows)
            if looks_like_store_group_title(title):
                score += 6
            elif looks_like_external_group_plain_title(title):
                score += 2
            scored_titles.append((score, title))
        if scored_titles:
            best_historical_title = max(scored_titles, key=lambda item: (item[0],) + title_priority(item[1]))

    for title, score in ranked_titles:
        if looks_like_store_group_title(title) and external_group_title_confident(title, score):
            return title, title_evidence.get(title, "")

    if preferred_raw_title and looks_like_store_group_title(preferred_raw_title):
        return preferred_raw_title, f"标准原始恢复命中: {preferred_raw_title}"

    if best_historical_title[1] and looks_like_store_group_title(best_historical_title[1]):
        return best_historical_title[1], f"历史整理结果命中: {best_historical_title[1]}"

    for title, score in ranked_titles:
        if external_group_title_confident(title, score):
            return title, title_evidence.get(title, "")

    local_group_titles = (naming_context or {}).get("local_group_titles", [])
    if local_group_titles:
        scored_titles = []
        for title in local_group_titles:
            score = local_external_group_title_match_score(title, rows)
            if score > 0:
                scored_titles.append((score, title))
        if scored_titles:
            score, title = max(scored_titles, key=lambda item: (item[0],) + title_priority(item[1]))
            if score >= 4:
                return title, f"本机会话索引命中: {title}"

    if best_historical_title[1]:
        score, title = best_historical_title
        if score >= 2 or looks_like_store_group_title(title):
            return title, f"历史整理结果命中: {title}"

    sender_stats = Counter(row["sender_id"] for row in rows if row.get("sender_id"))
    local_contact_names = (naming_context or {}).get("local_contact_names", {})
    self_user_id = str((naming_context or {}).get("self_user_id", "") or "")
    alias_names = []
    known_ids = set(local_contact_names) | {str(item) for item in sender_aliases}
    for participant_id in extract_known_participant_ids(rows, self_user_id=self_user_id, known_ids=known_ids):
        display = local_contact_names.get(participant_id) or sender_aliases.get(participant_id, {}).get("alias", "")
        name = display_name_primary_part(display)
        if name and name not in alias_names:
            alias_names.append(name)
        if len(alias_names) >= 3:
            break

    for sender_id, _count in sender_stats.most_common():
        guessed = sender_aliases.get(sender_id, {})
        alias = guessed.get("alias")
        if not alias:
            continue
        if guessed.get("score", 0) < 12 or sender_alias_identity_strength(alias) < 3:
            continue
        alias_name = display_name_primary_part(alias)
        if alias_name and alias_name not in alias_names:
            alias_names.append(alias_name)
        if len(alias_names) >= 2:
            break
    if len(alias_names) >= 2:
        return f"{alias_names[0]}、{alias_names[1]}等{len(alias_names)}人群", "按会话成员推测，未恢复到明确群名"

    return f"外部群_{conversation_tail(conversation_id)}", "当前恢复结果里未发现明确群名"


def guess_service_conversation_name(conversation_id: str, rows, kind: str):
    fixed_defaults = {
        "mail": "邮件助手",
        "approval": "审批提醒",
        "internal_group": "内部群",
    }
    if kind in fixed_defaults:
        return fixed_defaults[kind], "固定类型名称"

    title_scores, title_evidence = collect_conversation_title_candidates(rows, kind)
    title, evidence = choose_best_title(title_scores, title_evidence)
    if title:
        return title, evidence

    return f"{conversation_kind_label(kind)}_{conversation_tail(conversation_id)}", "按会话类型生成默认名称"


def guess_conversation_profile(conversation_id: str, rows, metadata=None, naming_context=None):
    kind, kind_evidence = infer_conversation_kind(conversation_id, rows)
    sender_aliases = collect_sender_aliases(rows)
    counterpart_id = ""

    if kind == "single_chat":
        self_user_id = str((naming_context or {}).get("self_user_id", "") or "")
        participants = split_single_chat_participants(conversation_id, self_user_id=self_user_id)
        counterpart_id = participants[0] if participants else ""
        display_name, name_evidence = guess_single_chat_name(
            conversation_id,
            rows,
            metadata,
            sender_aliases,
            naming_context=naming_context,
            counterpart_id=counterpart_id,
        )
    elif kind == "external_group":
        display_name, name_evidence = guess_external_group_name(
            conversation_id,
            rows,
            sender_aliases,
            naming_context=naming_context,
        )
    else:
        display_name, name_evidence = guess_service_conversation_name(conversation_id, rows, kind)

    display_name = safe_path_component(display_name, fallback=conversation_kind_label(kind), max_len=48)
    sender_aliases = propagate_external_group_sender_aliases(
        {"conversation_kind": kind, "conversation_display_name": display_name},
        rows,
        sender_aliases,
        metadata=metadata,
        naming_context=naming_context,
    )
    return {
        "conversation_kind": kind,
        "conversation_kind_evidence": kind_evidence,
        "conversation_display_name": display_name,
        "conversation_name_evidence": name_evidence,
        "folder_name": build_conversation_folder_name(conversation_id, display_name),
        "counterpart_id": counterpart_id,
        "sender_aliases": sender_aliases,
    }


def load_metadata_from_cursor(cur):
    tables = {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "metadata" not in tables:
        return {}
    return dict(cur.execute("SELECT key, value FROM metadata").fetchall())


def decode_loaded_row(row, source_sqlite=None):
    item = dict(row)
    if source_sqlite:
        item["_source_sqlite"] = str(source_sqlite)
    item["preview"] = decode_preview(item.get("content"))
    item["send_time_iso"] = iso_from_ts(item.get("send_time"))
    return item


def load_rows_from_single_sqlite(sqlite_path: Path):
    conn = None
    try:
        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = []
        for row in cur.execute(
            """
            SELECT *
            FROM message_table_partial
            ORDER BY conversation_id, send_time, message_id, recovered_rowid
            """
        ):
            rows.append(decode_loaded_row(row, source_sqlite=sqlite_path))
        metadata = load_metadata_from_cursor(cur)
        return rows, metadata
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def load_conversation_rows_from_single_sqlite(sqlite_path: Path, conversation_id: str):
    conn = None
    try:
        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = []
        for row in cur.execute(
            """
            SELECT *
            FROM message_table_partial
            WHERE conversation_id = ?
            ORDER BY send_time, message_id, recovered_rowid
            """,
            (conversation_id,),
        ):
            rows.append(decode_loaded_row(row, source_sqlite=sqlite_path))
        metadata = load_metadata_from_cursor(cur)
        return rows, metadata
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def merged_metadata_with_sources(metadata, sqlite_paths, history_sources=None):
    metadata = dict(metadata or {})
    if sqlite_paths:
        metadata["merged_source_sqlites"] = [str(path) for path in sqlite_paths]
        preferred_titles = preferred_raw_group_titles_map(sqlite_paths)
        if preferred_titles:
            metadata["preferred_raw_group_titles_map"] = preferred_titles
    if history_sources:
        metadata["merged_source_history_timelines"] = list(dict.fromkeys(history_sources))
    return metadata


def load_rows(sqlite_path: Path, source_dir=None):
    rows, metadata = load_rows_from_single_sqlite(sqlite_path)
    related_sqlites = discover_related_raw_sqlites(sqlite_path, source_dir=source_dir, metadata=metadata)
    merged_rows = list(rows)
    for candidate in related_sqlites:
        if Path(candidate).resolve() == Path(sqlite_path).resolve():
            continue
        extra_rows, _extra_metadata = load_rows_from_single_sqlite(candidate)
        if extra_rows:
            merged_rows = merge_conversation_rows(merged_rows, extra_rows)
    current_counts = Counter(str(row.get("conversation_id") or "").strip() for row in merged_rows if row.get("conversation_id"))
    history_rows, history_sources = load_useful_organized_history_rows(
        source_dir=source_dir,
        conversation_ids=list(current_counts.keys()),
        current_counts=current_counts,
    )
    if history_rows:
        merged_rows = merge_conversation_rows(merged_rows, history_rows)
    return merged_rows, merged_metadata_with_sources(metadata, related_sqlites, history_sources=history_sources)


def load_conversation_rows(sqlite_path: Path, conversation_id: str, source_dir=None):
    rows, metadata = load_conversation_rows_from_single_sqlite(sqlite_path, conversation_id)
    related_sqlites = discover_related_raw_sqlites(sqlite_path, source_dir=source_dir, metadata=metadata)
    merged_rows = list(rows)
    for candidate in related_sqlites:
        if Path(candidate).resolve() == Path(sqlite_path).resolve():
            continue
        extra_rows, _extra_metadata = load_conversation_rows_from_single_sqlite(candidate, conversation_id)
        if extra_rows:
            merged_rows = merge_conversation_rows(merged_rows, extra_rows)
    history_rows, history_sources = load_useful_organized_history_rows(
        source_dir=source_dir,
        conversation_ids=[conversation_id],
        current_counts={conversation_id: len(merged_rows)},
    )
    if history_rows:
        merged_rows = merge_conversation_rows(merged_rows, history_rows)
    return merged_rows, merged_metadata_with_sources(metadata, related_sqlites, history_sources=history_sources)


def emit(logger, message):
    if logger:
        logger(message)


def build_conversation_index(rows, metadata=None, naming_context=None, source_dir=None):
    naming_context = naming_context or build_naming_context(rows, metadata=metadata, source_dir=source_dir)
    grouped = defaultdict(list)
    for row in rows:
        # 内存页恢复出来的半截记录可能没有 conversation_id，这类记录无法归属到具体聊天，不能参与会话列表。
        conversation_id = str(row.get("conversation_id") or "").strip()
        if not conversation_id:
            continue
        grouped[conversation_id].append(row)

    index_rows = []
    for conversation_id, conv_rows in grouped.items():
        first_ts = min((row["send_time"] for row in conv_rows if row["send_time"]), default=0)
        last_ts = max((row["send_time"] for row in conv_rows if row["send_time"]), default=0)
        sender_count = len({row["sender_id"] for row in conv_rows if row.get("sender_id")})
        profile = guess_conversation_profile(conversation_id, conv_rows, metadata=metadata, naming_context=naming_context)
        kind = profile["conversation_kind"]
        index_rows.append(
            {
                "conversation_id": conversation_id,
                "conversation_kind": kind,
                "conversation_kind_evidence": profile.get("conversation_kind_evidence", ""),
                "conversation_display_name": profile["conversation_display_name"],
                "conversation_name_evidence": profile["conversation_name_evidence"],
                "counterpart_id": profile.get("counterpart_id", ""),
                "folder_name": profile["folder_name"],
                "conversation_prefix": conversation_prefix(conversation_id),
                "conversation_kind_label": conversation_kind_label(kind),
                "message_count": len(conv_rows),
                "sender_count": sender_count,
                "first_time": first_ts,
                "first_time_iso": iso_from_ts(first_ts),
                "last_time": last_ts,
                "last_time_iso": iso_from_ts(last_ts),
            }
        )
    index_rows.sort(
        key=lambda item: (
            str(item.get("conversation_kind") or "other"),
            -int(item.get("message_count") or 0),
            str(item.get("conversation_id") or ""),
        )
    )
    return grouped, index_rows


def resolve_input_sqlite(input_sqlite="", source_dir=None):
    if input_sqlite:
        return Path(input_sqlite)
    return latest_sqlite_in_dir(Path(source_dir) if source_dir else default_source_dir())


def list_conversations(input_sqlite="", source_dir=None, prefix=""):
    sqlite_path = resolve_input_sqlite(input_sqlite, source_dir)
    rows, metadata = load_rows(sqlite_path, source_dir=source_dir)
    naming_context = build_naming_context(rows, metadata=metadata, source_dir=source_dir)
    _grouped, index_rows = build_conversation_index(rows, metadata=metadata, naming_context=naming_context, source_dir=source_dir)
    if prefix:
        index_rows = [item for item in index_rows if conversation_matches_prefix(item["conversation_id"], prefix)]
    return {
        "sqlite_path": str(sqlite_path),
        "metadata": metadata,
        "self_user_id": naming_context.get("self_user_id", ""),
        "conversations": index_rows,
    }


def preferred_sender_name(sender_id, sender_aliases, local_contact_names=None):
    sender_id = str(sender_id or "")
    local_contact_names = local_contact_names or {}
    if sender_id and sender_id in local_contact_names:
        return local_contact_names[sender_id], f"本机联系人索引命中（ID {sender_id}）"
    guessed = sender_aliases.get(sender_id, {})
    alias = guessed.get("alias", "")
    if alias:
        return alias, guessed.get("evidence", "")
    return "", ""


def sender_display_text(sender_id, sender_aliases, local_contact_names=None):
    sender_id = str(sender_id or "")
    sender_name, sender_evidence = preferred_sender_name(sender_id, sender_aliases, local_contact_names)
    if sender_name and sender_id:
        return f"{sender_name} <{sender_id}>", sender_name, sender_evidence
    if sender_id:
        return sender_id, "", ""
    return "未知发送人", "", ""


def sender_role_label(role: str):
    """
    把内部使用的发送者角色编码转换成导出文件里更容易阅读的中文标签。
    """
    return SENDER_ROLE_LABELS.get(role, SENDER_ROLE_LABELS["unknown"])


def enterprise_sender_id_prefix(metadata=None, self_user_id=""):
    """
    从当前账号 ID 推出企业成员 ID 的高置信前缀；长度不足时不使用前缀，避免把短测试 ID 误判成规则。
    """
    values = [self_user_id]
    metadata = metadata or {}
    values.extend([metadata.get("self_user_id", ""), metadata.get("corp_id", "")])
    for value in values:
        text = str(value or "").strip()
        if re.fullmatch(r"\d{12,18}", text):
            return text[:6]
    return ""


def sender_name_has_staff_hint(sender_name: str):
    """
    用发送人名称中的商务侧关键词辅助判断角色，只做补充，不覆盖更高置信的 ID 判断。
    """
    text = str(sender_name or "").strip()
    if not text:
        return False
    return any(part in text for part in STAFF_NAME_HINT_PARTS)


def infer_sender_role(sender_id, sender_name="", conversation_profile=None, metadata=None, self_user_id=""):
    """
    给导出语料补发送者角色，重点区分商务/企业成员与外部联系人，无法高置信判断时保留 unknown。
    """
    sender_id = str(sender_id or "").strip()
    if not sender_id:
        return "unknown", "发送人 ID 为空"

    metadata = metadata or {}
    self_user_id = str(self_user_id or metadata.get("self_user_id", "") or "").strip()
    profile = conversation_profile or {}
    kind = str(profile.get("conversation_kind", "") or "")
    counterpart_id = str(profile.get("counterpart_id", "") or "").strip()
    enterprise_prefix = enterprise_sender_id_prefix(metadata=metadata, self_user_id=self_user_id)

    if self_user_id and sender_id == self_user_id:
        return "staff", "发送人是当前登录企业微信账号"

    if enterprise_prefix and sender_id.startswith(enterprise_prefix):
        return "staff", f"发送人 ID 命中企业成员前缀 {enterprise_prefix}"

    if sender_name_has_staff_hint(sender_name):
        return "staff", "发送人名称命中商务侧关键词"

    if kind == "external_group" and NUMERIC_ID_RE.fullmatch(sender_id):
        if enterprise_prefix and not sender_id.startswith(enterprise_prefix):
            return "external_contact", f"外部群内发送人未命中企业成员前缀 {enterprise_prefix}"

    if kind == "single_chat" and counterpart_id and sender_id == counterpart_id:
        if enterprise_prefix and not sender_id.startswith(enterprise_prefix):
            return "external_contact", "单聊对方未命中企业成员前缀"

    return "unknown", "当前本地索引无法高置信判断"


def propagate_external_group_sender_aliases(conversation_profile, rows, sender_aliases, metadata=None, local_contact_names=None, naming_context=None):
    """
    外部群里同一个外部联系人有时会残留多个 788 开头 sender_id。
    当同群只有一个高置信外部联系人名称时，把该名称沿用到高频未知外部 ID，避免时间线继续显示纯数字。
    """
    profile = conversation_profile or {}
    if profile.get("conversation_kind") != "external_group":
        return sender_aliases or {}

    merged_aliases = {str(sender_id): dict(alias_info) for sender_id, alias_info in (sender_aliases or {}).items()}
    local_contact_names = dict(local_contact_names or (naming_context or {}).get("local_contact_names", {}) or {})
    metadata = metadata or {}
    self_user_id = str((naming_context or {}).get("self_user_id", "") or "") or infer_self_user_id(rows, metadata=metadata)
    sender_stats = Counter(str(row.get("sender_id") or "") for row in rows if row.get("sender_id"))

    alias_sources = []
    for sender_id, alias_info in merged_aliases.items():
        alias = str(alias_info.get("alias") or "").strip()
        if not alias or sender_id in local_contact_names:
            continue
        role, _role_evidence = infer_sender_role(
            sender_id,
            sender_name=alias,
            conversation_profile=profile,
            metadata=metadata,
            self_user_id=self_user_id,
        )
        if role != "external_contact":
            continue
        try:
            score = int(alias_info.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        if score >= 10 and sender_alias_identity_strength(alias) >= 3:
            alias_sources.append((sender_id, alias, score, alias_info.get("evidence", "")))

    unique_aliases = {}
    for sender_id, alias, score, evidence in alias_sources:
        unique_aliases.setdefault(alias, []).append((sender_id, score, evidence))
    if len(unique_aliases) != 1:
        return merged_aliases

    alias, source_items = next(iter(unique_aliases.items()))
    source_id, source_score, _source_evidence = max(source_items, key=lambda item: (item[1], sender_stats.get(item[0], 0)))
    source_prefix = source_id[:6] if re.fullmatch(r"\d{8,18}", source_id) else ""

    for sender_id, count in sender_stats.items():
        if sender_id in local_contact_names:
            continue
        if merged_aliases.get(sender_id, {}).get("alias"):
            continue
        if count < 3:
            continue
        if source_prefix and not sender_id.startswith(source_prefix):
            continue
        role, _role_evidence = infer_sender_role(
            sender_id,
            conversation_profile=profile,
            metadata=metadata,
            self_user_id=self_user_id,
        )
        if role != "external_contact":
            continue
        merged_aliases[sender_id] = {
            "alias": alias,
            "score": source_score,
            "evidence": f"外部群唯一高置信联系人名称沿用：来源 {source_id}，当前 ID {sender_id} 共 {count} 条消息",
        }

    return merged_aliases


def export_sender_name_context(conversation_profile, rows, metadata=None):
    naming_context = build_naming_context(rows, metadata=metadata)
    local_contact_names = dict(naming_context.get("local_contact_names", {}))
    counterpart_id = str((conversation_profile or {}).get("counterpart_id", "") or "")
    display_name = str((conversation_profile or {}).get("conversation_display_name", "") or "").strip()
    conversation_kind = str((conversation_profile or {}).get("conversation_kind", "") or "")

    # 单聊场景下，如果已经识别出对方名称，则导出时也把这个名称回填给对方 sender_id。
    if conversation_kind == "single_chat" and counterpart_id and display_name and counterpart_id not in local_contact_names:
        local_contact_names[counterpart_id] = display_name

    return local_contact_names


def get_conversation_preview(
    conversation_id: str,
    input_sqlite="",
    source_dir=None,
    self_user_id="",
    limit=0,
):
    if not conversation_id:
        raise ValueError("conversation_id 不能为空。")

    source_dir = Path(source_dir) if source_dir else default_source_dir()
    sqlite_path = resolve_input_sqlite(input_sqlite, source_dir)
    rows, metadata = load_conversation_rows(sqlite_path, conversation_id, source_dir=source_dir)
    if not rows:
        raise ValueError(f"未找到会话：{conversation_id}")

    if self_user_id:
        metadata = dict(metadata)
        metadata["self_user_id"] = self_user_id

    naming_context = build_naming_context(rows, metadata=metadata, source_dir=source_dir)
    profile = guess_conversation_profile(conversation_id, rows, metadata=metadata, naming_context=naming_context)
    sender_aliases = profile.get("sender_aliases") or collect_sender_aliases(rows)
    local_contact_names = naming_context.get("local_contact_names", {})
    sender_aliases = propagate_external_group_sender_aliases(
        profile,
        rows,
        sender_aliases,
        metadata=metadata,
        local_contact_names=local_contact_names,
        naming_context=naming_context,
    )
    sender_stats = Counter(str(row.get("sender_id") or "") for row in rows if row.get("sender_id"))
    first_ts = min((row["send_time"] for row in rows if row["send_time"]), default=0)
    last_ts = max((row["send_time"] for row in rows if row["send_time"]), default=0)

    top_senders = []
    for sender_id, count in sender_stats.most_common(8):
        sender_display, sender_name, sender_evidence = sender_display_text(sender_id, sender_aliases, local_contact_names)
        top_senders.append(
            {
                "sender_id": sender_id,
                "sender_name": sender_name,
                "sender_display": sender_display,
                "message_count": count,
                "evidence": sender_evidence,
            }
        )

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 0

    display_rows = rows_with_recovery_gap_markers(rows)
    recovery_gap_marker_count = sum(1 for row in display_rows if row_is_recovery_gap_marker(row))

    if limit <= 0:
        preview_rows = display_rows
    elif len(display_rows) > limit:
        preview_rows = display_rows[-limit:]
    else:
        preview_rows = display_rows

    start_seq = len(display_rows) - len(preview_rows) + 1
    docs_dir = detect_docs_dir()
    media_cache_index = load_media_cache_index(docs_dir, extra_sources=preview_media_search_roots(source_dir))
    preview_messages = []
    for seq, row in enumerate(preview_rows, start=start_seq):
        media_metadata = extract_media_metadata(row)
        media_files = []
        if media_metadata["has_media"]:
            media_files = [str(path) for path in resolve_cached_media_files(media_metadata, media_cache_index)]
        missing_media_file_count = max(0, int(media_metadata.get("media_item_count") or 0) - len(media_files))
        if row_is_recovery_gap_marker(row):
            sender_display, sender_name, sender_evidence = "恢复提示", "恢复提示", "message_id 连续性检查"
        else:
            sender_display, sender_name, sender_evidence = sender_display_text(row.get("sender_id"), sender_aliases, local_contact_names)
        preview_text = build_message_preview_text(row, media_metadata=media_metadata)
        quote_metadata = extract_quote_reply_metadata(row)

        preview_messages.append(
            {
                "seq": seq,
                "message_id": row.get("message_id", ""),
                "send_time": row.get("send_time", ""),
                "send_time_iso": row.get("send_time_iso", ""),
                "sender_id": str(row.get("sender_id") or ""),
                "sender_name_guess": sender_name,
                "sender_display": sender_display,
                "sender_evidence": sender_evidence,
                "content_type": row.get("content_type", ""),
                "content_type_label": media_metadata["content_type_label"],
                "media_kind": media_metadata["media_kind"],
                "has_media": media_metadata["has_media"],
                "media_files": media_files,
                "media_file_count": len(media_files),
                "media_item_count": media_metadata.get("media_item_count", 0),
                "missing_media_file_count": missing_media_file_count,
                "flag": row.get("flag", ""),
                "preview": preview_text,
                "is_quote_reply": quote_metadata["is_quote_reply"],
                "quote_sender": quote_metadata["quote_sender"],
                "quote_text": quote_metadata["quote_text"],
                "quote_reply_text": quote_metadata["quote_reply_text"],
                "is_recovery_gap_marker": row_is_recovery_gap_marker(row),
            }
        )

    return {
        "sqlite_path": str(sqlite_path),
        "metadata": metadata,
        "conversation": {
            "conversation_id": conversation_id,
            "conversation_kind": profile["conversation_kind"],
            "conversation_kind_label": conversation_kind_label(profile["conversation_kind"]),
            "conversation_kind_evidence": profile.get("conversation_kind_evidence", ""),
            "conversation_display_name": profile["conversation_display_name"],
            "conversation_name_evidence": profile["conversation_name_evidence"],
            "counterpart_id": profile.get("counterpart_id", ""),
            "message_count": len(rows),
            "sender_count": len(sender_stats),
            "display_total_message_count": len(display_rows),
            "recovery_gap_marker_count": recovery_gap_marker_count,
            "first_time": first_ts,
            "first_time_iso": iso_from_ts(first_ts),
            "last_time": last_ts,
            "last_time_iso": iso_from_ts(last_ts),
            "displayed_message_count": len(preview_messages),
            "omitted_message_count": max(0, len(display_rows) - len(preview_messages)),
            "top_senders": top_senders,
        },
        "messages": preview_messages,
    }


def export_conversation(
    conversation_id: str,
    rows,
    output_root: Path,
    media_cache_index=None,
    metadata=None,
    conversation_profile=None,
    recovery_batch_id="",
    sqlite_path=None,
):
    profile = conversation_profile or guess_conversation_profile(conversation_id, rows, metadata=metadata)
    kind = profile.get("conversation_kind") or infer_conversation_kind(conversation_id, rows)[0]
    display_name = profile["conversation_display_name"]
    folder = output_root / profile["folder_name"]
    folder.mkdir(parents=True, exist_ok=True)
    media_cache_index = media_cache_index or {}
    copied_cache_map = {}

    local_contact_names = export_sender_name_context(profile, rows, metadata=metadata)
    sender_aliases = profile.get("sender_aliases") or collect_sender_aliases(rows)
    sender_aliases = propagate_external_group_sender_aliases(
        profile,
        rows,
        sender_aliases,
        metadata=metadata,
        local_contact_names=local_contact_names,
    )
    self_user_id = infer_self_user_id(rows, metadata=metadata)
    sender_stats = Counter(row["sender_id"] for row in rows)
    first_ts = min((row["send_time"] for row in rows if row["send_time"]), default=0)
    last_ts = max((row["send_time"] for row in rows if row["send_time"]), default=0)
    images_dir = folder / "图片"
    media_rows = []
    complete_records = []
    sqlite_path = Path(sqlite_path) if sqlite_path else Path("")
    timeline_rows = rows_with_recovery_gap_markers(rows)
    recovery_gap_marker_count = sum(1 for row in timeline_rows if row_is_recovery_gap_marker(row))

    senders_csv = choose_writable_output_path(folder / "发送人映射.csv")
    with senders_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sender_id", "sender_name_guess", "sender_role", "sender_role_label", "sender_role_evidence", "score", "messages", "evidence"])
        for sender_id, count in sender_stats.most_common():
            guessed = sender_aliases.get(sender_id, {})
            sender_name, sender_evidence = preferred_sender_name(sender_id, sender_aliases, local_contact_names)
            sender_role, sender_role_evidence = infer_sender_role(
                sender_id,
                sender_name=sender_name,
                conversation_profile=profile,
                metadata=metadata,
                self_user_id=self_user_id,
            )
            writer.writerow(
                [
                    sender_id,
                    sender_name,
                    sender_role,
                    sender_role_label(sender_role),
                    sender_role_evidence,
                    guessed.get("score", ""),
                    count,
                    sender_evidence or guessed.get("evidence", ""),
                ]
            )

    timeline_csv = choose_writable_output_path(folder / conversation_chat_record_filename(display_name, ".csv"))
    with timeline_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "send_time_iso",
                "sender_name_guess",
                "sender_display",
                "sender_role",
                "sender_role_label",
                "sender_role_evidence",
                "content_type_label",
                "media_kind",
                "media_uuid",
                "media_url",
                "cached_media_file",
                "exported_media_file",
                "message_preview",
            ]
        )
        for idx, row in enumerate(timeline_rows, start=1):
            is_gap_marker = row_is_recovery_gap_marker(row)
            sender_id = row.get("sender_id", "")
            if is_gap_marker:
                sender_name = "恢复提示"
                display = "恢复提示"
                sender_role = "unknown"
                sender_role_evidence = "message_id 连续性检查"
            else:
                sender_name, _sender_evidence = preferred_sender_name(sender_id, sender_aliases, local_contact_names)
                display, _sender_name_unused, _sender_evidence_unused = sender_display_text(sender_id, sender_aliases, local_contact_names)
                sender_role, sender_role_evidence = infer_sender_role(
                    sender_id,
                    sender_name=sender_name,
                    conversation_profile=profile,
                    metadata=metadata,
                    self_user_id=self_user_id,
                )
            media_metadata = extract_media_metadata(row)
            cached_media_files = resolve_cached_media_files(media_metadata, media_cache_index)
            exported_media_files = copy_media_files(cached_media_files, images_dir, copied_cache_map) if cached_media_files else []
            media_uuid = " | ".join(media_metadata["uuids"])
            media_url = " | ".join(media_metadata["urls"][:4])
            cached_media_file = " | ".join(str(path) for path in cached_media_files)
            exported_media_file = " | ".join(str(path) for path in exported_media_files)
            message_preview = build_message_preview_text(row, media_metadata=media_metadata)

            if media_metadata["has_media"] and not is_gap_marker:
                media_rows.append(
                    {
                        "seq": idx,
                        "message_id": row.get("message_id", ""),
                        "send_time_iso": row.get("send_time_iso", ""),
                        "sender_display": display,
                        "content_type": row.get("content_type", ""),
                        "content_type_label": media_metadata["content_type_label"],
                        "media_kind": media_metadata["media_kind"],
                        "media_uuid": media_uuid,
                        "media_url": media_url,
                        "cached_media_file": cached_media_file,
                        "exported_media_file": exported_media_file,
                        "preview": message_preview,
                    }
                )

            if not is_gap_marker:
                complete_records.append(
                    build_complete_chat_record(
                        row,
                        len(complete_records) + 1,
                        sqlite_path,
                        recovery_batch_id,
                        profile,
                        metadata,
                        sender_aliases,
                        local_contact_names,
                        self_user_id,
                        media_metadata=media_metadata,
                        cached_media_files=cached_media_files,
                        exported_media_files=exported_media_files,
                    )
                )

            writer.writerow(
                [
                    row.get("send_time_iso", ""),
                    sender_name,
                    display,
                    sender_role,
                    sender_role_label(sender_role),
                    sender_role_evidence,
                    media_metadata["content_type_label"],
                    media_metadata["media_kind"],
                    media_uuid,
                    media_url,
                    cached_media_file,
                    exported_media_file,
                    message_preview,
                ]
            )

    media_index_csv = choose_writable_output_path(folder / "媒体索引.csv")
    with media_index_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "conversation_display_name",
                "conversation_id",
                "seq",
                "message_id",
                "send_time_iso",
                "sender_display",
                "content_type",
                "content_type_label",
                "media_kind",
                "media_uuid",
                "media_url",
                "cached_media_file",
                "exported_media_file",
                "message_preview",
            ]
        )
        for item in media_rows:
            writer.writerow(
                [
                    display_name,
                    conversation_id,
                    item["seq"],
                    item["message_id"],
                    item["send_time_iso"],
                    item["sender_display"],
                    item["content_type"],
                    item["content_type_label"],
                    item["media_kind"],
                    item["media_uuid"],
                    item["media_url"],
                    item["cached_media_file"],
                    item["exported_media_file"],
                    item["preview"],
                ]
            )

    timeline_md = choose_writable_output_path(folder / "会话时间线.md")
    md_lines = [
        f"# 会话整理结果",
        "",
        f"- 会话名称: `{display_name}`",
        f"- 会话ID: `{conversation_id}`",
        f"- 会话类型: `{conversation_kind_label(kind)}`",
        f"- 消息数量: `{len(rows)}`",
        f"- 疑似缺失提示: `{recovery_gap_marker_count}`",
        f"- 发送人数: `{len(sender_stats)}`",
        f"- 时间范围: `{iso_from_ts(first_ts)} -> {iso_from_ts(last_ts)}`",
        f"- 媒体线索消息: `{len(media_rows)}`",
        f"- 已匹配导出图片文件: `{len(copied_cache_map)}`",
        f"- 会话名称依据: `{profile['conversation_name_evidence']}`",
        "",
        "## 发送人映射（推测）",
        "",
    ]
    if sender_stats:
        for sender_id, count in sender_stats.most_common():
            guessed = sender_aliases.get(sender_id, {})
            sender_name, sender_evidence = preferred_sender_name(sender_id, sender_aliases, local_contact_names)
            sender_role, sender_role_evidence = infer_sender_role(
                sender_id,
                sender_name=sender_name,
                conversation_profile=profile,
                metadata=metadata,
                self_user_id=self_user_id,
            )
            role_text = f"，角色：{sender_role_label(sender_role)}（{sender_role_evidence}）"
            if sender_name:
                md_lines.append(f"- `{sender_id}` -> `{sender_name}`{role_text}，消息 {count} 条，证据：{sender_evidence or guessed.get('evidence', '')}")
            else:
                md_lines.append(f"- `{sender_id}` -> 未识别姓名{role_text}，消息 {count} 条")
    else:
        md_lines.append("- 无发送人信息")

    md_lines.extend(["", "## 时间线", ""])
    for idx, row in enumerate(timeline_rows, start=1):
        if row_is_recovery_gap_marker(row):
            display = "恢复提示"
            sender_role = "unknown"
        else:
            display, sender_name_for_role, _sender_evidence_unused = sender_display_text(row.get("sender_id"), sender_aliases, local_contact_names)
            sender_role, _sender_role_evidence = infer_sender_role(
                row.get("sender_id"),
                sender_name=sender_name_for_role,
                conversation_profile=profile,
                metadata=metadata,
                self_user_id=self_user_id,
            )
        media_metadata = extract_media_metadata(row)
        cached_media_files = resolve_cached_media_files(media_metadata, media_cache_index)
        message_preview = build_message_preview_text(row, media_metadata=media_metadata)
        media_note = ""
        if cached_media_files:
            media_note = f" [已匹配图片 {len(cached_media_files)}]"
        elif media_metadata["has_media"]:
            media_note = " [检测到媒体线索]"
        md_lines.append(
            f"{idx}. [{row.get('send_time_iso', '')}] `{display}` `{sender_role_label(sender_role)}` `type={row.get('content_type', '')}` `{media_metadata['content_type_label']}`{media_note} {message_preview or '(空)'}"
        )
    timeline_md.write_text("\n".join(md_lines), encoding="utf-8")

    summary_json = choose_writable_output_path(folder / "会话摘要.json")
    sender_role_counts = Counter()
    for sender_id in sender_stats:
        sender_name, _sender_evidence = preferred_sender_name(sender_id, sender_aliases, local_contact_names)
        sender_role, _sender_role_evidence = infer_sender_role(
            sender_id,
            sender_name=sender_name,
            conversation_profile=profile,
            metadata=metadata,
            self_user_id=self_user_id,
        )
        sender_role_counts[sender_role] += sender_stats[sender_id]
    summary_json.write_text(
        json.dumps(
            {
                "conversation_id": conversation_id,
                "conversation_display_name": display_name,
                "conversation_name_evidence": profile["conversation_name_evidence"],
                "conversation_kind": kind,
                "message_count": len(rows),
                "sender_count": len(sender_stats),
                "first_time": first_ts,
                "first_time_iso": iso_from_ts(first_ts),
                "last_time": last_ts,
                "last_time_iso": iso_from_ts(last_ts),
                "media_message_count": len(media_rows),
                "exported_media_file_count": len(copied_cache_map),
                "recovery_gap_marker_count": recovery_gap_marker_count,
                "sender_role_message_counts": dict(sender_role_counts),
                "sender_aliases": sender_aliases,
                "complete_chat_record_count": len(complete_records),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    complete_chat_files = write_complete_chat_records(folder, "完整聊天记录.csv", "完整聊天记录.jsonl", complete_records)

    guide_txt = choose_writable_output_path(folder / "导出文件说明.txt")
    guide_lines = [
        f"会话名称：{display_name}",
        f"会话ID：{conversation_id}",
        "",
        "本目录主要文件说明：",
        f"1. {timeline_csv.name}",
        "   逐条时间线明细。每一行是一条消息；如果检测到小跨度 message_id 缺口，会插入“疑似缺失消息”提示便于人工复核。",
        f"2. {timeline_md.name}",
        "   适合直接阅读的 Markdown 版时间线，便于快速浏览聊天脉络。",
        f"3. {senders_csv.name}",
        "   发送人统计表。列出 sender_id、脚本推测的人名、消息条数，以及推测证据。",
        f"4. {media_index_csv.name}",
        "   媒体消息索引。只收录带图片/动画表情/媒体链接线索的消息，便于单独排查媒体。",
        f"5. {summary_json.name}",
        "   该会话的摘要信息，包含会话名称、会话类型、消息总数、时间范围等。",
        f"6. {Path(complete_chat_files['csv']).name}",
        "   完整聊天记录。每一行是一条已恢复消息，额外保留原始字段、强解码字段、来源和媒体线索。",
        f"7. {Path(complete_chat_files['jsonl']).name}",
        "   JSONL 版完整聊天记录，适合程序继续做二次解析或知识库清洗。",
        f"8. {images_dir.name}\\",
        "   如果本机企业微信缓存中还存在对应图片文件，会导出到这里。",
        "",
        "几个 CSV 的区别：",
        f"- {timeline_csv.name}：全量消息明细。",
        f"- {senders_csv.name}：按发送人聚合后的统计、姓名推测与商务/外部联系人角色推测。",
        f"- {media_index_csv.name}：只筛出带媒体线索的消息。",
        f"- {Path(complete_chat_files['csv']).name}：保留所有已恢复消息的原始字段和强解码字段，适合排查完整性。",
    ]
    guide_txt.write_text("\n".join(guide_lines), encoding="utf-8")

    return {
        "folder": str(folder),
        "conversation_display_name": display_name,
        "conversation_name_evidence": profile["conversation_name_evidence"],
        "timeline_csv": str(timeline_csv),
        "timeline_md": str(timeline_md),
        "senders_csv": str(senders_csv),
        "media_index_csv": str(media_index_csv),
        "complete_chat_csv": complete_chat_files["csv"],
        "complete_chat_jsonl": complete_chat_files["jsonl"],
        "summary_json": str(summary_json),
        "guide_txt": str(guide_txt),
        "message_count": len(rows),
        "sender_count": len(sender_stats),
        "first_time_iso": iso_from_ts(first_ts),
        "last_time_iso": iso_from_ts(last_ts),
        "media_message_count": len(media_rows),
        "exported_media_file_count": len(copied_cache_map),
        "recovery_gap_marker_count": recovery_gap_marker_count,
    }


def export_index(index_rows, output_root: Path, filename="外部群索引.csv"):
    index_csv = choose_writable_output_path(output_root / filename)
    with index_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "conversation_id",
                "conversation_display_name",
                "conversation_name_evidence",
                "counterpart_id",
                "conversation_prefix",
                "conversation_kind",
                "conversation_kind_label",
                "message_count",
                "sender_count",
                "first_time_iso",
                "last_time_iso",
                "output_folder",
            ]
        )
        for item in index_rows:
            writer.writerow(
                [
                    item["conversation_id"],
                    item.get("conversation_display_name", ""),
                    item.get("conversation_name_evidence", ""),
                    item.get("counterpart_id", ""),
                    conversation_prefix(item["conversation_id"]),
                    item["conversation_kind"],
                    conversation_kind_label(item["conversation_kind"]),
                    item["message_count"],
                    item["sender_count"],
                    item["first_time_iso"],
                    item["last_time_iso"],
                    item["folder"],
                ]
            )
    return index_csv


def export_all_conversations_index(conversation_index, output_root: Path, exported_rows):
    exported_map = {item["conversation_id"]: item for item in exported_rows}
    index_csv = choose_writable_output_path(output_root / "全部会话总表.csv")
    with index_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "conversation_id",
                "conversation_display_name",
                "conversation_name_evidence",
                "counterpart_id",
                "conversation_kind",
                "conversation_kind_label",
                "message_count",
                "sender_count",
                "first_time_iso",
                "last_time_iso",
                "exported_in_this_run",
                "output_folder",
            ]
        )
        for item in conversation_index:
            exported = exported_map.get(item["conversation_id"])
            kind = item["conversation_kind"]
            writer.writerow(
                [
                    item["conversation_id"],
                    item.get("conversation_display_name", ""),
                    item.get("conversation_name_evidence", ""),
                    item.get("counterpart_id", ""),
                    kind,
                    conversation_kind_label(kind),
                    item["message_count"],
                    item["sender_count"],
                    item["first_time_iso"],
                    item["last_time_iso"],
                    "是" if exported else "否",
                    exported["folder"] if exported else "",
                ]
            )
    return index_csv


def complete_chat_fieldnames():
    """
    “完整聊天记录”保留更多原始字段和强解码字段，供后续继续做人工核对或专门解析。
    """
    return [
        "recovery_batch_id",
        "source_kind",
        "source_path",
        "source_sqlite",
        "conversation_id",
        "conversation_display_name",
        "conversation_kind",
        "conversation_kind_label",
        "message_seq",
        "message_id",
        "server_id",
        "sequence",
        "recovered_rowid",
        "send_time",
        "send_time_iso",
        "sender_id",
        "sender_name_guess",
        "sender_display",
        "sender_role",
        "sender_role_label",
        "sender_role_evidence",
        "content_type",
        "content_type_label",
        "flag",
        "media_kind",
        "has_media",
        "media_uuid",
        "media_url",
        "media_file_names",
        "cached_media_file",
        "exported_media_file",
        "decode_status",
        "decoded_field_count",
        "structured_tokens",
        "message_text",
        "strong_decoded_text",
        "decoded_content",
        "decoded_extra_content",
        "decoded_local_extra_content",
        "decoded_devinfo",
        "decoded_msg_from_devinfo",
        "decoded_translate_info",
        "decoded_time_nlp",
        "decoded_approval_nlp",
        "raw_content",
        "raw_extra_content",
        "raw_local_extra_content",
        "raw_devinfo",
        "raw_msg_from_devinfo",
        "raw_translate_info",
        "raw_time_nlp",
        "raw_approval_nlp",
    ]


def raw_field_text(row, field_name: str):
    """
    CSV/JSONL 留档时统一把 None 转为空字符串，避免导出里出现 Python 的 None 文本。
    """
    value = (row or {}).get(field_name)
    return "" if value is None else str(value)


def row_source_kind_and_path(row, default_sqlite_path: Path):
    """
    标记消息来自原始恢复 sqlite 还是历史整理时间线，后续排查“完整记录来源”时能直接定位。
    """
    if row.get("_source_sqlite"):
        return "raw_sqlite", str(row.get("_source_sqlite"))
    if row.get("_history_source"):
        return "history_timeline", str(row.get("_history_source"))
    return "unknown", str(default_sqlite_path or "")


def audit_text_is_chat_text(text: str):
    """
    判断强解码结果是否像真实聊天正文；纯媒体占位、类型占位和文件线索不算正文。
    """
    text = clean_preview_text(text)
    if not text:
        return False
    if re.fullmatch(r"\[[^\]]+\]", text):
        return False
    if re.match(r"^\[(图片|动画表情|视频|语音/音频|媒体下载链接|媒体线索|文件/卡片|系统提示|富文本/通知|类型\d+)\]", text):
        return False
    return bool(CJK_RE.search(text) or ASCII_LETTER_RE.search(text))


def complete_chat_decode_status(message_text: str, strong_text: str, has_media: bool, raw_values, structured_tokens=None):
    """
    给完整聊天记录标记解码状态，便于后续快速筛出仍需继续专门解析的消息。
    """
    if audit_text_is_chat_text(strong_text) or audit_text_is_chat_text(message_text):
        return "text_decoded"
    if has_media and clean_preview_text(strong_text or message_text):
        return "media_with_detail"
    if has_media:
        return "media_only"
    if structured_tokens:
        return "structured_only"
    if any(clean_preview_text(value) for value in raw_values):
        return "raw_needs_decode"
    return "empty"


def build_complete_chat_record(
    row,
    seq: int,
    sqlite_path: Path,
    recovery_batch_id: str,
    profile,
    metadata,
    sender_aliases,
    local_contact_names,
    self_user_id,
    media_metadata=None,
    cached_media_files=None,
    exported_media_files=None,
):
    """
    生成单条完整聊天记录：既包含易读文本，也包含可继续强解析的原始字段。
    """
    media_metadata = media_metadata or extract_media_metadata(row)
    cached_media_files = cached_media_files or []
    exported_media_files = exported_media_files or []
    sender_id = str(row.get("sender_id") or "")
    sender_display, sender_name, _sender_evidence = sender_display_text(sender_id, sender_aliases, local_contact_names)
    sender_role, sender_role_evidence = infer_sender_role(
        sender_id,
        sender_name=sender_name,
        conversation_profile=profile,
        metadata=metadata,
        self_user_id=self_user_id,
    )
    source_kind, source_path = row_source_kind_and_path(row, sqlite_path)
    message_text = build_message_preview_text(row, media_metadata=media_metadata)
    decoded_fields = {
        "decoded_content": strong_decoded_field_text(row, "content"),
        "decoded_extra_content": strong_decoded_field_text(row, "extra_content"),
        "decoded_local_extra_content": strong_decoded_field_text(row, "local_extra_content"),
        "decoded_devinfo": strong_decoded_field_text(row, "devinfo"),
        "decoded_msg_from_devinfo": strong_decoded_field_text(row, "msg_from_devinfo"),
        "decoded_translate_info": strong_decoded_field_text(row, "local_extra_content_translate_info"),
        "decoded_time_nlp": strong_decoded_field_text(row, "local_extra_content_time_nlp"),
        "decoded_approval_nlp": strong_decoded_field_text(row, "local_extra_content_approval_nlp"),
    }
    strong_text = "\n".join(unique_decoded_texts([message_text] + list(decoded_fields.values())))
    raw_values = [
        raw_field_text(row, "content"),
        raw_field_text(row, "extra_content"),
        raw_field_text(row, "local_extra_content"),
        raw_field_text(row, "devinfo"),
        raw_field_text(row, "msg_from_devinfo"),
        raw_field_text(row, "local_extra_content_translate_info"),
        raw_field_text(row, "local_extra_content_time_nlp"),
        raw_field_text(row, "local_extra_content_approval_nlp"),
    ]
    structured_tokens = structured_tokens_from_row(row)
    decode_status = complete_chat_decode_status(
        message_text,
        strong_text,
        has_media=bool(media_metadata["has_media"]),
        raw_values=raw_values,
        structured_tokens=structured_tokens,
    )
    try:
        content_type_int = int(row.get("content_type") or 0)
    except (TypeError, ValueError):
        content_type_int = 0
    if decode_status == "raw_needs_decode" and content_type_int == 1023:
        decode_status = "structured_only"

    record = {
        "recovery_batch_id": recovery_batch_id,
        "source_kind": source_kind,
        "source_path": source_path,
        "source_sqlite": str(sqlite_path),
        "conversation_id": row.get("conversation_id", ""),
        "conversation_display_name": profile.get("conversation_display_name", ""),
        "conversation_kind": profile.get("conversation_kind", ""),
        "conversation_kind_label": conversation_kind_label(profile.get("conversation_kind", "")),
        "message_seq": seq,
        "message_id": row.get("message_id", ""),
        "server_id": row.get("server_id", ""),
        "sequence": row.get("sequence", ""),
        "recovered_rowid": row.get("recovered_rowid", ""),
        "send_time": row.get("send_time", ""),
        "send_time_iso": row.get("send_time_iso", ""),
        "sender_id": sender_id,
        "sender_name_guess": sender_name,
        "sender_display": sender_display,
        "sender_role": sender_role,
        "sender_role_label": sender_role_label(sender_role),
        "sender_role_evidence": sender_role_evidence,
        "content_type": row.get("content_type", ""),
        "content_type_label": media_metadata["content_type_label"],
        "flag": row.get("flag", ""),
        "media_kind": media_metadata["media_kind"],
        "has_media": "是" if media_metadata["has_media"] else "否",
        "media_uuid": " | ".join(media_metadata["uuids"]),
        "media_url": " | ".join(media_metadata["urls"][:4]),
        "media_file_names": " | ".join(media_metadata["file_names"]),
        "cached_media_file": " | ".join(str(path) for path in cached_media_files),
        "exported_media_file": " | ".join(str(path) for path in exported_media_files),
        "decode_status": decode_status,
        "decoded_field_count": sum(1 for value in decoded_fields.values() if clean_preview_text(value)),
        "structured_tokens": " | ".join(structured_tokens[:30]),
        "message_text": message_text,
        "strong_decoded_text": strong_text,
        "raw_content": raw_field_text(row, "content"),
        "raw_extra_content": raw_field_text(row, "extra_content"),
        "raw_local_extra_content": raw_field_text(row, "local_extra_content"),
        "raw_devinfo": raw_field_text(row, "devinfo"),
        "raw_msg_from_devinfo": raw_field_text(row, "msg_from_devinfo"),
        "raw_translate_info": raw_field_text(row, "local_extra_content_translate_info"),
        "raw_time_nlp": raw_field_text(row, "local_extra_content_time_nlp"),
        "raw_approval_nlp": raw_field_text(row, "local_extra_content_approval_nlp"),
    }
    record.update(decoded_fields)
    return record


def write_complete_chat_records(output_root: Path, csv_name: str, jsonl_name: str, records):
    """
    同时写 CSV 和 JSONL；CSV 方便表格检查，JSONL 方便程序继续处理。
    """
    csv_path = choose_writable_output_path(output_root / csv_name)
    jsonl_path = choose_writable_output_path(output_root / jsonl_name)
    fieldnames = complete_chat_fieldnames()
    with csv_path.open("w", encoding="utf-8-sig", newline="") as csv_handle, jsonl_path.open("w", encoding="utf-8") as jsonl_handle:
        writer = csv.DictWriter(csv_handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            safe_record = {field: record.get(field, "") for field in fieldnames}
            writer.writerow(safe_record)
            jsonl_handle.write(json.dumps(safe_record, ensure_ascii=False) + "\n")
    return {"csv": str(csv_path), "jsonl": str(jsonl_path), "message_count": len(records)}


def counter_to_sorted_dict(counter):
    """
    报告里统一按数量倒序输出统计项，便于快速看出主要缺口。
    """
    return {key: value for key, value in sorted(counter.items(), key=lambda item: (-item[1], str(item[0])))}


def build_complete_chat_audit(records):
    """
    汇总完整聊天记录的覆盖情况和强解码状态。
    """
    decode_status_counts = Counter()
    source_kind_counts = Counter()
    content_type_counts = Counter()
    conversation_stats = {}
    first_time = ""
    last_time = ""

    for record in records:
        decode_status_counts[record.get("decode_status", "") or "unknown"] += 1
        source_kind_counts[record.get("source_kind", "") or "unknown"] += 1
        content_type_counts[record.get("content_type_label", "") or "unknown"] += 1

        send_time_iso = str(record.get("send_time_iso") or "")
        if send_time_iso:
            first_time = send_time_iso if not first_time or send_time_iso < first_time else first_time
            last_time = send_time_iso if not last_time or send_time_iso > last_time else last_time

        conversation_id = str(record.get("conversation_id") or "")
        current = conversation_stats.setdefault(
            conversation_id,
            {
                "conversation_id": conversation_id,
                "conversation_display_name": record.get("conversation_display_name", ""),
                "conversation_kind": record.get("conversation_kind", ""),
                "message_count": 0,
                "decode_status_counts": Counter(),
                "source_kind_counts": Counter(),
                "first_time_iso": "",
                "last_time_iso": "",
            },
        )
        current["message_count"] += 1
        current["decode_status_counts"][record.get("decode_status", "") or "unknown"] += 1
        current["source_kind_counts"][record.get("source_kind", "") or "unknown"] += 1
        if send_time_iso:
            current["first_time_iso"] = send_time_iso if not current["first_time_iso"] or send_time_iso < current["first_time_iso"] else current["first_time_iso"]
            current["last_time_iso"] = send_time_iso if not current["last_time_iso"] or send_time_iso > current["last_time_iso"] else current["last_time_iso"]

    conversations = []
    for item in conversation_stats.values():
        normalized = dict(item)
        normalized["decode_status_counts"] = counter_to_sorted_dict(normalized["decode_status_counts"])
        normalized["source_kind_counts"] = counter_to_sorted_dict(normalized["source_kind_counts"])
        conversations.append(normalized)
    conversations.sort(key=lambda item: (-item["message_count"], item["conversation_id"]))

    return {
        "message_count": len(records),
        "conversation_count": len(conversation_stats),
        "first_time_iso": first_time,
        "last_time_iso": last_time,
        "decode_status_counts": counter_to_sorted_dict(decode_status_counts),
        "source_kind_counts": counter_to_sorted_dict(source_kind_counts),
        "content_type_counts": counter_to_sorted_dict(content_type_counts),
        "conversations": conversations,
    }


def write_undecoded_samples(output_root: Path, records, max_samples=200):
    """
    导出未充分解码样本，优先给后续继续强解析提供目标。
    """
    sample_path = choose_writable_output_path(output_root / "未充分解码消息样本.csv")
    fieldnames = [
        "conversation_id",
        "conversation_display_name",
        "message_seq",
        "message_id",
        "send_time_iso",
        "sender_display",
        "content_type",
        "content_type_label",
        "decode_status",
        "has_media",
        "message_text",
        "structured_tokens",
        "strong_decoded_text_head",
        "raw_content_head",
        "raw_extra_content_head",
        "raw_local_extra_content_head",
        "source_kind",
        "source_path",
    ]
    selected = [
        record
        for record in records
        if record.get("decode_status") in {"raw_needs_decode", "empty", "media_only"}
    ][:max_samples]
    with sample_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in selected:
            writer.writerow(
                {
                    "conversation_id": record.get("conversation_id", ""),
                    "conversation_display_name": record.get("conversation_display_name", ""),
                    "message_seq": record.get("message_seq", ""),
                    "message_id": record.get("message_id", ""),
                    "send_time_iso": record.get("send_time_iso", ""),
                    "sender_display": record.get("sender_display", ""),
                    "content_type": record.get("content_type", ""),
                    "content_type_label": record.get("content_type_label", ""),
                    "decode_status": record.get("decode_status", ""),
                    "has_media": record.get("has_media", ""),
                    "message_text": record.get("message_text", ""),
                    "structured_tokens": record.get("structured_tokens", ""),
                    "strong_decoded_text_head": clean_preview_text(record.get("strong_decoded_text", ""))[:500],
                    "raw_content_head": raw_field_text(record, "raw_content")[:500],
                    "raw_extra_content_head": raw_field_text(record, "raw_extra_content")[:500],
                    "raw_local_extra_content_head": raw_field_text(record, "raw_local_extra_content")[:500],
                    "source_kind": record.get("source_kind", ""),
                    "source_path": record.get("source_path", ""),
                }
            )
    return str(sample_path), len(selected)


def write_complete_chat_audit(output_root: Path, records):
    """
    写出完整聊天记录的 JSON/Markdown 核对报告和未充分解码样本。
    """
    audit = build_complete_chat_audit(records)
    sample_csv, sample_count = write_undecoded_samples(output_root, records)
    audit["undecoded_sample_csv"] = sample_csv
    audit["undecoded_sample_count"] = sample_count

    json_path = choose_writable_output_path(output_root / "完整聊天记录核对报告.json")
    json_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = choose_writable_output_path(output_root / "完整聊天记录核对报告.md")
    md_lines = [
        "# 完整聊天记录核对报告",
        "",
        f"- 消息总数: `{audit['message_count']}`",
        f"- 会话数量: `{audit['conversation_count']}`",
        f"- 时间范围: `{audit.get('first_time_iso', '')} -> {audit.get('last_time_iso', '')}`",
        f"- 未充分解码样本: `{sample_count}` 条，文件 `{Path(sample_csv).name}`",
        "",
        "## 解码状态",
        "",
    ]
    for status, count in audit["decode_status_counts"].items():
        md_lines.append(f"- `{status}`: `{count}`")
    md_lines.extend(["", "## 来源分布", ""])
    for source_kind, count in audit["source_kind_counts"].items():
        md_lines.append(f"- `{source_kind}`: `{count}`")
    md_lines.extend(["", "## 消息类型分布", ""])
    for label, count in list(audit["content_type_counts"].items())[:30]:
        md_lines.append(f"- `{label}`: `{count}`")
    md_lines.extend(["", "## 会话覆盖", ""])
    for item in audit["conversations"]:
        md_lines.append(
            f"- `{item['conversation_id']}` `{item['conversation_display_name']}`: `{item['message_count']}` 条，"
            f"解码状态 {json.dumps(item['decode_status_counts'], ensure_ascii=False)}"
        )
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    return {
        "audit_json": str(json_path),
        "audit_md": str(md_path),
        "undecoded_sample_csv": sample_csv,
        "undecoded_sample_count": sample_count,
        "decode_status_counts": audit["decode_status_counts"],
        "source_kind_counts": audit["source_kind_counts"],
    }


def export_batch_complete_chat_records(
    output_root: Path,
    recovery_batch_id: str,
    sqlite_path: Path,
    grouped,
    selected_ids,
    conversation_profile_map,
    media_cache_index=None,
    metadata=None,
):
    """
    导出批次级完整聊天记录，覆盖本次选中的所有会话和所有已合并消息。
    """
    media_cache_index = media_cache_index or {}
    records = []
    for conversation_id in selected_ids:
        rows = grouped.get(conversation_id, [])
        profile = conversation_profile_map.get(conversation_id) or guess_conversation_profile(conversation_id, rows, metadata=metadata)
        local_contact_names = export_sender_name_context(profile, rows, metadata=metadata)
        sender_aliases = profile.get("sender_aliases") or collect_sender_aliases(rows)
        sender_aliases = propagate_external_group_sender_aliases(
            profile,
            rows,
            sender_aliases,
            metadata=metadata,
            local_contact_names=local_contact_names,
        )
        self_user_id = infer_self_user_id(rows, metadata=metadata)
        for seq, row in enumerate(rows, start=1):
            media_metadata = extract_media_metadata(row)
            cached_media_files = resolve_cached_media_files(media_metadata, media_cache_index)
            records.append(
                build_complete_chat_record(
                    row,
                    seq,
                    sqlite_path,
                    recovery_batch_id,
                    profile,
                    metadata,
                    sender_aliases,
                    local_contact_names,
                    self_user_id,
                    media_metadata=media_metadata,
                    cached_media_files=cached_media_files,
                    exported_media_files=[],
                )
            )
    outputs = write_complete_chat_records(output_root, "完整聊天记录.csv", "完整聊天记录.jsonl", records)
    outputs.update(write_complete_chat_audit(output_root, records))
    return outputs


def vector_message_text_is_usable(message_text: str):
    """
    判断一条消息是否适合作为第一阶段向量语料；纯媒体占位和空文本先保留记录但不建议入向量库。
    """
    text = clean_preview_text(message_text)
    if not text:
        return False
    if text.startswith("[") and text.endswith("]") and len(text) <= 12:
        return False
    if not (CJK_RE.search(text) or ASCII_LETTER_RE.search(text)):
        return False
    return True


def export_vector_training_corpus(
    output_root: Path,
    recovery_batch_id: str,
    sqlite_path: Path,
    grouped,
    selected_ids,
    conversation_profile_map,
    metadata=None,
):
    """
    导出一份批次级干净语料，供后续知识库清洗、向量化或人工抽检直接读取。
    """
    csv_path = choose_writable_output_path(output_root / "向量训练语料.csv")
    jsonl_path = choose_writable_output_path(output_root / "向量训练语料.jsonl")
    fieldnames = [
        "recovery_batch_id",
        "source_sqlite",
        "conversation_id",
        "conversation_display_name",
        "conversation_kind",
        "conversation_kind_label",
        "message_seq",
        "message_id",
        "send_time_iso",
        "sender_id",
        "sender_name_guess",
        "sender_display",
        "sender_role",
        "sender_role_label",
        "sender_role_evidence",
        "content_type",
        "content_type_label",
        "media_kind",
        "has_media",
        "usable_for_vector",
        "message_text",
    ]
    role_counts = Counter()
    usable_count = 0
    message_count = 0

    with csv_path.open("w", encoding="utf-8-sig", newline="") as csv_handle, jsonl_path.open("w", encoding="utf-8") as jsonl_handle:
        writer = csv.DictWriter(csv_handle, fieldnames=fieldnames)
        writer.writeheader()
        for conversation_id in selected_ids:
            rows = grouped.get(conversation_id, [])
            profile = conversation_profile_map.get(conversation_id) or guess_conversation_profile(conversation_id, rows, metadata=metadata)
            local_contact_names = export_sender_name_context(profile, rows, metadata=metadata)
            sender_aliases = profile.get("sender_aliases") or collect_sender_aliases(rows)
            sender_aliases = propagate_external_group_sender_aliases(
                profile,
                rows,
                sender_aliases,
                metadata=metadata,
                local_contact_names=local_contact_names,
            )
            self_user_id = infer_self_user_id(rows, metadata=metadata)

            for seq, row in enumerate(rows, start=1):
                sender_id = str(row.get("sender_id") or "")
                sender_display, sender_name, _sender_evidence = sender_display_text(sender_id, sender_aliases, local_contact_names)
                sender_role, sender_role_evidence = infer_sender_role(
                    sender_id,
                    sender_name=sender_name,
                    conversation_profile=profile,
                    metadata=metadata,
                    self_user_id=self_user_id,
                )
                media_metadata = extract_media_metadata(row)
                message_text = build_message_preview_text(row, media_metadata=media_metadata)
                usable_for_vector = vector_message_text_is_usable(message_text)
                record = {
                    "recovery_batch_id": recovery_batch_id,
                    "source_sqlite": str(sqlite_path),
                    "conversation_id": conversation_id,
                    "conversation_display_name": profile.get("conversation_display_name", ""),
                    "conversation_kind": profile.get("conversation_kind", ""),
                    "conversation_kind_label": conversation_kind_label(profile.get("conversation_kind", "")),
                    "message_seq": seq,
                    "message_id": row.get("message_id", ""),
                    "send_time_iso": row.get("send_time_iso", ""),
                    "sender_id": sender_id,
                    "sender_name_guess": sender_name,
                    "sender_display": sender_display,
                    "sender_role": sender_role,
                    "sender_role_label": sender_role_label(sender_role),
                    "sender_role_evidence": sender_role_evidence,
                    "content_type": row.get("content_type", ""),
                    "content_type_label": media_metadata["content_type_label"],
                    "media_kind": media_metadata["media_kind"],
                    "has_media": "是" if media_metadata["has_media"] else "否",
                    "usable_for_vector": "是" if usable_for_vector else "否",
                    "message_text": message_text,
                }
                writer.writerow(record)
                jsonl_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                message_count += 1
                role_counts[sender_role] += 1
                if usable_for_vector:
                    usable_count += 1

    return {
        "csv": str(csv_path),
        "jsonl": str(jsonl_path),
        "message_count": message_count,
        "usable_message_count": usable_count,
        "sender_role_message_counts": dict(role_counts),
    }


def current_source_machine() -> str:
    """
    统一记录导出来源机器名，便于把离线导出结果同步到另一个项目时做批次追踪。
    """
    return os.environ.get("COMPUTERNAME") or socket.gethostname() or "unknown_machine"


def current_source_operator() -> str:
    """
    统一记录当前导出操作人，便于后续追溯批次来源。
    """
    return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown_operator"


def build_recovery_batch_id(exported_at, metadata):
    """
    生成本次整理批次 ID；导出前先生成，保证各类明细文件和 manifest 使用同一个批次号。
    """
    source_machine = current_source_machine()
    enterprise_account_id = str((metadata or {}).get("corp_id", "") or "")
    return f"{exported_at.strftime('%Y%m%d_%H%M%S')}_{source_machine}_{enterprise_account_id or 'unknown'}"


def build_recovery_manifest(
    sqlite_path: Path,
    output_dir: Path,
    metadata,
    exported,
    index_csv: Path,
    all_conversations_csv: Path,
    recovery_batch_id="",
    exported_at=None,
):
    """
    生成批次级清单，供后续知识库清洗、同步脚本和另一个实时客服项目读取离线导出元数据。
    """
    exported_at = exported_at or dt.datetime.now().astimezone().replace(microsecond=0)
    source_machine = current_source_machine()
    enterprise_account_id = str((metadata or {}).get("corp_id", "") or "")
    recovery_batch_id = recovery_batch_id or build_recovery_batch_id(exported_at, metadata)

    files = [
        {"kind": "source_sqlite", "path": str(sqlite_path)},
        {"kind": "selected_index_csv", "path": str(index_csv)},
        {"kind": "all_conversations_csv", "path": str(all_conversations_csv)},
    ]
    sessions = []
    for item in exported:
        session_files = [
            {"kind": "session_folder", "path": item.get("folder", "")},
            {"kind": "timeline_csv", "path": item.get("timeline_csv", "")},
            {"kind": "timeline_md", "path": item.get("timeline_md", "")},
            {"kind": "senders_csv", "path": item.get("senders_csv", "")},
            {"kind": "media_index_csv", "path": item.get("media_index_csv", "")},
            {"kind": "complete_chat_csv", "path": item.get("complete_chat_csv", "")},
            {"kind": "complete_chat_jsonl", "path": item.get("complete_chat_jsonl", "")},
            {"kind": "summary_json", "path": item.get("summary_json", "")},
            {"kind": "guide_txt", "path": item.get("guide_txt", "")},
        ]
        files.extend(
            [
                {
                    "kind": file_item["kind"],
                    "conversation_id": item.get("conversation_id", ""),
                    "path": file_item["path"],
                }
                for file_item in session_files
                if file_item["path"]
            ]
        )
        sessions.append(
            {
                "conversation_id": item.get("conversation_id", ""),
                "conversation_display_name": item.get("conversation_display_name", ""),
                "conversation_kind": item.get("conversation_kind", ""),
                "folder": item.get("folder", ""),
                "message_count": item.get("message_count", 0),
                "sender_count": item.get("sender_count", 0),
                "media_message_count": item.get("media_message_count", 0),
                "exported_media_file_count": item.get("exported_media_file_count", 0),
                "files": [file_item for file_item in session_files if file_item["path"]],
            }
        )

    return {
        "source_machine": source_machine,
        "source_operator": current_source_operator(),
        "enterprise_account_id": enterprise_account_id,
        "recovery_batch_id": recovery_batch_id,
        "exported_at": exported_at.isoformat(),
        "session_count": len(exported),
        "source_sqlite": str(sqlite_path),
        "output_dir": str(output_dir),
        "files": files,
        "sessions": sessions,
    }


def write_recovery_manifest(output_dir: Path, manifest: dict):
    """
    将当前批次 manifest 固定写到 recovery_manifest.json；若文件被占用则自动避让新文件名。
    """
    manifest_path = choose_writable_output_path(output_dir / "recovery_manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def run_organize(
    input_sqlite="",
    source_dir=None,
    output_dir=None,
    conversation_id="",
    conversation_ids=None,
    prefix="R:",
    logger=print,
):
    source_dir = Path(source_dir) if source_dir else default_source_dir()
    output_dir = Path(output_dir) if output_dir else default_organized_output_dir(source_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sqlite_path = resolve_input_sqlite(input_sqlite, source_dir)
    rows, metadata = load_rows(sqlite_path, source_dir=source_dir)
    naming_context = build_naming_context(rows, metadata=metadata, source_dir=source_dir)
    if naming_context.get("self_user_id"):
        metadata = dict(metadata)
        metadata["self_user_id"] = naming_context["self_user_id"]
    grouped, conversation_index = build_conversation_index(rows, metadata=metadata, naming_context=naming_context, source_dir=source_dir)
    conversation_profile_map = {item["conversation_id"]: item for item in conversation_index}
    selected_ids = resolve_selected_conversation_ids(
        grouped,
        conversation_index,
        conversation_id=conversation_id,
        conversation_ids=conversation_ids,
        prefix=prefix,
    )

    if not selected_ids:
        raise ValueError("没有找到符合条件的会话。")

    emit(logger, f"使用 sqlite: {sqlite_path}")
    emit(logger, f"输出目录: {output_dir}")
    if metadata:
        emit(logger, f"元数据: corp_id={metadata.get('corp_id', '')}, pid={metadata.get('pid', '')}")

    exported_at = dt.datetime.now().astimezone().replace(microsecond=0)
    recovery_batch_id = build_recovery_batch_id(exported_at, metadata)

    docs_dir = detect_docs_dir()
    media_cache_index = build_media_cache_index(docs_dir) if docs_dir and docs_dir.exists() else {}
    if docs_dir and docs_dir.exists():
        emit(logger, f"企业微信缓存目录: {docs_dir}")
        emit(logger, f"已建立图片缓存索引: {len(media_cache_index)} 个媒体键")
    else:
        emit(logger, "未找到企业微信文档目录，无法匹配本地图片缓存。")

    exported = []
    for current_id in selected_ids:
        profile = conversation_profile_map.get(current_id)
        result = export_conversation(
            current_id,
            grouped[current_id],
            output_dir,
            media_cache_index=media_cache_index,
            metadata=metadata,
            conversation_profile=profile,
            recovery_batch_id=recovery_batch_id,
            sqlite_path=sqlite_path,
        )
        result["conversation_id"] = current_id
        result["conversation_kind"] = profile.get("conversation_kind", "")
        exported.append(result)
        emit(
            logger,
            f"- 已导出 {result.get('conversation_display_name', current_id)} ({current_id}): {result['message_count']} 条，目录 {result['folder']}，"
            f"媒体线索 {result['media_message_count']} 条，导出图片 {result['exported_media_file_count']} 个",
        )

    selected_display_name = conversation_profile_map.get(conversation_id, {}).get("conversation_display_name", "") if conversation_id else ""
    index_csv = export_index(
        exported,
        output_dir,
        filename=choose_index_filename(conversation_index, selected_ids, conversation_id=conversation_id, display_name=selected_display_name),
    )
    all_conversations_csv = export_all_conversations_index(conversation_index, output_dir, exported)
    manifest = build_recovery_manifest(
        sqlite_path,
        output_dir,
        metadata,
        exported,
        index_csv,
        all_conversations_csv,
        recovery_batch_id=recovery_batch_id,
        exported_at=exported_at,
    )
    training_corpus = export_vector_training_corpus(
        output_dir,
        manifest.get("recovery_batch_id", ""),
        sqlite_path,
        grouped,
        selected_ids,
        conversation_profile_map,
        metadata=metadata,
    )
    complete_chat_records = export_batch_complete_chat_records(
        output_dir,
        manifest.get("recovery_batch_id", ""),
        sqlite_path,
        grouped,
        selected_ids,
        conversation_profile_map,
        media_cache_index=media_cache_index,
        metadata=metadata,
    )
    manifest["vector_training_corpus"] = training_corpus
    manifest["complete_chat_records"] = complete_chat_records
    manifest["files"].extend(
        [
            {"kind": "vector_training_corpus_csv", "path": training_corpus["csv"]},
            {"kind": "vector_training_corpus_jsonl", "path": training_corpus["jsonl"]},
            {"kind": "complete_chat_csv", "path": complete_chat_records["csv"]},
            {"kind": "complete_chat_jsonl", "path": complete_chat_records["jsonl"]},
            {"kind": "complete_chat_audit_json", "path": complete_chat_records["audit_json"]},
            {"kind": "complete_chat_audit_md", "path": complete_chat_records["audit_md"]},
            {"kind": "undecoded_message_samples_csv", "path": complete_chat_records["undecoded_sample_csv"]},
        ]
    )
    manifest_path = write_recovery_manifest(output_dir, manifest)
    emit(logger, f"索引文件: {index_csv}")
    emit(logger, f"全部会话总表: {all_conversations_csv}")
    emit(logger, f"向量训练语料 CSV: {training_corpus['csv']}")
    emit(logger, f"向量训练语料 JSONL: {training_corpus['jsonl']}")
    emit(logger, f"完整聊天记录 CSV: {complete_chat_records['csv']}")
    emit(logger, f"完整聊天记录 JSONL: {complete_chat_records['jsonl']}")
    emit(logger, f"完整聊天记录核对报告: {complete_chat_records['audit_md']}")
    emit(logger, f"未充分解码样本: {complete_chat_records['undecoded_sample_csv']}")
    emit(logger, f"批次清单: {manifest_path}")
    emit(logger, "说明: sender_name_guess 会优先使用本机联系人索引，其次再根据消息内容做推测，无法保证 100% 准确。")
    emit(logger, "说明: 单聊名称通常能较好推测；外部群真实群名未必在当前恢复数据里，因此群名是 best-effort。")
    emit(logger, "说明: 图片恢复依赖本地企业微信缓存；只有当前机器上仍然存在缓存图片时，才能导出原始 jpg/png。")
    return {
        "ok": True,
        "sqlite_path": str(sqlite_path),
        "output_dir": str(output_dir),
        "metadata": metadata,
        "exported": exported,
        "index_csv": str(index_csv),
        "all_conversations_csv": str(all_conversations_csv),
        "training_corpus_csv": training_corpus["csv"],
        "training_corpus_jsonl": training_corpus["jsonl"],
        "complete_chat_csv": complete_chat_records["csv"],
        "complete_chat_jsonl": complete_chat_records["jsonl"],
        "complete_chat_audit_json": complete_chat_records["audit_json"],
        "complete_chat_audit_md": complete_chat_records["audit_md"],
        "undecoded_sample_csv": complete_chat_records["undecoded_sample_csv"],
        "manifest_json": str(manifest_path),
        "recovery_batch_id": manifest.get("recovery_batch_id", ""),
    }


def main():
    default_source = default_source_dir()
    default_output = default_organized_output_dir(default_source)
    parser = argparse.ArgumentParser(description="整理恢复出的企业微信聊天记录，按单个会话导出更易读的时间线。")
    parser.add_argument("--input-sqlite", default="", help="恢复结果 sqlite 路径；留空则在输出目录自动找最新文件")
    parser.add_argument("--source-dir", default=str(default_source), help="自动寻找恢复 sqlite 的目录")
    parser.add_argument("--output-dir", default=str(default_output), help="整理后的输出目录")
    parser.add_argument("--conversation-id", default="", help="只导出指定会话，例如 R:10955007092635064")
    parser.add_argument("--prefix", default="R:", help="默认导出此前缀的会话，R: 通常是群聊/外部联系会话")
    args = parser.parse_args()

    run_organize(
        input_sqlite=args.input_sqlite,
        source_dir=Path(args.source_dir),
        output_dir=Path(args.output_dir),
        conversation_id=args.conversation_id,
        prefix=args.prefix,
        logger=print,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
