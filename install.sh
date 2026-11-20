#!/bin/sh
# HEXA MIDI installer for CardputerZero
# 使い方: cd ~/hexamidi && sh install.sh   (sudo は付けずに実行)
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
U=$(id -un)
if [ "$U" = "root" ]; then
  echo "sudo を付けずに実行してください (sh install.sh)"; exit 1
fi
APPL=/usr/share/APPLaunch
NEED_REBOOT=0

echo "== packages"
sudo apt-get install -y python3-pil python3-numpy python3-serial >/dev/null

echo "== files"
chmod +x "$DIR/hexamidi.py" "$DIR/hexamidi"
sudo ln -sf "$DIR/hexamidi" /usr/local/bin/hexamidi
mkdir -p "$DIR/songs" "$HOME/midi"
[ -f "$DIR/songs/01_twinkle.mid" ] || python3 "$DIR/make_demo.py" "$DIR/songs"

echo "== UART (Grove = GPIO14/15) を MIDI 用に空ける"
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
CMD=/boot/firmware/cmdline.txt; [ -f "$CMD" ] || CMD=/boot/cmdline.txt
if [ -f "$CFG" ] && ! grep -qE '^[[:space:]]*enable_uart=1' "$CFG"; then
  sudo cp "$CFG" "$CFG.hexamidi.bak"
  echo "enable_uart=1" | sudo tee -a "$CFG" >/dev/null
  echo "   $CFG に enable_uart=1 を追加 (元は .hexamidi.bak)"
  NEED_REBOOT=1
fi
if [ -f "$CMD" ] && grep -qE 'console=(serial0|ttyS0|ttyAMA0)' "$CMD"; then
  sudo cp "$CMD" "$CMD.hexamidi.bak"
  sudo sed -i -E 's/[[:space:]]*console=(serial0|ttyS0|ttyAMA0),[0-9]+//g' "$CMD"
  echo "   $CMD からシリアルコンソールを外しました (元は .hexamidi.bak)"
  NEED_REBOOT=1
fi
for s in serial-getty@ttyS0.service serial-getty@serial0.service serial-getty@ttyAMA0.service; do
  if systemctl is-enabled "$s" >/dev/null 2>&1 || systemctl is-active "$s" >/dev/null 2>&1; then
    sudo systemctl disable --now "$s" >/dev/null 2>&1 || true
    echo "   $s を止めました"
  fi
done

echo "== 日本語フォント"
if ! python3 "$DIR/hexamidi.py" --check-font; then
  echo "   字がそろったフォントが無いので fonts-noto-cjk を入れます"
  sudo apt-get install -y fonts-noto-cjk >/dev/null
fi

echo "== permissions"
for g in video input dialout; do
  if getent group "$g" >/dev/null && ! id -nG "$U" | tr ' ' '\n' | grep -qx "$g"; then
    sudo usermod -aG "$g" "$U"
    NEED_REBOOT=1
  fi
done

if [ -d "$APPL/applications" ]; then
  echo "== launcher (APPLaunch)"
  REF="$APPL/share/images/camera1.png"
  [ -f "$REF" ] || REF=64
  python3 "$DIR/make_icon.py" /tmp/hexamidi.png "$REF"
  sudo install -m 644 /tmp/hexamidi.png "$APPL/share/images/hexamidi.png"
  if [ ! -f "$APPL/applications/hexamidi.desktop" ]; then NEED_REBOOT=1; fi
  sudo tee "$APPL/applications/hexamidi.desktop" >/dev/null <<DESK
[Desktop Entry]
Type=Application
Name=HEXA MIDI
Comment=Play along with MIDI songs via Unit MIDI
Exec=/usr/local/bin/hexamidi
Icon=share/images/hexamidi.png
Terminal=false
Categories=AudioVideo;
StartupNotify=false
DESK
fi

echo "== done"
echo ""
echo "Unit MIDI の準備: 本体の Settings > ExtPort で Grove の 5V を ON (Grove は UART のまま)"
echo "                  Unit MIDI のスイッチは Bypass (外の鍵盤も使う時) / どちらでも (CPZ だけで弾く時)"
if [ "$NEED_REBOOT" = 1 ]; then
  echo ""
  echo "*** UART・権限・アイコンの反映のため、再起動してください: sudo reboot"
  echo "    再起動後に: hexamidi --probe   (ドミソが鳴れば OK)"
else
  /usr/local/bin/hexamidi --probe || true
fi
