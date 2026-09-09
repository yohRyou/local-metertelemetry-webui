"""
ac_sharedmem_claude.py
Assetto Corsa (無印) 共有メモリ アクセスモジュール

Windows 上の Assetto Corsa が公開している 3 つの共有メモリ
  Local\\acpmf_physics   : 物理データ (~333Hz 更新)
  Local\\acpmf_graphics  : セッション/ラップ情報 (~60Hz 更新)
  Local\\acpmf_static    : 車両/トラックの静的情報 (セッション開始時)
を読み出す。

--------------------------------------------------------------------------
なぜ mmap ではないのか
--------------------------------------------------------------------------
Python の ``mmap.mmap(-1, size, tagname=...)`` は Windows では
``CreateFileMapping`` を呼ぶため、**対象が存在しない場合は新しく作ってしまう**。
その結果

  * AC が起動していなくても open() が成功し、中身がゼロのまま「接続済み」に見える
  * 先にこちらが作った領域に AC が後から接続してしまい、実データが流れない

という問題が起きる。このモジュールは ``OpenFileMappingW`` を使う。
これは **既存のものを開くだけ**で、無ければ確実に失敗する。

--------------------------------------------------------------------------
使い方
--------------------------------------------------------------------------
    ac = ACSharedMemory()

    if ac.ensure_open():                       # AC 未起動なら False（例外は出ない）
        phys = ac.view("physics", SPageFilePhysics)
        gra  = ac.view("graphics", SPageFileGraphic)
        sta  = ac.view("static", SPageFileStatic)
        print(phys.rpms, gra.iCurrentTime, sta.carModel)

    ac.reopen()      # AC を落として再起動したとき
    ac.close()

* ``ensure_open()`` は開けるまで何度呼んでもよい（ブリッジ起動 → AC 起動の順でも安全）
* ``view()`` はゼロコピーの生ビューを返す。値を保持したいときは
  ``copy.deepcopy`` ではなく ``type(v).from_buffer_copy(v)`` を使うこと
"""

import ctypes
import sys

# ---------------------------------------------------------------------------
# 列挙値
# ---------------------------------------------------------------------------

AC_STATUS = {0: "OFF", 1: "REPLAY", 2: "LIVE", 3: "PAUSE"}

AC_SESSION_TYPE = {
    -1: "UNKNOWN",
    0: "PRACTICE",
    1: "QUALIFY",
    2: "RACE",
    3: "HOTLAP",
    4: "TIME_ATTACK",
    5: "DRIFT",
    6: "DRAG",
}

AC_FLAG_TYPE = {
    0: "NONE",
    1: "BLUE",
    2: "YELLOW",
    3: "BLACK",
    4: "WHITE",
    5: "CHECKERED",
    6: "PENALTY",
}


# ---------------------------------------------------------------------------
# 構造体定義（Assetto Corsa SDK の SPageFile* に対応）
# ---------------------------------------------------------------------------

class SPageFilePhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", ctypes.c_int32),
        ("gas", ctypes.c_float),
        ("brake", ctypes.c_float),
        ("fuel", ctypes.c_float),
        ("gear", ctypes.c_int32),
        ("rpms", ctypes.c_int32),
        ("steerAngle", ctypes.c_float),
        ("speedKmh", ctypes.c_float),
        ("velocity", ctypes.c_float * 3),
        ("accG", ctypes.c_float * 3),
        ("wheelSlip", ctypes.c_float * 4),
        ("wheelLoad", ctypes.c_float * 4),
        ("wheelsPressure", ctypes.c_float * 4),
        ("wheelAngularSpeed", ctypes.c_float * 4),
        ("tyreWear", ctypes.c_float * 4),
        ("tyreDirtyLevel", ctypes.c_float * 4),
        ("tyreCoreTemperature", ctypes.c_float * 4),
        ("camberRAD", ctypes.c_float * 4),
        ("suspensionTravel", ctypes.c_float * 4),
        ("drs", ctypes.c_float),
        ("tc", ctypes.c_float),
        ("heading", ctypes.c_float),
        ("pitch", ctypes.c_float),
        ("roll", ctypes.c_float),
        ("cgHeight", ctypes.c_float),
        ("carDamage", ctypes.c_float * 5),
        ("numberOfTyresOut", ctypes.c_int32),
        ("pitLimiterOn", ctypes.c_int32),
        ("abs", ctypes.c_float),
        ("kersCharge", ctypes.c_float),
        ("kersInput", ctypes.c_float),
        ("autoShifterOn", ctypes.c_int32),
        ("rideHeight", ctypes.c_float * 2),
        ("turboBoost", ctypes.c_float),
        ("ballast", ctypes.c_float),
        ("airDensity", ctypes.c_float),
        ("airTemp", ctypes.c_float),
        ("roadTemp", ctypes.c_float),
        ("localAngularVel", ctypes.c_float * 3),
        ("finalFF", ctypes.c_float),
        ("performanceMeter", ctypes.c_float),
        ("engineBrake", ctypes.c_int32),
        ("ersRecoveryLevel", ctypes.c_int32),
        ("ersPowerLevel", ctypes.c_int32),
        ("ersHeatCharging", ctypes.c_int32),
        ("ersIsCharging", ctypes.c_int32),
        ("kersCurrentKJ", ctypes.c_float),
        ("drsAvailable", ctypes.c_int32),
        ("drsEnabled", ctypes.c_int32),
        ("brakeTemp", ctypes.c_float * 4),
        ("clutch", ctypes.c_float),
        ("tyreTempI", ctypes.c_float * 4),
        ("tyreTempM", ctypes.c_float * 4),
        ("tyreTempO", ctypes.c_float * 4),
        ("isAIControlled", ctypes.c_int32),
        ("tyreContactPoint", (ctypes.c_float * 3) * 4),
        ("tyreContactNormal", (ctypes.c_float * 3) * 4),
        ("tyreContactHeading", (ctypes.c_float * 3) * 4),
        ("brakeBias", ctypes.c_float),
        ("localVelocity", ctypes.c_float * 3),
    ]


class SPageFileGraphic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", ctypes.c_int32),
        ("status", ctypes.c_int32),
        ("session", ctypes.c_int32),
        ("currentTime", ctypes.c_wchar * 15),
        ("lastTime", ctypes.c_wchar * 15),
        ("bestTime", ctypes.c_wchar * 15),
        ("split", ctypes.c_wchar * 15),
        ("completedLaps", ctypes.c_int32),
        ("position", ctypes.c_int32),
        ("iCurrentTime", ctypes.c_int32),
        ("iLastTime", ctypes.c_int32),
        ("iBestTime", ctypes.c_int32),
        ("sessionTimeLeft", ctypes.c_float),
        ("distanceTraveled", ctypes.c_float),
        ("isInPit", ctypes.c_int32),
        ("currentSectorIndex", ctypes.c_int32),
        ("lastSectorTime", ctypes.c_int32),
        ("numberOfLaps", ctypes.c_int32),
        ("tyreCompound", ctypes.c_wchar * 33),
        ("replayTimeMultiplier", ctypes.c_float),
        ("normalizedCarPosition", ctypes.c_float),
        ("carCoordinates", ctypes.c_float * 3),
        ("penaltyTime", ctypes.c_float),
        ("flag", ctypes.c_int32),
        ("idealLineOn", ctypes.c_int32),
        ("isInPitLane", ctypes.c_int32),
        ("surfaceGrip", ctypes.c_float),
        ("mandatoryPitDone", ctypes.c_int32),
        ("windSpeed", ctypes.c_float),
        ("windDirection", ctypes.c_float),
    ]


class SPageFileStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("smVersion", ctypes.c_wchar * 15),
        ("acVersion", ctypes.c_wchar * 15),
        ("numberOfSessions", ctypes.c_int32),
        ("numCars", ctypes.c_int32),
        ("carModel", ctypes.c_wchar * 33),
        ("track", ctypes.c_wchar * 33),
        ("playerName", ctypes.c_wchar * 33),
        ("playerSurname", ctypes.c_wchar * 33),
        ("playerNick", ctypes.c_wchar * 33),
        ("sectorCount", ctypes.c_int32),
        ("maxTorque", ctypes.c_float),
        ("maxPower", ctypes.c_float),
        ("maxRpm", ctypes.c_int32),
        ("maxFuel", ctypes.c_float),
        ("suspensionMaxTravel", ctypes.c_float * 4),
        ("tyreRadius", ctypes.c_float * 4),
        ("maxTurboBoost", ctypes.c_float),
        ("deprecated_1", ctypes.c_float),
        ("deprecated_2", ctypes.c_float),
        ("penaltiesEnabled", ctypes.c_int32),
        ("aidFuelRate", ctypes.c_float),
        ("aidTireRate", ctypes.c_float),
        ("aidMechanicalDamage", ctypes.c_float),
        ("aidAllowTyreBlankets", ctypes.c_int32),
        ("aidStability", ctypes.c_float),
        ("aidAutoClutch", ctypes.c_int32),
        ("aidAutoBlip", ctypes.c_int32),
        ("hasDRS", ctypes.c_int32),
        ("hasERS", ctypes.c_int32),
        ("hasKERS", ctypes.c_int32),
        ("kersMaxJ", ctypes.c_float),
        ("engineBrakeSettingsCount", ctypes.c_int32),
        ("ersPowerControllerCount", ctypes.c_int32),
        ("trackSPlineLength", ctypes.c_float),
        ("trackConfiguration", ctypes.c_wchar * 33),
        ("ersMaxJ", ctypes.c_float),
        ("isTimedRace", ctypes.c_int32),
        ("hasExtraLap", ctypes.c_int32),
        ("carSkin", ctypes.c_wchar * 33),
        ("reversedGridPositions", ctypes.c_int32),
        ("PitWindowStart", ctypes.c_int32),
        ("PitWindowEnd", ctypes.c_int32),
    ]


# ---------------------------------------------------------------------------
# Windows 共有メモリのマッピング（低レベル）
# ---------------------------------------------------------------------------

class MappingNotFound(Exception):
    """対象の共有メモリが存在しない（＝ AC が起動していない）。"""


class MappingError(Exception):
    """存在はするが開けなかった。"""


FILE_MAP_READ = 0x0004
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3


class _Win32Mapping:
    """OpenFileMappingW + MapViewOfFile の薄いラッパ。

    ``mmap`` と違い、存在しない共有メモリを新規作成することは無い。
    """

    def __init__(self, name):
        import ctypes.wintypes as wt

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        k32.OpenFileMappingW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
        k32.OpenFileMappingW.restype = wt.HANDLE
        k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD,
                                      wt.DWORD, ctypes.c_size_t]
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        k32.UnmapViewOfFile.restype = wt.BOOL
        k32.CloseHandle.argtypes = [wt.HANDLE]
        k32.CloseHandle.restype = wt.BOOL
        k32.VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_size_t]
        k32.VirtualQuery.restype = ctypes.c_size_t

        self._k32 = k32
        self.name = name
        self._handle = None
        self.address = None
        self.region_size = 0

        handle = k32.OpenFileMappingW(FILE_MAP_READ, False, name)
        if not handle:
            err = ctypes.get_last_error()
            if err in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
                raise MappingNotFound(f"{name} は存在しません (WinError {err})")
            raise MappingError(f"{name} を開けません (WinError {err})")

        addr = k32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, 0)
        if not addr:
            err = ctypes.get_last_error()
            k32.CloseHandle(handle)
            raise MappingError(f"{name} をマップできません (WinError {err})")

        self._handle = handle
        self.address = addr
        self.region_size = self._query_size(addr)

    def _query_size(self, addr):
        """マップされた領域のサイズを調べる（構造体が収まるかの確認用）。"""
        import ctypes.wintypes as wt

        class MEMORY_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD),
                ("__alignment1", wt.DWORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD),
                ("Protect", wt.DWORD),
                ("Type", wt.DWORD),
                ("__alignment2", wt.DWORD),
            ]

        mbi = MEMORY_BASIC_INFORMATION()
        try:
            n = self._k32.VirtualQuery(ctypes.c_void_p(addr),
                                       ctypes.byref(mbi), ctypes.sizeof(mbi))
            return int(mbi.RegionSize) if n else 0
        except Exception:
            return 0

    def close(self):
        if self.address:
            try:
                self._k32.UnmapViewOfFile(ctypes.c_void_p(self.address))
            except Exception:
                pass
            self.address = None
        if self._handle:
            try:
                self._k32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None


def _open_win32_mapping(name):
    if not sys.platform.startswith("win"):
        raise MappingNotFound(
            "Assetto Corsa の共有メモリは Windows でのみ利用できます "
            "(--demo で疑似データを流せます)")
    return _Win32Mapping(name)


# ---------------------------------------------------------------------------
# 公開クラス
# ---------------------------------------------------------------------------

class ACSharedMemory:
    """AC の 3 本の共有メモリをまとめて扱う。

    * ``ensure_open()``  … 開いていなければ開く。AC 未起動なら False を返すだけで
                           例外は投げない。ブリッジ起動 → AC 起動の順でも安全。
    * ``view(name, T)``  … 既存の ctypes 構造体をそのまま使えるゼロコピービュー。
    * ``reopen()``       … AC を落として再起動したときに呼ぶ。
    """

    NAMES = {
        "physics": "Local\\acpmf_physics",
        "graphics": "Local\\acpmf_graphics",
        "static": "Local\\acpmf_static",
    }

    def __init__(self, mapping_factory=None):
        # mapping_factory はテスト用の差し替え口。通常は指定しない。
        self._factory = mapping_factory or _open_win32_mapping
        self._maps = {}        # name -> mapping オブジェクト
        self._views = {}       # (name, 構造体) -> ビュー
        self.last_error = ""

    # -- 状態 --------------------------------------------------------------
    @property
    def is_open(self):
        return len(self._maps) == len(self.NAMES)

    # -- 開閉 --------------------------------------------------------------
    def ensure_open(self):
        """3本すべて開けていれば True。まだなら開こうとして、無理なら False。"""
        if self.is_open:
            return True
        for key, name in self.NAMES.items():
            if key in self._maps:
                continue
            try:
                self._maps[key] = self._factory(name)
            except MappingNotFound as exc:
                # AC がまだ起動していないだけ。開けた分は一旦戻して待機する。
                self.last_error = str(exc)
                self.close()
                return False
            except MappingError as exc:
                self.last_error = str(exc)
                self.close()
                return False
            except Exception as exc:              # 想定外
                self.last_error = f"{name}: {exc}"
                self.close()
                return False
        self.last_error = ""
        return True

    def reopen(self):
        """一度すべて閉じてから開き直す。AC の再起動後に呼ぶ。"""
        self.close()
        return self.ensure_open()

    def close(self):
        self._views.clear()
        for m in self._maps.values():
            try:
                m.close()
            except Exception:
                pass
        self._maps.clear()

    def __enter__(self):
        self.ensure_open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- 読み出し ----------------------------------------------------------
    def view(self, name, ctype):
        """共有メモリを ctypes 構造体として見る（コピーしない）。

        戻り値は共有メモリを直接指すビューなので、次のフレームでは中身が
        変わっている。値を残したいときは ``ctype.from_buffer_copy(v)``。
        開けていない場合は None。
        """
        if name not in self.NAMES:
            raise KeyError(f"不明な共有メモリ名: {name}")
        mapping = self._maps.get(name)
        if mapping is None or not mapping.address:
            return None

        key = (name, ctype)
        cached = self._views.get(key)
        if cached is not None:
            return cached

        size = ctypes.sizeof(ctype)
        if mapping.region_size and mapping.region_size < size:
            self.last_error = (
                f"{name}: 共有メモリが構造体より小さい "
                f"({mapping.region_size} < {size} バイト)。"
                "AC のバージョンと構造体定義が食い違っている可能性があります")
            return None

        v = ctypes.cast(ctypes.c_void_p(mapping.address),
                        ctypes.POINTER(ctype)).contents
        self._views[key] = v
        return v

    def snapshot(self, name, ctype):
        """``view()`` の内容をコピーして返す（後で参照しても変化しない）。"""
        v = self.view(name, ctype)
        return None if v is None else ctype.from_buffer_copy(v)

    # -- 便利メソッド ------------------------------------------------------
    def read_all(self):
        """(physics, graphics, static) のビューを返す。開けていなければ None。"""
        if not self.ensure_open():
            return None
        p = self.view("physics", SPageFilePhysics)
        g = self.view("graphics", SPageFileGraphic)
        s = self.view("static", SPageFileStatic)
        if p is None or g is None or s is None:
            return None
        return p, g, s


# 旧名との互換（既存コードからの参照用）
ACSharedMemoryClaude = ACSharedMemory


# ---------------------------------------------------------------------------
# 単体確認
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    wchar = ctypes.sizeof(ctypes.c_wchar)
    print(f"構造体サイズ（この環境: wchar_t = {wchar} バイト）")
    print("  SPageFilePhysics :", ctypes.sizeof(SPageFilePhysics), "bytes")
    print("  SPageFileGraphic :", ctypes.sizeof(SPageFileGraphic), "bytes")
    print("  SPageFileStatic  :", ctypes.sizeof(SPageFileStatic), "bytes")
    print("Windows での想定値 : 580 / 296 / 684 bytes")
    if wchar != 2:
        print("  ※ Windows の wchar_t は 2 バイトなので、文字列を含む "
              "graphics / static のサイズはこの環境とは一致しません")

    ac = ACSharedMemory()
    if ac.ensure_open():
        p, g, s = ac.read_all()
        print(f"\n接続: {s.carModel} @ {s.track}")
        print(f"  status={AC_STATUS.get(g.status)} rpm={p.rpms} "
              f"speed={p.speedKmh:.1f} gear={p.gear}")
        ac.close()
    else:
        print(f"\n未接続: {ac.last_error}")
