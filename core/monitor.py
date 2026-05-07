"""监控模块：从微信窗口抓取最近消息（兼容群聊与私聊）。"""

from __future__ import annotations

import ctypes
import re
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    import uiautomation as auto
except ImportError:
    auto = None


class MonitorError(Exception):
    """监控相关错误。"""


TraceFn = Optional[Callable[[str], None]]


def _trace(trace: TraceFn, message: str) -> None:
    """输出抓取过程日志。"""
    if trace is None:
        return
    try:
        trace(message)
    except Exception:
        pass


def _to_str_list(value: Any, default: Sequence[str]) -> List[str]:
    """将配置项规范为字符串列表。"""
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


def _to_bool(value: Any, default: bool = False) -> bool:
    """将配置项规范为 bool。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "y", "on", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否"}:
        return False
    return default


def _safe_get_children(control: Any) -> List[Any]:
    """安全读取子控件。"""
    try:
        children = control.GetChildren()
        return children or []
    except Exception:
        return []


def _safe_get_name(control: Any) -> str:
    """安全读取控件名称。"""
    try:
        return (getattr(control, "Name", "") or "").strip()
    except Exception:
        return ""


def _safe_get_control_type(control: Any) -> str:
    """安全读取控件类型名。"""
    try:
        return (getattr(control, "ControlTypeName", "") or "").strip().lower()
    except Exception:
        return ""


def _safe_get_class_name(control: Any) -> str:
    """安全读取控件类名。"""
    try:
        return (getattr(control, "ClassName", "") or "").strip()
    except Exception:
        return ""


def _safe_get_hwnd(control: Any) -> int:
    """安全读取原生窗口句柄。"""
    try:
        return int(getattr(control, "NativeWindowHandle", 0) or 0)
    except Exception:
        return 0


def _safe_get_process_id(control: Any) -> int:
    """安全读取控件所属进程 ID。"""
    try:
        return int(getattr(control, "ProcessId", 0) or 0)
    except Exception:
        return 0


def _safe_get_is_keyboard_focusable(control: Any) -> bool:
    """安全读取 IsKeyboardFocusable 属性。"""
    try:
        return bool(getattr(control, "IsKeyboardFocusable", False))
    except Exception:
        return False


def _safe_get_value(control: Any) -> str:
    """安全读取控件 Value 文本。"""
    # 1) 直接属性
    for attr in ("Value", "LegacyIAccessibleValue"):
        try:
            value = getattr(control, attr, "")
            text = str(value or "").strip()
            if text:
                return text
        except Exception:
            pass

    # 2) ValuePattern
    try:
        pattern = control.GetValuePattern()
        if pattern is not None:
            text = str(getattr(pattern, "Value", "") or "").strip()
            if text:
                return text
    except Exception:
        pass

    # 3) LegacyIAccessiblePattern
    try:
        pattern = control.GetLegacyIAccessiblePattern()
        if pattern is not None:
            text = str(getattr(pattern, "Value", "") or "").strip()
            if text:
                return text
    except Exception:
        pass

    return ""


def _safe_get_legacy_texts(control: Any) -> List[str]:
    """通过 LegacyIAccessiblePattern 尽可能提取文本。"""
    texts: List[str] = []
    pattern = None
    try:
        pattern = control.GetLegacyIAccessiblePattern()
    except Exception:
        pattern = None

    if pattern is None:
        return texts

    for attr in ("Name", "Value", "Description", "Help", "DefaultAction", "KeyboardShortcut"):
        try:
            value = getattr(pattern, attr, "")
            text = str(value or "").strip()
            if text:
                texts.append(text)
        except Exception:
            continue

    return texts


def _safe_get_text_pattern_text(control: Any) -> str:
    """通过 TextPattern 提取文本（若控件支持）。"""
    try:
        pattern = control.GetTextPattern()
        if pattern is None:
            return ""
        doc = getattr(pattern, "DocumentRange", None)
        if doc is None:
            return ""
        text = str(doc.GetText(-1) or "").strip()
        return text
    except Exception:
        return ""


def _extract_text_candidates(control: Any) -> List[str]:
    """统一提取控件的多来源文本（Name/Value/Legacy/TextPattern）。"""
    raw_values: List[str] = []

    name = _safe_get_name(control)
    if name:
        raw_values.append(name)

    value = _safe_get_value(control)
    if value:
        raw_values.append(value)

    help_text = ""
    try:
        help_text = str(getattr(control, "HelpText", "") or "").strip()
    except Exception:
        help_text = ""
    if help_text:
        raw_values.append(help_text)

    try:
        window_text = str(control.GetWindowText() or "").strip()
        if window_text:
            raw_values.append(window_text)
    except Exception:
        pass

    raw_values.extend(_safe_get_legacy_texts(control))

    text_pattern_text = _safe_get_text_pattern_text(control)
    if text_pattern_text:
        raw_values.append(text_pattern_text)

    # 去重并保序，过滤过长空白文本。
    result: List[str] = []
    seen: set[str] = set()
    for value_text in raw_values:
        text = " ".join(str(value_text or "").split())
        if not text:
            continue
        if len(text) > 2000:
            text = text[:2000]
        if text in seen:
            continue
        seen.add(text)
        result.append(text)

    return result


def _to_lower_set(values: Sequence[str]) -> set[str]:
    """将字符串序列规范为小写集合。"""
    return {str(v).strip().lower() for v in values if str(v).strip()}


def _normalize_control_type_name(value: str) -> str:
    """将配置里的控件类型规范到 ControlTypeName 风格。"""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    mapping = {
        "window": "windowcontrol",
        "windowcontrol": "windowcontrol",
        "pane": "panecontrol",
        "panecontrol": "panecontrol",
    }
    return mapping.get(raw, raw)


def _control_area(control: Any) -> int:
    """计算通用控件面积。"""
    try:
        rect = control.BoundingRectangle
        width = max(0, int(rect.width()))
        height = max(0, int(rect.height()))
        return width * height
    except Exception:
        return 0


def _window_key(window: Any) -> Tuple[str, str, str]:
    """窗口去重键。"""
    hwnd = str(getattr(window, "NativeWindowHandle", "") or "")
    return (_safe_get_class_name(window), _safe_get_name(window), hwnd)


def _deduplicate_windows(windows: Sequence[Any]) -> List[Any]:
    """按类名/标题/HWND 去重窗口。"""
    result: List[Any] = []
    seen: set[Tuple[str, str, str]] = set()
    for win in windows:
        key = _window_key(win)
        if key in seen:
            continue
        seen.add(key)
        result.append(win)
    return result


def _iter_top_level_candidates() -> List[Any]:
    """枚举桌面顶层候选控件。"""
    try:
        root = auto.GetRootControl()
    except Exception:
        return []

    candidates: List[Any] = []
    for child in _safe_get_children(root):
        ctype = _safe_get_control_type(child)
        if ctype in {"windowcontrol", "panecontrol", "customcontrol"}:
            candidates.append(child)
    return candidates


def _window_area(window: Any) -> int:
    """计算窗口面积，用于候选窗口评分。"""
    return _control_area(window)


def _window_score(window: Any, preferred_names: set[str]) -> Tuple[int, int, int, int, int, int]:
    """窗口评分：优先窗口名命中 + 可聚焦窗口控件。"""
    title = _safe_get_name(window).strip()
    title_lower = title.lower()
    control_type = _safe_get_control_type(window)
    is_focusable = _safe_get_is_keyboard_focusable(window)
    not_minimized = not _is_window_minimized(window)

    name_exact = 0
    name_contains = 0
    for expected in preferred_names:
        if not expected:
            continue
        if title_lower == expected:
            name_exact = 1
            name_contains = 1
            break
        if expected in title_lower:
            name_contains = 1

    type_is_window = 1 if control_type == "windowcontrol" else 0
    focusable_score = 1 if is_focusable else 0
    visible_score = 1 if not_minimized else 0
    area_score = _window_area(window)
    return (
        name_exact,
        name_contains,
        type_is_window,
        focusable_score,
        visible_score,
        area_score,
    )


def _activate_window(window: Any, trace: TraceFn = None) -> None:
    """尝试激活窗口，提升 UIA 可见性与可读性。"""
    hwnd = _safe_get_hwnd(window)
    if hwnd > 0:
        try:
            user32 = ctypes.windll.user32
            # SW_RESTORE=9，确保最小化窗口可见。
            user32.ShowWindow(int(hwnd), 9)
            user32.SetForegroundWindow(int(hwnd))
        except Exception as exc:
            _trace(trace, f"[UIA] SetForegroundWindow 失败: hwnd={hwnd}, 原因={exc}")

    for method_name in ("SetActive", "SetFocus"):
        try:
            method = getattr(window, method_name, None)
            if callable(method):
                method()
                _trace(trace, f"[UIA] 窗口激活成功: {method_name}()")
                return
        except Exception:
            continue

    try:
        click = getattr(window, "Click", None)
        if callable(click):
            click()
            _trace(trace, "[UIA] 窗口激活兜底: Click()")
    except Exception:
        pass


def _is_window_minimized(window: Any) -> bool:
    """通过窗口矩形和离屏状态判断窗口是否可能最小化。"""
    try:
        if getattr(window, "IsOffscreen", False):
            return True
    except Exception:
        pass

    try:
        rect = window.BoundingRectangle
        return rect.width() <= 0 or rect.height() <= 0
    except Exception:
        return False


def _collect_texts(control: Any, max_depth: int = 4) -> List[str]:
    """递归提取控件文本。"""
    texts: List[str] = []

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return

        texts.extend(_extract_text_candidates(node))

        for child in _safe_get_children(node):
            walk(child, depth + 1)

    walk(control, 0)
    return texts


def _normalize_prefixed_text(text: str, prefix: str) -> str:
    """将包含前缀的文本规范化为从前缀开始。"""
    value = (text or "").strip()
    if not value:
        return ""
    if value.startswith(prefix):
        return value
    idx = value.find(prefix)
    if idx >= 0:
        # 若前一位是 #/＃，保留在规范化结果中，避免丢失单号头
        if idx > 0 and value[idx - 1] in {"#", "＃"}:
            idx -= 1
        return value[idx:].strip()
    return ""


def _dedupe_texts_keep_order(texts: Sequence[str]) -> List[str]:
    """文本去重并保序。"""
    result: List[str] = []
    seen: set[str] = set()
    for text in texts:
        value = (text or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _scan_prefixed_texts_walkcontrol(
    root: Any,
    prefix: str,
    max_depth: int,
    trace: TraceFn = None,
) -> Tuple[List[str], int]:
    """使用 WalkControl 深扫 prefix 文本。"""
    raw_candidates: List[str] = []
    visited = 0
    try:
        for node, _depth in auto.WalkControl(root, includeTop=True, maxDepth=max_depth):
            visited += 1
            for text in _extract_text_candidates(node):
                normalized = _normalize_prefixed_text(text, prefix)
                if normalized:
                    raw_candidates.append(normalized)
    except Exception as exc:
        _trace(trace, f"[UIA] WalkControl 深扫异常: {exc}")
    return raw_candidates, visited


def _scan_prefixed_texts_descendants(
    root: Any,
    prefix: str,
    max_depth: int,
    max_nodes: int,
) -> Tuple[List[str], int]:
    """通过 GetChildren 做子孙节点全量扫描（BFS）。"""
    raw_candidates: List[str] = []
    visited = 0
    queue: deque[Tuple[Any, int]] = deque([(root, 0)])

    while queue and visited < max_nodes:
        node, depth = queue.popleft()
        visited += 1

        for text in _extract_text_candidates(node):
            normalized = _normalize_prefixed_text(text, prefix)
            if normalized:
                raw_candidates.append(normalized)

        if depth >= max_depth:
            continue

        for child in _safe_get_children(node):
            queue.append((child, depth + 1))

    return raw_candidates, visited


def _extract_prefixed_messages_deep_scan(
    root: Any,
    prefix: str,
    max_messages: int,
    trace: TraceFn = None,
    scan_label: str = "窗口",
) -> List[Dict[str, str]]:
    """深层全量扫描提取前缀消息。"""
    walk_raw, walk_visited = _scan_prefixed_texts_walkcontrol(root, prefix, max_depth=40, trace=trace)
    bfs_raw, bfs_visited = _scan_prefixed_texts_descendants(root, prefix, max_depth=40, max_nodes=50000)

    merged_raw = walk_raw + bfs_raw
    deduped = _dedupe_texts_keep_order(merged_raw)
    selected = deduped[-max_messages:]

    _trace(
        trace,
        f"[UIA] {scan_label}深扫: walk_visited={walk_visited}, bfs_visited={bfs_visited}, raw={len(merged_raw)}, deduped={len(deduped)}, selected={len(selected)}",
    )
    if selected:
        _trace(trace, f"[UIA] {scan_label}深扫样本: {selected[:5]}")

    return [{"timestamp": "", "message": text} for text in selected]


def _extract_prefixed_messages_from_window(
    window: Any, prefix: str, max_messages: int, trace: TraceFn = None
) -> List[Dict[str, str]]:
    """在无 ListControl 时，直接从窗口文本树提取前缀消息。"""
    return _extract_prefixed_messages_deep_scan(
        window,
        prefix,
        max_messages,
        trace=trace,
        scan_label="窗口",
    )


def _extract_message(item: Any, prefix: str) -> Optional[Dict[str, str]]:
    """从单个消息节点中提取时间与消息文本。"""
    texts = _collect_texts(item)
    if not texts:
        return None

    message = ""
    timestamp = ""

    for text in reversed(texts):
        candidate = text.strip()
        if candidate.startswith(prefix):
            message = candidate
            break

    if not message:
        for text in reversed(texts):
            candidate = text.strip()
            if prefix in candidate:
                idx = candidate.find(prefix)
                message = candidate[idx:].strip()
                break

    if not message:
        return None

    for text in texts:
        t = text.strip()
        # 常见时间格式：09:30 / 9:30 / 2026-05-06 09:30
        if ":" in t and len(t) <= 20:
            timestamp = t
            break

    return {
        "timestamp": timestamp,
        "message": message,
    }


def _collect_list_controls(control: Any, max_depth: int = 12) -> List[Any]:
    """递归收集 ListControl 候选。"""
    result: List[Any] = []

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if _safe_get_control_type(node) == "listcontrol":
            result.append(node)
        for child in _safe_get_children(node):
            walk(child, depth + 1)

    walk(control, 0)
    return result


def _walk_controls(control: Any, max_depth: int = 12) -> List[Any]:
    """深度优先遍历控件树。"""
    result: List[Any] = []

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        result.append(node)
        for child in _safe_get_children(node):
            walk(child, depth + 1)

    walk(control, 0)
    return result


def _collect_controls_by_type(control: Any, control_type: str, max_depth: int = 12) -> List[Any]:
    """按控件类型收集节点。"""
    expected = (control_type or "").strip().lower()
    if not expected:
        return []
    return [node for node in _walk_controls(control, max_depth=max_depth) if _safe_get_control_type(node) == expected]


def _collect_message_pane_candidates(
    window: Any,
    pane_names: Sequence[str],
    pane_class_names: Sequence[str],
    trace: TraceFn = None,
) -> List[Any]:
    """收集消息区域 Pane 候选（兼容新版微信无 ListControl 结构）。"""
    name_lowers = _to_lower_set(pane_names)
    class_lowers = _to_lower_set(pane_class_names)
    window_pid = _safe_get_process_id(window)

    candidates: List[Any] = []
    for node in _collect_controls_by_type(window, "panecontrol", max_depth=14):
        node_name = _safe_get_name(node)
        node_class = _safe_get_class_name(node)
        node_name_lower = node_name.lower()
        node_class_lower = node_class.lower()

        name_hit = bool(node_name_lower and node_name_lower in name_lowers)
        class_hit = bool(node_class_lower and node_class_lower in class_lowers)
        wechat_hint = "weixin" in node_name_lower or "wechat" in node_name_lower
        class_hint = "mmuirendersubwindowhw" in node_class_lower
        pid_hit = window_pid > 0 and _safe_get_process_id(node) == window_pid

        if name_hit or class_hit or wechat_hint or class_hint:
            # 优先同进程节点，避免跨窗口误抓。
            if pid_hit:
                candidates.append(node)
            else:
                candidates.append(node)

    # 面积优先，保留可能是主消息渲染区域的 Pane。
    candidates.sort(key=_control_area, reverse=True)
    _trace(
        trace,
        f"[UIA] Pane 消息区域候选数={len(candidates)} (names={list(pane_names)}, classes={list(pane_class_names)})",
    )
    return candidates


def _extract_messages_from_container(
    container: Any, prefix: str, max_messages: int, trace: TraceFn = None
) -> List[Dict[str, str]]:
    """从 Pane 容器中提取最近消息（优先 ListItem，失败则文本树兜底）。"""
    list_items = _collect_controls_by_type(container, "listitemcontrol", max_depth=10)
    if list_items:
        recent_items = list_items[-max_messages:]
        results: List[Dict[str, str]] = []
        for item in recent_items:
            extracted = _extract_message(item, prefix)
            if extracted and extracted["message"].startswith(prefix):
                results.append(extracted)
        _trace(
            trace,
            f"[UIA] Pane-ListItem 提取: list_items={len(list_items)}, recent={len(recent_items)}, prefix_hits={len(results)}",
        )
        if results:
            return results

    # 文本树兜底：从容器内文本中筛 prefix
    raw_candidates: List[str] = []
    try:
        for node, _depth in auto.WalkControl(container, includeTop=False, maxDepth=14):
            for text in _extract_text_candidates(node):
                normalized = _normalize_prefixed_text(text, prefix)
                if normalized:
                    raw_candidates.append(normalized)
    except Exception:
        pass

    if not raw_candidates:
        for text in _collect_texts(container, max_depth=14):
            normalized = _normalize_prefixed_text(text, prefix)
            if normalized:
                raw_candidates.append(normalized)

    deduped = _dedupe_texts_keep_order(raw_candidates)

    selected = deduped[-max_messages:]
    _trace(
        trace,
        f"[UIA] Pane 文本兜底提取: raw={len(raw_candidates)}, deduped={len(deduped)}, selected={len(selected)}",
    )
    if selected:
        return [{"timestamp": "", "message": text} for text in selected]

    # B 方案：对当前容器做全量子孙深扫
    deep_messages = _extract_prefixed_messages_deep_scan(
        container,
        prefix,
        max_messages,
        trace=trace,
        scan_label="Pane",
    )
    return deep_messages


def _pick_best_list_control(list_controls: Sequence[Any]) -> Optional[Any]:
    """从多个 ListControl 中选择最像消息区的一个。"""
    best: Optional[Any] = None
    best_score: Tuple[int, int] = (-1, -1)

    for ctrl in list_controls:
        child_count = len(_safe_get_children(ctrl))
        area = _control_area(ctrl)

        score = (child_count, area)
        if score > best_score:
            best_score = score
            best = ctrl

    return best


def _get_message_list(window: Any, list_names: Sequence[str], trace: TraceFn = None) -> Any:
    """获取微信消息列表控件（兼容群聊/私聊布局差异）。"""
    _trace(trace, f"[UIA] 尝试按名称定位消息列表: {list(list_names)}")
    for list_name in list_names:
        if not list_name:
            continue
        try:
            message_list = window.ListControl(Name=list_name)
            if message_list.Exists(1):
                _trace(trace, f"[UIA] 命中 ListControl(Name={list_name})")
                return message_list
            _trace(trace, f"[UIA] 未命中 ListControl(Name={list_name})")
        except Exception:
            _trace(trace, f"[UIA] 查询 ListControl(Name={list_name}) 异常")

        try:
            message_list = window.ListControl(searchDepth=8, Name=list_name)
            if message_list.Exists(1):
                _trace(trace, f"[UIA] 命中 ListControl(searchDepth=8, Name={list_name})")
                return message_list
            _trace(trace, f"[UIA] 未命中 ListControl(searchDepth=8, Name={list_name})")
        except Exception:
            _trace(trace, f"[UIA] 深度查询 ListControl(Name={list_name}) 异常")

    # 兜底：递归搜所有 ListControl，挑选最像消息区的一个
    list_controls = _collect_list_controls(window)
    _trace(trace, f"[UIA] 兜底扫描 ListControl 数量: {len(list_controls)}")
    best = _pick_best_list_control(list_controls)
    if best is not None:
        _trace(
            trace,
            f"[UIA] 选择兜底 ListControl: 名称={_safe_get_name(best) or '<空>'}, 子项数={len(_safe_get_children(best))}",
        )
        return best

    raise MonitorError("未找到可用的消息列表控件。")


def _iter_window_controls_by_class(
    class_name: str, max_count: int = 3, trace: TraceFn = None
) -> List[Any]:
    """按类名枚举窗口实例（foundIndex 从 1 开始）。"""
    windows: List[Any] = []
    # 先扫顶层窗口，避免 foundIndex 在某些环境下出现长时间阻塞。
    for child in _iter_top_level_candidates():
        child_class = _safe_get_class_name(child)
        if not child_class:
            continue
        if child_class == class_name:
            windows.append(child)
            continue
        # 允许弱匹配，兼容类名带版本后缀
        if class_name.lower() in child_class.lower():
            windows.append(child)

    # 不再使用 foundIndex 回退，避免某些环境下枚举阻塞导致抓取超时。

    windows = _deduplicate_windows(windows)
    _trace(trace, f"[UIA] 类名 {class_name} 命中窗口数: {len(windows)}")
    return windows


def _iter_wechat_windows_fallback(
    class_names: Sequence[str], trace: TraceFn = None
) -> List[Any]:
    """兜底枚举桌面顶层窗口中可能的微信窗口。"""
    windows: List[Any] = []
    top_level = _iter_top_level_candidates()
    class_name_lowers = [x.lower() for x in class_names if x]
    for child in top_level:
        class_name = _safe_get_class_name(child)
        class_name_lower = class_name.lower()
        title_lower = _safe_get_name(child).lower()

        # 优先：标题或类名显式包含微信关键词
        if (
            "wechat" in class_name_lower
            or "weixin" in class_name_lower
            or "wechat" in title_lower
            or "微信" in title_lower
        ):
            windows.append(child)
            continue

        # 次级：匹配用户配置的类名候选
        if class_name_lower and class_name_lower in class_name_lowers:
            windows.append(child)

    windows = _deduplicate_windows(windows)
    _trace(trace, f"[UIA] 兜底枚举微信窗口数: {len(windows)}")
    if not windows:
        samples: List[str] = []
        for child in top_level:
            cls = _safe_get_class_name(child)
            title = _safe_get_name(child)
            ctype = _safe_get_control_type(child)
            if not cls and not title:
                continue
            samples.append(f"{ctype}|{cls or '<空>'}|{title or '<空>'}")
            if len(samples) >= 8:
                break
        if samples:
            _trace(trace, f"[UIA] 顶层控件样本Top8: {samples}")
    return windows


def _select_chat_window_and_list(
    class_names: Sequence[str],
    list_names: Sequence[str],
    window_names: Sequence[str] = (),
    trace: TraceFn = None,
) -> Tuple[Any, Any]:
    """在候选窗口中选择可读取消息列表的窗口。"""
    candidates: List[Any] = []
    for class_name in class_names:
        candidates.extend(_iter_window_controls_by_class(class_name, trace=trace))

    if not candidates:
        candidates = _iter_wechat_windows_fallback(class_names, trace=trace)

    if not candidates:
        raise MonitorError("未检测到微信窗口，请先打开 PC 微信并进入目标群聊或私聊。")

    _trace(trace, f"[UIA] 候选窗口总数: {len(candidates)}")

    best_window: Optional[Any] = None
    best_list: Optional[Any] = None
    preferred_names = _to_lower_set(window_names)
    best_score: Tuple[int, int, int] = (-1, -1, -1)

    for window in candidates:
        title = _safe_get_name(window) or "<空>"
        cls = _safe_get_class_name(window) or "<空>"
        if _is_window_minimized(window):
            _trace(trace, f"[UIA] 跳过最小化窗口: 标题={title}, 类名={cls}")
            continue

        try:
            message_list = _get_message_list(window, list_names, trace=trace)
        except Exception as exc:
            _trace(trace, f"[UIA] 窗口未找到消息列表: 标题={title}, 类名={cls}, 原因={exc}")
            continue

        child_count = len(_safe_get_children(message_list))
        title_lower = title.lower()
        name_hit = 0
        for expected in preferred_names:
            if title_lower == expected or expected in title_lower:
                name_hit = 1
                break

        score = (name_hit, child_count, _window_area(window))
        _trace(
            trace,
            f"[UIA] 候选窗口可读: 标题={title}, 类名={cls}, 列表名称={_safe_get_name(message_list) or '<空>'}, 子项数={child_count}, 评分={score}",
        )
        if score > best_score:
            best_score = score
            best_window = window
            best_list = message_list

    if best_window is None or best_list is None:
        raise MonitorError("已找到微信窗口，但未找到可读取的消息区域。请切到目标群聊或私聊后重试。")

    return best_window, best_list


def _select_chat_window(
    class_names: Sequence[str],
    window_names: Sequence[str] = (),
    required_control_type: str = "",
    require_focusable: bool = False,
    trace: TraceFn = None,
) -> Any:
    """仅选择候选聊天窗口，不强依赖消息列表控件。"""
    candidates: List[Any] = []
    for class_name in class_names:
        candidates.extend(_iter_window_controls_by_class(class_name, trace=trace))

    if not candidates:
        candidates = _iter_wechat_windows_fallback(class_names, trace=trace)

    candidates = _deduplicate_windows(candidates)
    if not candidates:
        raise MonitorError("未检测到微信窗口，请先打开 PC 微信并进入目标群聊或私聊。")

    best_window: Optional[Any] = None
    preferred_names = _to_lower_set(window_names)
    required_control_type = _normalize_control_type_name(required_control_type)
    best_score: Tuple[int, int, int, int, int, int] = (-1, -1, -1, -1, -1, -1)
    _trace(trace, f"[UIA] 窗口候选数={len(candidates)}，窗口名优先={list(window_names)}")
    for window in candidates:
        title = _safe_get_name(window) or "<空>"
        cls = _safe_get_class_name(window) or "<空>"
        ctype = _safe_get_control_type(window) or "<空>"
        focusable = _safe_get_is_keyboard_focusable(window)
        minimized = _is_window_minimized(window)

        if required_control_type and ctype != required_control_type:
            _trace(
                trace,
                f"[UIA] 跳过窗口(控件类型不匹配): 标题={title}, 类型={ctype}, 期望={required_control_type}",
            )
            continue
        if require_focusable and not focusable:
            _trace(trace, f"[UIA] 跳过窗口(不可键盘聚焦): 标题={title}, 类型={ctype}")
            continue

        score = _window_score(window, preferred_names)
        _trace(
            trace,
            f"[UIA] 候选窗口评分: 标题={title}, 类名={cls}, 类型={ctype}, 可聚焦={focusable}, 最小化={minimized}, 评分={score}",
        )
        if score > best_score:
            best_score = score
            best_window = window

    if best_window is None:
        raise MonitorError("检测到微信窗口，但未找到可用窗口（可能最小化或不可聚焦）。")

    _trace(
        trace,
        f"[UIA] 选中窗口: 标题={_safe_get_name(best_window) or '<空>'}, 类名={_safe_get_class_name(best_window) or '<空>'}, 类型={_safe_get_control_type(best_window) or '<空>'}, 可聚焦={_safe_get_is_keyboard_focusable(best_window)}",
    )
    return best_window


def fetch_recent_messages(config: Dict[str, Any], trace: TraceFn = None) -> List[Dict[str, str]]:
    """抓取最近消息并按前缀筛选。"""
    if auto is None:
        raise MonitorError("缺少 uiautomation 依赖，请先运行 setup.py 或 main.py。")

    prefix = str(config.get("prefix", "")).strip()
    class_names = _to_str_list(
        config.get("wechat_class_names", config.get("wechat_class_name", "WeChatMainWndForPC")),
        ["WeChatMainWndForPC"],
    )
    window_names = _to_str_list(
        config.get("wechat_window_names", config.get("wechat_window_name", "")),
        [],
    )
    window_control_type = str(
        config.get("wechat_window_control_type", config.get("window_control_type", "Window"))
    ).strip()
    window_focusable = _to_bool(
        config.get("wechat_window_focusable", config.get("window_focusable", True)),
        default=True,
    )
    list_names = _to_str_list(
        config.get("message_list_names", config.get("message_list_name", "消息")),
        ["消息"],
    )
    pane_names = _to_str_list(
        config.get("message_pane_names", config.get("message_pane_name", "Weixin")),
        ["Weixin"],
    )
    pane_class_names = _to_str_list(
        config.get(
            "message_pane_class_names",
            config.get("message_pane_class_name", "MMUIRenderSubWindowHW"),
        ),
        ["MMUIRenderSubWindowHW"],
    )

    try:
        max_messages = int(config.get("max_messages", 30))
    except (TypeError, ValueError):
        max_messages = 30

    if not prefix:
        raise MonitorError("配置中的 prefix 不能为空。")
    if max_messages <= 0:
        max_messages = 30

    _trace(
        trace,
        f"[UIA] 抓取参数: prefix={prefix}, class_names={class_names}, window_names={window_names}, window_control_type={window_control_type}, window_focusable={window_focusable}, list_names={list_names}, pane_names={pane_names}, pane_classes={pane_class_names}, max_messages={max_messages}",
    )
    auto.SetGlobalSearchTimeout(3)

    try:
        window = _select_chat_window(
            class_names,
            window_names=window_names,
            required_control_type=window_control_type,
            require_focusable=window_focusable,
            trace=trace,
        )
    except Exception as exc:
        raise MonitorError(str(exc)) from exc

    _activate_window(window, trace=trace)

    try:
        message_list = _get_message_list(window, list_names, trace=trace)
        children = _safe_get_children(message_list)
    except Exception as exc:
        _trace(trace, f"[UIA] 当前窗口 ListControl 抓取失败，进入 Pane/文本兜底: {exc}")

        pane_candidates = _collect_message_pane_candidates(
            window, pane_names, pane_class_names, trace=trace
        )
        for pane in pane_candidates[:6]:
            pane_name = _safe_get_name(pane) or "<空>"
            pane_class = _safe_get_class_name(pane) or "<空>"
            _trace(trace, f"[UIA] 尝试从 Pane 提取消息: 名称={pane_name}, 类名={pane_class}")
            pane_messages = _extract_messages_from_container(pane, prefix, max_messages, trace=trace)
            if pane_messages:
                _trace(trace, "[UIA] Pane 兜底提取成功。")
                return pane_messages

        fallback_messages = _extract_prefixed_messages_from_window(
            window, prefix, max_messages, trace=trace
        )
        if fallback_messages:
            _trace(trace, "[UIA] 无列表兜底提取成功。")
            return fallback_messages
        _trace(trace, "[UIA] 无列表兜底提取无结果。")
        raise MonitorError("已定位到目标窗口，但未读取到消息文本。") from exc

    if _is_window_minimized(window):
        raise MonitorError("检测到微信窗口可能最小化，请还原窗口后重试。")

    _trace(
        trace,
        f"[UIA] 选中窗口: 标题={_safe_get_name(window) or '<空>'}, 类名={_safe_get_class_name(window) or '<空>'}",
    )
    _trace(
        trace,
        f"[UIA] 选中消息列表: 名称={_safe_get_name(message_list) or '<空>'}, 子项总数={len(children)}",
    )

    recent_items = children[-max_messages:]
    results: List[Dict[str, str]] = []
    candidate_text_count = 0

    for item in recent_items:
        extracted = _extract_message(item, prefix)
        if not extracted:
            continue
        candidate_text_count += 1
        if extracted["message"].startswith(prefix):
            results.append(extracted)

    _trace(
        trace,
        f"[UIA] 最近检查消息节点数={len(recent_items)}，可提取文本节点数={candidate_text_count}，前缀命中数={len(results)}",
    )

    if not results:
        pane_candidates = _collect_message_pane_candidates(
            window, pane_names, pane_class_names, trace=trace
        )
        for pane in pane_candidates[:4]:
            pane_messages = _extract_messages_from_container(pane, prefix, max_messages, trace=trace)
            if pane_messages:
                _trace(trace, "[UIA] ListControl 无命中，Pane 二次兜底提取成功。")
                return pane_messages

        previews: List[str] = []
        for item in recent_items[:5]:
            texts = _collect_texts(item)
            if not texts:
                continue
            preview = " | ".join(texts[:3]).strip()
            if preview:
                previews.append(preview[:120])
        if previews:
            _trace(trace, f"[UIA] 消息样本预览: {previews}")

    return results
