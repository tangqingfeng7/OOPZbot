#!/usr/bin/env bash
# Oopz Bot 一键部署（Linux / macOS）
# 实际逻辑都在 deploy.py，这里只负责找到一个够新的 Python 并把参数透传过去。
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 版本够、而且真的能建虚拟环境才算数。有些解释器（Debian 未装 python3-venv、
# 部分第三方发行版）缺 ensurepip，光看版本号会挑到一个用不了的。
usable_python() {
  "$1" -c 'import sys, ensurepip, venv; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

pick_python() {
  local candidate
  # 先看用户指定的，再按常见路径找；最后退一步只要版本够就用，
  # 让 deploy.py 去报出更具体的原因，而不是在这里含糊地说“找不到 Python”。
  for candidate in "${PYTHON_BIN:-}" python3 python3.13 python3.12 python3.11 python3.10 python; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    if usable_python "$candidate"; then
      echo "$candidate"
      return 0
    fi
  done
  for candidate in "${PYTHON_BIN:-}" python3 python; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

if ! PYTHON="$(pick_python)"; then
  echo "找不到 Python 3.10 或更高版本。"
  echo "  Ubuntu/Debian : sudo apt install python3 python3-venv"
  echo "  Fedora        : sudo dnf install python3"
  echo "  macOS         : brew install python"
  echo "装好后重新运行本脚本；也可以用 PYTHON_BIN 指定路径。"
  exit 1
fi

exec "$PYTHON" deploy.py "$@"
