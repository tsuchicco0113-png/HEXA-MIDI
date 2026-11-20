#!/usr/bin/env python3
"""HEXA MIDI — CardputerZero + M5Stack Unit MIDI (U187, SAM2695) で、伴奏を流しながら弾くアプリ (v0.1)

・CPZ のキーボードが鍵盤になる (PIANO 配列 / GUITAR 配列)。音は Unit MIDI の SAM2695 から
・SONG: .mid の伴奏を流す。ENTER で「抜くパート」(ﾒﾛﾃﾞｨ) を選ぶと、そのパートが上から降ってくる
・ﾏﾁ (ALT): 抜いたパートの次の音を弾くまで伴奏が待ってくれる (オクターブ違いでもOK)
・Unit MIDI の MIDI IN (Bypass) につないだ本物の鍵盤も、そのまま SAM2695 で鳴って、ﾏﾁの答えにもなる

キー
  鍵盤     PIANO: Z S X D C V G B H N J M = C〜B / Q 2 W 3 E R 5 T 6 Y 7 U I 9 O 0 P = 上のC〜E
           GUITAR: 下の段から Z… / A… / Q… / 1… が 4度ずつ上 (ギターの弦と同じ並び)
  SHIFT    押している間サステイン
  ↑↓ (fn+F/X)   オクターブ    ←→ (fn+Z/C)  音色
  SPACE    伴奏 再生/停止     TAB  次の曲     ENTER  抜くパート (なし→ch1→…)
  ALT      ﾏﾁ ON/OFF          BS   配列 PIANO/GUITAR    CTRL  MIDI IN のスルー ON/OFF
  ESC      終了
usage:
  hexamidi                # 通常起動
  hexamidi --probe        # UART の設定を調べて、テスト音 (ドミソ) を鳴らす
  hexamidi --sim OUTDIR   # PC 用: 偽の画面/キー/MIDI で動かして画面を PNG に保存
"""
import argparse
import bisect
import fcntl
import glob
import math
import os
import select
import signal
import struct
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFont

VERSION = "0.1.0"
LCD_W, LCD_H = 320, 170
TOP_H, BOT_H = 18, 16
BG, FG, SUB = (14, 16, 22), (235, 238, 245), (150, 158, 175)
ACC = (255, 80, 170)                      # HEXA MIDI の強調色 (CLIP=緑 / MATRIX=琥珀 / POWER=青)
ACC_DIM = (70, 22, 50)
EXT_COL = (80, 210, 255)                  # MIDI IN (外の鍵盤) の色
GUIDE_COL = (255, 214, 90)                # 抜いたパート (降ってくる音)
DIR = os.path.dirname(os.path.abspath(__file__))
SONG_DIRS = [os.path.join(DIR, "songs"), os.path.expanduser("~/midi")]
MY_CH = 15                                # 自分の演奏は ch16 (曲とぶつかりにくい)
ROLL_SEC = 2.5                            # 何秒先まで降らせるか
EARLY_OK = 0.30                           # ﾏﾁ: この秒数までは早弾きも正解にする

# ================================================================ 音色
GM_FAMILY = ["ピアノ", "鍵盤打楽器", "オルガン", "ギター", "ベース", "弦", "アンサンブル", "金管",
             "リード", "笛", "シンセリード", "シンセパッド", "シンセFX", "民族楽器", "打楽器", "効果音"]
VOICES = [(0, "ピアノ"), (4, "エレピ"), (11, "ビブラフォン"), (8, "チェレスタ"), (19, "パイプオルガン"),
          (21, "アコーディオン"), (24, "ナイロンギター"), (26, "ジャズギター"), (33, "ベース"),
          (40, "バイオリン"), (48, "ストリングス"), (52, "コーラス"), (56, "トランペット"),
          (65, "アルトサックス"), (71, "クラリネット"), (73, "フルート"), (79, "オカリナ"),
          (80, "矩形波リード"), (81, "ノコギリ波リード"), (88, "ニューエイジ"), (98, "クリスタル"),
          (108, "カリンバ"), (114, "スチールドラム")]


def voice_name(pg):
    for p, n in VOICES:
        if p == pg:
            return n
    return f"{GM_FAMILY[pg // 8]} {pg + 1}"


NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(n):
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


# ================================================================ キー配列 (Linux のキーコード)
K = {"1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
     "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
     "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
     "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50}
K_ESC, K_BS, K_TAB, K_ENTER, K_SPACE = 1, 14, 15, 28, 57
K_LSHIFT, K_RSHIFT, K_LCTRL, K_RCTRL, K_LALT, K_RALT = 42, 54, 29, 97, 56, 100
K_UP, K_LEFT, K_RIGHT, K_DOWN = 103, 105, 106, 108
CODE_CH = {v: k for k, v in K.items()}

LAYOUTS = {
    # 名前: ({キーコード: 基準からの半音}, 鍵盤に出す半音の数)
    "PIANO": ({**{K[c]: i for i, c in enumerate("zsxdcvgbhnjm")},
               **{K[c]: 12 + i for i, c in enumerate("q2w3er5t6y7ui9o0p")}}, 29),
    "GUITAR": ({**{K[c]: i for i, c in enumerate("zxcvbnm")},
                **{K[c]: 5 + i for i, c in enumerate("asdfghjkl")},
                **{K[c]: 10 + i for i, c in enumerate("qwertyuiop")},
                **{K[c]: 15 + i for i, c in enumerate("1234567890")}}, 25),
}
LAYOUT_ORDER = ["PIANO", "GUITAR"]


# ================================================================ キーボード (押した/離した を取る)
EVIOCGRAB = 0x40044590
EV_FMT = "llHHi"
EV_SIZE = struct.calcsize(EV_FMT)


class Keys:
    """キーボードを横取りして [(コード, 1=押した/0=離した)] を返す (オートリピートは捨てる)"""

    def __init__(self, dev):
        self.fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 1)
        except OSError as e:
            os.close(self.fd)
            if e.errno == 16:
                raise SystemExit("キーボードが使用中です (ほかのアプリが起動中)")
            raise

    def poll(self, timeout=0.0):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        try:
            data = os.read(self.fd, EV_SIZE * 64)
        except BlockingIOError:
            return []
        out = []
        for i in range(0, len(data) - EV_SIZE + 1, EV_SIZE):
            _, _, etype, code, value = struct.unpack(EV_FMT, data[i:i + EV_SIZE])
            if etype == 1 and value in (0, 1):
                out.append((code, value))
        return out

    def flush(self, sec=0.4):
        end = time.time() + sec
        while time.time() < end:
            self.poll(0.05)

    def close(self):
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 0)
        finally:
            os.close(self.fd)


def find_keyboard():
    name = None
    for line in open("/proc/bus/input/devices"):
        if line.startswith("N: Name="):
            name = line.strip().split("=", 1)[1].strip('"')
        elif line.startswith("H: Handlers=") and name and "tca8418" in name:
            for h in line.split("=", 1)[1].split():
                if h.startswith("event"):
                    return "/dev/input/" + h
    raise RuntimeError("keyboard (tca8418) not found")


# ================================================================ 画面 (HEXA CLIP と同じ)
class FrameBuffer:
    def __init__(self, dev="/dev/fb0"):
        import numpy as np
        self.np = np
        base = f"/sys/class/graphics/{os.path.basename(dev)}"
        self.bpp = int(open(f"{base}/bits_per_pixel").read())
        try:
            self.stride = int(open(f"{base}/stride").read())
        except OSError:
            self.stride = LCD_W * self.bpp // 8
        self.f = open(dev, "r+b", buffering=0)

    def show(self, img):
        np = self.np
        a = np.asarray(img.convert("RGB"), dtype=np.uint16)
        if self.bpp == 16:
            buf = (((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)).astype("<u2")
        else:
            buf = np.zeros((LCD_H, LCD_W, 4), np.uint8)
            buf[..., 0], buf[..., 1], buf[..., 2], buf[..., 3] = a[..., 2], a[..., 1], a[..., 0], 255
        row = LCD_W * self.bpp // 8
        raw = buf.tobytes()
        if self.stride == row:
            self.f.seek(0)
            self.f.write(raw)
        else:
            for y in range(LCD_H):
                self.f.seek(y * self.stride)
                self.f.write(raw[y * row:(y + 1) * row])

    def clear(self):
        self.show(Image.new("RGB", (LCD_W, LCD_H)))


JP_TEST = "あいおかがきくけこさしすずせただちっつてでとなにのはばぶへまみもやよりるれをんアイエオカガギクケコサシジスセゾタチッツテデトドニノバパビピフブプベボマミムメモャュョラリルレロンヴー伴再停待奏曲止生色抜音外終了未接続配列流答正解次"
_FONT_FILE = {}


def font_covers(font, text=JP_TEST):
    try:
        nd = font.getmask("\U0010FFFD")
        nd_key = (nd.size, bytes(nd))
        for ch in text:
            m = font.getmask(ch)
            if m.getbbox() is None or (m.size, bytes(m)) == nd_key:
                return False
        return True
    except Exception:
        return False


def font_candidates():
    cands = []
    for p in ["/usr/share/APPLaunch/**/*.tt[fc]", "/usr/share/APPLaunch/**/*.otf",
              "/usr/share/fonts/**/*CJK*", "/usr/share/fonts/**/*[Ii][Pp][Aa]*",
              "/usr/share/fonts/**/*[Jj][Pp]*", "/usr/share/fonts/**/*wqy*",
              "/usr/share/fonts/**/*Droid*Fallback*"]:
        cands += glob.glob(p, recursive=True)
    return sorted(set(cands))


def find_font(size, bold=False):
    f = _FONT_FILE.get(bold)
    if f:
        return ImageFont.truetype(f, size)

    def rank(f):
        n = os.path.basename(f).lower()
        if "serif" in n or "mono" in n:
            return 50
        order = (["bold", "85", "heavy", "75", "black", "medium", "65", "regular", "55", "light", "thin"]
                 if bold else ["medium", "65", "regular", "55", "bold", "75", "black", "light", "thin"])
        for i, w in enumerate(order):
            if w in n:
                return i
        return 5
    for f in sorted(font_candidates(), key=rank):
        try:
            font = ImageFont.truetype(f, size)
        except Exception:
            continue
        if font_covers(font):
            _FONT_FILE[bold] = f
            return font
    return ImageFont.load_default()


def hexpts(cx, cy, r, rot=30):
    return [(cx + r * math.cos(math.radians(60 * i + rot)),
             cy + r * math.sin(math.radians(60 * i + rot))) for i in range(6)]


def honeycomb_bg():
    img = Image.new("RGB", (LCD_W, LCD_H), BG)
    d = ImageDraw.Draw(img)
    for row in range(-1, 8):
        for col in range(-1, 12):
            d.polygon(hexpts(col * 30 + (15 if row % 2 else 0), 20 + row * 26, 17), outline=(20, 24, 34))
    return img


# ================================================================ MIDI 出入り (UART 31250bps)
def find_port():
    for p in (os.environ.get("HEXAMIDI_PORT"), "/dev/serial0", "/dev/ttyS0", "/dev/ttyAMA0"):
        if p and os.path.exists(p):
            return p
    return None


class MidiPort:
    """Unit MIDI への送信と、MIDI IN (Bypass 時) の受信。つながっていなくてもアプリは動く"""

    def __init__(self, port=None):
        self.lock = threading.Lock()
        self.ser, self.err = None, ""
        self.port = port or find_port()
        self._st, self._buf, self._sysex = None, [], False
        self.sent = 0
        if not self.port:
            self.err = "UART が見つかりません"
            return
        try:
            import serial
            self.ser = serial.Serial(self.port, 31250, timeout=0, write_timeout=0.5)
        except Exception as e:                      # 権限 / 31250 が設定できない など
            self.err = f"{self.port}: {e}"
            self.ser = None

    @property
    def ok(self):
        return self.ser is not None

    def send(self, data):
        self.sent += 1
        if not self.ser:
            return
        with self.lock:
            try:
                self.ser.write(bytes(data))
            except Exception as e:
                self.err = str(e)

    def receive(self):
        """届いたバイトを MIDI メッセージ (bytes) の列にする。リアルタイム系と SysEx は捨てる"""
        if not self.ser:
            return []
        try:
            raw = self.ser.read(512)
        except Exception:
            return []
        out = []
        for b in raw:
            if b >= 0xF8:
                continue
            if b == 0xF0:
                self._sysex = True
                continue
            if self._sysex:
                if b == 0xF7 or b & 0x80:
                    self._sysex = False
                if b == 0xF7 or not b & 0x80:
                    continue
            if b & 0x80:
                self._st, self._buf = (b if b < 0xF0 else None), []
                continue
            if self._st is None:
                continue
            self._buf.append(b)
            need = 1 if (self._st & 0xF0) in (0xC0, 0xD0) else 2
            if len(self._buf) == need:
                out.append(bytes([self._st] + self._buf))
                self._buf = []                      # ランニングステータスはそのまま
        return out

    def all_off(self, chans=range(16)):
        for ch in chans:
            self.send([0xB0 | ch, 64, 0])
            self.send([0xB0 | ch, 123, 0])
            self.send([0xB0 | ch, 120, 0])

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass


# ================================================================ SMF (.mid) 読み込み
class Song:
    def __init__(self, path):
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.events = []        # [(秒, bytes)]
        self.notes = {}         # ch → [(on秒, off秒, note)]
        self.program = {}       # ch → 最初の program
        self._load(open(path, "rb").read())
        self.times = [t for t, _ in self.events]
        self.length = self.events[-1][0] if self.events else 0.0
        self.channels = sorted(ch for ch in self.notes if ch != 9 and len(self.notes[ch]) >= 4)
        self.melody = self._guess_melody()

    @staticmethod
    def _varlen(data, p):
        n = 0
        while True:
            b = data[p]
            p += 1
            n = (n << 7) | (b & 0x7F)
            if not b & 0x80:
                return n, p

    def _load(self, data):
        if data[:4] != b"MThd":
            raise ValueError("MIDI ファイルではありません")
        hlen = struct.unpack(">I", data[4:8])[0]
        _, ntrk, div = struct.unpack(">HHH", data[8:14])
        pos = 8 + hlen
        raw = []                                     # (tick, 優先, 通し番号, 種類, 中身)
        seq = 0
        for _ in range(ntrk):
            while pos + 8 <= len(data) and data[pos:pos + 4] != b"MTrk":
                pos += 8 + struct.unpack(">I", data[pos + 4:pos + 8])[0]
            if pos + 8 > len(data):
                break
            ln = struct.unpack(">I", data[pos + 4:pos + 8])[0]
            p, end = pos + 8, min(pos + 8 + ln, len(data))
            pos = end
            tick, rs = 0, None
            try:
                while p < end:
                    dt, p = self._varlen(data, p)
                    tick += dt
                    st = data[p]
                    if st == 0xFF:
                        mtype = data[p + 1]
                        mlen, p = self._varlen(data, p + 2)
                        payload = data[p:p + mlen]
                        p += mlen
                        if mtype == 0x51 and mlen == 3:
                            raw.append((tick, 0, seq, "tempo", int.from_bytes(payload, "big")))
                        elif mtype == 0x2F:
                            break
                        seq += 1
                        continue
                    if st in (0xF0, 0xF7):
                        mlen, p = self._varlen(data, p + 1)
                        p += mlen
                        continue
                    if st & 0x80:
                        rs = st
                        p += 1
                    elif rs is None:
                        break
                    n = 1 if (rs & 0xF0) in (0xC0, 0xD0) else 2
                    msg = bytes([rs]) + data[p:p + n]
                    p += n
                    off = (rs & 0xF0) == 0x80 or ((rs & 0xF0) == 0x90 and msg[2] == 0)
                    raw.append((tick, 1 if off else 2, seq, "msg", msg))
                    seq += 1
            except IndexError:
                pass
        raw.sort(key=lambda e: (e[0], e[1], e[2]))
        smpte = div & 0x8000
        tps = ((256 - (div >> 8)) * (div & 0xFF)) if smpte else None
        tempo, t, last = 500000, 0.0, 0
        on = {}
        for tick, _, _, kind, val in raw:
            t += (tick - last) / tps if smpte else (tick - last) * tempo / 1e6 / div
            last = tick
            if kind == "tempo":
                tempo = val
                continue
            msg = val
            self.events.append((t, msg))
            ch, typ = msg[0] & 0x0F, msg[0] & 0xF0
            if typ == 0xC0 and ch not in self.program:
                self.program[ch] = msg[1]
            elif typ == 0x90 and msg[2] > 0:
                on.setdefault((ch, msg[1]), []).append(t)
            elif typ == 0x80 or typ == 0x90:
                starts = on.get((ch, msg[1]))
                if starts:
                    self.notes.setdefault(ch, []).append((starts.pop(0), t, msg[1]))
        for ch in self.notes:
            self.notes[ch].sort()

    def _guess_melody(self):
        best, score = None, -1
        for ch in self.channels:
            ns = self.notes[ch]
            avg = sum(n for _, _, n in ns) / len(ns)
            starts = [a for a, _, _ in ns]
            chordy = sum(1 for i in range(1, len(starts)) if starts[i] - starts[i - 1] < 0.03) / len(ns)
            s = avg - 30 * chordy                    # 高くて和音が少ないパート
            if s > score:
                best, score = ch, s
        return best


def list_songs():
    out = []
    for d in SONG_DIRS:
        out += glob.glob(os.path.join(d, "*.mid")) + glob.glob(os.path.join(d, "*.MID"))
        out += glob.glob(os.path.join(d, "*.midi"))
    return sorted(set(out), key=lambda p: os.path.basename(p).lower())


# ================================================================ 伴奏の再生 (別スレッド)
class Player:
    def __init__(self, midi):
        self.midi = midi
        self.song = None
        self.lock = threading.RLock()
        self.vt = 0.0                       # 曲の中の時間 (ﾏﾁで止まる)
        self.idx = 0
        self.playing = False
        self.waiting = False
        self.mute = None                    # 抜くパートの ch
        self.wait_mode = False
        self.clusters = []                  # [(秒, {note})] 抜くパートの「弾く所」
        self.ci = 0                         # 次の cluster
        self.hit = set()                    # 正解した cluster の番号
        self.missed = 0
        self.ended = False
        self._stop = False
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    def load(self, song):
        with self.lock:
            self.stop()
            self.song = song
            self.mute = None
            self._rebuild()

    def set_mute(self, ch):
        with self.lock:
            if self.mute is not None and self.playing:
                self.midi.send([0xB0 | self.mute, 123, 0])
            self.mute = ch
            self._rebuild()

    def _rebuild(self):
        self.clusters, self.hit, self.missed = [], set(), 0
        if self.song and self.mute is not None:
            for on, _, n in self.song.notes.get(self.mute, []):
                if self.clusters and on - self.clusters[-1][0] < 0.035:
                    self.clusters[-1][1].add(n)
                else:
                    self.clusters.append((on, {n}))
        self.ci = bisect.bisect_left([c[0] for c in self.clusters], self.vt - 1e-6)

    def start(self):
        with self.lock:
            if not self.song:
                return
            if self.ended or self.vt >= self.song.length:
                self.vt, self.idx = 0.0, 0
                self._rebuild()
            self.ended = False
            self.playing = True
            self._t = time.monotonic()

    def stop(self, rewind=True):
        with self.lock:
            was = self.playing
            self.playing = self.waiting = False
            if rewind:
                self.vt, self.idx = 0.0, 0
                self._rebuild()
            if was:
                self.midi.all_off([c for c in range(16) if c != MY_CH])

    def user_note(self, n):
        """弾いた音 (自分/外の鍵盤)。ﾏﾁの答え合わせ。正解なら True"""
        with self.lock:
            if not self.clusters or not self.playing:
                return False
            if self.waiting:
                if self.ci < len(self.clusters) and n % 12 in {x % 12 for x in self.clusters[self.ci][1]}:
                    self.hit.add(self.ci)
                    self.ci += 1
                    self.waiting = False
                    self._t = time.monotonic()
                    return True
                return False
            for j in range(self.ci, len(self.clusters)):    # 少し早く弾いた分も正解
                t, notes = self.clusters[j]
                if t - self.vt > EARLY_OK:
                    break
                if j not in self.hit and n % 12 in {x % 12 for x in notes}:
                    self.hit.add(j)
                    return True
            for j in range(self.ci - 1, -1, -1):              # 少し遅れた分も
                t, notes = self.clusters[j]
                if self.vt - t > 0.25:
                    break
                if j not in self.hit and n % 12 in {x % 12 for x in notes}:
                    self.hit.add(j)
                    return True
            return False

    def _run(self):
        while not self._stop:
            sleep = 0.02
            with self.lock:
                if self.playing and self.song:
                    now = time.monotonic()
                    if not self.waiting:
                        self.vt += now - self._t
                    self._t = now
                    # ﾏﾁ: 次の弾く所に来たら止める
                    while self.ci < len(self.clusters) and self.clusters[self.ci][0] <= self.vt:
                        if self.wait_mode and self.ci not in self.hit:
                            self.vt = self.clusters[self.ci][0]
                            self.waiting = True
                            break
                        self.ci += 1
                    ev = self.song.events
                    while self.idx < len(ev) and ev[self.idx][0] <= self.vt + 0.0005:
                        msg = ev[self.idx][1]
                        if not (self.mute is not None and (msg[0] & 0x0F) == self.mute
                                and (msg[0] & 0xF0) in (0x80, 0x90)):
                            self.midi.send(msg)
                        self.idx += 1
                    if self.idx >= len(ev):
                        self.missed = max(0, len(self.clusters) - len(self.hit))
                        self.playing = self.waiting = False
                        self.ended = True
                        self.midi.all_off([c for c in range(16) if c != MY_CH])
                    elif not self.waiting:
                        sleep = max(0.0005, min(0.01, ev[self.idx][0] - self.vt))
            time.sleep(sleep)

    def close(self):
        self._stop = True


# ================================================================ アプリ
class App:
    def __init__(self, fb, keys, midi):
        self.fb, self.keys, self.midi = fb, keys, midi
        self.player = Player(midi)
        self.font = find_font(14)
        self.font_bs = find_font(13, bold=True)
        self.font_t = find_font(11)
        self.font_s = find_font(10)
        self.font_k = find_font(9)
        self.bg = honeycomb_bg()
        self.layout = "PIANO"
        self.base = 48                          # C3
        self.vi = 0                             # VOICES の番号
        self.program = 0
        self.thru = True
        self.sustain = False
        self.held = {}                          # キーコード → 鳴らしている音
        self.ext = {}                           # 外の鍵盤で鳴っている音 → ch
        self.flash = {}                         # 正解した音 → 光らせる時刻まで
        self.toast = ("", 0.0)
        self.songs = list_songs()
        self.si = -1
        self._kb_cache = None
        self.set_program(VOICES[0][0], quiet=True)
        if self.songs:
            self.load_song(0, quiet=True)

    # ---- 音
    def set_program(self, pg, quiet=False):
        self.program = pg
        self.midi.send([0xC0 | MY_CH, pg])
        self.midi.send([0xB0 | MY_CH, 7, 110])
        if not quiet:
            self.say(f"音色: {voice_name(pg)}")

    def note_on(self, code, semi):
        n = self.base + semi
        if not 0 <= n <= 127:
            return
        if code in self.held:
            self.midi.send([0x80 | MY_CH, self.held[code], 0])
        self.held[code] = n
        self.midi.send([0x90 | MY_CH, n, 100])
        if self.player.user_note(n):
            self.flash[n] = time.time() + 0.35

    def note_off(self, code):
        n = self.held.pop(code, None)
        if n is not None:
            self.midi.send([0x80 | MY_CH, n, 0])

    def release_all(self):
        for code in list(self.held):
            self.note_off(code)

    def handle_in(self):
        for msg in self.midi.receive():
            typ, ch = msg[0] & 0xF0, msg[0] & 0x0F
            if not self.thru:
                continue
            self.midi.send(msg)                 # Bypass では IN は SAM2695 に行かないので、ここで鳴らす
            if typ == 0x90 and msg[2] > 0:
                self.ext[msg[1]] = ch
                if self.player.user_note(msg[1]):
                    self.flash[msg[1]] = time.time() + 0.35
            elif typ in (0x80, 0x90):
                self.ext.pop(msg[1], None)
            elif typ == 0xB0 and msg[1] in (120, 123):
                self.ext.clear()

    # ---- 曲
    def load_song(self, i, quiet=False):
        if not self.songs:
            self.say("曲がありません (songs/ に .mid を置く)")
            return
        self.si = i % len(self.songs)
        path = self.songs[self.si]
        try:
            song = Song(path)
        except Exception as e:
            self.say(f"読めません: {os.path.basename(path)} ({e})")
            return
        self.player.load(song)
        if not quiet:
            self.say(f"曲: {song.name}")

    def cycle_mute(self):
        s = self.player.song
        if not s:
            return
        opts = [None] + s.channels
        if s.melody in opts:                        # ﾒﾛﾃﾞｨらしい ch を最初に
            opts.remove(s.melody)
            opts.insert(1, s.melody)
        cur = opts.index(self.player.mute) if self.player.mute in opts else 0
        ch = opts[(cur + 1) % len(opts)]
        self.player.set_mute(ch)
        if ch is None:
            self.say("抜くパート: なし (全部流す)")
        else:
            pg = s.program.get(ch, 0)
            self.set_program(pg, quiet=True)
            self.vi = next((i for i, (p, _) in enumerate(VOICES) if p == pg), self.vi)
            self.say(f"ch{ch + 1} を抜く → 音色を {voice_name(pg)} に")

    # ---- 表示
    def say(self, msg, sec=2.2):
        self.toast = (msg, time.time() + sec)

    def kb_geom(self):
        """鍵盤の各音の四角 (半音 → (x0,x1,黒鍵か))"""
        span = LAYOUTS[self.layout][1]
        whites = [s for s in range(span) if (self.base + s) % 12 not in (1, 3, 6, 8, 10)]
        ww = LCD_W / len(whites)
        geo, wi = {}, 0
        for s in range(span):
            if (self.base + s) % 12 in (1, 3, 6, 8, 10):
                x = wi * ww
                geo[s] = (x - ww * 0.32, x + ww * 0.32, True)
            else:
                geo[s] = (wi * ww, (wi + 1) * ww, False)
                wi += 1
        return geo

    def display_semi(self, n, span):
        """曲の音を鍵盤の範囲に入るオクターブへ寄せる"""
        s = n - self.base
        while s < 0:
            s += 12
        while s >= span:
            s -= 12
        return s if s >= 0 else None

    def draw_keyboard_base(self):
        key = (self.layout, self.base)
        if self._kb_cache and self._kb_cache[0] == key:
            return self._kb_cache[1]
        y0, y1 = KB_Y, LCD_H - BOT_H
        img = Image.new("RGB", (LCD_W, y1 - y0), (0, 0, 0))
        d = ImageDraw.Draw(img)
        geo = self.kb_geom()
        labels = {}
        for code, semi in LAYOUTS[self.layout][0].items():
            labels.setdefault(semi, CODE_CH[code].upper())
        h = y1 - y0
        for s, (x0, x1, black) in geo.items():
            if not black:
                d.rectangle((x0 + 0.5, 0, x1 - 0.5, h - 1), fill=(220, 224, 232))
                if (self.base + s) % 12 == 0:
                    d.text((x0 + 2, 1), note_name(self.base + s), fill=(150, 150, 165), font=self.font_k)
                lb = labels.get(s)
                if lb:
                    d.text(((x0 + x1) / 2 - self.font_k.getlength(lb) / 2, h - 12), lb,
                           fill=(60, 64, 80), font=self.font_k)
        for s, (x0, x1, black) in geo.items():
            if black:
                d.rectangle((x0, 0, x1, h * 0.58), fill=(28, 30, 38))
                lb = labels.get(s)
                if lb:
                    d.text(((x0 + x1) / 2 - self.font_k.getlength(lb) / 2, h * 0.58 - 12), lb,
                           fill=(170, 175, 190), font=self.font_k)
        self._kb_cache = (key, img, geo)
        return img

    def draw(self):
        img = self.bg.copy()
        d = ImageDraw.Draw(img)
        span = LAYOUTS[self.layout][1]
        kb = self.draw_keyboard_base()
        geo = self._kb_cache[2]
        p = self.player
        song = p.song
        now = time.time()

        # ---- 降ってくる音
        roll_top, roll_bot = TOP_H + 1, KB_Y - 1
        rh = roll_bot - roll_top
        with p.lock:
            vt, mute, playing, waiting = p.vt, p.mute, p.playing, p.waiting
            hit, ci, clusters = set(p.hit), p.ci, p.clusters
        d.line((0, roll_bot, LCD_W, roll_bot), fill=ACC_DIM)
        ctimes = [c[0] for c in clusters]
        if song and (playing or vt > 0):
            chs = [mute] if mute is not None else song.channels
            for ch in chs:
                notes = song.notes.get(ch, [])
                starts = [a for a, _, _ in notes]
                j0 = bisect.bisect_left(starts, vt - 8)
                j1 = bisect.bisect_right(starts, vt + ROLL_SEC)
                for on, off, n in notes[j0:j1]:
                    if off < vt:
                        continue
                    s = self.display_semi(n, span)
                    if s is None or s not in geo:
                        continue
                    x0, x1, _ = geo[s]
                    ya = roll_bot - (on - vt) / ROLL_SEC * rh
                    yb = roll_bot - (off - vt) / ROLL_SEC * rh
                    ya, yb = min(ya, roll_bot), max(yb, roll_top)
                    if ya <= roll_top:
                        continue
                    if mute is not None:
                        k = bisect.bisect_left(ctimes, on - 0.04)
                        col = (120, 255, 160) if k in hit else GUIDE_COL
                        d.rounded_rectangle((x0 + 1, yb + 1, x1 - 1, max(yb + 2, ya - 1)), 2, fill=col)
                    else:
                        d.rectangle((x0 + 3, yb, x1 - 3, ya), fill=(70, 75, 95))
        if not song or not playing and vt == 0:
            self.draw_help(d, roll_top)
        img.paste(kb, (0, KB_Y))
        d = ImageDraw.Draw(img)

        # ---- 鍵盤の上の色
        def mark(n, col, full=True):
            s = self.display_semi(n, span) if not (0 <= n - self.base < span) else n - self.base
            if s is None or s not in geo:
                return
            x0, x1, black = geo[s]
            hh = (LCD_H - BOT_H - KB_Y) * (0.58 if black else 1) - 1
            if full:
                d.rectangle((x0 + 1, KB_Y + 1, x1 - 1, KB_Y + hh), fill=col)
            else:
                d.rectangle((x0 + 1, KB_Y + 1, x1 - 1, KB_Y + hh), outline=col, width=2)
        if waiting and ci < len(clusters):
            for n in clusters[ci][1]:
                mark(n, GUIDE_COL, full=False)
        for n in self.ext:
            mark(n, EXT_COL)
        for n in set(self.held.values()):
            mark(n, ACC)
        for n, until in list(self.flash.items()):
            if until < now:
                del self.flash[n]
            else:
                s = self.display_semi(n, span)
                if s in geo:
                    x0, x1, _ = geo[s]
                    d.ellipse(((x0 + x1) / 2 - 6, roll_bot - 14, (x0 + x1) / 2 + 6, roll_bot - 2),
                              outline=(120, 255, 160), width=2)
        if waiting:
            t = "まってるよ ♪"
            w = self.font_t.getlength(t) + 14
            d.rounded_rectangle((LCD_W - w - 4, TOP_H + 4, LCD_W - 4, TOP_H + 20), 6,
                                fill=(60, 50, 10), outline=GUIDE_COL)
            d.text((LCD_W - w + 3, TOP_H + 5), t, fill=GUIDE_COL, font=self.font_t)
        return self.chrome(img)

    def draw_help(self, d, y):
        lines = [("SPACE", "伴奏 再生/停止"), ("TAB", "次の曲"), ("ENTER", "抜くパート"),
                 ("ALT", "ﾏﾁ (弾くまで待つ)"), ("↑↓ ←→", "オクターブ / 音色"),
                 ("SHIFT", "サステイン  BS:配列")]
        for i, (k, v) in enumerate(lines):
            col, row = divmod(i, 3)
            x, yy = 8 + col * 156, y + 6 + row * 17
            d.text((x, yy), k, fill=ACC, font=self.font_s)
            d.text((x + 46, yy), v, fill=SUB, font=self.font_s)

    def chrome(self, img):
        d = ImageDraw.Draw(img)
        p = self.player
        d.rectangle((0, 0, LCD_W, TOP_H - 1), fill=(0, 0, 0))
        d.text((5, 1), "HEXA MIDI", fill=ACC, font=self.font_bs)
        x = 5 + self.font_bs.getlength("HEXA MIDI") + 8
        if p.song:
            st = "■" if not p.playing else ("待" if p.waiting else "▶")
            d.text((x, 3), st, fill=GUIDE_COL if p.waiting else FG, font=self.font_t)
            name = p.song.name
            room = LCD_W - x - 18 - 60
            while name and self.font_t.getlength(name) > room:
                name = name[:-1]
            d.text((x + 16, 3), name, fill=FG, font=self.font_t)
            frac = min(1.0, p.vt / p.song.length) if p.song.length else 0
            d.rectangle((LCD_W - 58, 7, LCD_W - 6, 10), fill=(40, 44, 56))
            d.rectangle((LCD_W - 58, 7, LCD_W - 58 + 52 * frac, 10), fill=ACC)
        else:
            d.text((x, 3), "曲なし", fill=SUB, font=self.font_t)
        # 下のバー
        d.rectangle((0, LCD_H - BOT_H, LCD_W, LCD_H), fill=(0, 0, 0))
        parts = [(voice_name(self.program), FG), (note_name(self.base), SUB),
                 ("P" if self.layout == "PIANO" else "G", SUB)]
        if p.mute is not None:
            parts.append((f"抜く ch{p.mute + 1}", GUIDE_COL))
        parts.append(("ﾏﾁ" if p.wait_mode else "ﾏﾁ off", GUIDE_COL if p.wait_mode else (90, 98, 115)))
        if p.clusters:
            parts.append((f"✓{len(p.hit)}/{len(p.clusters)}", (120, 255, 160)))
        if self.sustain:
            parts.append(("SUS", ACC))
        xx = 4
        for txt, col in parts:
            d.text((xx, LCD_H - BOT_H + 2), txt, fill=col, font=self.font_s)
            xx += self.font_s.getlength(txt) + 9
        mi = "MIDI" if self.midi.ok else "MIDI ×"
        mcol = (EXT_COL if self.ext else (120, 200, 140)) if self.midi.ok else (255, 110, 90)
        if self.midi.ok and not self.thru:
            mi, mcol = "IN off", (90, 98, 115)
        d.text((LCD_W - 4 - self.font_s.getlength(mi), LCD_H - BOT_H + 2), mi, fill=mcol, font=self.font_s)
        msg, until = self.toast
        if msg and time.time() < until:
            f = self.font_t
            w = min(int(f.getlength(msg)) + 16, LCD_W - 8)
            yy = KB_Y - 22
            d.rounded_rectangle(((LCD_W - w) // 2, yy, (LCD_W + w) // 2, yy + 17), 7,
                                fill=(70, 16, 48), outline=ACC)
            d.text(((LCD_W - w) // 2 + 8, yy + 2), msg, fill=(255, 255, 255), font=f)
        return img

    # ---- ループ
    def on_key(self, code, val):
        lay = LAYOUTS[self.layout][0]
        if code in lay:
            if val:
                self.note_on(code, lay[code])
            else:
                self.note_off(code)
            return None
        if code in (K_LSHIFT, K_RSHIFT):
            self.sustain = bool(val)
            self.midi.send([0xB0 | MY_CH, 64, 127 if val else 0])
            return None
        if not val:
            return None
        p = self.player
        if code == K_ESC:
            return "ESC"
        if code == K_SPACE:
            if p.playing:
                p.stop(rewind=False)
                self.say("停止 (もう一度 SPACE で続き / TAB で曲を変えると最初から)")
            else:
                p.start()
        elif code == K_TAB:
            self.load_song(self.si + 1)
        elif code == K_ENTER:
            self.cycle_mute()
        elif code in (K_LALT, K_RALT):
            p.wait_mode = not p.wait_mode
            if not p.wait_mode:
                with p.lock:
                    p.waiting = False
                    p._t = time.monotonic()
            self.say("ﾏﾁ ON: 抜いたパートを弾くまで待ちます" if p.wait_mode else "ﾏﾁ OFF")
            if p.wait_mode and p.mute is None:
                self.cycle_mute()
        elif code in (K_LCTRL, K_RCTRL):
            self.thru = not self.thru
            self.ext.clear()
            self.say("MIDI IN スルー ON" if self.thru else "MIDI IN スルー OFF")
        elif code == K_BS:
            self.release_all()
            self.layout = LAYOUT_ORDER[(LAYOUT_ORDER.index(self.layout) + 1) % len(LAYOUT_ORDER)]
            self.say(f"配列: {self.layout}")
        elif code in (K_UP, K_DOWN):
            self.release_all()
            self.base = max(12, min(96, self.base + (12 if code == K_UP else -12)))
            self.say(f"オクターブ: {note_name(self.base)} から")
        elif code in (K_LEFT, K_RIGHT):
            self.vi = (self.vi + (1 if code == K_RIGHT else -1)) % len(VOICES)
            self.set_program(VOICES[self.vi][0])
        return None

    def run(self):
        self.keys.flush(0.3)
        if not self.midi.ok:
            self.say("MIDI につながっていません (--probe で確認)", 4)
        last = 0.0
        was_end = False
        while True:
            for code, val in self.keys.poll(0.004):
                if self.on_key(code, val) == "ESC":
                    return "ESC"
            self.handle_in()
            if self.player.ended and not was_end:
                p = self.player
                if p.clusters:
                    self.say(f"おわり ✓{len(p.hit)}/{len(p.clusters)}", 4)
                else:
                    self.say("おわり", 2)
            was_end = self.player.ended
            now = time.time()
            busy = self.player.playing or self.held or self.ext or self.flash or now < self.toast[1] + 0.3
            if now - last > (0.066 if busy else 0.5):
                self.fb.show(self.draw())
                last = now

    def close(self):
        self.release_all()
        self.player.stop()
        self.player.close()
        self.midi.send([0xB0 | MY_CH, 64, 0])
        self.midi.send([0xB0 | MY_CH, 123, 0])


KB_Y = 112


# ================================================================ ランチャー (HEXA CLIP と同じ)
def launcher_pids():
    pids = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            cmd = open(f"/proc/{d}/cmdline", "rb").read()
        except OSError:
            continue
        if b"M5CardputerZero-APPLaunch" in cmd:
            pids.append(int(d))
    return pids


def signal_launcher(sig):
    for pid in launcher_pids():
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


# ================================================================ PC 用シミュレーション
class SimFB:
    def __init__(self, outdir):
        self.outdir, self.img = outdir, None
        os.makedirs(outdir, exist_ok=True)

    def show(self, img):
        self.img = img.copy()

    def snap(self, name):
        self.img.resize((LCD_W * 2, LCD_H * 2), Image.NEAREST).save(os.path.join(self.outdir, name))

    def clear(self):
        pass


class SimKeys:
    """台本: [(待つ秒, (code, val)) | (待つ秒, ('SNAP', name)) | (待つ秒, ('IN', bytes))]"""

    def __init__(self, script, fb, midi):
        self.script, self.fb, self.midi, self.t = list(script), fb, midi, time.time()

    def poll(self, timeout=0.0):
        time.sleep(min(timeout, 0.01))
        out = []
        while self.script and time.time() - self.t >= self.script[0][0]:
            _, ev = self.script.pop(0)
            self.t = time.time()
            if ev[0] == "SNAP":
                if self.fb.img is not None:
                    self.fb.snap(ev[1])
            elif ev[0] == "IN":
                self.midi.inbox.append(bytes(ev[1]))
            else:
                out.append(ev)
        if not self.script and not out:
            out.append((K_ESC, 1))
        return out

    def flush(self, sec=0):
        pass


class SimMidi(MidiPort):
    def __init__(self):
        self.lock = threading.Lock()
        self.ser, self.err, self.port, self.sent = None, "", "sim", 0
        self.log, self.inbox = [], []

    @property
    def ok(self):
        return True

    def send(self, data):
        self.sent += 1
        self.log.append((time.monotonic(), bytes(data)))

    def receive(self):
        out, self.inbox = self.inbox, []
        return out


def run_sim(outdir):
    import make_demo
    songdir = os.path.join(outdir, "songs")
    os.makedirs(songdir, exist_ok=True)
    make_demo.twinkle(os.path.join(songdir, "01_twinkle.mid"))
    make_demo.joy(os.path.join(songdir, "02_joy.mid"))
    SONG_DIRS[:] = [songdir]
    fb = SimFB(outdir)
    midi = SimMidi()
    P = lambda k: (K[k] if isinstance(k, str) else k, 1)    # noqa: E731
    R = lambda k: (K[k] if isinstance(k, str) else k, 0)    # noqa: E731
    script = [(0.8, ("SNAP", "01_home.png")),
              (0.1, P("z")), (0.05, P("c")), (0.05, P("b")), (0.3, ("SNAP", "02_chord.png")),
              (0.1, R("z")), (0.0, R("c")), (0.0, R("b")),
              (0.1, (K_SPACE, 1)), (2.0, ("SNAP", "03_playing_all.png")),
              (0.1, (K_SPACE, 1)), (0.2, (K_ENTER, 1)), (0.1, (K_LALT, 1)),
              (0.4, (K_SPACE, 1)), (3.2, ("SNAP", "04_wait.png")),
              (0.1, P("z")), (0.2, R("z")), (0.25, P("z")), (0.1, R("z")),
              (0.35, ("SNAP", "05_after_hit.png")),
              (0.1, ("IN", [0x90, 67, 90])), (0.1, ("SNAP", "06_ext_in.png")), (0.1, ("IN", [0x80, 67, 0])),
              (0.1, (K_BS, 1)), (0.3, ("SNAP", "07_guitar.png")),
              (0.1, (K_RIGHT, 1)), (0.1, ("SNAP", "08_voice.png"))]
    keys = SimKeys(script, fb, midi)
    app = App(fb, keys, midi)
    app.run()
    app.close()
    t0 = midi.log[0][0]
    with open(os.path.join(outdir, "sent.txt"), "w") as f:
        for t, b in midi.log:
            f.write(f"{t - t0:8.3f}  {b.hex(' ')}\n")
    print("sim done:", outdir, "sent", len(midi.log), "msgs")


# ================================================================ probe
def probe():
    print(f"HEXA MIDI {VERSION}")
    port = find_port()
    real = os.path.realpath(port) if port else None
    print("UART:", port, "->", real)
    for cfg in ("/boot/firmware/config.txt", "/boot/config.txt"):
        if os.path.exists(cfg):
            lines = [ln.strip() for ln in open(cfg) if ln.strip().startswith(("enable_uart", "dtoverlay"))]
            print(f"{cfg}:", ", ".join(lines) or "(enable_uart なし)")
            break
    for cl in ("/boot/firmware/cmdline.txt", "/boot/cmdline.txt"):
        if os.path.exists(cl):
            c = open(cl).read()
            bad = [w for w in c.split() if w.startswith("console=") and ("serial" in w or "tty" in w and "tty1" not in w)]
            print("シリアルコンソール:", " ".join(bad) if bad else "なし (OK)")
            break
    m = MidiPort(port)
    if not m.ok:
        print("開けません:", m.err)
        print("→ sh install.sh を実行して再起動、ユーザーが dialout グループに入っているか確認")
        return 1
    print("31250bps で開きました。ch16 でドミソを鳴らします (Unit MIDI のｽﾋﾟｰｶｰ/ｲﾔﾎﾝから聞こえればOK)")
    m.send([0xC0 | MY_CH, 0])
    m.send([0xB0 | MY_CH, 7, 110])
    for n in (60, 64, 67, 72):
        m.send([0x90 | MY_CH, n, 100])
        time.sleep(0.25)
        m.send([0x80 | MY_CH, n, 0])
    print("MIDI IN を2秒聞きます (Bypass にして外の鍵盤を弾いてみてください)...")
    end, got = time.time() + 2, []
    while time.time() < end:
        got += m.receive()
        time.sleep(0.02)
    print("受信:", " / ".join(b.hex(" ") for b in got[:10]) if got else "なし")
    m.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--sim", metavar="OUTDIR")
    ap.add_argument("--port", help="UART (既定 /dev/serial0)")
    ap.add_argument("--no-pause", action="store_true", help="ランチャーを一時停止しない")
    ap.add_argument("--check-font", action="store_true")
    a = ap.parse_args()
    if a.check_font:
        find_font(12)
        print("font:", _FONT_FILE.get(False) or "見つかりません")
        raise SystemExit(0 if _FONT_FILE.get(False) else 1)
    if a.probe:
        raise SystemExit(probe())
    if a.sim:
        return run_sim(a.sim)
    fb = FrameBuffer()
    keys = Keys(find_keyboard())
    midi = MidiPort(a.port)
    paused = False
    if not a.no_pause and launcher_pids():
        signal_launcher(signal.SIGSTOP)
        paused = True
        for sg in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sg, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    reason, app = "unknown", None
    try:
        print(f"HEXA MIDI {VERSION} port={midi.port} ok={midi.ok} {midi.err}", flush=True)
        app = App(fb, keys, midi)
        reason = app.run()
    except KeyboardInterrupt:
        reason = "Ctrl+C"
    except Exception:
        import traceback
        traceback.print_exc()
        reason = "error"
    finally:
        print("exit:", reason, flush=True)
        if app:
            app.close()
        midi.all_off()
        time.sleep(0.05)
        midi.close()
        keys.close()
        fb.clear()
        if paused:
            signal_launcher(signal.SIGCONT)


if __name__ == "__main__":
    sys.path.insert(0, DIR)
    main()
