"""HEXA MIDI — 音の仕組み (Aegis-Synth M3.5 から移植して整理)

  スケール   : SCALE モードの鍵盤 (外れない鍵盤)
  コード     : ホールド判定つきのコード伴奏 (短く押す=消える / 長く押して離す=残る)
  メロディ   : ポリ / モノ / レガート(ピッチベンドのグライド)
"""
import glob
import json
import os
import threading
import time

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(n):
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def is_black(n):
    return n % 12 in (1, 3, 6, 8, 10)


# GM 音色名 (Aegis-Synth の表)
GM = ["Ac Grand Pf", "Bright Pf", "El Grand Pf", "Honky Pf", "El Piano 1", "El Piano 2", "Harpsi", "Clavi",
      "Celesta", "Glocken", "Music Box", "Vibra", "Marimba", "Xylo", "Tub Bells", "Dulcimer",
      "Drawbar Org", "Perc Org", "Rock Org", "Church Org", "Reed Org", "Accordion", "Harmonica", "Tango Acc",
      "Guitar nylon", "Guitar steel", "Guitar jazz", "Guitar clean", "Guitar mute", "Overdrive", "Distortion",
      "Gtr Harm", "Ac Bass", "Fingr Bass", "Pick Bass", "Fretless", "Slap Bass1", "Slap Bass2", "Syn Bass1",
      "Syn Bass2", "Violin", "Viola", "Cello", "Contrabass", "Trem Str", "Pizz Str", "Harp", "Timpani",
      "Str Ens1", "Str Ens2", "SynStr1", "SynStr2", "Choir Aahs", "Voice Oohs", "Syn Voice", "Orch Hit",
      "Trumpet", "Trombone", "Tuba", "Mute Trpt", "Fr Horn", "Brass Sec", "SynBrass1", "SynBrass2",
      "Sop Sax", "Alto Sax", "Tenor Sax", "Bari Sax", "Oboe", "Eng Horn", "Bassoon", "Clarinet",
      "Piccolo", "Flute", "Recorder", "Pan Flute", "Blown Btl", "Shakuhachi", "Whistle", "Ocarina",
      "Lead Square", "Lead Saw", "Lead Callio", "Lead Chiff", "Lead Charang", "Lead Voice", "Lead 5th",
      "Lead Bass", "Pad NewAge", "Pad Warm", "Pad Poly", "Pad Choir", "Pad Bowed", "Pad Metal", "Pad Halo",
      "Pad Sweep", "FX Rain", "FX Sndtrk", "FX Crystal", "FX Atmos", "FX Bright", "FX Goblin", "FX Echo",
      "FX SciFi", "Sitar", "Banjo", "Shamisen", "Koto", "Kalimba", "Bagpipe", "Fiddle", "Shanai",
      "Tinkle", "Agogo", "Steel Drum", "Woodblock", "Taiko", "Melo Tom", "Syn Drum", "Rev Cymbal",
      "Fret Noise", "Breath", "Seashore", "Bird", "Tel Ring", "Helicopter", "Applause", "Gunshot"]
# メイン画面の ←→ で回すお気に入り
FAVORITES = [0, 4, 11, 8, 19, 21, 24, 26, 33, 40, 48, 52, 56, 65, 71, 73, 79, 80, 81, 85, 88, 98, 108, 114]

# ================================================================ スケール
SCALES = {
    "メジャー": [0, 2, 4, 5, 7, 9, 11],
    "マイナー": [0, 2, 3, 5, 7, 8, 10],
    "ハーモニックマイナー": [0, 2, 3, 5, 7, 8, 11],
    "ドリアン": [0, 2, 3, 5, 7, 9, 10],
    "ペンタ": [0, 2, 4, 7, 9],
    "マイナーペンタ": [0, 3, 5, 7, 10],
    "ブルース": [0, 3, 5, 6, 7, 10],
    "琉球": [0, 4, 5, 7, 11],
}
SCALE_ORDER = list(SCALES)

# SCALE モードのメロディ鍵盤: 下の段 Z…M → 上の段 Q…P が地続き (17音)
SCALE_KEYS = "zxcvbnmqwertyuiop"
# コード鍵盤: 数字の段 = 1〜0、A の段 = 数字の 1〜9 の 1オクターブ上
CHORD_KEYS = "1234567890"
CHORD_HI_KEYS = "asdfghjkl"


def scale_notes(root, steps, count=len(SCALE_KEYS)):
    return [root + 12 * (i // len(steps)) + steps[i % len(steps)] for i in range(count)]


# ---- 調の推定 (Krumhansl-Kessler)
_KK_MAJ = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_KK_MIN = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def detect_key(song):
    """曲の音の長さの合計から調を推定 → (主音 0-11, 'メジャー'|'マイナー')"""
    hist = [0.0] * 12
    for ch, notes in song.notes.items():
        if ch == 9:
            continue
        for on, off, n in notes:
            hist[n % 12] += max(0.05, off - on)
    if sum(hist) == 0:
        return 0, "メジャー"
    # 曲の最後 2 秒のいちばん低い音は主音のことが多いので少し足す
    end = max(off for ch, ns in song.notes.items() if ch != 9 for _, off, _ in ns)
    tail = [n for ch, ns in song.notes.items() if ch != 9 for on, _, n in ns if on >= end - 2]
    if tail:
        hist[min(tail) % 12] += 0.25 * sum(hist)

    def corr(prof, k):
        p = [prof[(i - k) % 12] for i in range(12)]
        mx, mp = sum(hist) / 12, sum(p) / 12
        num = sum((h - mx) * (q - mp) for h, q in zip(hist, p))
        den = (sum((h - mx) ** 2 for h in hist) * sum((q - mp) ** 2 for q in p)) ** 0.5
        return num / den if den else 0
    best = max([(corr(_KK_MAJ, k), k, "メジャー") for k in range(12)] +
               [(corr(_KK_MIN, k), k, "マイナー") for k in range(12)])
    return best[1], best[2]


# ================================================================ コード
class ChordDef:
    def __init__(self, name, notes):
        self.name, self.notes = name, list(notes)


def _quality(iv):
    """ルートからの音程の集合 → コード名の後ろ"""
    s = set(iv)
    if {4, 7, 11} <= s:
        return "maj7"
    if {4, 7, 10} <= s:
        return "7"
    if {3, 7, 10} <= s:
        return "m7"
    if {3, 6, 10} <= s:
        return "m7-5"
    if {3, 6, 9} <= s:
        return "dim7"
    if {3, 7, 11} <= s:
        return "mM7"
    if {3, 6} <= s:
        return "dim"
    if {4, 8} <= s:
        return "aug"
    if {3, 7} <= s:
        return "m"
    if {5, 7} <= s:
        return "sus4"
    return ""


def voice(root_pc, intervals, thick=True, center=60):
    """コードの音を並べる。thick: 低音(C2〜B2のルート) + 上に 3〜4 音 (Aegis の極厚)"""
    notes = []
    lo = center - 5                                  # 上の音はこの辺りから 1オクターブに収める
    for iv in intervals:
        pc = (root_pc + iv) % 12
        n = lo + (pc - lo) % 12
        notes.append(n)
    notes = sorted(set(notes))
    if thick:
        notes = [36 + root_pc % 12] + notes
    return notes


def auto_chords(root_pc, scale, thick=True):
    """スケールからコード表を作る: 1〜7 = ダイアトニック 4和音, 8 = V7, 9 = Vsus4, 0 = I add9
    A の段 = 1〜9 の 1オクターブ上。返り値 {キー文字: ChordDef}"""
    steps = SCALES[scale]
    if len(steps) != 7:                              # ペンタ等は親のメジャー/マイナーで作る
        steps = SCALES["マイナー"] if 3 in steps else SCALES["メジャー"]
    out = {}
    for d in range(7):
        r = steps[d]
        iv = sorted({(steps[(d + k) % 7] - r) % 12 for k in ((0, 2, 4, 6) if thick else (0, 2, 4))})
        rp = (root_pc + r) % 12
        out[CHORD_KEYS[d]] = ChordDef(NOTE_NAMES[rp] + _quality(iv), voice(rp, iv, thick))
    v = (root_pc + 7) % 12
    out["8"] = ChordDef(NOTE_NAMES[v] + "7", voice(v, [0, 4, 7, 10], thick))
    out["9"] = ChordDef(NOTE_NAMES[v] + "sus4", voice(v, [0, 5, 7], thick))
    third = 3 if 3 in SCALES[scale] else 4
    out["0"] = ChordDef(NOTE_NAMES[root_pc] + ("m" if third == 3 else "") + "add9",
                        voice(root_pc, [0, third, 7, 14], thick))
    for i, k in enumerate(CHORD_HI_KEYS):
        base = out[CHORD_KEYS[i]]
        out[k] = ChordDef(base.name + "+", [n + 12 for n in base.notes])
    return out


class ChordPreset:
    """chords/*.json  例: {"name": "...", "key": "G#", "scale": "マイナー",
                           "chords": [{"keyboard": "1", "name": "G#m7", "notes": [44,54,59,63]}, ...]}"""

    def __init__(self, path):
        d = json.load(open(path, encoding="utf-8"))
        self.path = path
        self.name = d.get("name") or os.path.splitext(os.path.basename(path))[0]
        self.key = NOTE_NAMES.index(d["key"]) if d.get("key") in NOTE_NAMES else None
        self.scale = d.get("scale") if d.get("scale") in SCALES else None
        self.chords = {}
        for c in d.get("chords", []):
            k = str(c.get("keyboard", "")).lower()
            if k in CHORD_KEYS + CHORD_HI_KEYS and c.get("notes"):
                self.chords[k] = ChordDef(c.get("name", "?"), [int(n) for n in c["notes"]][:8])


def list_presets(dirs):
    out = []
    for d in dirs:
        for p in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                out.append(ChordPreset(p))
            except Exception:
                pass
    return out


class ChordEngine:
    """Aegis M3.5 のホールド判定つきコード (常に 1 つだけ鳴る)
      押す           → 前のコードを消して鳴らす
      hold_ms 未満で離す → 消す (スタッカート)
      hold_ms 以上で離す → 残す (キープ)。次のコードを押すと入れ替わる
      hold_ms = 0 → 残さない / hold_ms < 0 → 常に残す"""

    def __init__(self, midi, ch):
        self.midi, self.ch = midi, ch
        self.chords = {}
        self.hold_ms = 350
        self.vel = 96
        self.sounding = []                 # 鳴っている音
        self.sounding_key = None
        self.pressing_key = None
        self.press_t0 = 0.0
        self.kept = False
        self.sustain = False

    def silence(self):
        if self.sustain and self.sounding:            # ペダル中でも確実に消す
            self.midi.cc(self.ch, 64, 0)
            for n in self.sounding:
                self.midi.note_off(self.ch, n)
            self.midi.cc(self.ch, 64, 127)
        else:
            for n in self.sounding:
                self.midi.note_off(self.ch, n)
        self.sounding, self.sounding_key, self.kept = [], None, False

    def key_down(self, k):
        c = self.chords.get(k)
        if not c:
            return None
        self.silence()
        for n in c.notes:
            if 0 <= n <= 127:
                self.midi.note_on(self.ch, n, self.vel)
        self.sounding, self.sounding_key = list(c.notes), k
        self.pressing_key, self.press_t0 = k, time.monotonic()
        return c

    def key_up(self, k):
        if self.pressing_key != k:
            return
        held = (time.monotonic() - self.press_t0) * 1000
        keep = self.hold_ms < 0 or (self.hold_ms > 0 and held >= self.hold_ms)
        if keep:
            self.kept = True
        else:
            self.silence()
        self.pressing_key = None

    def state(self):
        """画面用: (コード名, 'KEEP'|'KEEP>'|'hold'|'ring') or None"""
        if self.sounding_key is None:
            return None
        c = self.chords.get(self.sounding_key)
        name = c.name if c else "?"
        pressing = self.pressing_key == self.sounding_key
        over = pressing and self.hold_ms != 0 and (
            self.hold_ms < 0 or (time.monotonic() - self.press_t0) * 1000 >= self.hold_ms)
        if self.kept:
            return name, "KEEP"
        if over:
            return name, "KEEP>"
        return name, "hold" if pressing else "ring"


# ================================================================ メロディ
BEND_RANGE = 24
HEADROOM_CT = 2200.0


def bend14(cents):
    x = max(-1.0, min(1.0, cents / (BEND_RANGE * 100.0)))
    return max(0, min(16383, 8192 + int(round(x * 8191))))


def smoothstep(u):
    u = max(0.0, min(1.0, u))
    return u * u * (3 - 2 * u)


class MelodyEngine:
    """POLY  : 押した音を全部鳴らす
       MONO  : 1音ずつ。押すたびに前の音を確実に止めて鳴らし直す
       LEGATO: 1音を鳴らしたまま、ピッチベンドで次の音へ滑らせる (後着優先・離すと前の音へ戻る)
    tick() は送信スレッドから 2ms ごとに呼ばれる"""
    MODES = ["POLY", "MONO", "LEGATO"]

    def __init__(self, midi, ch):
        self.midi, self.ch = midi, ch
        self.lock = threading.RLock()
        self.mode = "POLY"
        self.vel = 100
        self.glide_ms = 200
        self.sustain = False
        self.poly = {}                        # キー → 音
        self.stack = []                       # [(キー, 音)] モノの押している順
        self.voice_on = False
        self.origin = 60
        self.cur = self.start = self.target = 0.0
        self.t0 = 0.0
        self.last_bend = 8192

    # ---- 共通
    def set_mode(self, mode):
        with self.lock:
            self.all_off()
            self.mode = mode

    def sounding(self):
        with self.lock:
            if self.mode == "POLY":
                return set(self.poly.values())
            if not self.voice_on:
                return set()
            return {int(round(self.origin + self.target / 100))}

    def _hard_off(self, n):
        if self.sustain:                     # ペダル中でもモノは前の音を確実に消す
            self.midi.cc(self.ch, 64, 0)
            self.midi.note_off(self.ch, n)
            self.midi.cc(self.ch, 64, 127)
        else:
            self.midi.note_off(self.ch, n)

    def all_off(self):
        with self.lock:
            for n in self.poly.values():
                self.midi.note_off(self.ch, n)
            self.poly.clear()
            self.stack.clear()
            self._voice_stop()

    def set_sustain(self, on):
        with self.lock:
            self.sustain = on
            self.midi.cc(self.ch, 64, 127 if on else 0)

    # ---- モノ/レガート
    def _voice_start(self, n):
        if self.voice_on:
            self._hard_off(self.origin)
        self.origin, self.cur, self.start, self.target = n, 0.0, 0.0, 0.0
        if self.last_bend != 8192:
            self.midi.pitch_bend(self.ch, 8192)
            self.last_bend = 8192
        self.midi.note_on(self.ch, n, self.vel)
        self.voice_on = True

    def _voice_stop(self):
        if self.voice_on:
            self._hard_off(self.origin)
            self.voice_on = False
        if self.last_bend != 8192:
            self.midi.pitch_bend(self.ch, 8192)
            self.last_bend = 8192
        self.cur = self.target = self.start = 0.0

    def _glide_to(self, n):
        self.start, self.target, self.t0 = self.cur, (n - self.origin) * 100.0, time.monotonic()

    def tick(self):
        with self.lock:
            if self.mode != "LEGATO" or not self.voice_on:
                return
            if self.target != self.start:
                u = (time.monotonic() - self.t0) * 1000 / max(1, self.glide_ms)
                if u >= 1:
                    self.cur = self.start = self.target
                else:
                    self.cur = self.start + (self.target - self.start) * smoothstep(u)
            self._send_bend()
            if abs(self.cur) >= HEADROOM_CT:            # ベンドの端に来たら音を打ち直す
                new = max(0, min(127, int(round(self.origin + self.cur / 100))))
                resid = self.origin + self.cur / 100 - new
                old = self.origin
                self.midi.note_on(self.ch, new, self.vel)
                self.origin, self.cur, self.start = new, resid * 100, resid * 100
                self.target -= (new - old) * 100
                self.last_bend = -1
                self._send_bend()
                self._hard_off(old)

    def _send_bend(self):
        b = bend14(self.cur)
        if b != self.last_bend:
            self.midi.pitch_bend(self.ch, b)
            self.last_bend = b

    # ---- キー
    def key_down(self, k, n):
        with self.lock:
            if self.mode == "POLY":
                if k in self.poly:
                    self.midi.note_off(self.ch, self.poly[k])
                self.poly[k] = n
                self.midi.note_on(self.ch, n, self.vel)
            elif self.mode == "MONO":
                self.stack = [(kk, nn) for kk, nn in self.stack if kk != k] + [(k, n)]
                self._voice_start(n)
            else:                                    # LEGATO
                was_empty = not self.stack
                self.stack = [(kk, nn) for kk, nn in self.stack if kk != k] + [(k, n)]
                if was_empty or not self.voice_on:
                    self._voice_start(n)
                else:
                    self._glide_to(n)

    def key_up(self, k):
        with self.lock:
            if self.mode == "POLY":
                n = self.poly.pop(k, None)
                if n is not None:
                    self.midi.note_off(self.ch, n)
                return
            was_top = bool(self.stack) and self.stack[-1][0] == k
            mine = [nn for kk, nn in self.stack if kk == k]
            self.stack = [(kk, nn) for kk, nn in self.stack if kk != k]
            if self.mode == "MONO":
                if was_top and mine and self.voice_on and self.origin == mine[0]:
                    self._voice_stop()
            else:
                if not self.stack:
                    self._voice_stop()
                elif was_top:
                    self._glide_to(self.stack[-1][1])
