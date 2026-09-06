#!/usr/bin/env python3
"""
trackmap_claude.py  —  Assetto Corsa のコース形状データ書き出しツール

AC のインストールフォルダから、コースの形状データを取り出して JSON にします。
書き出した JSON をダッシュボードが読むことで、**1周走る前から完全なコース図**を
表示できるようになります。

取り出せるもの（優先順）
  1. ai/fast_lane.ai  … AIラインの座標列。共有メモリの carCoordinates と
                        同じワールド座標系なので、座標変換なしでそのまま使えます。
  2. data/map.ini     … ゲーム内ミニマップの縮尺情報（参考値として同梱）
  3. map.png          … ゲーム内ミニマップ画像（パスのみ記録）

使い方（ゲームPCで実行）
    python trackmap_claude.py                      走行中のコースを自動判別して書き出し
    python trackmap_claude.py --track ks_nordschleife --layout endurance
    python trackmap_claude.py --all                インストール済み全コースを書き出し
    python trackmap_claude.py --dump               ファイル構造を表示（解析の確認用）
    python trackmap_claude.py --ac-path "D:\\SteamLibrary\\steamapps\\common\\assettocorsa"

出力先: web-claude/tracks-claude/<track>[__<layout>].json
"""

import argparse
import json
import math
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "web-claude", "tracks-claude")


# ===========================================================================
# AC インストール先の探索
# ===========================================================================

def find_ac_path(explicit=None):
    """Assetto Corsa のインストールフォルダを探す。見つからなければ None。"""
    cands = []
    if explicit:
        cands.append(explicit)

    # Steam の登録情報から
    for lib in _steam_libraries():
        cands.append(os.path.join(lib, "steamapps", "common", "assettocorsa"))

    # よくある場所
    for drive in ("C:", "D:", "E:", "F:"):
        cands.append(drive + r"\Program Files (x86)\Steam\steamapps\common\assettocorsa")
        cands.append(drive + r"\SteamLibrary\steamapps\common\assettocorsa")
        cands.append(drive + r"\Steam\steamapps\common\assettocorsa")

    for c in cands:
        if c and os.path.isdir(os.path.join(c, "content", "tracks")):
            return os.path.normpath(c)
    return None


def _steam_libraries():
    """Steam のライブラリフォルダ一覧（Windows のレジストリと vdf から）。"""
    libs = []
    steam = None
    try:
        import winreg
        for root, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(root, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            steam = winreg.QueryValueEx(k, name)[0]
                            break
                        except OSError:
                            continue
                if steam:
                    break
            except OSError:
                continue
    except Exception:
        pass

    if not steam:
        return libs
    libs.append(steam)

    # libraryfolders.vdf から他ドライブのライブラリも拾う
    for rel in (("steamapps", "libraryfolders.vdf"),
                ("config", "libraryfolders.vdf")):
        vdf = os.path.join(steam, *rel)
        if not os.path.isfile(vdf):
            continue
        try:
            with open(vdf, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if '"path"' in line.lower():
                        parts = line.split('"')
                        if len(parts) >= 4:
                            libs.append(parts[3].replace("\\\\", "\\"))
        except Exception:
            pass
    return libs


def track_dir(ac_path, track, layout=""):
    """コースのデータフォルダ。レイアウトがあればそのサブフォルダ。"""
    base = os.path.join(ac_path, "content", "tracks", track)
    if layout:
        sub = os.path.join(base, layout)
        if os.path.isdir(sub):
            return sub, base
    return base, base


# ===========================================================================
# fast_lane.ai の解析
# ===========================================================================
#
# 既知のレイアウト（コミュニティのAIライン編集ツールで使われているもの）:
#   ヘッダ  : int32 version, int32 detailCount, int32 lapTime, int32 sampleCount
#   本体1   : detailCount 個 × { float x, y, z; float length; int32 id }   (20 byte)
#   本体2   : int32 extraCount, int32 unknown,
#             extraCount 個 × float×18                                    (72 byte)
#             （speed, gas, brake, obsoleteLatG, radius, sideLeft, sideRight, ...）
#
# 実ファイルで必ず検証してから使うこと（--dump で確認できます）。

AI_HEADER = struct.Struct("<4i")
AI_POINT = struct.Struct("<4fi")
AI_EXTRA = struct.Struct("<18f")


class TrackAIParseError(Exception):
    pass


def parse_fast_lane(path, verbose=False):
    """fast_lane.ai を読んで {points, sides, meta} を返す。"""
    with open(path, "rb") as fh:
        blob = fh.read()

    if len(blob) < AI_HEADER.size:
        raise TrackAIParseError("ファイルが小さすぎます")

    version, count, lap_time, sample_count = AI_HEADER.unpack_from(blob, 0)
    if verbose:
        print(f"  version={version} points={count} lapTime={lap_time} samples={sample_count}")

    if not (0 < count < 2_000_000):
        raise TrackAIParseError(f"点数が異常です: {count}")

    need = AI_HEADER.size + count * AI_POINT.size
    if len(blob) < need:
        raise TrackAIParseError(
            f"サイズ不足: {len(blob)} バイト（座標ブロックに {need} 必要）")

    pts, lengths = [], []
    off = AI_HEADER.size
    for _ in range(count):
        x, y, z, length, _id = AI_POINT.unpack_from(blob, off)
        off += AI_POINT.size
        pts.append((x, y, z))
        lengths.append(length)

    _sanity_check(pts, lengths)

    # ---- 追加ブロック（トラック幅）。取れなければ黙って諦める ----
    sides = None
    try:
        extra_count, _unknown = struct.unpack_from("<2i", blob, off)
        off += 8
        if extra_count == count and len(blob) >= off + count * AI_EXTRA.size:
            left, right = [], []
            for _ in range(count):
                v = AI_EXTRA.unpack_from(blob, off)
                off += AI_EXTRA.size
                left.append(v[5])
                right.append(v[6])
            if _plausible_widths(left) and _plausible_widths(right):
                sides = (left, right)
            elif verbose:
                print("  追加ブロックの幅が不自然なため無視します")
    except Exception:
        pass

    return {"points": pts, "lengths": lengths, "sides": sides,
            "version": version, "lapTime": lap_time}


def _sanity_check(pts, lengths):
    """座標が現実的か検証する（フォーマット違いを早期に弾く）。"""
    for x, y, z in pts[:200]:
        for v in (x, y, z):
            if not math.isfinite(v) or abs(v) > 100_000:
                raise TrackAIParseError(
                    "座標が現実的な範囲を超えています（フォーマット違いの可能性）")
    # 隣り合う点の間隔が極端でないか
    gaps = []
    for a, b in zip(pts, pts[1:201]):
        gaps.append(math.dist((a[0], a[2]), (b[0], b[2])))
    if gaps:
        med = sorted(gaps)[len(gaps) // 2]
        if med <= 0 or med > 200:
            raise TrackAIParseError(f"点の間隔が異常です（中央値 {med:.1f} m）")
    # length は 0 から単調増加のはず
    if lengths and (lengths[0] > 5 or lengths[-1] < lengths[0]):
        raise TrackAIParseError("距離データが単調増加していません")


def _plausible_widths(vals):
    ok = [v for v in vals if math.isfinite(v) and 0.5 <= v <= 40]
    return len(ok) > len(vals) * 0.8


# ===========================================================================
# map.ini
# ===========================================================================

def parse_map_ini(path):
    if not os.path.isfile(path):
        return None
    out = {}
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.split(";")[0].split("//")[0].strip()
                if "=" not in line or line.startswith("["):
                    continue
                k, v = line.split("=", 1)
                try:
                    out[k.strip().upper()] = float(v.strip())
                except ValueError:
                    out[k.strip().upper()] = v.strip()
    except Exception:
        return None
    return out


# ===========================================================================
# 書き出し
# ===========================================================================

def build_track_data(ac_path, track, layout="", verbose=False):
    """AC のインストールからコース形状を読み、そのまま配信できる dict を返す。

    ブリッジからも呼ばれる。読めなければ None（呼び出し側は走行から生成に切替）。
    """
    tdir, base = track_dir(ac_path, track, layout)
    ai_path = os.path.join(tdir, "ai", "fast_lane.ai")
    if not os.path.isfile(ai_path):
        alt = os.path.join(base, "ai", "fast_lane.ai")
        ai_path = alt if os.path.isfile(alt) else None

    key = track + ("__" + layout if layout else "")
    if verbose:
        print(f"\n[{key}]")
        print(f"  フォルダ: {tdir}")

    if not ai_path:
        if verbose:
            print(f"  × {key}: ai/fast_lane.ai がありません")
        return None

    try:
        ai = parse_fast_lane(ai_path, verbose)
    except TrackAIParseError as exc:
        if verbose:
            print(f"  × {key}: 解析できません — {exc}")
        return None

    # 走行方向の平面座標 (x, z)。y（高さ）は使わない
    xs = [p[0] for p in ai["points"]]
    zs = [p[2] for p in ai["points"]]
    total = ai["lengths"][-1] if ai["lengths"] else 0.0

    # 点数を間引く（描画には 800 点もあれば十分）
    step = max(1, math.ceil(len(xs) / 800))
    line = [[round(xs[i], 2), round(zs[i], 2)] for i in range(0, len(xs), step)]
    # 始点を最後にも足して閉じる
    if line and line[0] != line[-1]:
        line.append(line[0])

    data = {
        "track": track,
        "layout": layout,
        "source": "fast_lane.ai",
        "lengthM": round(total, 1),
        "bounds": {"minX": round(min(xs), 2), "maxX": round(max(xs), 2),
                   "minZ": round(min(zs), 2), "maxZ": round(max(zs), 2)},
        "line": line,
        "mapIni": parse_map_ini(os.path.join(tdir, "data", "map.ini")),
        "mapPng": os.path.isfile(os.path.join(tdir, "map.png")),
    }

    return data


def track_key(track, layout=""):
    return track + ("__" + layout if layout else "")


def save_track_data(data):
    """build_track_data() の結果を web-claude/tracks-claude/ に保存する。"""
    key = track_key(data["track"], data.get("layout", ""))
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, key + ".json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    return out


def export(ac_path, track, layout="", verbose=False):
    """CLI 用。読み出して JSON に保存し、結果を表示する。"""
    key = track_key(track, layout)
    data = build_track_data(ac_path, track, layout, verbose=True)
    if data is None:
        return None
    out = save_track_data(data)
    b = data["bounds"]
    print(f"  ○ {key}: {len(data['line'])} 点 / 全長 {data['lengthM']:,.0f} m / "
          f"範囲 {b['maxX']-b['minX']:,.0f}×{b['maxZ']-b['minZ']:,.0f} m "
          f"→ {os.path.basename(out)}")
    return data


def current_track_from_sharedmem():
    """走行中なら共有メモリから今のコース名を取る。"""
    try:
        from ac_sharedmem_claude import ACSharedMemory, SPageFileStatic
    except Exception:
        return None, None
    ac = ACSharedMemory()
    if not ac.ensure_open():
        return None, None
    st = ac.view("static", SPageFileStatic)
    # ビューは共有メモリを直接指すので、close() する前に値を取り出しておく
    track = (st.track or None) if st is not None else None
    layout = (st.trackConfiguration or "") if st is not None else ""
    ac.close()
    return track, layout


def list_tracks(ac_path):
    root = os.path.join(ac_path, "content", "tracks")
    for name in sorted(os.listdir(root)):
        tdir = os.path.join(root, name)
        if not os.path.isdir(tdir):
            continue
        layouts = []
        for sub in sorted(os.listdir(tdir)):
            if os.path.isfile(os.path.join(tdir, sub, "ai", "fast_lane.ai")):
                layouts.append(sub)
        if layouts:
            for lay in layouts:
                yield name, lay
        else:
            yield name, ""


def main():
    ap = argparse.ArgumentParser(
        description="AC のコース形状を JSON に書き出す")
    ap.add_argument("--ac-path", help="Assetto Corsa のインストールフォルダ")
    ap.add_argument("--track", help="コースのフォルダ名（例 ks_barcelona）")
    ap.add_argument("--layout", default="", help="レイアウト名（例 layout_moto）")
    ap.add_argument("--all", action="store_true", help="全コースを書き出す")
    ap.add_argument("--dump", action="store_true",
                    help="ファイルの中身の要約を表示（解析の確認用）")
    args = ap.parse_args()

    ac = find_ac_path(args.ac_path)
    if not ac:
        print("Assetto Corsa のインストールフォルダが見つかりませんでした。")
        print("--ac-path でフォルダを指定してください。例:")
        print(r'  python trackmap_claude.py --ac-path "D:\SteamLibrary\steamapps\common\assettocorsa"')
        return 1
    print(f"AC: {ac}")

    if args.all:
        ok = bad = 0
        for track, layout in list_tracks(ac):
            if export(ac, track, layout, args.dump):
                ok += 1
            else:
                bad += 1
        print(f"\n完了: 成功 {ok} / 失敗 {bad}")
        print(f"出力先: {OUT_DIR}")
        return 0

    track, layout = args.track, args.layout
    if not track:
        track, layout = current_track_from_sharedmem()
        if track:
            print(f"走行中のコースを検出: {track} {layout}")
        else:
            print("コースを特定できません。走行中に実行するか、--track で指定するか、")
            print("--all で全コースを書き出してください。")
            return 1

    return 0 if export(ac, track, layout, verbose=True) else 1


if __name__ == "__main__":
    sys.exit(main())
