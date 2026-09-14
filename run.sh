#!/usr/bin/env bash
# A股每日复盘 —— 一键运行（Linux / macOS）
# 用法：  ./run.sh              全量生成
#         ./run.sh --no-news    跳过新闻（更快）
#         ./run.sh --refresh    忽略缓存强制重抓
set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[错误] 没有找到 Python，请先安装 Python 3。"
  exit 1
fi

echo "=== A股每日复盘 (解释器: $PY) ==="
"$PY" stock_review.py "$@"
echo
echo "报告目录: $(pwd)/reports"
