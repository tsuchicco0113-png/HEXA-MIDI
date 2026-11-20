"""HEXA MIDI — 曲 (.mid) の読み込みと伴奏の再生 (別スレッド)"""
import bisect
import glob
import os
import struct
import threading
import time

EARLY_OK = 0.30                           # ﾏﾁ: この秒数までは早弾きも正解にする


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


def list_songs(dirs):
    out = []
    for d in dirs:
        out += glob.glob(os.path.join(d, "*.mid")) + glob.glob(os.path.join(d, "*.MID"))
        out += glob.glob(os.path.join(d, "*.midi"))
    return sorted(set(out), key=lambda p: os.path.basename(p).lower())


# ================================================================ 伴奏の再生 (別スレッド)
class Player:
    def __init__(self, midi, own_channels=()):
        self.midi = midi
        self.own = set(own_channels)        # 自分の演奏に使う ch (止める時に触らない)
        self.tempo = 1.0
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
                self.midi.raw([0xB0 | self.mute, 123, 0])
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
                self.midi.all_off([c for c in range(16) if c not in self.own])

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
            early = 1.5 if self.wait_mode else EARLY_OK       # ﾏﾁの時は先に弾いてもよい
            for j in range(self.ci, len(self.clusters)):    # 少し早く弾いた分も正解
                t, notes = self.clusters[j]
                if t - self.vt > early:
                    break
                if j in self.hit:
                    continue
                if n % 12 in {x % 12 for x in notes}:
                    self.hit.add(j)
                    return True
                if self.wait_mode:                           # 次の音が違えばそこで止める
                    break
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
                        self.vt += (now - self._t) * self.tempo
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
                            self.midi.raw(msg)
                        self.idx += 1
                    if self.idx >= len(ev):
                        self.missed = max(0, len(self.clusters) - len(self.hit))
                        self.playing = self.waiting = False
                        self.ended = True
                        self.midi.all_off([c for c in range(16) if c not in self.own])
                    elif not self.waiting:
                        sleep = max(0.0005, min(0.01, (ev[self.idx][0] - self.vt) / max(0.1, self.tempo)))
            time.sleep(sleep)

    def close(self):
        self._stop = True


