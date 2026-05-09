"""分析微信抓取失败原因：定位“已找到微信窗口，但未找到可读取消息区域”的根因。"""

from __future__ import annotations

import ctypes
import platform
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.constants import DEFAULT_CONFIG_PATH, DEFAULT_PREFIX


def _safe_import() -> Tuple[Any, Any]:
    try:
        import yaml
    except ImportError:
        print("[错误] 缺少 pyyaml，请先运行：python setup.py")
        raise SystemExit(1)

    try:
        import uiautomation as auto
    except ImportError:
        print("[错误] 缺少 uiautomation，请先运行：python setup.py")
        raise SystemExit(1)

    return yaml, auto


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def safe_get(obj: Any, attr: str, default: Any = "") -> Any:
    try:
        value = getattr(obj, attr, default)
        return default if value is None else value
    except Exception:
        return default


def safe_children(ctrl: Any) -> List[Any]:
    try:
        children = ctrl.GetChildren()
        return children or []
    except Exception:
        return []


def control_type(ctrl: Any) -> str:
    return str(safe_get(ctrl, "ControlTypeName", "")).strip().lower()


def control_name(ctrl: Any) -> str:
    return str(safe_get(ctrl, "Name", "")).strip()


def control_class(ctrl: Any) -> str:
    return str(safe_get(ctrl, "ClassName", "")).strip()


def control_pid(ctrl: Any) -> int:
    try:
        return int(safe_get(ctrl, "ProcessId", 0) or 0)
    except Exception:
        return 0


def control_hwnd(ctrl: Any) -> int:
    try:
        return int(safe_get(ctrl, "NativeWindowHandle", 0) or 0)
    except Exception:
        return 0


def control_area(ctrl: Any) -> int:
    try:
        rect = ctrl.BoundingRectangle
        width = max(0, int(rect.width()))
        height = max(0, int(rect.height()))
        return width * height
    except Exception:
        return 0


def is_minimized(ctrl: Any) -> bool:
    try:
        if bool(safe_get(ctrl, "IsOffscreen", False)):
            return True
    except Exception:
        pass

    try:
        rect = ctrl.BoundingRectangle
        return rect.width() <= 0 or rect.height() <= 0
    except Exception:
        return False


def normalize_list(value: Any, default: Sequence[str]) -> List[str]:
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


def collect_texts(ctrl: Any, max_depth: int = 4, max_nodes: int = 200) -> List[str]:
    texts: List[str] = []
    visited = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal visited
        if depth > max_depth or visited >= max_nodes:
            return
        visited += 1

        name = control_name(node)
        if name:
            texts.append(name)

        for child in safe_children(node):
            walk(child, depth + 1)

    walk(ctrl, 0)
    return texts


def collect_type_stats(ctrl: Any, max_depth: int = 4, max_nodes: int = 2000) -> Counter:
    stats: Counter = Counter()
    queue: List[Tuple[Any, int]] = [(ctrl, 0)]
    visited = 0

    while queue and visited < max_nodes:
        node, depth = queue.pop(0)
        visited += 1
        stats[control_type(node) or "<empty>"] += 1

        if depth >= max_depth:
            continue
        for child in safe_children(node):
            queue.append((child, depth + 1))

    return stats


def collect_list_controls(ctrl: Any, max_depth: int = 6) -> List[Any]:
    result: List[Any] = []

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if control_type(node) == "listcontrol":
            result.append(node)
        for child in safe_children(node):
            walk(child, depth + 1)

    walk(ctrl, 0)
    return result


def pick_best_list_control(controls: Sequence[Any]) -> Optional[Any]:
    best: Optional[Any] = None
    best_score: Tuple[int, int] = (-1, -1)

    for ctrl in controls:
        child_count = len(safe_children(ctrl))
        score = (child_count, control_area(ctrl))
        if score > best_score:
            best_score = score
            best = ctrl

    return best


def enumerate_windows_by_class(auto: Any, class_name: str, max_count: int = 10) -> List[Any]:
    windows: List[Any] = []
    for idx in range(1, max_count + 1):
        try:
            win = auto.WindowControl(ClassName=class_name, foundIndex=idx)
            if not win.Exists(0.6):
                if idx == 1:
                    continue
                break
            windows.append(win)
        except Exception:
            if idx == 1:
                continue
            break
    return windows


def enumerate_wechat_windows_fallback(auto: Any) -> List[Any]:
    windows: List[Any] = []
    try:
        root = auto.GetRootControl()
    except Exception:
        return windows

    for child in safe_children(root):
        ctype = control_type(child)
        if ctype not in {"windowcontrol", "panecontrol"}:
            continue

        class_name = control_class(child).lower()
        title = control_name(child).lower()
        if (
            "wechat" in class_name
            or "weixin" in class_name
            or "qt" in class_name and ("wechat" in title or "微信" in title)
            or "wechat" in title
            or "微信" in title
        ):
            windows.append(child)

    return windows


def dedup_windows(windows: Sequence[Any]) -> List[Any]:
    seen: set[Tuple[int, int, str]] = set()
    result: List[Any] = []

    for win in windows:
        key = (control_pid(win), control_hwnd(win), control_class(win))
        if key in seen:
            continue
        seen.add(key)
        result.append(win)

    return result


@dataclass
class ListProbeResult:
    message_list: Optional[Any]
    reason: str
    attempts: List[str]


def try_get_message_list(window: Any, list_names: Sequence[str]) -> ListProbeResult:
    attempts: List[str] = []

    for list_name in list_names:
        if not list_name:
            continue

        try:
            ctrl = window.ListControl(Name=list_name)
            exists = bool(ctrl.Exists(1))
            attempts.append(f"Name={list_name} Exists={exists} (searchDepth默认)")
            if exists:
                return ListProbeResult(ctrl, "by_name", attempts)
        except Exception as exc:
            attempts.append(f"Name={list_name} 异常: {exc}")

        try:
            ctrl = window.ListControl(searchDepth=10, Name=list_name)
            exists = bool(ctrl.Exists(1))
            attempts.append(f"Name={list_name} Exists={exists} (searchDepth=10)")
            if exists:
                return ListProbeResult(ctrl, "by_name_depth", attempts)
        except Exception as exc:
            attempts.append(f"Name={list_name} depth异常: {exc}")

    list_controls = collect_list_controls(window)
    if list_controls:
        best = pick_best_list_control(list_controls)
        return ListProbeResult(best, f"fallback_listcontrols={len(list_controls)}", attempts)

    return ListProbeResult(None, "no_list_control", attempts)


def inspect_message_list(message_list: Any, prefix: str, max_items: int = 15) -> Dict[str, Any]:
    children = safe_children(message_list)
    recent_items = children[-max_items:]

    textful_count = 0
    prefix_hit_count = 0
    sample_lines: List[str] = []

    for idx, item in enumerate(recent_items, start=1):
        texts = [t.strip() for t in collect_texts(item, max_depth=5, max_nodes=250) if t.strip()]
        if texts:
            textful_count += 1
        merged = " | ".join(texts[:4])
        if merged:
            sample_lines.append(f"item{idx}: {merged[:120]}")

        has_prefix = any(t.startswith(prefix) or prefix in t for t in texts)
        if has_prefix:
            prefix_hit_count += 1

    return {
        "list_name": control_name(message_list),
        "list_class": control_class(message_list),
        "list_area": control_area(message_list),
        "child_count": len(children),
        "recent_checked": len(recent_items),
        "textful_count": textful_count,
        "prefix_hit_count": prefix_hit_count,
        "samples": sample_lines[:8],
    }


def load_config(yaml_mod: Any, config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        data = yaml_mod.safe_load(f) or {}

    return data


def build_summary(
    windows: Sequence[Any],
    inspected: List[Dict[str, Any]],
    prefix: str,
) -> List[str]:
    reasons: List[str] = []

    if not windows:
        reasons.append("未找到任何候选微信窗口：类名可能不匹配，或微信未在当前桌面会话。")
        return reasons

    visible = [w for w in windows if not is_minimized(w)]
    if not visible:
        reasons.append("候选微信窗口全部处于最小化/离屏状态。")

    with_list = [x for x in inspected if x.get("message_list") is not None]
    if not with_list:
        reasons.append("窗口存在，但未找到 ListControl 消息区域：可能是微信版本 UI 树变化，或权限层级导致不可见。")
        return reasons

    child_counts = [int(x.get("list_info", {}).get("child_count", 0)) for x in with_list]
    if child_counts and max(child_counts) == 0:
        reasons.append("找到了消息列表控件，但子项数量为 0：当前窗口可能不在具体聊天页，或消息区域尚未渲染。")
        return reasons

    textful_counts = [int(x.get("list_info", {}).get("textful_count", 0)) for x in with_list]
    if textful_counts and max(textful_counts) == 0:
        reasons.append("列表中有子项但读不到文本：常见于权限不一致（微信管理员运行而脚本非管理员）或 UIA 不可访问。")

    prefix_hits = [int(x.get("list_info", {}).get("prefix_hit_count", 0)) for x in with_list]
    if prefix_hits and max(prefix_hits) == 0:
        reasons.append(
            f"消息列表可读但没有命中前缀“{prefix}”：可能前缀不一致、消息不在最近抓取范围、或内容格式不匹配。"
        )

    if not reasons:
        reasons.append("未发现明显结构性错误，建议扩大 max_messages 并重新聚焦聊天窗口后重试。")

    return reasons


def main() -> None:
    yaml_mod, auto = _safe_import()

    config_path = Path(DEFAULT_CONFIG_PATH)
    try:
        config = load_config(yaml_mod, config_path)
    except Exception as exc:
        print(f"[错误] 读取配置失败：{exc}")
        raise SystemExit(1)

    prefix = str(config.get("prefix", DEFAULT_PREFIX)).strip()
    class_names = normalize_list(
        config.get("wechat_class_names", config.get("wechat_class_name", "WeChatMainWndForPC")),
        ["WeChatMainWndForPC"],
    )
    list_names = normalize_list(
        config.get("message_list_names", config.get("message_list_name", "消息")),
        ["消息"],
    )

    print("========== 抓取失败原因诊断 ==========")
    print(f"[时间] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[环境] Python: {platform.python_version()} | 管理员权限: {'是' if is_admin() else '否'}")
    print(f"[配置] prefix={prefix} | wechat_class_names={class_names} | message_list_names={list_names}")

    auto.SetGlobalSearchTimeout(2)

    by_class: Dict[str, List[Any]] = {}
    for class_name in class_names:
        by_class[class_name] = enumerate_windows_by_class(auto, class_name)

    for class_name, wins in by_class.items():
        print(f"[扫描] 类名 {class_name} 命中窗口数: {len(wins)}")

    candidates: List[Any] = []
    for wins in by_class.values():
        candidates.extend(wins)

    if not candidates:
        fallback = enumerate_wechat_windows_fallback(auto)
        print(f"[扫描] 兜底微信窗口数: {len(fallback)}")
        candidates.extend(fallback)

    windows = dedup_windows(candidates)
    print(f"[结果] 候选窗口总数: {len(windows)}")

    inspected: List[Dict[str, Any]] = []
    for idx, win in enumerate(windows, start=1):
        title = control_name(win)
        cls = control_class(win)
        ctype = control_type(win)
        pid = control_pid(win)
        hwnd = control_hwnd(win)
        area = control_area(win)
        minimized = is_minimized(win)

        print("--------------------------------------")
        print(f"[窗口{idx}] 标题={title or '<空>'}")
        print(f"[窗口{idx}] 类名={cls or '<空>'} 类型={ctype or '<空>'} PID={pid} HWND={hwnd} 面积={area}")
        print(f"[窗口{idx}] 最小化/离屏={minimized}")

        type_stats = collect_type_stats(win, max_depth=3)
        top_types = type_stats.most_common(8)
        print(f"[窗口{idx}] 控件类型Top8={top_types}")

        probe = try_get_message_list(win, list_names)
        print(f"[窗口{idx}] 消息控件定位方式={probe.reason}")
        for a in probe.attempts[:6]:
            print(f"  - {a}")

        item: Dict[str, Any] = {"window": win, "message_list": probe.message_list, "list_info": None}
        if probe.message_list is not None:
            list_info = inspect_message_list(probe.message_list, prefix)
            item["list_info"] = list_info
            print(
                f"[窗口{idx}] 消息区: 名称={list_info['list_name'] or '<空>'} 类名={list_info['list_class'] or '<空>'} "
                f"子项={list_info['child_count']} 近期检查={list_info['recent_checked']} "
                f"可读文本项={list_info['textful_count']} 前缀命中={list_info['prefix_hit_count']}"
            )
            for line in list_info["samples"]:
                print(f"  * {line}")

        inspected.append(item)

    reasons = build_summary(windows, inspected, prefix)
    print("========== 诊断结论 ==========")
    for i, reason in enumerate(reasons, start=1):
        print(f"{i}. {reason}")

    print("========== 建议操作 ==========")
    print("1. 让微信聊天窗口保持前台且非最小化。")
    print("2. 若微信是管理员运行，请用管理员方式启动脚本。")
    print("3. 把上面的窗口类名/消息区名称结果回填到 config.yaml。")
    print("4. 运行本脚本后，将完整输出发给我，我可以直接帮你改到可用配置。")


if __name__ == "__main__":
    main()
