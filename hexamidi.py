#!/usr/bin/env python3
"""HEXA MIDI — CardputerZero + M5Stack Unit MIDI (U187, SAM2695) の演奏アプリ (v0.2)

モード (TAB で切替)
  FREE   : クロマチック鍵盤。配列は PIANO (Z〜M / Q〜P) か GUITAR (段ごとに4度)
  SCALE  : 外れない鍵盤。Z の段 → Q の段 にスケールの音が 17 音、地続きで並ぶ
           数字の段 = コード (1〜7 ダイアトニック, 8 V7, 9 Vsus4, 0 I add9)、A の段 = その 1オクターブ上
           コードはホールド判定つき: 短く押す=消える / 長く押して離す=残る (Aegis-Synth M3.5)
メロディの発音 (設定): ポリ / モノ / レガート (ピッチベンドで滑らせる)
伴奏: .mid を流す。抜くパートを選ぶと上から降ってきて、ﾏﾁ (ALT) で弾くまで待ってくれる

キー
  SHIFT 押している間サステイン     ↑↓ (fn+F/X) オクターブ   ←→ (fn+Z/C) 音色
  TAB   FREE ↔ SCALE               SPACE 伴奏 再生/停止     BS 次の曲
  ALT   ﾏﾁ ON/OFF                  CTRL  全部の音を止める   ENTER 設定   ESC 終了
usage:
  hexamidi                # 通常起動
  hexamidi --probe        # UART の設定を調べて、テスト音 (ドミソ) を鳴らす
  hexamidi --sim OUTDIR   # PC 用: 偽の画面/キー/MIDI で動かして画面を PNG に保存
"""
import argparse
import bisect
import fcntl
import glob
import json
import math
import os
import select
import signal
import struct
import sys
import time

from PIL import Image, ImageDraw, ImageFont

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)

from hm_midi import MidiPort, find_port                                   # noqa: E402
from hm_music import (CHORD_HI_KEYS, CHORD_KEYS, FAVORITES, GM, NOTE_NAMES, SCALE_KEYS,  # noqa: E402
                      SCALE_ORDER, SCALES, ChordEngine, MelodyEngine, auto_chords, detect_key,
                      is_black, list_presets, note_name, scale_notes)
from hm_song import Player, Song, list_songs, write_song                 # noqa: E402

VERSION = "0.2.0"
LCD_W, LCD_H = 320, 170
TOP_H, BOT_H = 18, 16
KB_Y = 112
BG, FG, SUB = (14, 16, 22), (235, 238, 245), (150, 158, 175)
ACC = (255, 80, 170)                      # HEXA MIDI の強調色 (CLIP=緑 / MATRIX=琥珀 / POWER=青)
ACC_DIM = (70, 22, 50)
EXT_COL = (80, 210, 255)                  # MIDI IN (外の鍵盤)
GUIDE_COL = (255, 214, 90)                # 抜いたパート (降ってくる音)
OK_COL = (120, 255, 160)
CHORD_COL = (170, 140, 255)               # コードの音
MODE_COL = {"FREE": (80, 210, 255), "SCALE": (255, 170, 60)}   # Aegis と同じ FREE=シアン / SCALE=アンバー
SONG_DIRS = [os.path.join(DIR, "songs"), os.path.expanduser("~/midi")]
CHORD_DIRS = [os.path.join(DIR, "chords"), os.path.expanduser("~/midi/chords")]
CONFIG = os.path.expanduser("~/.config/hexamidi.json")
CH_MEL, CH_CHORD = 15, 14                 # 自分のメロディ = ch16 / コード = ch15
ROLL_SEC = 2.5

# ================================================================ キー配列 (Linux のキーコード)
K = {"1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
     "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
     "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
     "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50}
K_ESC, K_BS, K_TAB, K_ENTER, K_SPACE = 1, 14, 15, 28, 57
K_LSHIFT, K_RSHIFT, K_LCTRL, K_RCTRL, K_LALT, K_RALT = 42, 54, 29, 97, 56, 100
K_UP, K_LEFT, K_RIGHT, K_DOWN = 103, 105, 106, 108
CODE_CH = {v: k for k, v in K.items()}

FREE_LAYOUTS = {
    # 名前: ({キーコード: 基準からの半音}, 鍵盤に出す半音の数)
    "PIANO": ({**{K[c]: i for i, c in enumerate("zsxdcvgbhnjm")},
               **{K[c]: 12 + i for i, c in enumerate("q2w3er5t6y7ui9o0p")}}, 29),
    "GUITAR": ({**{K[c]: i for i, c in enumerate("zxcvbnm")},
                **{K[c]: 5 + i for i, c in enumerate("asdfghjkl")},
                **{K[c]: 10 + i for i, c in enumerate("qwertyuiop")},
                **{K[c]: 15 + i for i, c in enumerate("1234567890")}}, 25),
}
SCALE_CODES = [K[c] for c in SCALE_KEYS]
CHORD_CODES = {K[c]: c for c in CHORD_KEYS + CHORD_HI_KEYS}
HOLD_OPTS = [0, 200, 250, 300, 350, 450, 600, 800, -1]       # 0=残さない / -1=常に残す
VOICE_JP = {"POLY": "ポリ", "MONO": "モノ", "LEGATO": "レガート"}

DEFAULTS = {"mode": "FREE", "layout": "PIANO", "oct": 0,
            "mel_prog": 0, "mel_vol": 110, "voice": "POLY", "glide_ms": 200,
            "key": "auto", "scale": "メジャー",
            "chordset": "auto", "chord_prog": 0, "chord_vol": 100, "hold": 350, "voicing": "厚め",
            "thru": True, "reverb": 40, "tempo": 100, "wait": False, "song": ""}


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


def _jp_chars():
    """画面に出る日本語の字 (ソースから集める)。□にならないフォントだけ使う"""
    chars = set()
    for f in ("hexamidi.py", "hm_music.py", "hm_song.py"):
        try:
            for ch in open(os.path.join(DIR, f), encoding="utf-8").read():
                if "\u3040" <= ch <= "\u9fff" or "\uff61" <= ch <= "\uff9f":
                    chars.add(ch)
        except OSError:
            pass
    return "".join(sorted(chars))


JP_TEST = None
_FONT_FILE = {}


def font_covers(font, text=None):
    global JP_TEST
    if text is None:
        JP_TEST = JP_TEST or _jp_chars()
        text = JP_TEST
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


# ================================================================ 設定
def load_config():
    cfg = dict(DEFAULTS)
    try:
        d = json.load(open(CONFIG, encoding="utf-8"))
        cfg.update({k: v for k, v in d.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    if cfg["voice"] not in MelodyEngine.MODES:
        cfg["voice"] = "POLY"
    if cfg["scale"] not in SCALES:
        cfg["scale"] = "メジャー"
    if cfg["hold"] not in HOLD_OPTS:
        cfg["hold"] = 350
    return cfg


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        tmp = CONFIG + ".tmp"
        json.dump(cfg, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, CONFIG)
    except OSError:
        pass


def song_base(path):
    """sunday_park_interlude_rec2.mid / _edit.mid → sunday_park_interlude"""
    b = os.path.splitext(os.path.basename(path))[0]
    for suf in ("_edit", "_rec"):
        if suf in b:
            b = b.split(suf)[0]
    return b


# ================================================================ ﾀｲﾐﾝｸﾞ調整
class TimingEditor:
    """曲の1つのﾊﾟｰﾄ (抜くﾊﾟｰﾄ/ﾒﾛﾃﾞｨ) の音を 1 つずつ前後にずらす画面
      Z C (←→) 音を選ぶ   F X (↑↓) 早く/遅く   TAB 刻み (16分/32分/10ms)
      SPACE 前後を聞く     A 最初から全部聞く   BS 1つ戻す   ENTER 保存   ESC もどる"""
    STEPS = [("16分", 4), ("32分", 8), ("10ms", None)]

    def __init__(self, app, song, ch):
        self.app, self.ch = app, ch
        self.src = song
        self.beat = song.tempo_us / 1e6
        self.notes = [[on, off, n] for on, off, n in song.notes.get(ch, [])]
        self.others = [(t, m) for t, m in song.events
                       if not ((m[0] & 0x0F) == ch and (m[0] & 0xF0) in (0x80, 0x90))]
        self.sel = 0
        self.step_i = 0
        self.undo = []
        self.dirty = False
        self.saved_path = None
        self.confirm = False
        self.preview_song = None

    # ---- 編集
    def step_sec(self):
        name, div = self.STEPS[self.step_i]
        return self.beat / div if div else 0.010

    def move(self, d):
        if not self.notes:
            return
        self.undo.append([x[:] for x in self.notes])
        del self.undo[:-50]
        nt = self.notes[self.sel]
        dur = nt[1] - nt[0]
        g = self.step_sec()
        k = nt[0] / g
        new_on = (math.floor(k + 1e-6) + 1) * g if d > 0 else (math.ceil(k - 1e-6) - 1) * g   # 次の目盛りへ
        new_on = max(0.0, new_on)
        prev = self.notes[self.sel - 1] if self.sel > 0 else None
        nxt = self.notes[self.sel + 1] if self.sel + 1 < len(self.notes) else None
        if prev and new_on <= prev[0] + 0.02:         # 前の音を追い越さない
            new_on = prev[0] + 0.02
        if nxt and new_on >= nxt[0] - 0.02:           # 次の音を追い越さない
            new_on = nxt[0] - 0.02
        nt[0], nt[1] = new_on, new_on + dur
        if nxt and nt[1] > nxt[0] - 0.02:             # 次と重なったら切る
            nt[1] = max(nt[0] + 0.04, nxt[0] - 0.02)
        if prev and prev[1] > nt[0] - 0.02:           # 前の音の伸びを切る
            prev[1] = max(prev[0] + 0.04, nt[0] - 0.02)
        self.dirty = True

    def events(self):
        ev = list(self.others)
        for on, off, n in self.notes:
            ev.append((on, bytes([0x90 | self.ch, n, 100])))
            ev.append((off, bytes([0x80 | self.ch, n, 0])))
        ev.sort(key=lambda e: (e[0], 0 if (e[1][0] & 0xF0) == 0x80 else 1))
        return ev

    def save(self):
        base = song_base(self.src.path)
        outdir = os.path.expanduser("~/midi")
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, f"{base}_edit.mid")
        write_song(path, self.events(), f"{base} edit", self.src.tempo_us)
        self.saved_path, self.dirty = path, False
        return path

    # ---- 試聴 (今の編集を一時ﾌｧｲﾙにして鳴らす)
    def preview(self, whole=False):
        tmp = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "hexamidi_preview.mid")
        write_song(tmp, self.events(), "preview", self.src.tempo_us)
        song = Song(tmp)
        p = self.app.player
        p.load(song)
        p.wait_mode = False
        if whole:
            p.play_range(0.0, song.length + 0.1)
            return
        bar = self.beat * 4
        t = self.notes[self.sel][0] if self.notes else 0
        b = int(t // bar)
        p.play_range(max(0, b - 1) * bar, (b + 1) * bar + 0.05)

    # ---- キー
    def key(self, code):
        ch = CODE_CH.get(code)
        if self.confirm:
            if code == K_ENTER:
                self.save()
                return "close"
            if code == K_ESC:
                return "close"
            if code == K_BS:
                self.confirm = False
            return None
        if code in (K_LEFT,) or ch == "z":
            self.sel = max(0, self.sel - 1)
        elif code in (K_RIGHT,) or ch == "c":
            self.sel = min(len(self.notes) - 1, self.sel + 1)
        elif code in (K_UP,) or ch == "f":
            self.move(-1)
        elif code in (K_DOWN,) or ch == "x":
            self.move(1)
        elif code == K_TAB:
            self.step_i = (self.step_i + 1) % len(self.STEPS)
            self.app.say(f"刻み: {self.STEPS[self.step_i][0]}", 1.2)
        elif code == K_SPACE:
            self.preview()
        elif ch == "a":
            self.preview(whole=True)
        elif code == K_BS:
            if self.undo:
                self.notes = self.undo.pop()
                self.dirty = True
                self.app.say("1つ戻しました", 1)
        elif code == K_ENTER:
            path = self.save()
            self.app.say(f"保存: {os.path.basename(path)}", 2.5)
        elif code == K_ESC:
            if self.dirty:
                self.confirm = True
            else:
                return "close"
        return None

    # ---- 画面
    def draw(self):
        a = self.app
        img = a.bg.copy()
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, LCD_W, TOP_H - 1), fill=(0, 0, 0))
        d.text((5, 1), "HEXA MIDI", fill=ACC, font=a.font_bs)
        d.text((5 + a.font_bs.getlength("HEXA MIDI") + 8, 3), f"ﾀｲﾐﾝｸﾞ調整  ch{self.ch + 1}  刻み {self.STEPS[self.step_i][0]}",
               fill=FG, font=a.font_t)
        if not self.notes:
            d.text((10, 60), "このﾊﾟｰﾄには音がありません", fill=SUB, font=a.font)
            return img
        bar = self.beat * 4
        sel = self.notes[self.sel]
        span = bar * 2
        t0 = min(max(0.0, sel[0] - span / 2), max(0.0, self.notes[-1][1] - span))
        y0, y1 = TOP_H + 20, LCD_H - BOT_H - 22
        lo = min(n for _, _, n in self.notes) - 1
        hi = max(n for _, _, n in self.notes) + 1
        rh = (y1 - y0) / (hi - lo + 1)

        def X(t):
            return (t - t0) / span * LCD_W
        # 目盛り
        g = self.beat / 4
        k = int(t0 // g)
        while k * g <= t0 + span:
            x = X(k * g)
            col = (90, 60, 110) if k % 16 == 0 else (52, 56, 72) if k % 4 == 0 else (30, 33, 44)
            d.line((x, y0, x, y1), fill=col, width=2 if k % 16 == 0 else 1)
            if k % 16 == 0:
                d.text((x + 2, y0 - 13), f"{k // 16 + 1}小節", fill=SUB, font=a.font_s)
            k += 1
        # 音
        for i, (on, off, n) in enumerate(self.notes):
            if off < t0 or on > t0 + span:
                continue
            y = y1 - (n - lo + 1) * rh
            col = ACC if i == self.sel else GUIDE_COL
            d.rounded_rectangle((X(on), y, max(X(on) + 3, X(off)), y + rh - 1), 2, fill=col)
            if i == self.sel:
                d.text((X(on), y - 12), note_name(n), fill=ACC, font=a.font_s)
        # 再生位置
        p = a.player
        if p.playing:
            x = X(p.vt)
            d.line((x, y0, x, y1), fill=(120, 255, 160), width=1)
        # 選んでいる音の情報
        beat_pos = sel[0] / self.beat
        bi = int(beat_pos // 4)
        rem = beat_pos - bi * 4
        sixteenth = rem * 4
        info = (f"{self.sel + 1}/{len(self.notes)}  {note_name(sel[2])}  {bi + 1}小節 {int(rem) + 1}拍目"
                f"{'' if abs(sixteenth - round(sixteenth)) > 0.05 else ' +' + str(int(round(sixteenth)) % 4) + '/16'}"
                f"  ({sel[0]:.2f}秒)")
        d.text((6, LCD_H - BOT_H - 18), info, fill=FG, font=a.font_s)
        d.rectangle((0, LCD_H - BOT_H, LCD_W, LCD_H), fill=(0, 0, 0))
        d.text((4, LCD_H - BOT_H + 2), "ZC:選ぶ FX:前後 TAB:刻み SPACE:聞く A:全部 BS:戻す ENTER:保存",
               fill=(210, 210, 210), font=a.font_s)
        if self.confirm:
            img2 = Image.blend(img, Image.new("RGB", img.size), 0.6)
            d2 = ImageDraw.Draw(img2)
            d2.rounded_rectangle((40, 45, LCD_W - 40, LCD_H - 40), 8, fill=(26, 30, 38), outline=ACC, width=2)
            for i, (txt, f, c) in enumerate([("変更を保存しますか?", a.font, FG),
                                             ("ENTER:保存  ESC:捨てる  BS:続ける", a.font_s, ACC)]):
                d2.text(((LCD_W - f.getlength(txt)) / 2, 62 + i * 26), txt, fill=c, font=f)
            img = img2
        a.draw_toast(ImageDraw.Draw(img), TOP_H + 2)
        return img


# ================================================================ アプリ
class App:
    def __init__(self, fb, keys, midi, cfg=None, save=True):
        self.fb, self.keys, self.midi = fb, keys, midi
        self.cfg = cfg or load_config()
        self.save = save
        self.player = Player(midi, own_channels=(CH_MEL, CH_CHORD))
        self.mel = MelodyEngine(midi, CH_MEL)
        self.chord = ChordEngine(midi, CH_CHORD)
        midi.tickers.append(self.mel.tick)
        self.font = find_font(14)
        self.font_bs = find_font(13, bold=True)
        self.font_t = find_font(11)
        self.font_s = find_font(10)
        self.font_k = find_font(9)
        self.bg = honeycomb_bg()
        self.toast = ("", 0.0)
        self.ext = {}                           # 外の鍵盤で鳴っている音 → ch
        self.flash = {}                         # 正解した音 → 光らせる時刻まで
        self.chord_notes_on = []
        self.menu = None                        # 設定画面の選択位置 (None = 閉じている)
        self.menu_top = 0
        self.aud = {"main": False, "sub": False, "last": 0.0, "off_at": 0.0, "ringing": []}
        self._kb_cache = None
        self.songs = list_songs(SONG_DIRS)
        self.presets = list_presets(CHORD_DIRS)
        self.si = -1
        self.song_key = None                    # 曲から推定した (主音, 種類)
        self.rec_armed = False                  # 録音: 次に最初から再生した時に録る
        self.rec = None                         # [(on秒, off秒, 音)] 曲の中の時間
        self.rec_open = {}                      # キーコード → (on秒, 音)
        self.editor = None                      # ﾀｲﾐﾝｸﾞ調整画面
        self.init_synth()
        if self.songs:
            names = [os.path.basename(p) for p in self.songs]
            i = names.index(self.cfg["song"]) if self.cfg["song"] in names else 0
            self.load_song(i, quiet=True)
        self.refresh_music()

    # ---- 音源の初期化
    def init_synth(self):
        m = self.midi
        m.all_off()
        m.gm_reset()
        m.drain()
        time.sleep(0.1)
        m.master_volume(127)
        m.bend_range(CH_MEL, 24)
        self.apply_sound()
        m.pitch_bend(CH_MEL, 8192)
        m.drain()

    def apply_sound(self):
        c, m = self.cfg, self.midi
        m.program(CH_MEL, c["mel_prog"])
        m.program(CH_CHORD, c["chord_prog"])
        m.cc(CH_MEL, 7, c["mel_vol"])
        m.cc(CH_CHORD, 7, c["chord_vol"])
        m.cc(CH_MEL, 91, c["reverb"])
        m.cc(CH_CHORD, 91, c["reverb"] + 10)
        self.mel.set_mode(c["voice"])
        self.mel.glide_ms = c["glide_ms"]
        self.chord.hold_ms = c["hold"]
        self.player.tempo = c["tempo"] / 100
        self.player.wait_mode = c["wait"]

    # ---- スケールとコード
    def preset(self):
        for p in self.presets:
            if p.name == self.cfg["chordset"]:
                return p
        return None

    def effective_key(self):
        """→ (主音 0-11, スケール名, 出どころの説明)"""
        p = self.preset()
        if p and p.key is not None:
            return p.key, p.scale or self.cfg["scale"], "コードセット"
        sc = self.cfg["scale"]
        if self.cfg["key"] == "auto":
            if self.song_key:
                pc, kind = self.song_key
                if sc in ("メジャー", "マイナー"):
                    sc = kind
                return pc, sc, "曲から"
            return 0, sc, "曲なし"
        return NOTE_NAMES.index(self.cfg["key"]), sc, "手動"

    def scale_root(self):
        pc, _, _ = self.effective_key()
        return 48 + pc + 12 * self.cfg["oct"]          # Aegis と同じ (G# なら G#3)

    def refresh_music(self):
        pc, sc, _ = self.effective_key()
        self.scale_list = scale_notes(self.scale_root(), SCALES[sc])
        p = self.preset()
        self.chord.chords = p.chords if p else auto_chords(pc, sc, self.cfg["voicing"] == "厚め")
        self._kb_cache = None

    # ---- 鍵盤の範囲
    def kb_range(self):
        if self.cfg["mode"] == "SCALE":
            lo, hi = self.scale_list[0], self.scale_list[-1]
            lo -= 1 if is_black(lo) else 0
            hi += 1 if is_black(hi) else 0
            return lo, hi - lo + 1
        base = 48 + 12 * self.cfg["oct"]
        return base, FREE_LAYOUTS[self.cfg["layout"]][1]

    # ---- 音
    def note_for(self, code):
        if self.cfg["mode"] == "SCALE":
            if code in SCALE_CODES:
                return self.scale_list[SCALE_CODES.index(code)]
            return None
        lay = FREE_LAYOUTS[self.cfg["layout"]][0]
        if code in lay:
            n = 48 + 12 * self.cfg["oct"] + lay[code]
            return n if 0 <= n <= 127 else None
        return None

    def answer(self, n):
        if self.player.user_note(n):
            self.flash[n] = time.time() + 0.35

    def handle_in(self):
        for msg in self.midi.receive():
            typ, ch = msg[0] & 0xF0, msg[0] & 0x0F
            if not self.cfg["thru"]:
                continue
            self.midi.raw(msg)                  # Bypass では IN は SAM2695 に行かないので、ここで鳴らす
            if typ == 0x90 and msg[2] > 0:
                self.ext[msg[1]] = ch
                self.answer(msg[1])
            elif typ in (0x80, 0x90):
                self.ext.pop(msg[1], None)
            elif typ == 0xB0 and msg[1] in (120, 123):
                self.ext.clear()

    def panic(self):
        self.mel.all_off()
        self.chord.silence()
        self.chord.pressing_key = None
        self.ext.clear()
        self.midi.all_off([CH_MEL, CH_CHORD])
        if self.mel.sustain:
            self.midi.cc(CH_MEL, 64, 127)

    # ---- 確認音 (Aegis の audition: 連打の時は最後の 1 回だけ鳴らす)
    def audition(self, which):
        if self.player.playing:
            return
        self.aud[which] = True

    def audition_tick(self):
        a, now = self.aud, time.time()
        if a["ringing"] and now >= a["off_at"]:
            for ch, n in a["ringing"]:
                self.midi.note_off(ch, n)
            a["ringing"] = []
        if not (a["main"] or a["sub"]) or now - a["last"] < 0.14:
            return
        for ch, n in a["ringing"]:
            self.midi.note_off(ch, n)
        a["ringing"] = []
        if a["main"]:
            n = self.scale_list[0] + 12 if self.cfg["mode"] == "SCALE" else 60 + 12 * self.cfg["oct"]
            self.midi.note_on(CH_MEL, n, 100)
            a["ringing"].append((CH_MEL, n))
        if a["sub"]:
            c = self.chord.chords.get("1")
            for n in (c.notes if c else [48, 52, 55]):
                self.midi.note_on(CH_CHORD, n, 90)
                a["ringing"].append((CH_CHORD, n))
        a["main"] = a["sub"] = False
        a["last"], a["off_at"] = now, now + 0.45

    # ---- 曲
    def load_song(self, i, quiet=False):
        if not self.songs:
            self.say("曲がありません (songs/ か ~/midi/ に .mid を置く)")
            return
        self.si = i % len(self.songs)
        path = self.songs[self.si]
        try:
            song = Song(path)
        except Exception as e:
            self.say(f"読めません: {os.path.basename(path)} ({e})")
            return
        self.player.load(song)
        self.cfg["song"] = os.path.basename(path)
        self.song_key = detect_key(song)
        self.refresh_music()
        if not quiet:
            pc, kind = self.song_key
            self.say(f"曲: {song.name}  ({NOTE_NAMES[pc]} {kind})")

    def mute_options(self):
        s = self.player.song
        if not s:
            return [None]
        opts = [None] + s.channels
        if s.melody in opts:                        # ﾒﾛﾃﾞｨらしい ch を最初に
            opts.remove(s.melody)
            opts.insert(1, s.melody)
        return opts

    def set_mute(self, ch):
        self.player.set_mute(ch)
        if ch is not None:
            pg = self.player.song.program.get(ch, 0)
            self.cfg["mel_prog"] = pg
            self.midi.program(CH_MEL, pg)

    def mute_label(self, ch):
        if ch is None:
            return "なし"
        pg = self.player.song.program.get(ch, 0) if self.player.song else 0
        return f"ch{ch + 1} {GM[pg]}"

    # ---- ﾀｲﾐﾝｸﾞ調整
    def open_editor(self):
        p = self.player
        if not p.song:
            self.say("曲がありません")
            return
        ch = p.mute if p.mute is not None else p.song.melody
        if ch is None:
            self.say("調整するﾊﾟｰﾄがありません")
            return
        self.menu = None
        self.panic()
        p.stop()
        self.editor = TimingEditor(self, p.song, ch)
        self.say("Z C で音を選んで F X でずらす。SPACE で前後を聞く", 3)

    def close_editor(self):
        ed = self.editor
        self.editor = None
        self.player.stop()
        self.songs = list_songs(SONG_DIRS)
        target = ed.saved_path or ed.src.path
        if target in self.songs:
            self.load_song(self.songs.index(target), quiet=True)
            if ed.saved_path:
                self.say(f"{os.path.basename(target)} を選びました", 2.5)
        else:
            self.load_song(self.si, quiet=True)

    # ---- 録音 (伴奏に合わせて弾いたﾀｲﾐﾝｸﾞを .mid に残す)
    def toggle_rec(self):
        self.rec_armed = not self.rec_armed
        if self.rec_armed and self.cfg["wait"]:
            self.cfg["wait"] = False                # ﾏﾁ中は曲の時間が止まるので録音とは一緒に使わない
            self.apply_sound()
        self.say("録音 ON: SPACE で最初から再生すると、弾いたﾀｲﾐﾝｸﾞを録ります (ﾏﾁは OFF)"
                 if self.rec_armed else "録音 OFF", 3)

    def rec_note(self, code, n, down):
        p = self.player
        if self.rec is None or not p.playing:
            return
        with p.lock:
            t = p.vt
        if down:
            if code in self.rec_open:
                on, nn = self.rec_open.pop(code)
                self.rec.append((on, t, nn))
            self.rec_open[code] = (t, n)
        elif code in self.rec_open:
            on, nn = self.rec_open.pop(code)
            self.rec.append((on, max(t, on + 0.05), nn))

    def save_rec(self):
        """録った音を、曲の伴奏と一緒に ~/midi/<曲名>_recN.mid に保存 (抜いたﾊﾟｰﾄは録った音に置き換え)"""
        rec, p = self.rec, self.player
        self.rec = None
        if not rec and not self.rec_open or not p.song:
            self.rec_open = {}
            return None
        with p.lock:
            t_end = p.vt
        for on, n in self.rec_open.values():
            rec.append((on, max(t_end, on + 0.05), n))
        self.rec_open = {}
        song = p.song
        used = {m[0] & 0x0F for _, m in song.events}
        ch = p.mute if p.mute is not None else next((c for c in range(16) if c not in used and c != 9), 0)
        ev = [(0.0, bytes([0xC0 | ch, self.cfg["mel_prog"]])), (0.0, bytes([0xB0 | ch, 7, self.cfg["mel_vol"]]))]
        for t, m in song.events:
            if (m[0] & 0x0F) == ch and (m[0] & 0xF0) in (0x80, 0x90, 0xC0):
                continue
            ev.append((t, m))
        for on, off, n in sorted(rec):
            ev.append((on, bytes([0x90 | ch, n, 100])))
            ev.append((off, bytes([0x80 | ch, n, 0])))
        base = song_base(song.path)
        outdir = os.path.expanduser("~/midi")
        os.makedirs(outdir, exist_ok=True)
        i = 1
        while os.path.exists(os.path.join(outdir, f"{base}_rec{i}.mid")):
            i += 1
        path = os.path.join(outdir, f"{base}_rec{i}.mid")
        try:
            write_song(path, ev, f"{base} rec{i}", song.tempo_us)
        except Exception as e:
            self.say(f"録音を保存できません: {e}", 4)
            return None
        cur = song.path
        self.songs = list_songs(SONG_DIRS)
        if cur in self.songs:
            self.si = self.songs.index(cur)
        self.say(f"録音を保存: {os.path.basename(path)} ({len(rec)}音)", 4)
        return path

    # ---- 表示の部品
    def say(self, msg, sec=2.2):
        self.toast = (msg, time.time() + sec)

    def kb_geom(self, start, span):
        whites = [s for s in range(span) if not is_black(start + s)]
        ww = LCD_W / max(1, len(whites))
        geo, wi = {}, 0
        for s in range(span):
            if is_black(start + s):
                x = wi * ww
                geo[s] = (x - ww * 0.32, x + ww * 0.32, True)
            else:
                geo[s] = (wi * ww, (wi + 1) * ww, False)
                wi += 1
        return geo

    def key_labels(self):
        """音 → キー文字 (鍵盤に書く)"""
        out = {}
        if self.cfg["mode"] == "SCALE":
            for c, n in zip(SCALE_KEYS, self.scale_list):
                out.setdefault(n, c.upper())
        else:
            base = 48 + 12 * self.cfg["oct"]
            for code, semi in FREE_LAYOUTS[self.cfg["layout"]][0].items():
                out.setdefault(base + semi, CODE_CH[code].upper())
        return out

    def keyboard_img(self):
        start, span = self.kb_range()
        key = (self.cfg["mode"], self.cfg["layout"], start, span, tuple(self.scale_list))
        if self._kb_cache and self._kb_cache[0] == key:
            return self._kb_cache[1], self._kb_cache[2], start, span
        h = LCD_H - BOT_H - KB_Y
        img = Image.new("RGB", (LCD_W, h), (0, 0, 0))
        d = ImageDraw.Draw(img)
        geo = self.kb_geom(start, span)
        labels = self.key_labels()
        in_scale = set(self.scale_list) if self.cfg["mode"] == "SCALE" else None
        for s, (x0, x1, black) in geo.items():
            if black:
                continue
            n = start + s
            used = in_scale is None or n in in_scale
            d.rectangle((x0 + 0.5, 0, x1 - 0.5, h - 1), fill=(220, 224, 232) if used else (110, 114, 126))
            if n % 12 == 0:
                d.text((x0 + 2, 1), note_name(n), fill=(150, 150, 165), font=self.font_k)
            lb = labels.get(n)
            if lb:
                d.text(((x0 + x1) / 2 - self.font_k.getlength(lb) / 2, h - 12), lb,
                       fill=(60, 64, 80), font=self.font_k)
        for s, (x0, x1, black) in geo.items():
            if not black:
                continue
            n = start + s
            used = in_scale is None or n in in_scale
            d.rectangle((x0, 0, x1, h * 0.58), fill=(28, 30, 38) if used else (52, 54, 62))
            if used and in_scale is not None:
                d.rectangle((x0 + 2, 1, x1 - 2, 3), fill=MODE_COL["SCALE"])
            lb = labels.get(n)
            if lb:
                d.text(((x0 + x1) / 2 - self.font_k.getlength(lb) / 2, h * 0.58 - 12), lb,
                       fill=(170, 175, 190), font=self.font_k)
        self._kb_cache = (key, img, geo)
        return img, geo, start, span

    @staticmethod
    def fold(n, start, span):
        """曲の音を鍵盤の範囲に入るオクターブへ寄せる"""
        s = n - start
        while s < 0:
            s += 12
        while s >= span:
            s -= 12
        return s if s >= 0 else None

    # ---- メイン画面
    def draw(self):
        if self.editor is not None:
            return self.editor.draw()
        if self.menu is not None:
            return self.draw_menu()
        img = self.bg.copy()
        d = ImageDraw.Draw(img)
        kb, geo, start, span = self.keyboard_img()
        p, song, now = self.player, self.player.song, time.time()
        roll_top, roll_bot = TOP_H + 1, KB_Y - 1
        rh = roll_bot - roll_top
        with p.lock:
            vt, mute, playing, waiting = p.vt, p.mute, p.playing, p.waiting
            hit, ci, clusters = set(p.hit), p.ci, p.clusters
        ctimes = [c[0] for c in clusters]
        d.line((0, roll_bot, LCD_W, roll_bot), fill=ACC_DIM)
        if song and (playing or vt > 0):
            for ch in ([mute] if mute is not None else song.channels):
                notes = song.notes.get(ch, [])
                starts = [a for a, _, _ in notes]
                for on, off, n in notes[bisect.bisect_left(starts, vt - 8):bisect.bisect_right(starts, vt + ROLL_SEC)]:
                    if off < vt:
                        continue
                    s = self.fold(n, start, span)
                    if s is None or s not in geo:
                        continue
                    x0, x1, _ = geo[s]
                    ya = min(roll_bot - (on - vt) / ROLL_SEC * rh, roll_bot)
                    yb = max(roll_bot - (off - vt) / ROLL_SEC * rh, roll_top)
                    if ya <= roll_top:
                        continue
                    if mute is not None:
                        k = bisect.bisect_left(ctimes, on - 0.04)
                        col = OK_COL if k in hit else GUIDE_COL
                        d.rounded_rectangle((x0 + 1, yb + 1, x1 - 1, max(yb + 2, ya - 1)), 2, fill=col)
                    else:
                        d.rectangle((x0 + 3, yb, x1 - 3, ya), fill=(70, 75, 95))
        else:
            self.draw_help(d, roll_top)
        img.paste(kb, (0, KB_Y))
        d = ImageDraw.Draw(img)

        def mark(n, col, full=True):
            s = self.fold(n, start, span)
            if s is None or s not in geo:
                return
            x0, x1, black = geo[s]
            hh = (LCD_H - BOT_H - KB_Y) * (0.58 if black else 1) - 1
            if full:
                d.rectangle((x0 + 1, KB_Y + 1, x1 - 1, KB_Y + hh), fill=col)
            else:
                d.rectangle((x0 + 1, KB_Y + 1, x1 - 1, KB_Y + hh), outline=col, width=2)
        for n in self.chord.sounding:
            if start <= n < start + span:
                mark(n, CHORD_COL)
        for n in self.ext:
            mark(n, EXT_COL)
        for n in self.mel.sounding():
            mark(n, ACC)
        if waiting and ci < len(clusters):
            for n in clusters[ci][1]:
                mark(n, GUIDE_COL, full=False)
        for n, until in list(self.flash.items()):
            if until < now:
                del self.flash[n]
                continue
            s = self.fold(n, start, span)
            if s in geo:
                x0, x1, _ = geo[s]
                cx = (x0 + x1) / 2
                d.ellipse((cx - 6, roll_bot - 14, cx + 6, roll_bot - 2), outline=OK_COL, width=2)
        self.draw_chord_state(d, roll_top)
        if waiting:
            t = "まってるよ ♪"
            w = self.font_t.getlength(t) + 14
            d.rounded_rectangle((4, TOP_H + 4, 4 + w, TOP_H + 20), 6, fill=(60, 50, 10), outline=GUIDE_COL)
            d.text((11, TOP_H + 5), t, fill=GUIDE_COL, font=self.font_t)
        return self.chrome(img)

    def draw_chord_state(self, d, y):
        st = self.chord.state()
        if not st or self.cfg["mode"] != "SCALE":
            return
        name, tag = st
        col = {"KEEP": OK_COL, "KEEP>": OK_COL, "hold": GUIDE_COL, "ring": (255, 150, 60)}[tag]
        t = f"{name}  {tag}"
        w = self.font_t.getlength(t) + 14
        x = LCD_W - w - 4
        d.rounded_rectangle((x, y + 4, x + w, y + 20), 6, fill=(30, 24, 50), outline=col)
        d.text((x + 7, y + 5), t, fill=col, font=self.font_t)

    def draw_help(self, d, y):
        if self.cfg["mode"] == "SCALE":
            pc, sc, src = self.effective_key()
            d.text((8, y + 3), f"{NOTE_NAMES[pc]} {sc}  ({src})", fill=MODE_COL["SCALE"], font=self.font_s)
            for i, k in enumerate(CHORD_KEYS):
                c = self.chord.chords.get(k)
                x, yy = 8 + (i % 5) * 62, y + 20 + (i // 5) * 15
                d.text((x, yy), k, fill=ACC, font=self.font_s)
                d.text((x + 10, yy), c.name if c else "-", fill=FG if c else SUB, font=self.font_s)
            hold = self.cfg["hold"]
            ht = "残さない" if hold == 0 else "常に残す" if hold < 0 else f"{hold}ms 以上押して離すと残る"
            if self.preset():
                hi = " ".join(f"{k.upper()}:{c.name}" for k, c in self.chord.chords.items() if k in CHORD_HI_KEYS)
                d.text((8, y + 52), f"A段 {hi or 'なし'}", fill=SUB, font=self.font_s)
                d.text((LCD_W - 8 - self.font_s.getlength(ht), y + 68), ht, fill=SUB, font=self.font_s)
                return
            d.text((8, y + 52), f"A段 = 1オクターブ上   コード: {ht}", fill=SUB, font=self.font_s)
            d.text((8, y + 68), "TAB:FREE へ  ENTER:設定  SPACE:伴奏", fill=SUB, font=self.font_s)
            return
        lines = [("SPACE", "伴奏 再生/停止"), ("TAB", "SCALE モードへ"),
                 ("BS", "次の曲"), ("ALT", "ﾏﾁ (弾くまで待つ)"),
                 ("ENTER", "設定"), ("CTRL", "音を全部止める"),
                 ("↑↓", "オクターブ"), ("←→", "音色"), ("SHIFT", "サステイン"), ("ESC", "終了")]
        for i, (k, v) in enumerate(lines):
            row, col = divmod(i, 2)
            x, yy = 8 + col * 156, y + 4 + row * 17
            d.text((x, yy), k, fill=ACC, font=self.font_s)
            d.text((x + 40, yy), v, fill=SUB, font=self.font_s)

    def chrome(self, img):
        d = ImageDraw.Draw(img)
        p = self.player
        d.rectangle((0, 0, LCD_W, TOP_H - 1), fill=(0, 0, 0))
        d.text((5, 1), "HEXA MIDI", fill=ACC, font=self.font_bs)
        x = 5 + self.font_bs.getlength("HEXA MIDI") + 8
        mode = self.cfg["mode"]
        mw = 50
        d.rectangle((LCD_W - mw, 0, LCD_W, TOP_H - 1), fill=MODE_COL[mode])
        d.text((LCD_W - mw / 2 - self.font_bs.getlength(mode) / 2, 1), mode, fill=(0, 0, 0), font=self.font_bs)
        right = LCD_W - mw - 6
        if p.song:
            st = "■" if not p.playing else ("待" if p.waiting else "▶")
            d.text((x, 3), st, fill=GUIDE_COL if p.waiting else FG, font=self.font_t)
            frac = min(1.0, p.vt / p.song.length) if p.song.length else 0
            d.rectangle((right - 34, 7, right, 10), fill=(40, 44, 56))
            d.rectangle((right - 34, 7, right - 34 + 34 * frac, 10), fill=ACC)
            name = p.song.name
            room = right - 40 - (x + 16)
            while name and self.font_t.getlength(name) > room:
                name = name[:-1]
            d.text((x + 16, 3), name, fill=FG, font=self.font_t)
        else:
            d.text((x, 3), "曲なし", fill=SUB, font=self.font_t)
        # 下のバー
        d.rectangle((0, LCD_H - BOT_H, LCD_W, LCD_H), fill=(0, 0, 0))
        c = self.cfg
        parts = [(GM[c["mel_prog"]], FG), (VOICE_JP[c["voice"]], SUB),
                 (note_name(48 + 12 * c["oct"]) if mode == "FREE" else note_name(self.scale_list[0]), SUB)]
        if p.mute is not None:
            parts.append((f"抜く ch{p.mute + 1}", GUIDE_COL))
        if p.wait_mode:
            parts.append(("ﾏﾁ", GUIDE_COL))
        if p.clusters:
            parts.append((f"✓{len(p.hit)}/{len(p.clusters)}", OK_COL))
        if self.mel.sustain:
            parts.append(("SUS", ACC))
        if self.rec_armed:
            parts.insert(0, ("●REC" if self.rec is not None and p.playing else "●", (255, 70, 70)))
        xx = 4
        for txt, col in parts:
            d.text((xx, LCD_H - BOT_H + 2), txt, fill=col, font=self.font_s)
            xx += self.font_s.getlength(txt) + 8
        if not self.midi.ok:
            mi, mcol = "MIDI ×", (255, 110, 90)
        elif not c["thru"]:
            mi, mcol = "IN off", (90, 98, 115)
        else:
            mi = f"{self.midi.duty:.0f}%"
            mcol = EXT_COL if self.ext else ((120, 200, 140) if self.midi.duty < 50 else
                                             GUIDE_COL if self.midi.duty < 68 else (255, 110, 90))
        d.text((LCD_W - 4 - self.font_s.getlength(mi), LCD_H - BOT_H + 2), mi, fill=mcol, font=self.font_s)
        self.draw_toast(d)
        return img

    def draw_toast(self, d, y=None):
        msg, until = self.toast
        if msg and time.time() < until:
            f = self.font_t
            w = min(int(f.getlength(msg)) + 16, LCD_W - 8)
            yy = KB_Y - 22 if y is None else y
            d.rounded_rectangle(((LCD_W - w) // 2, yy, (LCD_W + w) // 2, yy + 17), 7,
                                fill=(70, 16, 48), outline=ACC)
            d.text(((LCD_W - w) // 2 + 8, yy + 2), msg, fill=(255, 255, 255), font=f)

    # ---- 設定画面
    def menu_items(self):
        """[(見出し,) | (id, ラベル, 値の文字, 変える関数(dir))]"""
        c, p = self.cfg, self.player
        pc, sc, src = self.effective_key()

        def cyc(lst, cur, d):
            i = lst.index(cur) if cur in lst else 0
            return lst[(i + d) % len(lst)]

        def setv(key, val, aud=None, refresh=False):
            c[key] = val
            self.apply_sound()
            if refresh:
                self.refresh_music()
            if aud:
                self.audition(aud)

        hold = c["hold"]
        items = [("曲",),
                 ("song", "曲", p.song.name if p.song else "なし",
                  lambda d: self.load_song(self.si + d, quiet=True) if self.songs else None),
                 ("mute", "抜くパート", self.mute_label(p.mute),
                  lambda d: self.set_mute(cyc(self.mute_options(), p.mute, d))),
                 ("wait", "ﾏﾁ (弾くまで待つ)", "ON" if c["wait"] else "OFF", lambda d: setv("wait", not c["wait"])),
                 ("rec", "録音 (最初から再生すると録る)", "ON ●" if self.rec_armed else "OFF",
                  lambda d: self.toggle_rec()),
                 ("edit", "ﾀｲﾐﾝｸﾞ調整 (→ で開く)",
                  f"ch{(p.mute if p.mute is not None else (p.song.melody if p.song and p.song.melody is not None else 0)) + 1}",
                  lambda d: self.open_editor() if d > 0 else None),
                 ("tempo", "テンポ", f"{c['tempo']}%",
                  lambda d: setv("tempo", max(50, min(150, c["tempo"] + 5 * d)))),
                 ("メロディ",),
                 ("mel_prog", "音色", f"{c['mel_prog'] + 1} {GM[c['mel_prog']]}",
                  lambda d: setv("mel_prog", (c["mel_prog"] + d) % 128, "main")),
                 ("mel_vol", "音量", str(c["mel_vol"]),
                  lambda d: setv("mel_vol", max(0, min(127, c["mel_vol"] + 4 * d)), "main")),
                 ("voice", "発音", VOICE_JP[c["voice"]],
                  lambda d: setv("voice", cyc(MelodyEngine.MODES, c["voice"], d))),
                 ("glide_ms", "グライド時間 (レガート)", f"{c['glide_ms']} ms",
                  lambda d: setv("glide_ms", max(40, min(600, c["glide_ms"] + 20 * d)))),
                 ("layout", "FREE の配列", c["layout"],
                  lambda d: (setv("layout", cyc(list(FREE_LAYOUTS), c["layout"], d)), self.mel.all_off())),
                 ("スケール (SCALE モード)",),
                 ("key", "キー", ("自動" if c["key"] == "auto" else c["key"]) + f"  → {NOTE_NAMES[pc]} ({src})",
                  lambda d: setv("key", cyc(["auto"] + NOTE_NAMES, c["key"], d), "sub", True)),
                 ("scale", "スケール", sc if src != "コードセット" else f"{sc} (コードセット)",
                  lambda d: setv("scale", cyc(SCALE_ORDER, c["scale"], d), "sub", True)),
                 ("コード (SCALE モードの数字/A段)",),
                 ("chordset", "コードセット", "自動 (スケールから)" if c["chordset"] == "auto" else c["chordset"],
                  lambda d: setv("chordset", cyc(["auto"] + [x.name for x in self.presets], c["chordset"], d),
                                 "sub", True)),
                 ("hold", "ホールド判定",
                  "残さない" if hold == 0 else "常に残す" if hold < 0 else f"{hold} ms 以上で残す",
                  lambda d: setv("hold", cyc(HOLD_OPTS, c["hold"], d))),
                 ("voicing", "ボイシング (自動)", c["voicing"],
                  lambda d: setv("voicing", cyc(["厚め", "ふつう"], c["voicing"], d), "sub", True)),
                 ("chord_prog", "音色", f"{c['chord_prog'] + 1} {GM[c['chord_prog']]}",
                  lambda d: setv("chord_prog", (c["chord_prog"] + d) % 128, "sub")),
                 ("chord_vol", "音量", str(c["chord_vol"]),
                  lambda d: setv("chord_vol", max(0, min(127, c["chord_vol"] + 4 * d)), "sub")),
                 ("MIDI",),
                 ("thru", "MIDI IN スルー", "ON" if c["thru"] else "OFF",
                  lambda d: (setv("thru", not c["thru"]), self.ext.clear())),
                 ("reverb", "リバーブ", str(c["reverb"]),
                  lambda d: setv("reverb", max(0, min(127, c["reverb"] + 8 * d)), "main")),
                 ("info", "送信", f"帯域 {self.midi.duty:.0f}%  滞留ピーク {self.midi.p0_peak}", None)]
        return items

    def menu_selectable(self, items):
        return [i for i, it in enumerate(items) if len(it) > 1 and it[3] is not None]

    def draw_menu(self):
        img = Image.new("RGB", (LCD_W, LCD_H), BG)
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, LCD_W, TOP_H - 1), fill=(0, 0, 0))
        d.text((5, 1), "HEXA MIDI", fill=ACC, font=self.font_bs)
        d.text((5 + self.font_bs.getlength("HEXA MIDI") + 8, 3), "設定", fill=FG, font=self.font_t)
        items = self.menu_items()
        rows_h, y0 = 15, TOP_H + 2
        nvis = (LCD_H - BOT_H - y0) // rows_h
        if self.menu < self.menu_top + 1:
            self.menu_top = max(0, self.menu - 1)
        if self.menu >= self.menu_top + nvis - 1:
            self.menu_top = min(self.menu - nvis + 2, max(0, len(items) - nvis))
        for r in range(nvis):
            i = self.menu_top + r
            if i >= len(items):
                break
            it = items[i]
            y = y0 + r * rows_h
            if len(it) == 1:
                d.text((6, y + 1), it[0], fill=ACC, font=self.font_s)
                d.line((8 + self.font_s.getlength(it[0]) + 4, y + 8, LCD_W - 8, y + 8), fill=ACC_DIM)
                continue
            sel = i == self.menu
            if sel:
                d.rounded_rectangle((3, y, LCD_W - 3, y + rows_h - 1), 4, fill=(40, 30, 50), outline=ACC)
            d.text((14, y + 1), it[1], fill=FG if sel else SUB, font=self.font_s)
            val = it[2]
            vx = 150
            if sel and it[3] is not None:
                d.text((vx - 10, y + 1), "◀", fill=ACC, font=self.font_s)
                d.text((LCD_W - 14, y + 1), "▶", fill=ACC, font=self.font_s)
            while val and self.font_s.getlength(val) > LCD_W - vx - 18:
                val = val[:-1]
            d.text((vx, y + 1), val, fill=(255, 255, 255) if sel else (190, 196, 210), font=self.font_s)
        d.rectangle((0, LCD_H - BOT_H, LCD_W, LCD_H), fill=(0, 0, 0))
        d.text((4, LCD_H - BOT_H + 2), "↑↓:選ぶ  ←→:変える  ENTER/ESC:もどる (保存)",
               fill=(210, 210, 210), font=self.font_s)
        self.draw_toast(d, LCD_H - BOT_H - 20)
        return img

    def menu_key(self, code):
        items = self.menu_items()
        sel = self.menu_selectable(items)
        ch = CODE_CH.get(code)
        if code in (K_ENTER, K_ESC, K_TAB):
            self.menu = None
            if self.save:
                save_config(self.cfg)
            self.say("設定を保存しました", 1.2)
            return
        if code == K_UP or ch == "f":
            j = sel.index(self.menu) if self.menu in sel else 0
            self.menu = sel[(j - 1) % len(sel)]
        elif code == K_DOWN or ch == "x":
            j = sel.index(self.menu) if self.menu in sel else 0
            self.menu = sel[(j + 1) % len(sel)]
        elif code in (K_LEFT, K_RIGHT) or ch in ("z", "c"):
            fn = items[self.menu][3]
            if fn:
                fn(1 if code == K_RIGHT or ch == "c" else -1)

    # ---- ループ
    def on_key(self, code, val):
        if self.editor is not None:
            if val and self.editor.key(code) == "close":
                self.close_editor()
            return None
        if self.menu is not None:
            if val:
                self.menu_key(code)
            return None
        c, p = self.cfg, self.player
        if self.cfg["mode"] == "SCALE" and code in CHORD_CODES:
            k = CHORD_CODES[code]
            if val:
                if not self.chord.key_down(k):
                    self.say(f"{k} にはコードがありません", 1.2)
            else:
                self.chord.key_up(k)
            return None
        n = self.note_for(code)
        if n is not None:
            if val:
                self.mel.key_down(code, n)
                self.answer(n)
                self.rec_note(code, n, True)
            else:
                self.mel.key_up(code)
                self.rec_note(code, n, False)
            return None
        if code in (K_LSHIFT, K_RSHIFT):
            self.mel.set_sustain(bool(val))
            return None
        if not val:
            return None
        if code == K_ESC:
            return "ESC"
        if code == K_ENTER:
            self.mel.all_off()
            items = self.menu_items()
            self.menu = self.menu_selectable(items)[0]
            self.menu_top = 0
            self.toast = ("", 0.0)
        elif code == K_TAB:
            self.panic()
            c["mode"] = "SCALE" if c["mode"] == "FREE" else "FREE"
            self._kb_cache = None
            if c["mode"] == "SCALE":
                pc, sc, _ = self.effective_key()
                self.say(f"SCALE: {NOTE_NAMES[pc]} {sc}  数字/A段 = コード")
            else:
                self.say(f"FREE: {c['layout']} 配列")
        elif code == K_SPACE:
            if p.playing:
                p.stop(rewind=False)
                self.say("一時停止 (SPACE で続き)")
            else:
                if self.rec_armed and p.song and (p.vt == 0 or p.ended):
                    self.rec, self.rec_open = [], {}
                    self.say("● 録音中 (曲の終わりで保存)", 2)
                p.start()
        elif code == K_BS:
            self.save_rec()
            self.load_song(self.si + 1)
        elif code in (K_LALT, K_RALT):
            c["wait"] = not c["wait"]
            if c["wait"] and p.mute is None and p.song:
                opts = self.mute_options()
                if len(opts) > 1:
                    self.set_mute(opts[1])
            self.apply_sound()
            if not c["wait"]:
                with p.lock:
                    p.waiting = False
                    p._t = time.monotonic()
            self.say(f"ﾏﾁ ON: {self.mute_label(p.mute)} を弾くまで待ちます" if c["wait"] else "ﾏﾁ OFF")
        elif code in (K_LCTRL, K_RCTRL):
            self.panic()
            self.say("音を全部止めました", 1.2)
        elif code in (K_UP, K_DOWN):
            self.panic()
            c["oct"] = max(-3, min(3, c["oct"] + (1 if code == K_UP else -1)))
            self.refresh_music()
            lo, _ = self.kb_range()
            self.say(f"オクターブ {c['oct']:+d}  ({note_name(lo)} から)")
        elif code in (K_LEFT, K_RIGHT):
            fav = FAVORITES
            i = fav.index(c["mel_prog"]) if c["mel_prog"] in fav else 0
            c["mel_prog"] = fav[(i + (1 if code == K_RIGHT else -1)) % len(fav)]
            self.midi.program(CH_MEL, c["mel_prog"])
            self.say(f"音色: {GM[c['mel_prog']]}", 1.5)
        return None

    def confirm_quit(self):
        base = self.draw()
        img = Image.blend(base, Image.new("RGB", base.size), 0.65)
        d = ImageDraw.Draw(img)
        x0, y0, x1, y1 = 40, 42, LCD_W - 40, LCD_H - 38
        d.rounded_rectangle((x0, y0, x1, y1), 8, fill=(26, 30, 38), outline=ACC, width=2)
        for i, (txt, font, col) in enumerate([("HEXA MIDI を終了しますか?", self.font, FG),
                                              ("設定は保存されます", self.font_t, SUB),
                                              ("ENTER: 終了    ESC: 戻る", self.font_s, ACC)]):
            d.text(((LCD_W - font.getlength(txt)) / 2, y0 + 12 + i * 22), txt, fill=col, font=font)
        self.fb.show(img)
        while True:
            for code, val in self.keys.poll(0.5):
                if val and code == K_ENTER:
                    return True
                if val and code == K_ESC:
                    return False

    def run(self):
        self.keys.flush(0.3)
        if not self.midi.ok:
            self.say("MIDI につながっていません (--probe で確認)", 4)
        last, was_end = 0.0, False
        while True:
            for code, val in self.keys.poll(0.004):
                if self.on_key(code, val) == "ESC":
                    self.panic()
                    if self.confirm_quit():
                        return "ESC"
            self.handle_in()
            self.audition_tick()
            if self.player.ended and not was_end:
                p = self.player
                self.say(f"おわり ✓{len(p.hit)}/{len(p.clusters)}" if p.clusters else "おわり", 4)
                self.save_rec()
            was_end = self.player.ended
            now = time.time()
            busy = (self.player.playing or self.mel.sounding() or self.ext or self.flash or self.chord.sounding
                    or now < self.toast[1] + 0.3 or self.menu is not None or self.editor is not None)
            if now - last > (0.066 if busy else 0.5):
                self.fb.show(self.draw())
                last = now

    def close(self):
        self.save_rec()
        self.panic()
        self.player.stop()
        self.player.close()
        if self.save:
            save_config(self.cfg)


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

    def __init__(self, script, fb, inbox):
        self.script, self.fb, self.inbox, self.t = list(script), fb, inbox, time.time()

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
                self.inbox.append(bytes(ev[1]))
            else:
                out.append(ev)
        if not self.script and not out:
            out.append((K_ESC, 1))
        return out

    def flush(self, sec=0):
        pass


class SimMidi(MidiPort):
    def __init__(self):
        self.log = []
        self.inbox = []
        super().__init__(writer=lambda b: self.log.append((time.monotonic(), b)))

    def receive(self):
        out, self.inbox[:] = list(self.inbox), []
        return out


class SimApp(App):
    def confirm_quit(self):
        return True


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
    script = [(0.5, ("SNAP", "01_free_home.png")),
              (0.1, P("z")), (0.05, P("c")), (0.05, P("b")), (0.3, ("SNAP", "02_free_chord.png")),
              (0.1, R("z")), (0.0, R("c")), (0.0, R("b")),
              (0.1, (K_TAB, 1)), (0.3, ("SNAP", "03_scale_home.png")),
              # ホールド判定: 1 を短く (消える) → 2 を長く (残る)
              (0.1, P("1")), (0.1, R("1")), (0.1, ("SNAP", "04_short.png")),
              (0.1, P("2")), (0.5, ("SNAP", "05_keep_pending.png")), (0.0, R("2")),
              (0.2, ("SNAP", "06_kept.png")),
              (0.1, P("q")), (0.2, ("SNAP", "07_scale_melody.png")), (0.1, R("q")),
              (0.1, (K_LCTRL, 1)),
              # 設定画面: 発音をレガートに、ｸﾞﾗｲﾄﾞ
              (0.2, (K_ENTER, 1)), (0.2, ("SNAP", "08_menu.png")),
              ] + [(0.05, (K_DOWN, 1))] * 7 + [
              (0.05, (K_RIGHT, 1)), (0.05, (K_RIGHT, 1)), (0.2, ("SNAP", "09_menu_legato.png")),
              ] + [(0.05, (K_DOWN, 1))] * 6 + [
              (0.05, (K_RIGHT, 1)), (0.2, ("SNAP", "10_menu_chordset.png")),
              (0.05, (K_LEFT, 1)),
              (0.1, (K_ENTER, 1)),
              # レガート: z を押したまま q へ (グライド)
              (0.3, P("z")), (0.1, P("q")), (0.08, ("SNAP", "11_glide.png")),
              (0.4, R("q")), (0.4, R("z")),
              # 伴奏 + ﾏﾁ
              (0.1, (K_LALT, 1)), (0.1, (K_SPACE, 1)), (3.0, ("SNAP", "12_scale_wait.png")),
              (0.1, P("1")), (0.6, ("SNAP", "13_chord_over_song.png")), (0.0, R("1")),
              (0.2, (K_SPACE, 1)), (0.3, (K_TAB, 1)), (0.3, ("SNAP", "14_back_free.png"))]
    keys = SimKeys(script, fb, midi.inbox)
    cfg = dict(DEFAULTS)
    app = SimApp(fb, keys, midi, cfg=cfg, save=False)
    app.run()
    app.close()
    midi.close()
    t0 = midi.log[0][0]
    with open(os.path.join(outdir, "sent.txt"), "w") as f:
        for t, b in midi.log:
            f.write(f"{t - t0:8.3f}  {b.hex(' ')}\n")
    print("sim done:", outdir, "writes", len(midi.log))


# ================================================================ probe
def probe():
    print(f"HEXA MIDI {VERSION}")
    port = find_port()
    print("UART:", port, "->", os.path.realpath(port) if port else None)
    for cfg in ("/boot/firmware/config.txt", "/boot/config.txt"):
        if os.path.exists(cfg):
            lines = [ln.strip() for ln in open(cfg) if ln.strip().startswith(("enable_uart", "dtoverlay"))]
            print(f"{cfg}:", ", ".join(lines) or "(enable_uart なし)")
            break
    for cl in ("/boot/firmware/cmdline.txt", "/boot/cmdline.txt"):
        if os.path.exists(cl):
            bad = [w for w in open(cl).read().split()
                   if w.startswith("console=") and any(s in w for s in ("serial", "ttyS", "ttyAMA"))]
            print("シリアルコンソール:", " ".join(bad) if bad else "なし (OK)")
            break
    m = MidiPort(port)
    if not m.ser:
        print("開けません:", m.err)
        print("→ sh install.sh を実行して再起動、ユーザーが dialout グループに入っているか確認")
        return 1
    print("31250bps で開きました。ch16 でドミソを鳴らします (Unit MIDI のｲﾔﾎﾝ端子から聞こえればOK)")
    got = []

    def listen(sec):
        end = time.time() + sec
        while time.time() < end:
            got.extend(m.receive())
            time.sleep(0.01)
    m.receive()                                   # 前に溜まっていた分を捨てる
    got.clear()
    m.program(CH_MEL, 0)
    m.cc(CH_MEL, 7, 110)
    for n in (60, 64, 67, 72):
        m.note_on(CH_MEL, n, 100)
        listen(0.25)
        m.note_off(CH_MEL, n)
    listen(0.2)
    print("MIDI IN を3秒聞きます (Bypass にして外の鍵盤を弾いてみてください)...")
    end = time.time() + 3
    while time.time() < end:
        got += m.receive()
        time.sleep(0.02)
    print("受信 (外の鍵盤):", " / ".join(b.hex(" ") for b in got[:10]) if got else "なし")
    if m.echo_count:
        print(f"※ 送ったﾒｯｾｰｼﾞが {m.echo_count} 個そのまま戻ってきました (ｴｺｰ)。UART の速度は合っています。")
        print("  Unit MIDI に電源が来ていない (Grove 5V が OFF) 時などに起きます。ｱﾌﾟﾘではｴｺｰは無視します")
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
        midi.close()
        keys.close()
        fb.clear()
        if paused:
            signal_launcher(signal.SIGCONT)


if __name__ == "__main__":
    main()
