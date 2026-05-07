"""Win32 clipboard backend: capture chat content by simulated copy."""

from __future__ import annotations

import ctypes
import re
import time
from ctypes import wintypes
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


class Win32MonitorError(Exception):
    """Win32 clipboard capture error."""


TraceFn = Optional[Callable[[str], None]]


SW_RESTORE = 9
CF_UNICODETEXT = 13
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
VK_CONTROL = 0x11
VK_A = 0x41
VK_C = 0x43


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
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


def _to_lower_set(values: Sequence[str]) -> set[str]:
    return {str(v).strip().lower() for v in values if str(v).strip()}


def _is_window_minimized(hwnd: int) -> bool:
    try:
        return bool(user32.IsIconic(int(hwnd)))
    except Exception:
        return False


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


def _get_window_rect(hwnd: int) -> Optional[RECT]:
    rect = RECT()
    if not user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
        return None
    return rect


def _window_area(rect: Optional[RECT]) -> int:
    if rect is None:
        return 0
    width = max(0, int(rect.right - rect.left))
    height = max(0, int(rect.bottom - rect.top))
    return width * height


def _enum_top_windows() -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum_proc_type
    def callback(hwnd: int, _lparam: int) -> bool:
        try:
            if not user32.IsWindow(hwnd):
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            if not user32.IsWindowEnabled(hwnd):
                return True
            title = _get_window_text(hwnd)
            class_name = _get_class_name(hwnd)
            if not title and not class_name:
                return True
            rect = _get_window_rect(hwnd)
            results.append(
                {
                    "hwnd": int(hwnd),
                    "title": title,
                    "class_name": class_name,
                    "rect": rect,
                    "area": _window_area(rect),
                    "minimized": _is_window_minimized(hwnd),
                }
            )
        except Exception:
            pass
        return True

    user32.EnumWindows(callback, 0)
    return results


def _score_window(
    item: Dict[str, Any], class_names: Sequence[str], window_names: Sequence[str]
) -> Tuple[int, int, int, int, int]:
    title = str(item.get("title", "")).strip().lower()
    class_name = str(item.get("class_name", "")).strip().lower()
    class_set = _to_lower_set(class_names)
    name_set = _to_lower_set(window_names)

    name_exact = 0
    name_contains = 0
    for expected in name_set:
        if title == expected:
            name_exact = 1
            name_contains = 1
            break
        if expected and expected in title:
            name_contains = 1

    class_exact = 1 if class_name in class_set else 0
    class_contains = 0
    if not class_exact:
        for expected in class_set:
            if expected and expected in class_name:
                class_contains = 1
                break

    visible = 0 if bool(item.get("minimized", False)) else 1
    area = int(item.get("area", 0) or 0)
    return (name_exact, name_contains, class_exact or class_contains, visible, area)


def _select_target_window(config: Dict[str, Any], trace: TraceFn = None) -> Dict[str, Any]:
    class_names = _to_str_list(
        config.get("wechat_class_names", config.get("wechat_class_name", "Qt51514QWindowIcon")),
        ["Qt51514QWindowIcon", "WeChatMainWndForPC"],
    )
    window_names = _to_str_list(
        config.get("wechat_window_names", config.get("wechat_window_name", "")),
        [],
    )

    windows = _enum_top_windows()
    _trace(trace, f"[WIN32] 顶层可见窗口数={len(windows)}")
    if not windows:
        raise Win32MonitorError("未枚举到可见顶层窗口。")

    class_set = _to_lower_set(class_names)
    name_set = _to_lower_set(window_names)
    candidates: List[Dict[str, Any]] = []
    for item in windows:
        title_lower = str(item.get("title", "")).lower()
        class_lower = str(item.get("class_name", "")).lower()

        class_hit = class_lower in class_set or any(
            expected and expected in class_lower for expected in class_set
        )
        name_hit = title_lower in name_set or any(
            expected and expected in title_lower for expected in name_set
        )
        wechat_hint = (
            "wechat" in title_lower
            or "weixin" in title_lower
            or "微信" in title_lower
            or "wechat" in class_lower
            or "weixin" in class_lower
        )

        if class_hit or name_hit or wechat_hint:
            candidates.append(item)

    if not candidates:
        raise Win32MonitorError("未找到匹配的微信窗口候选。")

    candidates.sort(key=lambda x: _score_window(x, class_names, window_names), reverse=True)
    top = candidates[0]
    _trace(
        trace,
        f"[WIN32] 选中窗口: hwnd={top['hwnd']}, title={top['title'] or '<空>'}, class={top['class_name'] or '<空>'}, minimized={top['minimized']}, area={top['area']}",
    )
    return top


def _activate_window(hwnd: int, trace: TraceFn = None) -> None:
    user32.ShowWindow(int(hwnd), SW_RESTORE)
    user32.SetForegroundWindow(int(hwnd))
    _trace(trace, f"[WIN32] 已请求激活窗口 hwnd={hwnd}")


def _click_window_area(hwnd: int, ratio_x: float, ratio_y: float, trace: TraceFn = None) -> bool:
    rect = _get_window_rect(hwnd)
    if rect is None:
        return False
    width = max(1, int(rect.right - rect.left))
    height = max(1, int(rect.bottom - rect.top))
    x = int(rect.left + width * max(0.0, min(1.0, ratio_x)))
    y = int(rect.top + height * max(0.0, min(1.0, ratio_y)))

    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    _trace(trace, f"[WIN32] 已点击窗口区域: ({x}, {y}) ratio=({ratio_x:.2f}, {ratio_y:.2f})")
    return True


def _tap_key(vk: int) -> None:
    user32.keybd_event(int(vk), 0, 0, 0)
    user32.keybd_event(int(vk), 0, KEYEVENTF_KEYUP, 0)


def _send_ctrl_combo(vk: int, wait: float = 0.04) -> None:
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(wait)
    _tap_key(vk)
    time.sleep(wait)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def _clear_clipboard(trace: TraceFn = None) -> None:
    for _ in range(30):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                _trace(trace, "[WIN32] 已清空剪贴板。")
                return
            finally:
                user32.CloseClipboard()
        time.sleep(0.02)
    _trace(trace, "[WIN32] 清空剪贴板失败（非致命）。")


def _read_clipboard_text(wait_seconds: float, trace: TraceFn = None) -> str:
    deadline = time.time() + max(0.2, wait_seconds)
    while time.time() < deadline:
        if not user32.OpenClipboard(None):
            time.sleep(0.02)
            continue
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                time.sleep(0.02)
                continue
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                time.sleep(0.02)
                continue
            try:
                text = ctypes.wstring_at(pointer) or ""
            finally:
                kernel32.GlobalUnlock(handle)
            text = text.strip()
            if text:
                _trace(trace, f"[WIN32] 剪贴板文本长度={len(text)}")
                return text
        finally:
            user32.CloseClipboard()
        time.sleep(0.02)
    return ""


def _extract_prefixed_messages_from_clipboard(
    clipboard_text: str, prefix: str, max_messages: int, trace: TraceFn = None
) -> List[Dict[str, str]]:
    if not clipboard_text:
        return []

    normalized = clipboard_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]

    # Strategy 1: collect line-level hits.
    candidates: List[str] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if text.startswith(prefix):
            candidates.append(text)
            continue
        idx = text.find(prefix)
        if idx >= 0:
            if idx > 0 and text[idx - 1] in {"#", "＃"}:
                idx -= 1
            candidates.append(text[idx:].strip())

    # Strategy 2: collect block-level hits for multiline entries.
    if not candidates:
        blocks = [blk.strip() for blk in re.split(r"\n\s*\n+", normalized) if blk.strip()]
        for block in blocks:
            idx = block.find(prefix)
            if idx >= 0:
                if idx > 0 and block[idx - 1] in {"#", "＃"}:
                    idx -= 1
                candidates.append(block[idx:].strip())

    deduped: List[str] = []
    seen: set[str] = set()
    for text in candidates:
        value = text.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)

    selected = deduped[-max_messages:]
    _trace(
        trace,
        f"[WIN32] 剪贴板提取: raw_candidates={len(candidates)}, deduped={len(deduped)}, selected={len(selected)}",
    )
    if selected:
        _trace(trace, f"[WIN32] 剪贴板样本: {selected[:3]}")
    return [{"timestamp": "", "message": text} for text in selected]


def fetch_recent_messages_win32_clipboard(
    config: Dict[str, Any], trace: TraceFn = None
) -> List[Dict[str, str]]:
    """Capture recent messages by Win32 clipboard simulation."""
    prefix = str(config.get("prefix", "")).strip()
    if not prefix:
        raise Win32MonitorError("配置中的 prefix 不能为空。")

    max_messages = _safe_int(config.get("max_messages", 30), 30)
    if max_messages <= 0:
        max_messages = 30

    copy_wait_seconds = _safe_float(config.get("win32_clipboard_wait_seconds", 0.8), 0.8)
    attempts = _safe_int(config.get("win32_clipboard_attempts", 3), 3)
    if attempts <= 0:
        attempts = 3

    # Click points from top-middle to center-middle (chat history is usually in upper half).
    click_y_candidates = [0.28, 0.36, 0.45]
    click_x = _safe_float(config.get("win32_clipboard_click_ratio_x", 0.56), 0.56)

    _trace(
        trace,
        f"[WIN32] 抓取参数: prefix={prefix}, max_messages={max_messages}, attempts={attempts}, wait={copy_wait_seconds}",
    )

    target = _select_target_window(config, trace=trace)
    hwnd = int(target["hwnd"])

    for attempt in range(1, attempts + 1):
        _trace(trace, f"[WIN32] 复制尝试 {attempt}/{attempts}")
        _activate_window(hwnd, trace=trace)
        time.sleep(0.15)

        click_y = click_y_candidates[(attempt - 1) % len(click_y_candidates)]
        _click_window_area(hwnd, click_x, click_y, trace=trace)
        time.sleep(0.08)

        _clear_clipboard(trace=trace)
        _send_ctrl_combo(VK_A)
        time.sleep(0.05)
        _send_ctrl_combo(VK_C)
        time.sleep(0.08)

        clipboard_text = _read_clipboard_text(copy_wait_seconds, trace=trace)
        if not clipboard_text:
            _trace(trace, "[WIN32] 剪贴板为空，继续重试。")
            continue

        messages = _extract_prefixed_messages_from_clipboard(
            clipboard_text, prefix, max_messages, trace=trace
        )
        if messages:
            return messages

    raise Win32MonitorError("Win32 剪贴板方案未抓取到前缀消息。")
