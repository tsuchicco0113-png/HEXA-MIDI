"""HEXA MIDI — 鳥を聞く (ﾏｲｸから鳴き声を拾って音名にする)

HEXA-NEST (Cardputer-ADV の野鳥ﾛｶﾞｰ) の考え方を、高さごとに広げたもの:
  1. 0.05秒ごとに FFT (4096点, 48kHz)。隣の高さとならしてﾉｲｽﾞのばらつきを抑える
  2. 高さごとに「いつもの静かさ」を覚える (EMA。音が出ている間は覚えるのを遅く)
  3. 1.5〜10kHz で、静かさより thr_db 以上飛び出た一番強い高さを探す
  4. 近い高さ (±2半音) が 3回 (0.15秒) 以上続いたら「鳴き声」として拾う
     → 単発の雑音は捨てられる (ﾃｽﾄで、雑音は 0.1秒の単発・鳴き声は 0.3〜0.8秒の塊だった)
  5. 鳴き声の高さを 2ｵｸﾀｰﾌﾞ下げて音名に。最近の鳴き声から「その場の調」を推定
起動直後 1秒は覚えるだけ (HEXA-NEST の Fast-Adapt と同じ、空振り防止)
"""
import collections
import os
import subprocess
import threading
import time

import numpy as np

from hm_music import NOTE_NAMES, key_from_hist

RATE = 48000
NFFT = 4096
HOP = 2400                     # 0.05秒
F_LO, F_HI = 1500.0, 10000.0   # 鳥の高さ
SHOW_LO, SHOW_HI = 1000.0, 12000.0
MIN_FRAMES = 3                 # 0.15秒以上続いたら鳴き声
WARMUP = 20                    # 1秒は覚えるだけ
DOWN = 24                      # 2ｵｸﾀｰﾌﾞ下げて音名にする


def mic_devices():
    env = os.environ.get("HEXAMIDI_MIC")
    return [env] if env else ["plughw:CARD=ES8389Audio,DEV=0", "plughw:1,0", "default"]


def hz_to_midi(f):
    return 69 + 12 * np.log2(f / 440.0)


def midi_name(m):
    m = int(round(m))
    return f"{NOTE_NAMES[m % 12]}{m // 12 - 1}"


class Call:
    """拾った鳴き声 1 つ"""

    def __init__(self, t, freq, dur, db):
        self.t, self.freq, self.dur, self.db = t, freq, dur, db
        self.midi = int(round(hz_to_midi(freq))) - DOWN     # 2ｵｸﾀｰﾌﾞ下の音
        self.name = midi_name(self.midi)


class Listener:
    """ﾏｲｸを読んで鳴き声を拾う。source に WAV のﾊﾟｽを渡すとﾌｧｲﾙから (PC 用ﾃｽﾄ)"""

    def __init__(self, source=None, thr_db=20.0, realtime=True):
        self.source, self.realtime = source, realtime
        self.thr_db = thr_db
        self.lock = threading.Lock()
        self.err = ""
        self.device = None
        self.freqs = np.fft.rfftfreq(NFFT, 1 / RATE)
        self.win = np.hanning(NFFT).astype(np.float32)
        self.band = (self.freqs >= F_LO) & (self.freqs <= F_HI)
        self.show = (self.freqs >= SHOW_LO) & (self.freqs <= SHOW_HI)
        self.floor = None                       # 高さごとの静かさ (dB)
        self.frames = 0
        self.level_db = -120.0                  # 全体の音量 (dBFS)
        self.cols = collections.deque(maxlen=400)   # 表示用: (表示帯の dB 差, 検出した高さ or None)
        self.calls = collections.deque(maxlen=200)
        self.new_calls = collections.deque()    # 画面/ｱﾌﾟﾘが取りに来る
        self._run_freqs = []                    # 続いている候補
        self._run_db = []
        self.ignore = (0.0, [])                 # (この時刻まで, [Hz]) 自分が鳴らした音の倍音は拾わない
        self.t = 0.0                            # 聞き始めからの秒
        self._buf = np.zeros(NFFT, np.float32)
        self._stop = False
        self._proc = None
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    # ---- 取り込み
    def _open(self):
        if self.source:
            import wave
            w = wave.open(self.source)
            assert w.getframerate() == RATE and w.getsampwidth() == 2
            self._wave = w
            self.device = os.path.basename(self.source)
            return True
        for dev in mic_devices():
            try:
                p = subprocess.Popen(["arecord", "-q", "-D", dev, "-f", "S16_LE", "-r", str(RATE),
                                      "-c", "1", "-t", "raw"], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
            except FileNotFoundError:
                self.err = "arecord がありません"
                return False
            time.sleep(0.3)
            if p.poll() is None:
                self._proc, self.device = p, dev
                return True
            self.err = (p.stderr.read() or b"").decode(errors="ignore").strip()[:60]
        return False

    def _read(self):
        """HOP ｻﾝﾌﾟﾙ (float32) を返す。終わりなら None"""
        if self.source:
            raw = self._wave.readframes(HOP)
            if len(raw) < HOP * 2:
                return None
            if self.realtime:
                time.sleep(HOP / RATE)
        else:
            raw = b""
            while len(raw) < HOP * 2:
                chunk = self._proc.stdout.read(HOP * 2 - len(raw))
                if not chunk:
                    return None
                raw += chunk
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0

    def _run(self):
        if not self._open():
            return
        while not self._stop:
            x = self._read()
            if x is None:
                break
            self.process(x)
        self.close()

    # ---- 解析 (0.05秒ごと)
    def process(self, x):
        self._buf = np.concatenate([self._buf[len(x):], x])
        self.t += len(x) / RATE
        self.frames += 1
        rms = float(np.sqrt(np.mean(x * x))) + 1e-9
        S = np.abs(np.fft.rfft((self._buf - self._buf.mean()) * self.win)) ** 2
        S = np.convolve(S, np.ones(3) / 3, "same") + 1e-16
        db = 10 * np.log10(S)
        if self.floor is None:
            self.floor = db.copy()
        diff = db - self.floor
        # 静かさを覚える: 静かな所は速く、飛び出ている所はゆっくり (鳴き声を覚えてしまわない)
        warm = self.frames <= WARMUP
        a = np.where(diff > 6, 0.002, 0.03) if not warm else 0.3
        self.floor += a * diff
        # 一番飛び出た高さ
        bd = np.where(self.band, diff, -99)
        j = int(np.argmax(bd))
        hit = None
        if not warm and bd[j] >= self.thr_db:
            hit = float(self.freqs[j])
            until, fs = self.ignore
            if time.monotonic() < until and any(abs(hz_to_midi(hit) - hz_to_midi(f)) < 0.7 for f in fs):
                hit = None
        with self.lock:
            self.level_db = 20 * np.log10(rms)
            self.cols.append((np.clip(diff[self.show], 0, 40).astype(np.float32), hit))
        self._track(hit, float(bd[j]))

    def _track(self, f, db):
        run = self._run_freqs
        if f is not None and (not run or abs(hz_to_midi(f) - hz_to_midi(np.median(run))) <= 2):
            run.append(f)
            self._run_db.append(db)
            return
        if len(run) >= MIN_FRAMES:                 # 続いた → 鳴き声
            c = Call(self.t - len(run) * HOP / RATE, float(np.median(run)),
                     len(run) * HOP / RATE, float(max(self._run_db)))
            with self.lock:
                self.calls.append(c)
                self.new_calls.append(c)
        self._run_freqs = [f] if f is not None else []
        self._run_db = [db] if f is not None else []

    # ---- 画面用
    def take_new(self):
        with self.lock:
            out = list(self.new_calls)
            self.new_calls.clear()
        return out

    def key(self, last_sec=60):
        """最近の鳴き声から「その場の調」→ (主音, 種類, 一致度, 使った鳴き声の数, 音名の重み)"""
        with self.lock:
            cs = [c for c in self.calls if c.t >= self.t - last_sec]
        hist = [0.0] * 12
        for c in cs:
            hist[c.midi % 12] += c.dur
        pc, kind, score = key_from_hist(hist)
        return pc, kind, score, len(cs), hist

    def reset(self):
        with self.lock:
            self.floor = None
            self.frames = 0
            self.calls.clear()
            self.cols.clear()

    def close(self):
        self._stop = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
