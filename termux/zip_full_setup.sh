#!/bin/bash
# ============================================================
#  TERMUX 24/7 BOT SETUP — COMPLETE READYMADE (zip সহ)
#  এই পুরো কোডটি Termux-এ paste করলেই সব হয়ে যাবে।
#  zip-এর নাম যাই হোক, নিজে থেকে Download folder-এ খুঁজে নেবে।
# ============================================================

echo "###################################################"
echo "#  STEP 1/7: Termux package install"
echo "###################################################"

pkg update -y
pkg upgrade -y

pkg install -y python python-pip clang rust binutils openssl libffi git nano termux-api termux-services unzip

pip install --upgrade pip

echo ""
echo "###################################################"
echo "#  STEP 2/7: Download folder-এ zip খোঁজা"
echo "###################################################"

termux-setup-storage
sleep 3

ZIP_FILE=$(ls ~/storage/downloads/*.zip 2>/dev/null | head -1)
if [ -z "$ZIP_FILE" ]; then
    echo "ERROR: ~/storage/downloads/ এ কোনো .zip পাওয়া যায়নি!"
    echo "জেনে নিন: termux-setup-storage দিলে Termux storage access পায়।"
    echo "zip এখনও ওখানে না থাকলে: ফোন-এ zip download করুন, তারপর আবার এই script run করুন।"
    exit 1
fi

echo "পাওয়া গেল: $ZIP_FILE"

echo ""
echo "###################################################"
echo "#  STEP 3/7: zip unzip করে project বের করা"
echo "###################################################"

mkdir -p ~/CA
cd ~/CA

unzip -o "$ZIP_FILE" -d /tmp/bot_extract > /dev/null 2>&1

# zip-এর ভেতরে folder থাকলে সেটাকে CA-তে নিয়ে আসা
BOT_SRC="/tmp/bot_extract"
if [ -d "$BOT_SRC/bot" ]; then
    cp -r "$BOT_SRC/." ~/CA/
elif [ -d "$(find $BOT_SRC -maxdepth 2 -name bot -type d | head -1)" ]; then
    INNER=$(find $BOT_SRC -maxdepth 2 -name bot -type d | head -1)
    cp -r "$(dirname $INNER)/." ~/CA/
else
    cp -r "$BOT_SRC/." ~/CA/
fi

cd ~/CA

# ঠিক আছে কিনা চেক
if [ ! -d "bot" ] || [ ! -f "requirements.txt" ]; then
    echo "ERROR: zip-এ bot/ বা requirements.txt পাওয়া যায়নি!"
    echo "ফোল্ডার কাঠামো দেখুন:"
    find /tmp/bot_extract -maxdepth 3 | head -30
    exit 1
fi

echo "Project structure OK:"
ls

echo ""
echo "###################################################"
echo "#  STEP 4/7: Python dependency install"
echo "###################################################"

pip install -r requirements.txt

echo ""
echo "--- Installed versions ---"
python -c "import pyrogram; print('Pyrogram/Pyrofork:', pyrogram.__version__)"
python -c "import tgcrypto; print('Tgcrypto: OK')"
python -c "import aiosqlite; print('Aiosqlite: OK')"
python -c "import aiohttp; print('Aiohttp: OK')"
python -c "import redis; print('Redis: OK')"
python -c "import phonenumbers; print('Phonenumbers: OK')"
python -c "import cryptography; print('Cryptography:', cryptography.__version__)"

echo ""
echo "###################################################"
echo "#  STEP 5/7: bot.lock মুছে clean state"
echo "###################################################"

rm -f ~/CA/bot/bot.lock
rm -f /tmp/bot_extract -rf 2>/dev/null

echo ""
echo "###################################################"
echo "#  STEP 6/7: 24hr auto-restart service (runit)"
echo "###################################################"

SERVICE_DIR="$PREFIX/var/service/bot"
mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_DIR/run" << 'EOF'
#!/bin/sh
exec 2>&1
cd "$HOME/CA" || exit 1
termux-wake-lock
exec python -u bot/main.py
EOF

chmod +x "$SERVICE_DIR/run"
sv-enable bot
sv start bot

echo ""
echo "###################################################"
echo "#  STEP 7/7: Reboot auto-start (Termux:Boot)"
echo "###################################################"

mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start-bot.sh << 'EOF'
#!/data/data/com.termux/files/usr/bin/sh
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
echo "###################################################"
echo "#  COMPLETE! বট এখন চলছে"
echo "###################################################"
echo ""
echo "  Status check:     sv status bot"
echo "  লাইভ log:         tail -f \$PREFIX/var/log/bot/current"
echo "  Stop:             sv stop bot"
echo "  Start:            sv start bot"
echo "  Restart:          sv restart bot"
echo ""
echo "  বট চলছে কিনা নিশ্চিত:"
sleep 10
sv status bot
tail -20 "$PREFIX/var/log/bot/current" 2>/dev/null
echo ""
echo "  শেষ ধাপ (আপনার হাতে):"
echo "  1. Settings > Apps > Termux > Battery > Unrestricted"
echo "  2. Settings > Apps > Termux:API > Battery > Unrestricted"
echo "  3. Wi-Fi > Keep Wi-Fi on during sleep: Always"
echo "  4. F-Droid থেকে Termux:Boot install (reboot auto-start)"
echo "###################################################"
