#!/usr/bin/env python3
"""
selftest_claude.py  —  実行環境の自己診断

インターネットに繋がっていないPCへ持ち込んだあと、
「この Python でブリッジがちゃんと動くか」を走る前に確かめるためのスクリプト。

    python selftest_claude.py
    python-claude\\python.exe selftest_claude.py     （同梱ランタイムの場合）

すべて [OK] なら start-claude.bat で起動できます。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NG = []


def check(label, fn):
    try:
        detail = fn()
        print(f"  [OK]  {label}" + (f" — {detail}" if detail else ""))
        return True
    except Exception as exc:
        print(f"  [NG]  {label} — {exc}")
        NG.append((label, exc))
        return False


def main():
    print()
    print("=" * 64)
    print("  AC テレメトリーブリッジ 自己診断 (claude)")
    print("=" * 64)
    print()

    # ---- Python 本体 ----
    print("Python")
    check("バージョン 3.8 以上", lambda: _version())
    check("実行ファイル", lambda: sys.executable)
    check("OS", lambda: _platform())
    print()

    # ---- 必要な標準ライブラリ ----
    print("標準ライブラリ")
    for mod in ("asyncio", "ctypes", "json", "socket", "struct",
                "mimetypes", "hashlib", "base64", "webbrowser", "argparse",
                "threading", "math", "time",
                "gzip", "csv", "queue", "shutil"):
        check(mod, lambda m=mod: _import(m))
    if sys.platform.startswith("win"):
        check("winreg（AC のインストール先の自動検出に使用）",
              lambda: _import("winreg"))
    print()

    # ---- 同梱ファイル ----
    print("ファイル構成")
    for rel in ("bridge_claude.py", "ac_sharedmem_claude.py",
                "trackmap_claude.py", "logger_claude.py",
                os.path.join("web-claude", "index-claude.html"),
                os.path.join("web-claude", "mobile-claude.html"),
                os.path.join("web-claude", "engineer-claude.html")):
        check(rel, lambda r=rel: _exists(r))
    print()

    # ---- プロジェクトのモジュール ----
    print("モジュールの読み込み")
    sys.path.insert(0, HERE)
    check("ac_sharedmem_claude", lambda: _structs())
    check("bridge_claude", lambda: _bridge())
    check("trackmap_claude", lambda: _import("trackmap_claude"))
    check("logger_claude", lambda: _import("logger_claude"))
    print()

    # ---- 記録の保存先 ----
    print("走行ログ")
    check("保存先フォルダに書き込める", lambda: _logdir())
    print()

    # ---- 共有メモリ ----
    print("Assetto Corsa")
    if not sys.platform.startswith("win"):
        print("  [--]  共有メモリの確認は Windows でのみ行えます")
    else:
        try:
            from ac_sharedmem_claude import ACSharedMemory, SPageFileStatic
            ac = ACSharedMemory()
            if ac.ensure_open():
                st = ac.view("static", SPageFileStatic)
                car = st.carModel if st else "?"
                track = st.track if st else "?"
                ac.close()
                print(f"  [OK]  共有メモリに接続できました — {car} @ {track}")
            else:
                print("  [--]  AC が起動していないため未接続"
                      "（走行中に実行すれば接続確認ができます）")
        except Exception as exc:
            print(f"  [NG]  共有メモリの確認に失敗 — {exc}")
            NG.append(("共有メモリ", exc))
    print()

    # ---- 結果 ----
    print("=" * 64)
    if NG:
        print(f"  {len(NG)} 件の問題があります:")
        for label, exc in NG:
            print(f"    - {label}: {exc}")
        print()
        print("  よくある原因:")
        print("   * Python が古い → 3.8 以上（3.11 以降推奨）を入れ直してください")
        print("   * 同梱ランタイムでモジュールが読めない →")
        print("     python-claude\\pythonXXX._pth に「..」の行を追加してください")
    else:
        print("  問題ありません。start-claude.bat で起動できます。")
    print("=" * 64)
    print()
    return 1 if NG else 0


# ---------------------------------------------------------------- 個別チェック

def _version():
    v = sys.version_info
    s = f"{v.major}.{v.minor}.{v.micro}"
    if v < (3, 8):
        raise RuntimeError(f"{s} は古すぎます（3.8 以上が必要）")
    return s + ("（3.11 以降推奨）" if v < (3, 11) else "")


def _platform():
    import platform
    s = f"{platform.system()} {platform.release()} / {platform.machine()}"
    if not sys.platform.startswith("win"):
        s += "  ※共有メモリの読み取りは Windows のみ（--demo は動作します）"
    return s


def _import(name):
    __import__(name)
    return None


def _exists(rel):
    path = os.path.join(HERE, rel)
    if not os.path.isfile(path):
        raise FileNotFoundError("見つかりません")
    return f"{os.path.getsize(path):,} バイト"


def _logdir():
    """logs-claude に書き込めるか、空き容量が十分かを確認する。"""
    import shutil
    d = os.path.join(HERE, "logs-claude")
    os.makedirs(d, exist_ok=True)
    probe = os.path.join(d, ".write-test-claude")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("ok")
    os.remove(probe)
    free = shutil.disk_usage(d).free / 1073741824.0
    hours = free * 1024 / 76.0            # 実測 約76MB/時（60Hz・全項目）
    if free < 0.5:
        raise OSError("空き容量が %.1f GB しかありません" % free)
    return "空き %.1f GB（60Hz 記録で約 %.0f 時間ぶん）" % (free, hours)


def _structs():
    import ctypes
    from ac_sharedmem_claude import (SPageFilePhysics, SPageFileGraphic,
                                     SPageFileStatic)
    sizes = [ctypes.sizeof(t) for t in
             (SPageFilePhysics, SPageFileGraphic, SPageFileStatic)]

    # physics は文字列を含まないのでどの環境でも 580 バイト
    if sizes[0] != 580:
        raise RuntimeError(f"physics のサイズが異常です {sizes[0]}（想定 580）")

    # graphics / static は wchar_t の幅で変わる。Windows は 2 バイト。
    wchar = ctypes.sizeof(ctypes.c_wchar)
    expect = [580, 296, 684] if wchar == 2 else [580, 480, 1200]
    if sizes != expect:
        raise RuntimeError(f"構造体サイズが想定と違います {sizes}（想定 {expect}）")
    return ("構造体サイズ " + " / ".join(str(s) for s in sizes) +
            f"  (wchar_t = {wchar} バイト)")


def _bridge():
    import bridge_claude
    src = bridge_claude.TelemetrySourceClaude(demo=True)
    f = src.frame()
    if not f.get("connected"):
        raise RuntimeError("デモデータを生成できません")
    return f"デモ生成 OK（{len(f)} 項目）"


if __name__ == "__main__":
    code = main()
    if sys.platform.startswith("win") and sys.stdout.isatty():
        try:
            input("Enter キーで閉じます...")
        except Exception:
            pass
    sys.exit(code)
