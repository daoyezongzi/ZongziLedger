from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict

# 需要的依赖：键是导入名，值是 pip 包名
REQUIRED_PACKAGES: Dict[str, str] = {
    "yaml": "pyyaml",
}

# 可选依赖：不影响基础抓取/入账流程。
OPTIONAL_PACKAGES: Dict[str, str] = {
    # xlsx 导出依赖（canonical bill Excel 输出）
    "openpyxl": "openpyxl",
    # 图片 OCR 扩展依赖
    "PIL": "Pillow",
    "pytesseract": "pytesseract",
}


def _install_package(pip_name: str) -> bool:
    """安装指定 pip 包。"""
    temp_dir = Path(__file__).resolve().parent / ".pip_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    pip_env = os.environ.copy()
    pip_env["TMP"] = str(temp_dir)
    pip_env["TEMP"] = str(temp_dir)

    commands = [
        [sys.executable, "-m", "pip", "install", pip_name],
        [sys.executable, "-m", "pip", "install", "--user", "--no-cache-dir", pip_name],
    ]
    for cmd in commands:
        try:
            subprocess.check_call(cmd, env=pip_env)
            return True
        except subprocess.CalledProcessError:
            continue
    return False


def _ensure_package(import_name: str, pip_name: str) -> bool:
    """确保包可导入，不可导入时尝试自动安装。"""
    try:
        importlib.import_module(import_name)
        print(f"[环境检查] 已安装：{pip_name}")
        return True
    except ImportError:
        print(f"[环境检查] 缺少依赖：{pip_name}，正在自动安装...")
        if not _install_package(pip_name):
            print(f"[环境检查] 安装失败：{pip_name}")
            return False

    try:
        importlib.import_module(import_name)
        print(f"[环境检查] 安装成功：{pip_name}")
        return True
    except ImportError:
        print(f"[环境检查] 安装后仍无法导入：{pip_name}")
        return False


def check_environment(include_optional: bool = False) -> bool:
    """执行环境检查并自动补齐依赖。"""
    print("[环境检查] 开始检查 Python 依赖...")

    all_ok = True
    for import_name, pip_name in REQUIRED_PACKAGES.items():
        ok = _ensure_package(import_name, pip_name)
        all_ok = all_ok and ok

    if include_optional:
        print("[环境检查] 检查可选依赖（Excel / 图片 OCR）...")
        for import_name, pip_name in OPTIONAL_PACKAGES.items():
            ok = _ensure_package(import_name, pip_name)
            all_ok = all_ok and ok
    else:
        print("[环境检查] 已跳过可选依赖检查（Excel / 图片 OCR）。")

    if all_ok:
        print("[环境检查] 所有依赖已就绪。")
    else:
        print("[环境检查] 依赖检查未通过，请手动排查后重试。")
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if check_environment(include_optional=True) else 1)
