#!/bin/bash
# 文献リファレンスツール 起動スクリプト(macOS / Linux 用)
# Windows の start.bat と同じ役割。ダブルクリックで起動し、ウィンドウを閉じると停止します。
# ※このファイルは改行コード LF 必須(.gitattributes で固定)。CRLF だと起動に失敗します。

# スクリプトのある場所へ移動(どこから実行してもアプリのフォルダを基準にする)
cd "$(dirname "$0")" || exit 1

# Homebrew などで入れた Python が PATH に無い GUI 起動でも見つかるように、よくある場所を足す
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PY=".venv/bin/python"

# --- 設定ファイルの読み込みに tomllib を使うため Python 3.11 以降が必要 ---
find_python() {
  for cand in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        echo "$cand"
        return 0
      fi
    fi
  done
  return 1
}

# --- 壊れた .venv の検出(別の PC からフォルダごとコピーした場合) ---
# venv は pyvenv.cfg に元の PC の Python のパスが焼き込まれるため、そのままでは動きません。
# python はあるのに実行できないときは、壊れているとみなして作り直します。
if [ -x "$PY" ] && ! "$PY" --version >/dev/null 2>&1; then
  echo "[setup] 既存の .venv が使えません(別の PC からコピーされた可能性)。作り直します..."
  rm -rf .venv
fi

# --- 初回起動 / 作り直し: 仮想環境の作成と依存パッケージの導入 ---
if [ ! -x "$PY" ]; then
  echo "[setup] 初回起動: 仮想環境を作成します..."
  BASE_PY="$(find_python)"
  if [ -z "$BASE_PY" ]; then
    echo
    echo "Python 3.11 以降が見つかりません。"
    echo "インストールしてください: https://www.python.org/downloads/"
    echo "(Homebrew を使う場合: brew install python)"
    echo
    read -r -p "Enter キーで閉じます..." _
    exit 1
  fi
  "$BASE_PY" -m venv .venv || {
    echo "仮想環境の作成に失敗しました。"
    read -r -p "Enter キーで閉じます..." _
    exit 1
  }
  echo "[setup] 依存パッケージを導入します(初回のみ・数分かかることがあります)..."
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r requirements.txt || {
    echo "依存パッケージの導入に失敗しました。ネットワーク接続を確認してください。"
    read -r -p "Enter キーで閉じます..." _
    exit 1
  }
fi

echo
"$PY" run.py

echo
echo "サーバーを停止しました。"
read -r -p "Enter キーで閉じます..." _
