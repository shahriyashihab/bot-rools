#!/bin/bash
# ======================================================
#  STEP 5: ফোন reboot হলে bot নিজে থেকে চালু হবে
#  Termux:Boot app লাগবে (F-Droid থেকে install করুন)
# ======================================================

# NOTE: এই script টা phone-এর Termux-এ run করবেন না।
# Termux:Boot app install করার পর নিচের কোডটি Termux-এ
# run করলে script টা boot folder-এ save হবে।

echo "========================================="
echo "  Setting up auto-start on boot"
echo "========================================="

# ~/.termux/boot folder তৈরি
mkdir -p ~/.termux/boot

# boot script লিখুন
cat > ~/.termux/boot/start-bot.sh << 'EOF'
#!/data/data/com.termux/files/usr/bin/sh
# ফোন চালু হলে bot auto-start হবে
termux-wake-lock
sleep 15
if [ -x "$PREFIX/var/service/bot/run" ]; then
    sv start bot
else
    cd "$HOME/CA"
    nohup python -u bot/main.py > bot.log 2>&1 &
fi
EOF

chmod +x ~/.termux/boot/start-bot.sh

echo ""
echo "========================================="
echo "  Auto-start configured!"
echo ""
echo "  এখন করতে হবে:"
echo "  1. F-Droid থেকে Termux:Boot app install করুন"
echo "  2. ফোন settings-এ Termux:Boot-কে battery unrestricted করুন"
echo "  3. ফোন reboot করে test করুন"
echo "========================================="
