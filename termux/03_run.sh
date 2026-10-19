#!/bin/bash
# ======================================================
#  STEP 3: bot চালানো + 24 ঘণ্টা lock (wake-lock)
#  Termux app-এ এই কোডটি paste করুন এবং run করুন
# ======================================================

echo "========================================="
echo "  Starting Bot with Wake-Lock (24hr)"
echo "========================================="

cd ~/CA

# 1) Termux:API থেকে wake-lock নিন
#    ফোন screen বন্ধ থাকলেও bot CPU sleep হবে না
termux-wake-lock
echo "[1/3] Wake-lock active - bot সবসময় জাগানো থাকবে"

# 2) Battery optimization থেকে Termux exempt
#    Android Settings > Apps > Termux > Battery > Unrestricted
#    (নিচে বিস্তারিত লেখা আছে — এই command Termux API দিয়ে করা যায় না,
#     ফোন settings-এ manually করতে হবে)

# 3) bot চালান (background-এ, log সহ)
#    bot লগ saved হবে bot.log ফাইলে
nohup python -u bot/main.py > bot.log 2>&1 &

BOT_PID=$!
echo "[2/3] Bot started with PID: $BOT_PID"

# 4) 30 সেকেন্ড পরে bot চলছে কিনা check
sleep 30
if kill -0 $BOT_PID 2>/dev/null; then
    echo "[3/3] Bot is RUNNING! PID: $BOT_PID"
else
    echo "[3/3] ERROR: Bot crash করেছে! নিচের লগ দেখুন:"
fi

echo ""
echo "===== Last 30 lines of bot.log ====="
tail -30 bot.log
echo ""
echo "বট চলছে কিনা check করতে:"
echo "  ps aux | grep bot"
echo ""
echo "বট বন্ধ করতে:"
echo "  kill \$(pgrep -f 'bot/main.py')"
echo ""
echo "আবার নতুন করে চালাতে:"
echo "  nohup python -u bot/main.py > bot.log 2>&1 &"
