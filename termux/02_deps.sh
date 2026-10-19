#!/bin/bash
# ======================================================
#  STEP 2: bot-এর Python dependency install
#  Termux app-এ এই কোডটি paste করুন এবং run করুন
#  NOTE: আগে 01_setup.sh চালাতে হবে
# ======================================================

echo "========================================="
echo "  Installing Python Dependencies"
echo "========================================="

# project folder-এ যান (folder name আগে git clone/copy করে এনেছেন)
cd ~/CA

# dependency install
pip install -r requirements.txt

# install ঠিক হয়েছে কিনা check
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
echo "========================================="
echo "  Dependency install complete!"
echo "  এখন STEP 3 চালান"
echo "========================================="
