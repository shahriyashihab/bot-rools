#!/bin/bash
# ======================================================
#  FULL TERMUX SETUP — A থেকে Z, এক কোডে
#  এই script run করলেই সব হয়ে যাবে
#  (Termux:Boot শুধু reboot auto-start-এর জন্য আলাদা লাগবে)
# ======================================================

echo "#################################################"
echo "#  TELEGRAM BOT - TERMUX FULL SETUP"
echo "#  ধাপ 1/4: System package install"
echo "#################################################"

pkg update -y
pkg upgrade -y

pkg install -y python python-pip clang rust binutils openssl libffi git nano termux-api

pip install --upgrade pip

echo ""
echo "#################################################"
echo "#  ধাপ 2/4: Bot code transfer"
echo "#  আপনার bot folder (CA) অবশ্যই নিচের path-এ থাকতে হবে:"
echo "#      ~/CA"
echo "#  অর্থাৎ Termux-এ:"
echo "#      cd ~/CA && ls"
echo "#  দিলে bot/, requirements.txt, .env দেখা যাবে।"
echo "#################################################"

if [ ! -d "$HOME/CA/bot" ]; then
    echo ""
    echo "ERROR: ~/CA folder-এ bot code পাওয়া যায়নি!"
    echo ""
    echo "আপনার code এখন Termux-এ নিয়ে আসুন:"
    echo "  Option A - git থেকে:"
    echo "      git clone <আপনার repo url> ~/CA"
    echo "  Option B - GitHub থেকে zip:"
    echo "      cd ~ && wget <zip url> && unzip <file>.zip && mv <folder> CA"
    echo "  Option C - ফোনের storage থেকে copy:"
    echo "      termux-setup-storage   (একবার permission দিলে হবে)"
    echo "      cp -r ~/storage/downloads/CA ~/CA"
    echo ""
    echo "code নিয়ে আসার পর আবার এই script চালান।"
    exit 1
fi

cd ~/CA

echo ""
echo "#################################################"
echo "#  ধাপ 3/4: Python dependency install"
echo "#################################################"

pip install -r requirements.txt

echo ""
echo "--- Installed versions check ---"
python -c "import pyrogram; print('Pyrogram/Pyrofork:', pyrogram.__version__)"
python -c "import tgcrypto; print('Tgcrypto: OK')"
python -c "import aiosqlite; print('Aiosqlite: OK')"
python -c "import aiohttp; print('Aiohttp: OK')"
python -c "import redis; print('Redis: OK')"
python -c "import phonenumbers; print('Phonenumbers: OK')"
python -c "import cryptography; print('Cryptography:', cryptography.__version__)"

echo ""
echo "#################################################"
echo "#  ধাপ 4/4: 24hr auto-restart service setup"
echo "#################################################"

pkg install -y termux-services

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
echo "#################################################"
echo "#  SETUP COMPLETE!"
echo "#################################################"
echo ""
echo "  বট এখন চলছে! Status check:"
echo "      sv status bot"
echo ""
echo "  Log দেখতে:"
echo "      tail -f \$PREFIX/var/log/bot/current"
echo ""
echo "  Stop করতে:        sv stop bot"
echo "  Start করতে:       sv start bot"
echo "  Restart করতে:     sv restart bot"
echo ""
echo "  রেখে দিন — ফোন screen বন্ধ থাকলেও 24 ঘণ্টা চলবে,"
echo "  crash হলেও নিজে থেকে restart হবে।"
echo ""
echo "  শেষ কাজ: ফোন settings-এ Termux battery Unrestricted করুন"
echo "  + Termux:Boot দিয়ে reboot auto-start (alada setup)"
echo "#################################################"
