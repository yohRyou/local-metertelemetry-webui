#!/usr/bin/env python3
"""
bridge_claude.py  —  Assetto Corsa テレメトリー LAN ブリッジ

ゲームPC(Windows)で起動すると
  * AC の共有メモリを 60Hz でポーリング
  * 同一LAN上の端末へ WebSocket でリアルタイム配信
  * ダッシュボードHTMLを配信する簡易HTTPサーバー
を1プロセスで行う。

外部ライブラリ不要（Python 3.8+ 標準ライブラリのみ）。
インターネット接続は一切行わない。

  起動:  python bridge_claude.py
  デモ:  python bridge_claude.py --demo      (ゲーム無しで疑似データ)
  ポート: python bridge_claude.py --port 8720

  スマホ/タブレット  http://<ゲームPCのIP>:8720/mobile
  別PC(エンジニア)   http://<ゲームPCのIP>:8720/engineer
"""

import argparse
import asyncio
import base64
import hashlib
import json
import math
import mimetypes
import os
import socket
import struct
import sys
import time

import logger_claude
from logger_claude import TelemetryLoggerClaude
from ac_sharedmem_claude import (
    ACSharedMemory,
    SPageFilePhysics,
    SPageFileGraphic,
    SPageFileStatic,
    AC_STATUS,
    AC_SESSION_TYPE,
    AC_FLAG_TYPE,
)

# 共有メモリの更新が止まってから、開き直しを試みるまでの秒数。
# AC を終了してもマッピング自体はこちらが握っている限り残り、値が固まったまま
# 読めてしまうため、更新の停止で「落ちた」ことを検知する。
STALE_SECONDS = 5.0

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web-claude")
TRACKS_DIR = os.path.join(WEB_DIR, "tracks-claude")
LOGS_DIR = os.path.join(BASE_DIR, "logs-claude")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

GEAR_NAMES = ["R", "N", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]


# ===========================================================================
# ラップ / デルタ計算
# ===========================================================================

class LapTrackerClaude:
    """ベストラップを基準にしたリアルタイムデルタと燃費を計算する。"""

    SAMPLES = 1000  # トラック1周を1000分割して基準ラップを保持

    def __init__(self):
        self.reset()

    def reset(self):
        self.ref = None            # 基準ラップ [ms] × SAMPLES
        self.ref_time = None       # 基準ラップのタイム [ms]
        self.cur = [None] * self.SAMPLES
        self.last_norm = 0.0
        self.last_laps = -1
        self.lap_start_fuel = None
        self.fuel_per_lap = None
        self.last_lap_ms = 0
        self.best_lap_ms = 0

    def update(self, norm_pos, cur_time_ms, completed_laps,
               last_time_ms, best_time_ms, fuel, valid):
        delta = None

        # 新しいラップに入った
        if completed_laps != self.last_laps:
            if self.last_laps >= 0 and last_time_ms > 0:
                # 直前ラップが自己ベストなら基準として採用
                if self.ref_time is None or last_time_ms < self.ref_time:
                    filled = self._fill(self.cur)
                    if filled is not None:
                        self.ref = filled
                        self.ref_time = last_time_ms
                if self.lap_start_fuel is not None and fuel is not None:
                    used = self.lap_start_fuel - fuel
                    if 0.05 < used < 30:
                        self.fuel_per_lap = (
                            used if self.fuel_per_lap is None
                            else self.fuel_per_lap * 0.6 + used * 0.4)
            self.cur = [None] * self.SAMPLES
            self.lap_start_fuel = fuel
            self.last_laps = completed_laps

        if self.lap_start_fuel is None:
            self.lap_start_fuel = fuel

        # 現在ラップのサンプル記録
        if valid and 0.0 <= norm_pos <= 1.0 and cur_time_ms > 0:
            x = norm_pos * self.SAMPLES
            idx = min(self.SAMPLES - 1, int(x))
            if self.cur[idx] is None:
                self.cur[idx] = cur_time_ms
            if self.ref is not None:
                # 区間の刻み（1周/1000）そのままだとデルタが段々に跳ねるので、
                # 前後のサンプルを線形補間して滑らかにする。
                a = self.ref[idx]
                b = self.ref[min(self.SAMPLES - 1, idx + 1)]
                ref_t = a + (b - a) * (x - idx)
                delta = (cur_time_ms - ref_t) / 1000.0

        self.last_norm = norm_pos
        self.last_lap_ms = last_time_ms
        self.best_lap_ms = best_time_ms
        return delta

    @staticmethod
    def _fill(samples):
        """欠損サンプルを線形補間して埋める。埋められなければ None。"""
        known = [i for i, v in enumerate(samples) if v is not None]
        if len(known) < len(samples) * 0.5:
            return None
        out = list(samples)
        # 先頭
        for i in range(known[0]):
            out[i] = out[known[0]]
        # 末尾
        for i in range(known[-1] + 1, len(out)):
            out[i] = out[known[-1]]
        # 中間
        for a, b in zip(known, known[1:]):
            if b - a > 1:
                span = b - a
                for k in range(1, span):
                    out[a + k] = out[a] + (out[b] - out[a]) * k / span
        return out


# ===========================================================================
# テレメトリー収集
# ===========================================================================

def ms_to_str(ms):
    if ms is None or ms <= 0 or ms >= 999999999:
        return "--:--.---"
    ms = int(ms)
    m, rem = divmod(ms, 60000)
    s, milli = divmod(rem, 1000)
    return f"{m}:{s:02d}.{milli:03d}"


class TelemetrySourceClaude:
    """共有メモリ or デモ生成器から、配信用 dict を作る。"""

    def __init__(self, demo=False):
        self.demo = demo
        self.ac = ACSharedMemory()
        self.lap = LapTrackerClaude()
        self.connected = False
        self.t0 = time.perf_counter()
        self._last_packet = None       # 更新停止の検知用
        self._stale_since = None
        self._demo_state = {"laps": 0, "lap_t": 0.0, "fuel": 42.0,
                            "best": 0, "last": 0, "tyre": [78.0] * 4,
                            "brake": [320.0] * 4, "kers": 0.6}
        self._last_note = ""

    # -- 接続管理 ----------------------------------------------------------
    def ensure(self):
        """共有メモリが開けているか確認する。AC 未起動なら False を返すだけ。"""
        if self.demo:
            self.connected = True
            return True
        was = self.connected
        ok = self.ac.ensure_open()
        if ok and not was:
            print("[bridge] Assetto Corsa の共有メモリに接続しました")
            self._last_packet = None
            self._stale_since = None
        self.connected = ok
        if not ok and self.ac.last_error != self._last_note:
            self._last_note = self.ac.last_error
            print(f"[bridge] 待機中: {self.ac.last_error}")
        return ok

    def _check_stale(self, packet_id):
        """更新が止まっていたら開き直す。AC が終了していれば次回 WAITING に戻る。"""
        now = time.perf_counter()
        if packet_id != self._last_packet:
            self._last_packet = packet_id
            self._stale_since = None
            return
        if self._stale_since is None:
            self._stale_since = now
        elif now - self._stale_since > STALE_SECONDS:
            self._stale_since = None
            self._last_packet = None
            if not self.ac.reopen():
                self.connected = False
                print("[bridge] Assetto Corsa が終了したようです。再接続を待ちます")

    # -- 1フレーム ---------------------------------------------------------
    def frame(self):
        if self.demo:
            return self._demo_frame()
        if not self.ensure():
            return {"connected": False, "statusText": "WAITING",
                    "note": "Assetto Corsa を起動してセッションに入ってください"}

        phys = self.ac.view("physics", SPageFilePhysics)
        gra = self.ac.view("graphics", SPageFileGraphic)
        sta = self.ac.view("static", SPageFileStatic)
        if phys is None or gra is None or sta is None:
            note = self.ac.last_error or "共有メモリの読み取りに失敗しました"
            self.ac.reopen()
            self.connected = False
            return {"connected": False, "statusText": "WAITING", "note": note}

        self._check_stale(phys.packetId)
        if not self.connected:
            return {"connected": False, "statusText": "WAITING",
                    "note": "Assetto Corsa を起動してセッションに入ってください"}
        return self._build(phys, gra, sta)

    # -- 実データ整形 ------------------------------------------------------
    def _build(self, p, g, s):
        max_rpm = s.maxRpm if s.maxRpm > 0 else 8000
        status = AC_STATUS.get(g.status, "OFF")
        live = (g.status == 2)

        delta = self.lap.update(
            norm_pos=g.normalizedCarPosition,
            cur_time_ms=g.iCurrentTime,
            completed_laps=g.completedLaps,
            last_time_ms=g.iLastTime,
            best_time_ms=g.iBestTime,
            fuel=p.fuel,
            valid=live and not g.isInPitLane,
        )

        fuel_per_lap = self.lap.fuel_per_lap
        laps_left = (p.fuel / fuel_per_lap) if fuel_per_lap else None

        gear_idx = max(0, min(len(GEAR_NAMES) - 1, p.gear))

        return {
            "connected": True,
            "t": int((time.perf_counter() - self.t0) * 1000),
            "statusText": status,
            "session": AC_SESSION_TYPE.get(g.session, "UNKNOWN"),
            "flag": AC_FLAG_TYPE.get(g.flag, "NONE"),

            # --- ドライビング基本 ---
            "speed": p.speedKmh,
            "rpm": p.rpms,
            "maxRpm": max_rpm,
            "gear": GEAR_NAMES[gear_idx],
            "throttle": p.gas,
            "brake": p.brake,
            "clutch": p.clutch,
            "steer": p.steerAngle,
            "abs": p.abs,
            "tc": p.tc,
            "drsAvail": bool(p.drsAvailable),
            "drsOn": bool(p.drsEnabled),
            "pitLimiter": bool(p.pitLimiterOn),
            "turbo": p.turboBoost,
            "maxTurbo": s.maxTurboBoost,
            "brakeBias": p.brakeBias,
            "autoShifter": bool(p.autoShifterOn),

            # --- KERS / ERS（AC には MGU-H / MGU-K の個別項目は無く、
            #     これらの ERS 系の値がその役割を担う） ---
            "kers": {
                "charge": p.kersCharge,          # 0..1
                "input": p.kersInput,            # 0..1（放出中の量）
                "kj": p.kersCurrentKJ,           # 現在の蓄積 [kJ]
                "maxJ": s.kersMaxJ,              # 容量 [J]
            },
            "ers": {
                "recovery": p.ersRecoveryLevel,
                "power": p.ersPowerLevel,
                "powerLevels": s.ersPowerControllerCount,
                "heatCharging": p.ersHeatCharging,   # 0=ブレーキ回生, 1=熱回生
                "isCharging": bool(p.ersIsCharging),
                "maxJ": s.ersMaxJ,
            },

            # --- ラップ ---
            "lap": g.completedLaps + 1,
            "totalLaps": g.numberOfLaps,
            "position": g.position,
            "curTime": g.iCurrentTime,
            "lastTime": g.iLastTime,
            "bestTime": g.iBestTime,
            "curTimeStr": ms_to_str(g.iCurrentTime),
            "lastTimeStr": ms_to_str(g.iLastTime),
            "bestTimeStr": ms_to_str(g.iBestTime),
            "delta": delta,
            "sector": g.currentSectorIndex,
            "lastSector": g.lastSectorTime,
            "sessionTimeLeft": g.sessionTimeLeft,
            "normPos": g.normalizedCarPosition,
            "coords": [g.carCoordinates[0], g.carCoordinates[2]],
            "isInPit": bool(g.isInPit),
            "isInPitLane": bool(g.isInPitLane),
            "penalty": g.penaltyTime,

            # --- 燃料 ---
            "fuel": p.fuel,
            "maxFuel": s.maxFuel,
            "fuelPerLap": fuel_per_lap,
            "lapsLeft": laps_left,

            # --- タイヤ / ブレーキ ---
            "tyre": {
                "core": list(p.tyreCoreTemperature),
                "inner": list(p.tyreTempI),
                "middle": list(p.tyreTempM),
                "outer": list(p.tyreTempO),
                "press": list(p.wheelsPressure),
                "wear": list(p.tyreWear),
                "slip": list(p.wheelSlip),
                "load": list(p.wheelLoad),
                "dirt": list(p.tyreDirtyLevel),
                "camber": [c * 180.0 / math.pi for c in p.camberRAD],
                "susp": list(p.suspensionTravel),
                "compound": g.tyreCompound,
            },
            "brakeTemp": list(p.brakeTemp),

            # --- 車体 ---
            "g": {"lat": p.accG[0], "vert": p.accG[1], "lon": p.accG[2]},
            "damage": list(p.carDamage),
            "tyresOut": p.numberOfTyresOut,
            "rideHeight": list(p.rideHeight),

            # --- 環境 ---
            "airTemp": p.airTemp,
            "roadTemp": p.roadTemp,
            "surfaceGrip": g.surfaceGrip,
            "wind": [g.windSpeed, g.windDirection],

            # --- 静的 ---
            "car": s.carModel,
            "track": s.track,
            "trackConfig": s.trackConfiguration,
            "driver": (s.playerName + " " + s.playerSurname).strip(),
            "maxPower": s.maxPower,
            "maxTorque": s.maxTorque,
            "hasDRS": bool(s.hasDRS),
            "hasERS": bool(s.hasERS),
            "hasKERS": bool(s.hasKERS),
        }

    # -- デモ --------------------------------------------------------------
    def _demo_frame(self):
        st = self._demo_state
        t = time.perf_counter() - self.t0
        dt = 1 / 60

        lap_len = 92.0                      # 疑似ラップ長 [s]
        st["lap_t"] += dt
        if st["lap_t"] >= lap_len:
            st["lap_t"] -= lap_len
            st["laps"] += 1
            st["last"] = int(lap_len * 1000 + math.sin(st["laps"]) * 900)
            st["best"] = st["last"] if st["best"] == 0 else min(st["best"], st["last"])
        norm = st["lap_t"] / lap_len

        # コーナーを模したスピードプロファイル
        base = 0.5 + 0.5 * math.sin(norm * math.pi * 8)
        corner = max(0.0, math.sin(norm * math.pi * 6) ** 4)
        speed = 60 + 200 * base * (1 - 0.7 * corner)
        speed = max(35.0, speed + 8 * math.sin(t * 3.1))

        gear_num = min(6, max(1, int(speed / 45) + 1))
        rpm = 2200 + (speed % 45) / 45 * 5200 + 900 * math.sin(t * 11)
        rpm = max(1200, min(7600, rpm))

        throttle = max(0.0, min(1.0, 0.55 + 0.5 * math.sin(t * 2.3) - corner))
        brake = max(0.0, min(1.0, corner * 1.4 - 0.15))
        steer = 55 * math.sin(norm * math.pi * 6) * corner

        st["fuel"] = max(1.5, st["fuel"] - 0.0006)
        lat = steer / 55 * 2.4 * (speed / 200)
        lon = (throttle * 0.9 - brake * 2.6)

        # KERS: ブレーキで回生、スロットルで放出
        st["kers"] = max(0.0, min(1.0, st["kers"] + brake * 0.0022 - throttle * 0.0016))

        for i in range(4):
            target = 82 + 14 * corner + (3 if i < 2 else 0)
            st["tyre"][i] += (target - st["tyre"][i]) * 0.02
            bt = 280 + 420 * brake + (60 if i < 2 else 0)
            st["brake"][i] += (bt - st["brake"][i]) * 0.05

        cur_ms = int(st["lap_t"] * 1000)
        delta = self.lap.update(norm, cur_ms, st["laps"], st["last"],
                                st["best"], st["fuel"], True)

        ang = norm * math.pi * 2
        return {
            "connected": True, "demo": True,
            "t": int(t * 1000),
            "statusText": "LIVE", "session": "PRACTICE", "flag": "NONE",
            "speed": speed, "rpm": rpm, "maxRpm": 7800,
            "gear": GEAR_NAMES[gear_num + 1],
            "throttle": throttle, "brake": brake, "clutch": 0.0,
            "steer": steer, "abs": brake * 0.3, "tc": max(0, throttle - 0.8),
            "drsAvail": False, "drsOn": False, "pitLimiter": False,
            "turbo": 0.62 * throttle + 0.12, "maxTurbo": 1.8,
            "brakeBias": 0.62, "autoShifter": False,
            "kers": {"charge": st["kers"], "input": throttle * 0.8 if st["kers"] > 0.02 else 0.0,
                     "kj": st["kers"] * 4000, "maxJ": 4_000_000},
            "ers": {"recovery": 6, "power": 3, "powerLevels": 5,
                    "heatCharging": 0, "isCharging": brake > 0.2, "maxJ": 4_000_000},
            "lap": st["laps"] + 1, "totalLaps": 0, "position": 1,
            "curTime": cur_ms, "lastTime": st["last"], "bestTime": st["best"],
            "curTimeStr": ms_to_str(cur_ms),
            "lastTimeStr": ms_to_str(st["last"]),
            "bestTimeStr": ms_to_str(st["best"]),
            "delta": delta, "sector": int(norm * 3), "lastSector": 31000,
            "sessionTimeLeft": 1800000 - t * 1000,
            "normPos": norm,
            "coords": [520 * math.sin(ang) + 120 * math.sin(ang * 3),
                       380 * math.cos(ang) + 90 * math.cos(ang * 2)],
            "isInPit": False, "isInPitLane": False, "penalty": 0.0,
            "fuel": st["fuel"], "maxFuel": 60.0,
            "fuelPerLap": self.lap.fuel_per_lap, "lapsLeft": (
                st["fuel"] / self.lap.fuel_per_lap
                if self.lap.fuel_per_lap else None),
            "tyre": {
                "core": list(st["tyre"]),
                "inner": [v + 6 for v in st["tyre"]],
                "middle": list(st["tyre"]),
                "outer": [v - 4 + 10 * corner for v in st["tyre"]],
                "press": [26.4 + (v - 80) * 0.06 for v in st["tyre"]],
                "wear": [100 - st["laps"] * 0.9 - i * 0.3 for i in range(4)],
                "slip": [corner * 3 + throttle * 0.5 for _ in range(4)],
                "load": [3200 + 900 * math.sin(t * 4 + i) for i in range(4)],
                "dirt": [0.0] * 4,
                "camber": [-3.2, -3.2, -2.6, -2.6],
                "susp": [0.06 + 0.01 * math.sin(t * 5 + i) for i in range(4)],
                "compound": "Demo Soft",
            },
            "brakeTemp": list(st["brake"]),
            "g": {"lat": lat, "vert": 1.0, "lon": lon},
            "damage": [0.0] * 5, "tyresOut": 0, "rideHeight": [0.06, 0.07],
            "airTemp": 24.5, "roadTemp": 33.2, "surfaceGrip": 0.98,
            "wind": [2.0, 180.0],
            "car": "DEMO_CAR", "track": "demo_circuit", "trackConfig": "",
            "driver": "Demo Driver", "maxPower": 340, "maxTorque": 420,
            "hasDRS": False, "hasERS": True, "hasKERS": True,
        }


# ===========================================================================
# コースマップの供給
# ===========================================================================

class TrackMapProviderClaude:
    """走行中のコース形状を AC のインストールから直接読み出して供給する。

    優先順位
      1. web-claude/tracks-claude/ に書き出し済みの JSON（手で置いたものも尊重）
      2. AC のインストールフォルダの ai/fast_lane.ai を直接解析
         （読めたら 1. の場所に保存し、次回以降は即座に返す）

    どちらも駄目なら None。画面側は走行座標からの自動生成に切り替わる。
    """

    def __init__(self, ac_path=None):
        self._explicit = ac_path
        self.ac_path = None
        self._searched = False
        self._cache = {}          # key -> bytes(JSON) or None（見つからなかった記録）

    def _find_ac(self):
        if self._searched:
            return self.ac_path
        self._searched = True
        try:
            from trackmap_claude import find_ac_path
            self.ac_path = find_ac_path(self._explicit)
        except Exception as exc:
            print(f"[bridge] コース形状の読み出しを初期化できません: {exc}")
            self.ac_path = None
        if self.ac_path:
            print(f"[bridge] Assetto Corsa: {self.ac_path}")
        else:
            print("[bridge] AC のインストールフォルダが見つかりません"
                  "（--ac-path で指定できます）。コースマップは走行から生成します")
        return self.ac_path

    def get(self, track, layout):
        """JSON のバイト列を返す。無ければ None。※ブロックするのでスレッドで呼ぶ。"""
        if not track:
            return None
        key = track + ("__" + layout if layout else "")
        if key in self._cache:
            return self._cache[key]

        # 1) 書き出し済み JSON
        for name in ([key, track] if layout else [track]):
            path = os.path.join(TRACKS_DIR, os.path.basename(name) + ".json")
            if os.path.isfile(path):
                try:
                    with open(path, "rb") as fh:
                        blob = fh.read()
                    self._cache[key] = blob
                    return blob
                except Exception:
                    pass

        # 2) AC から直接読む
        ac = self._find_ac()
        if not ac:
            self._cache[key] = None
            return None
        try:
            from trackmap_claude import build_track_data, save_track_data
            data = build_track_data(ac, track, layout)
        except Exception as exc:
            print(f"[bridge] {key}: コース形状を読めません — {exc}")
            data = None
        if data is None:
            print(f"[bridge] {key}: コース形状が取得できないため走行から生成します")
            self._cache[key] = None
            return None

        blob = json.dumps(data, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        self._cache[key] = blob
        try:
            out = save_track_data(data)
            print(f"[bridge] {key}: コース形状を AC から取得しました "
                  f"({len(data['line'])} 点 / {data['lengthM']:,.0f} m) "
                  f"→ {os.path.basename(out)} に保存")
        except Exception:
            print(f"[bridge] {key}: コース形状を AC から取得しました（保存はできず）")
        return blob


# ===========================================================================
# 最小 WebSocket サーバー (RFC 6455 / サーバー送信のみ)
# ===========================================================================

class WSError(Exception):
    pass


def ws_frame(payload: bytes, opcode=0x1) -> bytes:
    """サーバー→クライアント（マスク無し）フレームを組み立てる。"""
    header = bytearray()
    header.append(0x80 | opcode)          # FIN + opcode
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + payload


async def ws_read_frame(reader):
    """クライアント→サーバーのフレームを1つ読む。(opcode, payload)"""
    hdr = await reader.readexactly(2)
    fin_op = hdr[0]
    opcode = fin_op & 0x0F
    masked = hdr[1] & 0x80
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    if length > 1 << 20:
        raise WSError("frame too large")
    mask = await reader.readexactly(4) if masked else None
    data = await reader.readexactly(length) if length else b""
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data


# ===========================================================================
# HTTP + WebSocket サーバー
# ===========================================================================

class BridgeServerClaude:

    ROUTES = {
        "/": "index-claude.html",
        "/index.html": "index-claude.html",
        "/mobile": "mobile-claude.html",
        "/mobile.html": "mobile-claude.html",
        "/engineer": "engineer-claude.html",
        "/engineer.html": "engineer-claude.html",
    }

    def __init__(self, source, host="0.0.0.0", port=8720, rate=60,
                 open_browser=False, ac_path=None, log_dir=LOGS_DIR,
                 log_enabled=True):
        self.source = source
        self.tracks = TrackMapProviderClaude(ac_path)
        self.host = host
        self.port = port
        self.rate = rate
        self.open_browser = open_browser
        self.latest = {"connected": False, "statusText": "STARTING"}
        self.tick = asyncio.Event()
        self.clients = 0
        self.log_dir = log_dir
        self.logger = TelemetryLoggerClaude(log_dir, enabled=log_enabled,
                                            dumps=json_dumps_claude)

    # -- ポーリングループ ---------------------------------------------------
    async def poll_loop(self):
        interval = 1.0 / self.rate
        next_t = time.perf_counter()
        while True:
            try:
                self.latest = self.source.frame()
            except Exception as exc:
                self.latest = {"connected": False, "statusText": "ERROR",
                               "note": str(exc)}
            # 記録は「rec」を足す前のフレームを渡す。書き出しは別スレッドで
            # 後から行われるので、同じ dict に足すと記録側にも混ざってしまう。
            # 配信用は rec を足した別の dict にする。
            self.logger.feed(self.latest)
            self.latest = dict(self.latest, rec=self.logger.status())
            self.tick.set()
            self.tick.clear()
            next_t += interval
            sleep = next_t - time.perf_counter()
            if sleep < -0.5:            # 大幅に遅れたらリセット
                next_t = time.perf_counter()
                sleep = 0
            await asyncio.sleep(max(0.0, sleep))

    # -- 接続処理 ----------------------------------------------------------
    async def handle(self, reader, writer):
        try:
            request = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=10)
        except Exception:
            writer.close()
            return

        lines = request.decode("latin-1").split("\r\n")
        try:
            method, path, _ = lines[0].split(" ", 2)
        except ValueError:
            writer.close()
            return
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        path, _, query = path.partition("?")

        if path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
            await self.serve_ws(reader, writer, headers)
        else:
            await self.serve_http(writer, method, path, query)

    # -- サーバー情報 ------------------------------------------------------
    def info_json(self):
        """画面側が「ゲームPCのIP」を知るための情報。localhost を出さないため。"""
        ips = local_ips()
        return json.dumps({
            "ip": ips[0] if ips else "localhost",
            "ips": ips,
            "port": self.port,
        }, ensure_ascii=False).encode("utf-8")

    # -- コースマップ ------------------------------------------------------
    async def current_track_json(self, query=""):
        """コース形状 JSON（バイト列）。無ければ None。

        track= / layout= を指定すればそのコースを返す（過去ログの再生用）。
        指定が無ければ現在走行中のコースを返す。
        """
        q = _qs(query)
        d = self.latest or {}
        track = str(q.get("track") or d.get("track") or "").strip()
        layout = str(q.get("layout") if "track" in q
                     else (d.get("trackConfig") or "")).strip()
        if not track:
            return None
        loop = asyncio.get_running_loop()
        # ファイル読み込みと解析はブロックするので別スレッドで
        return await loop.run_in_executor(None, self.tracks.get, track, layout)

    # -- ロガー ------------------------------------------------------------
    async def serve_log(self, writer, method, path, query):
        """記録・再生まわりのエンドポイント。処理済みなら True。"""
        loop = asyncio.get_running_loop()

        if path in ("/log/start", "/log/stop", "/log/toggle", "/log/status"):
            if path == "/log/start":
                self.logger.set_recording(True)
            elif path == "/log/stop":
                self.logger.set_recording(False)
            elif path == "/log/toggle":
                self.logger.toggle()
            await self._send_json(writer, self.logger.status())
            return True

        if path == "/logs.json":
            sessions = await loop.run_in_executor(
                None, logger_claude.list_sessions, self.log_dir)
            await self._send_json(writer, {
                "dir": self.log_dir,
                "rec": self.logger.status(),
                "sessions": sessions,
            })
            return True

        if path == "/log/delete":
            if method not in ("POST", "DELETE"):
                await self._send_404(writer)
                return True
            sid = _qs(query).get("s", "")
            ok = await loop.run_in_executor(
                None, logger_claude.delete_session, self.log_dir, sid)
            await self._send_json(writer, {"ok": bool(ok)})
            return True

        if path.startswith("/logs-data/") or path.startswith("/logs-csv/"):
            csv_mode = path.startswith("/logs-csv/")
            parts = path.split("/", 3)          # ['', 'logs-data', sess, file]
            if len(parts) < 4:
                await self._send_404(writer)
                return True
            sess = os.path.basename(parts[2])
            name = os.path.basename(parts[3])
            full = os.path.join(self.log_dir, sess, name)
            if not name.endswith(".jsonl.gz") or not os.path.isfile(full):
                await self._send_404(writer)
                return True
            if csv_mode:
                body = await loop.run_in_executor(
                    None, logger_claude.to_csv_bytes, full)
                fname = (sess + "_" + name[:-9] + ".csv")
                await self._send_bytes(
                    writer, body, "text/csv; charset=utf-8",
                    extra=[b"Content-Disposition: attachment; filename=\"" +
                           fname.encode("ascii", "ignore") + b"\""])
            else:
                with open(full, "rb") as fh:
                    body = fh.read()
                # gzip のまま渡す。ブラウザ側が透過的に展開してくれる。
                await self._send_bytes(
                    writer, body, "application/x-ndjson; charset=utf-8",
                    extra=[b"Content-Encoding: gzip"])
            return True

        return False

    # -- 静的ファイル ------------------------------------------------------
    async def serve_http(self, writer, method, path, query=""):
        if path.startswith("/log"):
            if await self.serve_log(writer, method, path, query):
                return
        if path == "/favicon.ico":
            # 用意していないので、ブラウザのコンソールに 404 を出さずに終わらせる
            writer.write(b"HTTP/1.1 204 No Content\r\n"
                         b"Cache-Control: max-age=86400\r\n"
                         b"Connection: close\r\n\r\n")
            try:
                await writer.drain()
            except Exception:
                pass
            writer.close()
            return
        if path == "/info.json":
            await self._send_bytes(writer, self.info_json(),
                                   "application/json; charset=utf-8")
            return
        elif path == "/track.json":
            blob = await self.current_track_json(query)
            if blob is None:
                await self._send_404(writer)
                return
            await self._send_bytes(writer, blob, "application/json; charset=utf-8")
            return
        elif path.startswith("/tracks-claude/"):
            full = os.path.join(TRACKS_DIR, os.path.basename(path))
        else:
            name = self.ROUTES.get(path)
            if name is None:
                name = os.path.basename(path)
            full = os.path.join(WEB_DIR, name)
        if method not in ("GET", "HEAD") or not os.path.isfile(full):
            body = b"404 Not Found"
            writer.write(b"HTTP/1.1 404 Not Found\r\n"
                         b"Content-Type: text/plain; charset=utf-8\r\n"
                         b"Content-Length: " + str(len(body)).encode() +
                         b"\r\nConnection: close\r\n\r\n" + body)
        else:
            with open(full, "rb") as fh:
                body = fh.read()
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: " + ctype.encode() + b"\r\n"
                b"Cache-Control: no-store\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + (b"" if method == "HEAD" else body))
        try:
            await writer.drain()
        except Exception:
            pass
        writer.close()

    async def _send_json(self, writer, obj):
        body = json.dumps(obj, ensure_ascii=False,
                          default=_json_default).encode("utf-8")
        await self._send_bytes(writer, body, "application/json; charset=utf-8")

    async def _send_bytes(self, writer, body, ctype, extra=None):
        head = (b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: " + ctype.encode() + b"\r\n"
                b"Cache-Control: no-store\r\n")
        for line in (extra or []):
            head += line + b"\r\n"
        writer.write(
            head +
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body)
        try:
            await writer.drain()
        except Exception:
            pass
        writer.close()

    async def _send_404(self, writer):
        body = b"404 Not Found"
        writer.write(b"HTTP/1.1 404 Not Found\r\n"
                     b"Content-Type: text/plain; charset=utf-8\r\n"
                     b"Content-Length: " + str(len(body)).encode() +
                     b"\r\nConnection: close\r\n\r\n" + body)
        try:
            await writer.drain()
        except Exception:
            pass
        writer.close()

    # -- WebSocket ---------------------------------------------------------
    async def serve_ws(self, reader, writer, headers):
        key = headers.get("sec-websocket-key")
        if not key:
            writer.close()
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n")
        await writer.drain()

        self.clients += 1
        peer = writer.get_extra_info("peername")
        print(f"[bridge] 接続: {peer}  (合計 {self.clients})")

        stop = asyncio.Event()

        async def rx():
            try:
                while not stop.is_set():
                    op, data = await ws_read_frame(reader)
                    if op == 0x8:                       # close
                        break
                    if op == 0x9:                       # ping → pong
                        writer.write(ws_frame(data, 0xA))
                        await writer.drain()
            except Exception:
                pass
            finally:
                stop.set()

        rx_task = asyncio.create_task(rx())
        try:
            while not stop.is_set():
                await self.tick.wait()
                payload = json_dumps_claude(self.latest).encode("utf-8")
                writer.write(ws_frame(payload))
                await writer.drain()
        except Exception:
            pass
        finally:
            stop.set()
            rx_task.cancel()
            self.clients -= 1
            print(f"[bridge] 切断: {peer}  (合計 {self.clients})")
            try:
                writer.close()
            except Exception:
                pass

    # -- 起動 --------------------------------------------------------------
    async def run(self):
        server = await asyncio.start_server(self.handle, self.host, self.port)
        asyncio.create_task(self.poll_loop())
        self._print_banner()
        if self.open_browser:
            self._launch_browser()
        async with server:
            await server.serve_forever()

    def _launch_browser(self):
        """このPCの既定ブラウザでポータルページを開く（ローカルのみ）。"""
        import threading
        import webbrowser

        # localhost ではなく LAN の IP で開く（そのままスマホに伝えられる）
        ips = local_ips()
        host = ips[0] if ips and not ips[0].startswith("127.") else "localhost"
        url = "http://%s:%d/" % (host, self.port)

        def go():
            try:
                webbrowser.open(url)
            except Exception:
                print(f"[bridge] ブラウザを開けませんでした。手動で {url} を開いてください")

        threading.Timer(0.8, go).start()

    def _print_banner(self):
        ips = local_ips()
        print()
        print("=" * 64)
        print("  Assetto Corsa Telemetry Bridge (claude)")
        print("=" * 64)
        if self.source.demo:
            print("  モード     : DEMO（疑似データ）")
        else:
            print("  モード     : LIVE（AC 共有メモリ）")
        print(f"  配信レート : {self.rate} Hz")
        st = self.logger.status()
        print("  記録       : %s  → %s"
              % ("ON（走行を自動記録）" if st["on"] else "OFF（画面の REC で開始）",
                 self.log_dir))
        print()
        print("  ブラウザで開く URL:")
        for ip in ips:
            print(f"    スマホ/タブレット  http://{ip}:{self.port}/mobile")
            print(f"    PC(エンジニア)     http://{ip}:{self.port}/engineer")
            print()
        print("  終了: Ctrl+C")
        print("=" * 64)
        print()


def _qs(query):
    """'s=abc&x=1' → {'s': 'abc', 'x': '1'}"""
    import urllib.parse
    out = {}
    for k, v in urllib.parse.parse_qsl(query or "", keep_blank_values=True):
        out[k] = v
    return out


def _json_default(o):
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    return str(o)


def _finite(o):
    """NaN / Infinity を None に置き換える（JSON として不正になるのを防ぐ）。"""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    return o


def json_dumps_claude(obj):
    """WebSocket 配信とログ書き出しで使う JSON 化。

    AC の共有メモリには稀に NaN / Inf が入る。Python の json は既定でそれを
    NaN / Infinity と書いてしまい、ブラウザの JSON.parse が失敗して画面が
    止まるので、失敗したときだけ全体を掃除してから書き直す。
    """
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                          default=_json_default, allow_nan=False)
    except ValueError:
        return json.dumps(_finite(obj), ensure_ascii=False,
                          separators=(",", ":"), default=_json_default,
                          allow_nan=False)


def local_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 80))     # 送信はしない（経路確認のみ）
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        ips.append("localhost")
    return ips


def main():
    ap = argparse.ArgumentParser(
        description="Assetto Corsa テレメトリー LAN ブリッジ")
    ap.add_argument("--port", type=int, default=8720, help="待ち受けポート")
    ap.add_argument("--host", default="0.0.0.0", help="待ち受けアドレス")
    ap.add_argument("--rate", type=int, default=60, help="配信レート(Hz)")
    ap.add_argument("--demo", action="store_true",
                    help="ゲーム無しで疑似データを配信")
    ap.add_argument("--open", action="store_true", dest="open_browser",
                    help="起動後このPCの既定ブラウザでポータルページを開く")
    ap.add_argument("--no-log", action="store_true",
                    help="テレメトリーの記録を最初から止めておく"
                         "（画面の REC ボタンで後から開始できる）")
    ap.add_argument("--log-dir", dest="log_dir", default=LOGS_DIR,
                    help="ログの保存先フォルダ（既定: logs-claude）")
    ap.add_argument("--ac-path", dest="ac_path",
                    help="Assetto Corsa のインストールフォルダ"
                         "（コース形状の自動読み出しに使う。通常は自動検出）")
    args = ap.parse_args()

    src = TelemetrySourceClaude(demo=args.demo)
    srv = BridgeServerClaude(src, host=args.host, port=args.port,
                             rate=args.rate, open_browser=args.open_browser,
                             ac_path=args.ac_path, log_dir=args.log_dir,
                             log_enabled=not args.no_log)
    try:
        asyncio.run(srv.run())
    except KeyboardInterrupt:
        print("\n[bridge] 停止しました")
    finally:
        srv.logger.close()


if __name__ == "__main__":
    main()
