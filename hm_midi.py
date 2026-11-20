"""HEXA MIDI — MIDI 送信/受信 (Aegis-Synth M4 の送信サービスを移植)

31,250bps = 3,125 byte/s。これを超えると Note が遅れて演奏が破綻するので、
出口を送信スレッド 1 か所に集めて、優先度順に予算内で送り出す。

  P0 即時キュー : NoteOn/Off/Program/順序が大事な CC/SysEx。落とさない (FIFO)。予算が尽きても送る
  P2 ベンド合流 : ch ごとに最新値 1 つ。足りなければ古い値は捨てる
  P3 CC 合流    : (ch,cc) ごとに最新値 1 つ (音量・リバーブなどの連続量)
合流 (最新値だけ) は連続量には正しい: 途中の値を捨てても最後の位置は合う。
"""
import collections
import os
import threading
import time

BYTES_PER_SEC = 3125.0
DUTY_CAP = 0.70              # 合流分 (ベンド/CC) は帯域の 70% まで
BURST = 48.0
TICK_SEC = 0.002             # 送信スレッドの周期 (グライドの計算もこの周期)
# 順序に意味がある CC は P0 へ (ch mode / RPN / バンク / サステイン)
ORDERED_CC = {0, 6, 32, 38, 64, 98, 99, 100, 101} | set(range(120, 128))


def find_port():
    for p in (os.environ.get("HEXAMIDI_PORT"), "/dev/serial0", "/dev/ttyS0", "/dev/ttyAMA0"):
        if p and os.path.exists(p):
            return p
    return None


class MidiPort:
    """Unit MIDI への送信 (優先度つき) と MIDI IN の受信。つながっていなくてもアプリは動く"""

    def __init__(self, port=None, writer=None):
        self.lock = threading.Lock()
        self.ser, self.err = None, ""
        self.port = port or ("sim" if writer else find_port())
        self._writer = writer                       # シミュレーション用 (bytes を受け取る関数)
        self.p0 = collections.deque()
        self.bend = {}                              # ch → 14bit 値 (未送信の最新)
        self.ccs = {}                               # (ch, cc) → 値 (未送信の最新)
        self.tokens = 0.0
        self.tx_bytes = 0
        self.duty = 0.0                             # 直近 1 秒の帯域占有率 (%)
        self.p0_peak = 0
        self.tickers = []                           # 送信スレッドで毎周期呼ぶ関数 (グライド)
        self._rx_st, self._rx_buf, self._rx_sysex = None, [], False
        self._stop = False
        if not writer:
            if not self.port:
                self.err = "UART が見つかりません"
            else:
                try:
                    import serial
                    self.ser = serial.Serial(self.port, 31250, timeout=0, write_timeout=0.5)
                except Exception as e:              # 権限 / 31250 が設定できない など
                    self.err = f"{self.port}: {e}"
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    @property
    def ok(self):
        return self.ser is not None or self._writer is not None

    # ---- 送る (どのスレッドから呼んでもよい)
    def raw(self, msg):
        with self.lock:
            self.p0.append(bytes(msg))

    def note_on(self, ch, n, vel=100):
        self.raw([0x90 | ch, n & 0x7F, max(1, min(127, vel))])

    def note_off(self, ch, n):
        self.raw([0x80 | ch, n & 0x7F, 0x40])

    def program(self, ch, pg):
        self.raw([0xC0 | ch, pg & 0x7F])

    def cc(self, ch, cc, v):
        v = max(0, min(127, int(v)))
        if cc in ORDERED_CC:
            self.raw([0xB0 | ch, cc, v])
        else:
            with self.lock:
                self.ccs[(ch, cc)] = v

    def pitch_bend(self, ch, v14):
        with self.lock:
            self.bend[ch] = max(0, min(16383, int(v14)))

    def bend_range(self, ch, semi):
        for c, v in ((101, 0), (100, 0), (6, semi), (38, 0), (101, 127), (100, 127)):
            self.raw([0xB0 | ch, c, v])

    def gm_reset(self):
        self.raw([0xF0, 0x7E, 0x7F, 0x09, 0x01, 0xF7])

    def master_volume(self, v):
        self.raw([0xF0, 0x7F, 0x7F, 0x04, 0x01, 0x00, v & 0x7F, 0xF7])

    def all_off(self, chans=range(16)):
        for ch in chans:
            self.raw([0xB0 | ch, 64, 0])
            self.raw([0xB0 | ch, 123, 0])
            self.raw([0xB0 | ch, 120, 0])

    def drain(self, timeout=0.5):
        """P0 が空になるまで待つ (起動時の初期化を吐き切る用)"""
        end = time.time() + timeout
        while self.p0 and time.time() < end:
            time.sleep(0.005)

    # ---- 送信スレッド (M4 の midiTxService)
    def _write(self, data):
        self.tx_bytes += len(data)
        if self._writer:
            self._writer(data)
        elif self.ser:
            try:
                self.ser.write(data)
            except Exception as e:
                self.err = str(e)

    def _run(self):
        last = time.monotonic()
        t_duty = last
        while not self._stop:
            for fn in list(self.tickers):
                try:
                    fn()
                except Exception as e:          # グライドの計算で落ちても送信は止めない
                    self.err = f"ticker: {e}"
            now = time.monotonic()
            self.tokens = min(BURST, self.tokens + BYTES_PER_SEC * DUTY_CAP * (now - last))
            last = now
            with self.lock:
                self.p0_peak = max(self.p0_peak, len(self.p0))
                chunk = b"".join(self.p0)
                self.p0.clear()
                self.tokens = max(0.0, self.tokens - len(chunk))
                for ch in list(self.bend):              # P2 ベンド
                    if self.tokens < 3:
                        break
                    v = self.bend.pop(ch)
                    chunk += bytes([0xE0 | ch, v & 0x7F, (v >> 7) & 0x7F])
                    self.tokens -= 3
                for key in list(self.ccs):              # P3 CC
                    if self.tokens < 3:
                        break
                    v = self.ccs.pop(key)
                    chunk += bytes([0xB0 | key[0], key[1], v])
                    self.tokens -= 3
            if chunk:
                self._write(chunk)
            if now - t_duty >= 1.0:
                self.duty = self.tx_bytes / BYTES_PER_SEC / (now - t_duty) * 100
                self.tx_bytes, t_duty = 0, now
            time.sleep(TICK_SEC)

    # ---- 受信 (メインスレッドから)
    def receive(self):
        """届いたバイトを MIDI メッセージ (bytes) の列にする。リアルタイム系と SysEx は捨てる"""
        if not self.ser:
            return []
        try:
            raw = self.ser.read(512)
        except Exception:
            return []
        return self.parse(raw)

    def parse(self, raw):
        out = []
        for b in raw:
            if b >= 0xF8:
                continue
            if b == 0xF0:
                self._rx_sysex = True
                continue
            if self._rx_sysex:
                if b & 0x80:
                    self._rx_sysex = False
                if b == 0xF7 or not b & 0x80:
                    continue
            if b & 0x80:
                self._rx_st, self._rx_buf = (b if b < 0xF0 else None), []
                continue
            if self._rx_st is None:
                continue
            self._rx_buf.append(b)
            need = 1 if (self._rx_st & 0xF0) in (0xC0, 0xD0) else 2
            if len(self._rx_buf) == need:
                out.append(bytes([self._rx_st] + self._rx_buf))
                self._rx_buf = []                   # ランニングステータスはそのまま
        return out

    def close(self):
        self.drain(0.3)
        self._stop = True
        time.sleep(TICK_SEC * 3)
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
