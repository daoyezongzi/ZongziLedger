"""微信窗口诊断脚本：用于定位真实窗口类名和消息列表控件。"""

from __future__ import annotations

import ctypes
from typing import Any, List, Tuple


def _load_uiautomation() -> Any:
    """加载 uiautomation，缺失时尝试自动安装。"""
    try:
        import uiautomation as _auto

        return _auto
    except ImportError:
        print("[提示] 未检测到 uiautomation，正在尝试自动安装依赖...")

    try:
        from setup import check_environment
    except Exception as exc:
        print(f"[错误] 无法加载 setup.py 进行自动安装：{exc}")
        print("[建议] 先执行：python setup.py")
        return None

    if not check_environment():
        print("[错误] 自动安装依赖失败。")
        print("[建议] 手动执行以下命令后重试：")
        print("  1) mkdir .tmp")
        print("  2) set TMP=%CD%\\.tmp")
        print("  3) set TEMP=%CD%\\.tmp")
        print("  4) python -m pip install --user --no-cache-dir uiautomation pyyaml")
        return None

    try:
        import uiautomation as _auto

        return _auto
    except ImportError:
        print("[错误] 依赖安装后仍无法导入 uiautomation。")
        print("[建议] 先执行：run_zongziledger.bat")
        return None


auto = _load_uiautomation()
if auto is None:
    raise SystemExit(1)


def safe_get(obj: Any, attr: str, default: Any = "") -> Any:
    try:
        value = getattr(obj, attr, default)
        return default if value is None else value
    except Exception:
        return default


def get_rect_area(ctrl: Any) -> int:
    try:
        rect = ctrl.BoundingRectangle
        width = max(0, int(rect.width()))
        height = max(0, int(rect.height()))
        return width * height
    except Exception:
        return 0


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def collect_children(ctrl: Any) -> List[Any]:
    try:
        children = ctrl.GetChildren()
        return children or []
    except Exception:
        return []


def collect_list_controls(ctrl: Any, max_depth: int = 5) -> List[Any]:
    result: List[Any] = []

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        ctype = str(safe_get(node, "ControlTypeName", "")).lower()
        if ctype == "listcontrol":
            result.append(node)
        for child in collect_children(node):
            walk(child, depth + 1)

    walk(ctrl, 0)
    return result


def is_wechat_candidate(ctrl: Any) -> bool:
    class_name = str(safe_get(ctrl, "ClassName", "")).lower()
    title = str(safe_get(ctrl, "Name", "")).lower()
    return (
        "wechat" in class_name
        or "weixin" in class_name
        or "wechat" in title
        or "微信" in title
    )


def summarize_window(ctrl: Any) -> Tuple[str, str, str, int, int]:
    title = str(safe_get(ctrl, "Name", "")).strip()
    class_name = str(safe_get(ctrl, "ClassName", "")).strip()
    ctype = str(safe_get(ctrl, "ControlTypeName", "")).strip()
    pid = int(safe_get(ctrl, "ProcessId", 0) or 0)
    area = get_rect_area(ctrl)
    return title, class_name, ctype, pid, area


def main() -> None:
    print("========== 微信窗口诊断 ==========")
    print(f"[环境] Python 管理员权限: {'是' if is_admin() else '否'}")
    print("[说明] 若微信以管理员身份运行，而脚本不是管理员，UIA 通常无法抓取窗口。")

    auto.SetGlobalSearchTimeout(2)

    try:
        root = auto.GetRootControl()
    except Exception as exc:
        print(f"[错误] 无法获取桌面根节点: {exc}")
        return

    top_windows = collect_children(root)
    print(f"[信息] 顶层窗口数量: {len(top_windows)}")

    candidates = [w for w in top_windows if is_wechat_candidate(w)]
    print(f"[信息] 微信候选窗口数量: {len(candidates)}")

    if not candidates:
        print("[结果] 未发现任何微信候选窗口。")
        print("[建议] 先把微信主界面或私聊窗口置于前台，再重新运行该脚本。")
        return

    class_names = []
    list_names = []

    for i, win in enumerate(candidates, start=1):
        title, class_name, ctype, pid, area = summarize_window(win)
        if class_name:
            class_names.append(class_name)

        print("----------------------------------")
        print(f"[窗口{i}] 标题: {title or '<空>'}")
        print(f"[窗口{i}] 类名: {class_name or '<空>'}")
        print(f"[窗口{i}] 类型: {ctype or '<空>'} | PID: {pid} | 面积: {area}")

        list_controls = collect_list_controls(win)
        print(f"[窗口{i}] ListControl 数量: {len(list_controls)}")

        for j, lst in enumerate(list_controls[:10], start=1):
            lst_name = str(safe_get(lst, "Name", "")).strip()
            if lst_name:
                list_names.append(lst_name)
            lst_area = get_rect_area(lst)
            child_count = len(collect_children(lst))
            print(
                f"  - List{j}: 名称={lst_name or '<空>'} | 子项数={child_count} | 面积={lst_area}"
            )

    uniq_class = sorted({x for x in class_names if x})
    uniq_list = sorted({x for x in list_names if x})

    print("========== 建议配置 ==========")
    print(f"wechat_class_names: {uniq_class if uniq_class else ['WeChatMainWndForPC']}")
    print(f"message_list_names: {uniq_list if uniq_list else ['消息']}")
    print("[操作] 把上面两行结果贴给我，我来帮你落到 config.yaml。")


if __name__ == "__main__":
    main()
