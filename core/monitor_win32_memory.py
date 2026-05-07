"""Win32 memory backend: extract well-formed ledger blocks from WeChat process memory."""

from __future__ import annotations

import ctypes
import html
import re
from collections import Counter
from ctypes import wintypes
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple


class Win32MemoryMonitorError(Exception):
    """Raised when Win32 memory capture cannot return usable ledger messages."""


TraceFn = Optional[Callable[[str], None]]


PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
MEM_COMMIT = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MAX_CHUNK_BYTES = 2 * 1024 * 1024
DEFAULT_OVERLAP_BYTES = 4096

CLEAN_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
TRIM_EDGE_RE = re.compile(r"^[,，;；:：|/\\\-~.。\s]+|[,，;；:：|/\\\-~.。\s]+$")
COUNT_LINE_RE = re.compile(r"^(.+?)(?:\s*[xX*×]\s*|\s+)?(\d+(?:\.\d+)?)$")
HTML_TAG_RE = re.compile(r"<[^>]+>")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


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


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def _trace(trace: TraceFn, message: str) -> None:
    if trace is None:
        return
    try:
        trace(message)
    except Exception:
        pass


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否"}:
        return False
    return default


def _to_str_list(value: Any, default: Sequence[str]) -> List[str]:
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return list(default)
        parts = [x.strip() for x in re.split(r"[,，;；|]+", raw) if x.strip()]
        return parts or list(default)

    if isinstance(value, (list, tuple, set)):
        parts = [str(x).strip() for x in value if str(x).strip()]
        return parts or list(default)

    return list(default)


def _get_window_text(hwnd: int) -> str:
    length = int(user32.GetWindowTextLengthW(int(hwnd)) or 0)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(int(hwnd), buf, length + 1)
    return str(buf.value or "").strip()


def _get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(int(hwnd), buf, 256)
    return str(buf.value or "").strip()


def _is_window_visible(hwnd: int) -> bool:
    try:
        return bool(user32.IsWindowVisible(int(hwnd)))
    except Exception:
        return False


def _get_window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    try:
        user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        return int(pid.value or 0)
    except Exception:
        return 0


def _get_window_area(hwnd: int) -> int:
    rect = wintypes.RECT()
    try:
        if not user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
            return 0
        width = max(0, int(rect.right) - int(rect.left))
        height = max(0, int(rect.bottom) - int(rect.top))
        return width * height
    except Exception:
        return 0


def _is_minimized(hwnd: int) -> bool:
    try:
        return bool(user32.IsIconic(int(hwnd)))
    except Exception:
        return False


def _enum_wechat_windows(class_names: Sequence[str], window_names: Sequence[str]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    class_set = {str(x).strip().lower() for x in class_names if str(x).strip()}
    name_set = {str(x).strip().lower() for x in window_names if str(x).strip()}

    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum_proc_type
    def callback(hwnd: int, _lparam: int) -> bool:
        try:
            if not user32.IsWindow(hwnd) or not _is_window_visible(hwnd):
                return True

            title = _get_window_text(hwnd)
            class_name = _get_class_name(hwnd)
            title_lower = title.lower()
            class_lower = class_name.lower()

            class_hit = class_lower in class_set or any(c and c in class_lower for c in class_set)
            name_hit = title_lower in name_set or any(n and n in title_lower for n in name_set)
            wechat_hint = (
                "wechat" in title_lower
                or "weixin" in title_lower
                or "微信" in title_lower
                or "wechat" in class_lower
                or "weixin" in class_lower
            )

            if not (class_hit or name_hit or wechat_hint):
                return True

            results.append(
                {
                    "hwnd": int(hwnd),
                    "title": title,
                    "class_name": class_name,
                    "pid": _get_window_pid(hwnd),
                    "area": _get_window_area(hwnd),
                    "minimized": _is_minimized(hwnd),
                }
            )
        except Exception:
            pass
        return True

    user32.EnumWindows(callback, 0)
    return results


def _pick_target_window(
    windows: Sequence[Dict[str, Any]], preferred_names: Sequence[str], preferred_classes: Sequence[str]
) -> Optional[Dict[str, Any]]:
    if not windows:
        return None

    preferred_name_set = {x.strip().lower() for x in preferred_names if x.strip()}
    preferred_class_set = {x.strip().lower() for x in preferred_classes if x.strip()}

    def score(item: Dict[str, Any]) -> Tuple[int, int, int, int, int, int]:
        title = str(item.get("title") or "").lower()
        class_name = str(item.get("class_name") or "").lower()
        exact_name = 1 if title in preferred_name_set else 0
        contains_name = 1 if any(n and n in title for n in preferred_name_set) else 0
        exact_class = 1 if class_name in preferred_class_set else 0
        contains_class = 1 if any(c and c in class_name for c in preferred_class_set) else 0
        not_minimized = 1 if not item.get("minimized") else 0
        area = _safe_int(item.get("area"), 0)
        return (exact_name, contains_name, exact_class, contains_class, not_minimized, area)

    return max(windows, key=score)


def _open_process(pid: int) -> Optional[int]:
    if pid <= 0:
        return None
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, int(pid))
    if not handle:
        return None
    return int(handle)


def _close_handle(handle: Optional[int]) -> None:
    if handle:
        try:
            kernel32.CloseHandle(int(handle))
        except Exception:
            pass


def _read_process_memory(handle: int, address: int, size: int) -> bytes:
    if size <= 0:
        return b""
    buf = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    if not kernel32.ReadProcessMemory(int(handle), ctypes.c_void_p(address), buf, size, ctypes.byref(read)):
        return b""
    return buf.raw[: int(read.value or 0)]


def _iter_memory_regions(handle: int) -> Iterator[Tuple[int, int, int, int]]:
    mbi = MEMORY_BASIC_INFORMATION()
    address = 0

    virtual_query_ex = kernel32.VirtualQueryEx
    virtual_query_ex.restype = ctypes.c_size_t
    virtual_query_ex.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(MEMORY_BASIC_INFORMATION),
        ctypes.c_size_t,
    ]

    while True:
        ret = virtual_query_ex(int(handle), ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not ret:
            break

        base_address = int(mbi.BaseAddress or 0)
        region_size = int(mbi.RegionSize or 0)
        if region_size <= 0:
            break

        yield (
            base_address,
            region_size,
            int(mbi.State or 0),
            int(mbi.Protect or 0),
        )

        next_address = base_address + region_size
        if next_address <= address:
            break
        address = next_address


def _is_readable(protect: int, state: int) -> bool:
    if state != MEM_COMMIT:
        return False
    if protect & PAGE_GUARD or protect & PAGE_NOACCESS:
        return False
    readable_flags = {
        PAGE_READONLY,
        PAGE_READWRITE,
        PAGE_WRITECOPY,
        PAGE_EXECUTE_READ,
        PAGE_EXECUTE_READWRITE,
    }
    return any(protect & flag for flag in readable_flags)


def _normalize_marker_aliases(text: str, marker: str) -> str:
    if not marker:
        return text
    pattern = re.compile(rf"{re.escape(marker)}")
    return pattern.sub(marker, text)


def _normalize_text_fragment(text: str, start_marker: str, end_marker: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("\ufeff", "").replace("\u200b", "")
    text = HTML_COMMENT_RE.sub("\n", text)
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*div\s*>", "\n", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _normalize_marker_aliases(text, start_marker)
    text = _normalize_marker_aliases(text, end_marker)
    return text


def _extract_blocks_from_text(text: str, start_marker: str, end_marker: str) -> List[Tuple[str, int]]:
    blocks: List[Tuple[str, int]] = []
    if not text or not start_marker or not end_marker:
        return blocks

    index = 0
    while True:
        start_index = text.find(start_marker, index)
        if start_index < 0:
            break

        start_index_adj = start_index
        if start_index > 0 and text[start_index - 1] in {"#", "＃"}:
            start_index_adj = start_index - 1

        end_index = text.find(end_marker, start_index + len(start_marker))
        if end_index < 0:
            break

        block = text[start_index_adj : end_index + len(end_marker)].strip()
        if block:
            blocks.append((block, start_index_adj))

        index = end_index + len(end_marker)

    return blocks


def _clean_edge(text: str) -> str:
    return TRIM_EDGE_RE.sub("", text or "")


def _is_payload_line_valid(line: str) -> bool:
    compact = _clean_edge(line.strip())
    if not compact:
        return False
    if "\x00" in compact:
        return False
    if CLEAN_CONTROL_RE.search(compact):
        return False

    matched = COUNT_LINE_RE.match(compact)
    if not matched:
        return False

    item = _clean_edge(matched.group(1).strip())
    if not item:
        return False

    return True


def _is_today_token_present(text: str) -> bool:
    today = datetime.now()
    tokens = [
        today.strftime("%Y-%m-%d"),
        today.strftime("%Y/%m/%d"),
        f"{today.year}年{today.month}月{today.day}日",
        f"{today.month}/{today.day}",
        f"{today.month}-{today.day}",
    ]
    compact = text.replace(" ", "")
    return any(token.replace(" ", "") in compact for token in tokens)


def _build_order_header_pattern(start_marker: str, digits: int = 8, require_hash: bool = True) -> re.Pattern[str]:
    marker = re.escape((start_marker or "记账").strip() or "记账")
    d = max(1, int(digits))
    if require_hash:
        return re.compile(rf"^[#＃]{marker}(\d{{{d}}})$")
    return re.compile(rf"^[#＃]?{marker}(\d{{{d}}})$")


def _is_valid_order_id_ymd_seq(order_id: str) -> bool:
    value = str(order_id or "").strip()
    if not re.fullmatch(r"\d{8}", value):
        return False
    # 规则: yymmdd + 两位序号
    try:
        datetime.strptime(value[:6], "%y%m%d")
    except ValueError:
        return False
    return True


def _validate_block(
    block: str,
    start_marker: str,
    end_marker: str,
    max_block_chars: int,
    min_payload_lines: int,
    max_payload_lines: int,
    min_valid_ratio: float,
    require_today_token: bool,
    header_pattern: Optional[re.Pattern[str]] = None,
) -> Tuple[bool, str]:
    if not block:
        return False, "empty"
    if len(block) > max_block_chars:
        return False, "too_long"
    if "\x00" in block:
        return False, "contains_nul"
    if CLEAN_CONTROL_RE.search(block):
        return False, "contains_control"

    printable = sum(1 for ch in block if ch in "\n\t" or ch.isprintable())
    if printable / max(1, len(block)) < 0.98:
        return False, "low_printable_ratio"

    normalized = block.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    if len(lines) < 3:
        return False, "too_few_lines"

    if header_pattern is not None:
        matched = header_pattern.fullmatch(lines[0])
        if not matched:
            return False, "start_not_standalone"
        order_id = matched.group(1) if matched.lastindex else ""
        if order_id and not _is_valid_order_id_ymd_seq(order_id):
            return False, "order_id_invalid_date"
    elif lines[0] != start_marker:
        return False, "start_not_standalone"
    if lines[-1] != end_marker:
        return False, "end_not_standalone"

    payload_lines = lines[1:-1]
    if len(payload_lines) < min_payload_lines:
        return False, "payload_too_few"
    if len(payload_lines) > max_payload_lines:
        return False, "payload_too_many"

    valid_line_count = sum(1 for line in payload_lines if _is_payload_line_valid(line))
    valid_ratio = valid_line_count / max(1, len(payload_lines))
    if valid_ratio < min_valid_ratio:
        return False, "payload_invalid_ratio"

    if require_today_token and not _is_today_token_present(normalized):
        return False, "today_token_missing"

    return True, "ok"


def _extract_candidates_from_blob(blob: bytes, start_marker: str, end_marker: str) -> List[Tuple[str, int]]:
    if not blob:
        return []

    candidates: List[Tuple[str, int]] = []
    for encoding in ("utf-16le", "utf-8", "gbk", "cp936"):
        try:
            decoded = blob.decode(encoding, errors="ignore")
        except Exception:
            continue

        normalized = _normalize_text_fragment(decoded, start_marker, end_marker)
        blocks = _extract_blocks_from_text(normalized, start_marker, end_marker)
        if not blocks:
            continue

        candidates.extend(blocks)

    return candidates


def fetch_recent_messages_win32_memory(config: Dict[str, Any], trace: TraceFn = None) -> List[Dict[str, str]]:
    start_marker = str(config.get("record_start_marker", config.get("prefix", "记账"))).strip()
    start_marker = start_marker.lstrip("#＃").strip() or "记账"
    end_marker = str(config.get("record_end_marker", "结束")).strip()
    if not start_marker or not end_marker:
        raise Win32MemoryMonitorError("开始或结束标记不能为空。")

    max_messages = _safe_int(config.get("max_messages", 30), 30)
    if max_messages <= 0:
        max_messages = 30

    class_names = _to_str_list(
        config.get("wechat_class_names", config.get("wechat_class_name", "Qt51514QWindowIcon")),
        ["Qt51514QWindowIcon", "WeChatMainWndForPC"],
    )
    window_names = _to_str_list(
        config.get("wechat_window_names", config.get("wechat_window_name", "微信")),
        ["微信"],
    )

    overlap_bytes = _safe_int(config.get("win32_memory_overlap_bytes", DEFAULT_OVERLAP_BYTES), DEFAULT_OVERLAP_BYTES)
    if overlap_bytes < 0:
        overlap_bytes = 0

    max_chunk_bytes = _safe_int(config.get("win32_memory_max_chunk_bytes", MAX_CHUNK_BYTES), MAX_CHUNK_BYTES)
    if max_chunk_bytes <= 0:
        max_chunk_bytes = MAX_CHUNK_BYTES

    recent_blocks = _safe_int(config.get("win32_memory_recent_blocks", max(max_messages, 6)), max(max_messages, 6))
    if recent_blocks <= 0:
        recent_blocks = max(max_messages, 6)

    max_block_chars = _safe_int(config.get("win32_memory_max_block_chars", 600), 600)
    min_payload_lines = _safe_int(config.get("win32_memory_min_payload_lines", 2), 2)
    max_payload_lines = _safe_int(config.get("win32_memory_max_payload_lines", 40), 40)
    min_valid_ratio = _safe_float(config.get("win32_memory_min_valid_line_ratio", 0.8), 0.8)
    min_valid_ratio = min(max(min_valid_ratio, 0.0), 1.0)
    require_today_token = _safe_bool(config.get("win32_memory_require_today_token", False), False)
    require_order_header = _safe_bool(config.get("order_id_required", True), True)
    order_id_digits = _safe_int(config.get("order_id_digits", 8), 8)
    order_id_require_hash = _safe_bool(config.get("order_id_require_hash", True), True)
    header_pattern = (
        _build_order_header_pattern(start_marker, order_id_digits, order_id_require_hash)
        if require_order_header
        else None
    )

    _trace(
        trace,
        (
            "[WIN32MEM] 抓取参数: "
            f"start={start_marker}, end={end_marker}, max_messages={max_messages}, "
            f"recent_blocks={recent_blocks}, max_block_chars={max_block_chars}"
        ),
    )

    windows = _enum_wechat_windows(class_names, window_names)
    _trace(trace, f"[WIN32MEM] 微信窗口候选数={len(windows)}")
    if not windows:
        raise Win32MemoryMonitorError("未找到匹配的微信窗口。")

    target = _pick_target_window(windows, window_names, class_names)
    if not target:
        raise Win32MemoryMonitorError("未能选中有效的微信窗口。")

    pid = _safe_int(target.get("pid"), 0)
    _trace(
        trace,
        (
            "[WIN32MEM] 选中窗口: "
            f"hwnd={target.get('hwnd')}, title={target.get('title') or '<空>'}, "
            f"class={target.get('class_name') or '<空>'}, pid={pid}"
        ),
    )
    if pid <= 0:
        raise Win32MemoryMonitorError("选中窗口未获取到有效进程 ID。")

    handle = _open_process(pid)
    if not handle:
        raise Win32MemoryMonitorError(f"无法打开微信进程进行内存读取: pid={pid}")

    try:
        regions_total = 0
        bytes_total = 0
        raw_candidates = 0
        rejected = 0
        rejected_reasons: Counter[str] = Counter()

        best_by_block: Dict[str, Dict[str, Any]] = {}

        for base_address, region_size, state, protect in _iter_memory_regions(handle):
            regions_total += 1
            if region_size <= 0 or not _is_readable(protect, state):
                continue

            tail = b""
            offset = 0
            while offset < region_size:
                chunk_size = min(max_chunk_bytes, region_size - offset)
                if chunk_size <= 0:
                    break

                chunk_address = base_address + offset
                blob = _read_process_memory(handle, chunk_address, chunk_size)
                offset += chunk_size
                if not blob:
                    tail = b""
                    continue

                bytes_total += len(blob)
                combined = tail + blob if tail else blob
                combined_base_address = chunk_address - len(tail)
                candidates = _extract_candidates_from_blob(combined, start_marker, end_marker)
                raw_candidates += len(candidates)

                for block, local_index in candidates:
                    ok, reason = _validate_block(
                        block,
                        start_marker,
                        end_marker,
                        max_block_chars,
                        min_payload_lines,
                        max_payload_lines,
                        min_valid_ratio,
                        require_today_token,
                        header_pattern=header_pattern,
                    )
                    if not ok:
                        rejected += 1
                        rejected_reasons[reason] += 1
                        continue

                    approx_address = combined_base_address + max(0, local_index)
                    existing = best_by_block.get(block)
                    if existing is None or approx_address > _safe_int(existing.get("address"), -1):
                        best_by_block[block] = {"text": block, "address": approx_address}

                if overlap_bytes > 0:
                    tail = combined[-overlap_bytes:]
                else:
                    tail = b""

        valid_blocks = list(best_by_block.values())
        valid_blocks.sort(key=lambda item: _safe_int(item.get("address"), -1), reverse=True)

        if recent_blocks > 0:
            valid_blocks = valid_blocks[:recent_blocks]

        selected_blocks = valid_blocks[:max_messages]
        selected_blocks.reverse()  # 旧->新

        _trace(
            trace,
            (
                "[WIN32MEM] 扫描完成: "
                f"regions={regions_total}, bytes={bytes_total}, raw={raw_candidates}, "
                f"valid={len(best_by_block)}, selected={len(selected_blocks)}, rejected={rejected}"
            ),
        )
        if rejected_reasons:
            _trace(trace, f"[WIN32MEM] 拒绝原因统计: {dict(rejected_reasons)}")

        if selected_blocks:
            sample_blocks = [str(item.get("text", "")) for item in selected_blocks[:3]]
            _trace(trace, f"[WIN32MEM] 样本: {sample_blocks}")
            return [
                {
                    "timestamp": "",
                    "message": str(item.get("text", "")).strip(),
                    "source_id": f"addr:{_safe_int(item.get('address'), 0):x}",
                }
                for item in selected_blocks
            ]
    finally:
        _close_handle(handle)

    raise Win32MemoryMonitorError(
        f"内存扫描未找到包含“{start_marker} ... {end_marker}”的高质量完整记账块。"
    )
