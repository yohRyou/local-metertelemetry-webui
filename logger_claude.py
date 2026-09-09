#!/usr/bin/env python3
"""
logger_claude.py  —  Assetto Corsa テレメトリーロガー

bridge_claude.py が 60Hz で取得したフレームを、そのまま全チャンネル記録する。

  * 保存形式は gzip 圧縮した JSONL（1行 = 1フレームの JSON）
      - 項目を後から増やしても、古いログはそのまま読める（列固定の CSV と違う）
      - 圧縮で 1/10 前後になるので 60Hz でも実用的なサイズに収まる
      - ブラウザへは Content-Encoding: gzip で渡せるので、解凍処理が要らない
  * ラップごとにファイルを分割し、セッション単位でフォルダにまとめる
  * Excel で見たい時は to_csv_bytes() で CSV に変換できる（列は自動生成）

書き込みは専用スレッドで行うので、60Hz のポーリングループを止めない。
外部ライブラリ不要（標準ライブラリのみ）。インターネットには一切繋がない。

  logs-claude/
    20260908-143012_ks_nordschleife_bmw_m3_e30/
      session-claude.json      … セッションとラップの一覧
      lap000.jsonl.gz          … アウトラップ（部分ラップ）
      lap001.jsonl.gz
      lap002.jsonl.gz
"""

import csv
import gzip
import io
import json
import os
import queue
import re
import shutil
import threading
import time
import zlib

META_NAME = "session-claude.json"
QUEUE_MAX = 1800                       # 30秒ぶん。あふれたら捨てて記録は続ける
MIN_FREE_BYTES = 500 * 1024 * 1024     # 空きがこれを下回ったら記録を止める
FLUSH_SECONDS = 2.0
IDLE_CLOSE_FRAMES = 60 * 60            # 走行以外が60秒続いたらセッションを閉じる
WHEELS = ("FL", "FR", "RL", "RR")


# ===========================================================================
# 小物
# ===========================================================================

def sanitize(name, fallback="unknown"):
    """フォルダ名に使える形へ。日本語や記号は _ に潰す。"""
    s = re.sub(r"[^0-9A-Za-z._\-]+", "_", str(name or "")).strip("._-")
    return (s[:48] or fallback).lower()


def ms_to_name(ms):
    """134883 → '2-14.883'（ファイル名に使えるラップタイム表記）"""
    if not ms or ms <= 0 or ms > 3600000:
        return None
    m, rest = divmod(int(ms), 60000)
    return "%d-%06.3f" % (m, rest / 1000.0)


def ms_to_str(ms):
    if not ms or ms <= 0 or ms > 3600000:
        return "--:--.---"
    m, rest = divmod(int(ms), 60000)
    return "%d:%06.3f" % (m, rest / 1000.0)


def _walk(obj, key, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk(v, (key + "." + k) if key else str(k), out)
    elif isinstance(obj, (list, tuple)):
        names = None
        if len(obj) == 4 and (key.startswith("tyre.") or key in ("brakeTemp",)):
            names = WHEELS
        elif key == "coords" and len(obj) == 2:
            names = ("x", "z")
        elif key == "wind" and len(obj) == 2:
            names = ("speed", "dir")
        for i, v in enumerate(obj):
            sub = names[i] if names and i < len(names) else str(i)
            _walk(v, key + "." + sub, out)
    elif isinstance(obj, bool):
        out[key] = 1 if obj else 0
    else:
        out[key] = obj


def flatten_frame(frame):
    """入れ子のフレームを {'tyre.core.FL': 82.1, ...} という平らな辞書にする。"""
    out = {}
    _walk(frame, "", out)
    return out


def iter_lines(path):
    """.jsonl.gz を1行ずつ返す。

    gzip モジュールは末尾のトレーラが無いファイル（記録中に電源が落ちた等）を
    読めないので、zlib で自前に展開して読めるところまで読む。
    """
    dec = zlib.decompressobj(31)          # 31 = gzip ヘッダ込み
    buf = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 16)
            if not chunk:
                break
            try:
                buf += dec.decompress(chunk)
            except zlib.error:
                break
            while True:
                i = buf.find(b"\n")
                if i < 0:
                    break
                line, buf = buf[:i], buf[i + 1:]
                yield line
    # 末尾の中途半端な1行は捨てる（切れた JSON は読めない）


def _iter_objects(path):
    for raw in iter_lines(path):
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw.decode("utf-8"))
        except Exception:
            continue


def to_csv_bytes(path):
    """ラップの .jsonl.gz を CSV のバイト列に変換する（Excel 用に BOM 付き）。"""
    keys, seen, rows = [], set(), []
    for obj in _iter_objects(path):
        flat = flatten_frame(obj)
        for k in flat:
            if k not in seen:
                seen.add(k)
                keys.append(k)
        rows.append(flat)
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(keys)
    for r in rows:
        w.writerow([r.get(k, "") for k in keys])
    return buf.getvalue().encode("utf-8-sig")


def read_frames(path, limit=None):
    """.jsonl.gz を読んでフレームのリストにする（再生・検証用）。"""
    out = []
    for obj in _iter_objects(path):
        out.append(obj)
        if limit and len(out) >= limit:
            break
    return out


# ===========================================================================
# セッション一覧
# ===========================================================================

def _dir_bytes(path):
    total = 0
    try:
        for name in os.listdir(path):
            f = os.path.join(path, name)
            if os.path.isfile(f):
                total += os.path.getsize(f)
    except OSError:
        pass
    return total


def _rebuild_meta(path, name):
    """メタが壊れている / 無い場合に、ファイル名から最低限の一覧を作る。"""
    laps = []
    try:
        for fn in sorted(os.listdir(path)):
            if fn.endswith(".jsonl.gz"):
                laps.append({
                    "n": len(laps), "file": fn, "ms": 0, "time": "--:--.---",
                    "valid": False, "partial": True,
                    "frames": 0, "bytes": os.path.getsize(os.path.join(path, fn)),
                })
    except OSError:
        pass
    return {"id": name, "started": "", "track": "", "layout": "",
            "car": "", "driver": "", "recovered": True, "laps": laps}


def list_sessions(base_dir):
    """logs-claude/ の中身を新しい順に返す。"""
    out = []
    if not os.path.isdir(base_dir):
        return out
    for name in sorted(os.listdir(base_dir), reverse=True):
        d = os.path.join(base_dir, name)
        if not os.path.isdir(d):
            continue
        meta_path = os.path.join(d, META_NAME)
        meta = None
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
            except Exception:
                meta = None
        if meta is None:
            meta = _rebuild_meta(d, name)
        meta["id"] = name
        meta["bytes"] = _dir_bytes(d)
        # 実際に残っているファイルだけを残す（手で消された場合の保険）
        meta["laps"] = [l for l in meta.get("laps", [])
                        if os.path.isfile(os.path.join(d, str(l.get("file", ""))))]
        if meta["laps"]:
            out.append(meta)
    return out


def delete_session(base_dir, session_id):
    """セッションフォルダをまるごと削除する。"""
    name = os.path.basename(str(session_id or ""))
    if not name or name.startswith("."):
        return False
    d = os.path.join(base_dir, name)
    if not os.path.isdir(d) or os.path.dirname(os.path.abspath(d)) != os.path.abspath(base_dir):
        return False
    shutil.rmtree(d, ignore_errors=True)
    return not os.path.isdir(d)


# ===========================================================================
# ロガー本体
# ===========================================================================

class TelemetryLoggerClaude:
    """
    60Hz のフレームを受け取り、別スレッドでラップごとに書き出す。

        log = TelemetryLoggerClaude(base_dir)
        log.feed(frame)      # ポーリングループから毎フレーム呼ぶ（非ブロッキング）
        log.status()         # 配信フレームに載せる記録状態
        log.set_recording(False)
    """

    def __init__(self, base_dir, enabled=True, dumps=None):
        self.dir = base_dir
        # dumps(frame) -> str。ブリッジ側は NaN/Inf を落とす安全版を渡す。
        self.dumps = dumps or (
            lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":")))
        try:
            os.makedirs(base_dir, exist_ok=True)
        except OSError:
            pass

        self.q = queue.Queue(QUEUE_MAX)
        self._enabled = bool(enabled)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._was_connected = False

        self._stat = {"on": self._enabled, "session": None, "lap": None,
                      "frames": 0, "laps": 0, "bytes": 0, "dropped": 0, "note": ""}

        # 書き出しスレッド側の状態
        self._fh = None
        self._sess_dir = None
        self._sess_meta = None
        self._lap_no = None
        self._lap_file = None
        self._lap_frames = 0
        self._lap_pit = False
        self._lap_out = 0
        self._lap_start_norm = None
        self._last_flush = 0.0
        self._disk_warned = False
        self._idle = 0

        self._thread = threading.Thread(target=self._run, name="logger-claude",
                                        daemon=True)
        self._thread.start()

    # -- 呼び出し側 API ----------------------------------------------------

    def feed(self, frame):
        """ポーリングループから毎フレーム呼ぶ。決してブロックしない。"""
        if not self._enabled:
            return
        connected = bool(frame and frame.get("connected"))
        if not connected:
            if self._was_connected:
                self._was_connected = False
                self._put(("end", None))
            return
        self._was_connected = True
        self._put(("f", frame))

    def _put(self, item):
        try:
            self.q.put_nowait(item)
        except queue.Full:
            with self._lock:
                self._stat["dropped"] += 1

    def set_recording(self, on):
        on = bool(on)
        if on == self._enabled:
            return self.status()
        self._enabled = on
        with self._lock:
            self._stat["on"] = on
            if on:
                self._stat["note"] = ""
        if not on:
            self._put(("end", None))
        return self.status()

    def toggle(self):
        return self.set_recording(not self._enabled)

    def status(self):
        with self._lock:
            return dict(self._stat)

    def close(self):
        self._put(("end", None))
        self._stop.set()
        try:
            self._thread.join(timeout=3.0)
        except Exception:
            pass

    # -- 書き出しスレッド --------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            try:
                kind, frame = self.q.get(timeout=0.25)
            except queue.Empty:
                self._maybe_flush()
                continue
            try:
                if kind == "end":
                    self._close_session()
                else:
                    self._write(frame)
            except Exception as exc:            # 記録の失敗で本体を止めない
                self._note("記録エラー: %s" % exc)
                self._close_session()
        self._close_session()

    def _note(self, text):
        with self._lock:
            self._stat["note"] = text
        print("[logger] " + text)

    # -- セッション --------------------------------------------------------

    def _session_id(self, frame):
        track = sanitize(frame.get("track"), "track")
        layout = sanitize(frame.get("trackConfig"), "")
        car = sanitize(frame.get("car"), "car")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        parts = [stamp, track]
        if layout:
            parts.append(layout)
        parts.append(car)
        return "_".join(parts)

    def _disk_ok(self):
        try:
            free = shutil.disk_usage(self.dir).free
        except Exception:
            return True
        if free < MIN_FREE_BYTES:
            if not self._disk_warned:
                self._disk_warned = True
                self._note("空き容量が少ないため記録を停止しました（残り %.1f GB）"
                           % (free / 1073741824.0))
                self._enabled = False
                with self._lock:
                    self._stat["on"] = False
            return False
        self._disk_warned = False
        return True

    def _open_session(self, frame):
        if not self._disk_ok():
            return False
        sid = self._session_id(frame)
        d = os.path.join(self.dir, sid)
        os.makedirs(d, exist_ok=True)
        self._sess_dir = d
        self._sess_meta = {
            "id": sid,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "track": frame.get("track") or "",
            "layout": frame.get("trackConfig") or "",
            "car": frame.get("car") or "",
            "driver": frame.get("driver") or "",
            "session": frame.get("session") or "",
            "rate": 60,
            "laps": [],
        }
        with self._lock:
            self._stat["session"] = sid
            self._stat["laps"] = 0
        self._save_meta()
        print("[logger] 記録開始: %s" % d)
        return True

    def _close_session(self):
        self._close_lap(0)
        if self._sess_meta is not None:
            self._save_meta()
            print("[logger] 記録終了: %s" % self._sess_dir)
        self._sess_dir = None
        self._sess_meta = None
        self._lap_no = None
        with self._lock:
            self._stat["session"] = None
            self._stat["lap"] = None
            self._stat["frames"] = 0

    def _save_meta(self):
        if not self._sess_dir or self._sess_meta is None:
            return
        tmp = os.path.join(self._sess_dir, META_NAME + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._sess_meta, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(self._sess_dir, META_NAME))
        except OSError as exc:
            self._note("メタ情報を保存できません: %s" % exc)

    # -- ラップ ------------------------------------------------------------

    def _lap_path(self, lap_no):
        base = "lap%03d" % max(0, int(lap_no))
        name = base + ".jsonl.gz"
        n = 1
        while os.path.exists(os.path.join(self._sess_dir, name)):
            name = "%s-%d.jsonl.gz" % (base, n)
            n += 1
        return name

    def _open_lap(self, lap_no, frame):
        self._lap_file = self._lap_path(lap_no)
        path = os.path.join(self._sess_dir, self._lap_file)
        # newline を固定しないと Windows で改行が \r\n に変換される
        self._fh = gzip.open(path, "wt", encoding="utf-8", newline="\n",
                             compresslevel=5)
        self._lap_no = lap_no
        self._lap_frames = 0
        self._lap_pit = False
        self._lap_out = 0
        self._lap_start_norm = frame.get("normPos")
        with self._lock:
            self._stat["lap"] = lap_no
            self._stat["frames"] = 0

    def _close_lap(self, last_time_ms):
        if self._fh is None:
            return
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None
        if self._sess_meta is None:
            return
        path = os.path.join(self._sess_dir, self._lap_file)
        size = os.path.getsize(path) if os.path.isfile(path) else 0
        if self._lap_frames < 10:            # 実質空のラップは残さない
            try:
                os.remove(path)
            except OSError:
                pass
            return
        # 先頭付近から始まっていないラップは部分ラップ扱い
        partial = (last_time_ms or 0) <= 0 or (self._lap_start_norm or 0) > 0.1
        entry = {
            "n": self._lap_no,
            "file": self._lap_file,
            "ms": int(last_time_ms or 0),
            "time": ms_to_str(last_time_ms) if not partial else "—",
            "valid": (not partial) and (not self._lap_pit) and self._lap_out < 3,
            "partial": partial,
            "pit": self._lap_pit,
            "frames": self._lap_frames,
            "bytes": size,
        }
        self._sess_meta["laps"].append(entry)
        with self._lock:
            self._stat["laps"] = len(self._sess_meta["laps"])
            self._stat["bytes"] = _dir_bytes(self._sess_dir)
        self._save_meta()
        print("[logger] LAP %d  %s  %s  %.1f MB"
              % (entry["n"], entry["time"],
                 "OK" if entry["valid"] else ("部分" if partial else "無効"),
                 size / 1048576.0))

    # -- 1フレーム ---------------------------------------------------------

    def _write(self, frame):
        if not self._enabled:
            return

        # 走行中（AC の status が LIVE）以外は記録しない。
        # メニューやリプレイ、ポーズ中の値は残しても意味がなく、
        # 放置しているだけでディスクを食い潰してしまう。
        if frame.get("statusText") != "LIVE":
            self._idle += 1
            if self._sess_meta is not None and self._idle > IDLE_CLOSE_FRAMES:
                print("[logger] 走行が止まったのでセッションを閉じます")
                self._close_session()
            return
        self._idle = 0

        lap = int(frame.get("lap") or 1)

        # セッションが変わった（コース・車の変更、あるいはセッション再開）
        if self._sess_meta is not None:
            changed = ((frame.get("track") or "") != self._sess_meta["track"] or
                       (frame.get("car") or "") != self._sess_meta["car"])
            if changed or (self._lap_no is not None and lap < self._lap_no):
                self._close_session()

        if self._sess_meta is None:
            if not self._open_session(frame):
                return
            self._open_lap(lap, frame)
        elif lap != self._lap_no:
            self._close_lap(frame.get("lastTime"))
            if not self._disk_ok():
                self._close_session()
                return
            self._open_lap(lap, frame)

        self._fh.write(self.dumps(frame))
        self._fh.write("\n")
        self._lap_frames += 1
        if frame.get("isInPitLane") or frame.get("isInPit"):
            self._lap_pit = True
        self._lap_out = max(self._lap_out, int(frame.get("tyresOut") or 0))
        if self._lap_frames % 30 == 0:
            with self._lock:
                self._stat["frames"] = self._lap_frames
        self._maybe_flush()

    def _maybe_flush(self):
        now = time.time()
        if self._fh is not None and now - self._last_flush > FLUSH_SECONDS:
            self._last_flush = now
            try:
                self._fh.flush()
            except Exception:
                pass
            if self._sess_dir:
                with self._lock:
                    self._stat["bytes"] = _dir_bytes(self._sess_dir)


# ===========================================================================
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1].endswith(".jsonl.gz"):
        src = sys.argv[1]
        dst = sys.argv[2] if len(sys.argv) > 2 else src[:-9] + ".csv"
        with open(dst, "wb") as fh:
            fh.write(to_csv_bytes(src))
        print("CSV に変換しました: %s" % dst)
    else:
        base = sys.argv[1] if len(sys.argv) > 1 else "logs-claude"
        for s in list_sessions(base):
            print("%s  %s / %s  %d laps  %.1f MB"
                  % (s["id"], s.get("track"), s.get("car"),
                     len(s["laps"]), s.get("bytes", 0) / 1048576.0))
