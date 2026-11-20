#!/usr/bin/env python3
"""HEXA MIDI のデモ曲を作る (どちらもパブリックドメインの曲)
  twinkle.mid … きらきら星      ch1 ﾒﾛﾃﾞｨ / ch2 ｽﾄﾘﾝｸﾞｽ / ch3 ﾍﾞｰｽ / ch10 ﾄﾞﾗﾑ
  joy.mid     … 歓喜の歌 (ﾍﾞｰﾄｰｳﾞｪﾝ)
usage: python3 make_demo.py OUTDIR"""
import os
import struct
import sys

TPQ = 480
NOTE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
CHORD = {"C": [48, 52, 55], "F": [48, 53, 57], "G": [47, 50, 55], "Am": [48, 52, 57], "Dm": [50, 53, 57]}
ROOT = {"C": 36, "F": 41, "G": 43, "Am": 45, "Dm": 38}


def varlen(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.insert(0, (n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out)


def write_smf(path, events, name, bpm):
    """events: [(tick, bytes)] → format 0"""
    head = [(0, b"\xff\x03" + varlen(len(name.encode())) + name.encode()),
            (0, b"\xff\x51\x03" + struct.pack(">I", int(60_000_000 / bpm))[1:])]
    evs = head + sorted(events, key=lambda e: (e[0], 0 if (e[1][0] & 0xF0) == 0x80 else 1))
    body, last = b"", 0
    for tick, data in evs:
        body += varlen(tick - last) + data
        last = tick
    body += varlen(0) + b"\xff\x2f\x00"
    with open(path, "wb") as f:
        f.write(b"MThd" + struct.pack(">IHHH", 6, 0, 1, TPQ))
        f.write(b"MTrk" + struct.pack(">I", len(body)) + body)


def note(evs, ch, t, dur, n, vel):
    evs.append((t, bytes([0x90 | ch, n, vel])))
    evs.append((t + dur, bytes([0x80 | ch, n, 0])))


def melody(evs, tokens, start=0, base=60):
    """'E4 D8. C2' のような並び。数字=音符 (4=4分, 8=8分, 2=2分)、'.'=付点、'-' は休符"""
    t = start
    for tok in tokens.split():
        dot = tok.endswith(".")
        tok = tok.rstrip(".")
        name, length = tok[0], int(tok[1:])
        dur = TPQ * 4 // length
        if dot:
            dur = dur * 3 // 2
        if name != "-":
            note(evs, 0, t, dur - 20, base + NOTE[name], 100)
        t += dur
    return t


def backing(evs, chords, bars_beats=4):
    """chords: 半小節ごとのコード名"""
    half = TPQ * bars_beats // 2
    for i, c in enumerate(chords):
        t = i * half
        for n in CHORD[c]:
            note(evs, 1, t, half - 10, n + 12, 52)
        note(evs, 2, t, TPQ - 30, ROOT[c], 90)
        note(evs, 2, t + TPQ, TPQ - 30, ROOT[c] + (7 if c != "G" else 12), 80)
    total = len(chords) * half
    for b in range(0, total, TPQ):
        beat = (b // TPQ) % 4
        note(evs, 9, b, 60, 36 if beat in (0, 2) else 38, 90 if beat in (0, 2) else 70)
        note(evs, 9, b, 60, 42, 50)
        note(evs, 9, b + TPQ // 2, 60, 42, 38)
    return total


def programs(evs, mel=0):
    evs += [(0, bytes([0xC0, mel])), (0, bytes([0xC1, 48])), (0, bytes([0xC2, 33])),
            (0, bytes([0xB1, 7, 80])), (0, bytes([0xB2, 7, 100]))]


def twinkle(path):
    evs = []
    programs(evs, 0)
    a = "C4 C4 G4 G4 A4 A4 G2 F4 F4 E4 E4 D4 D4 C2"
    b = "G4 G4 F4 F4 E4 E4 D2 G4 G4 F4 F4 E4 E4 D2"
    t = melody(evs, a, TPQ * 4)                    # 1小節前奏
    t = melody(evs, b, t)
    melody(evs, a, t)
    ca = ["C", "C", "F", "C", "F", "C", "G", "C"]
    cb = ["C", "F", "C", "G", "C", "F", "C", "G"]
    backing(evs, ["C", "C"] + ca + cb + ca + ["C", "C"])
    write_smf(path, evs, "Twinkle Twinkle Little Star", 100)


def joy(path):
    evs = []
    programs(evs, 73)                              # ﾌﾙｰﾄ
    a = "E4 E4 F4 G4 G4 F4 E4 D4 C4 C4 D4 E4 E4. D8 D2"
    a2 = "E4 E4 F4 G4 G4 F4 E4 D4 C4 C4 D4 E4 D4. C8 C2"
    b = "D4 D4 E4 C4 D4 E8 F8 E4 C4 D4 E8 F8 E4 D4 C4 D4 G2"
    t = melody(evs, a, TPQ * 4)
    t = melody(evs, a2, t)
    t = melody(evs, b, t)
    melody(evs, a2, t)
    ca = ["C", "C", "G", "G", "C", "C", "G", "G"]
    ca2 = ["C", "C", "G", "G", "C", "C", "G", "C"]
    cb = ["G", "G", "G", "C", "G", "C", "C", "G"]
    backing(evs, ["C", "C"] + ca + ca2 + cb + ca2 + ["C", "C"])
    write_smf(path, evs, "Ode to Joy", 112)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "songs"
    os.makedirs(out, exist_ok=True)
    twinkle(os.path.join(out, "01_twinkle.mid"))
    joy(os.path.join(out, "02_joy.mid"))
    print("demo songs ->", out)
