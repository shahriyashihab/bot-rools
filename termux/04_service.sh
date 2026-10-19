#!/bin/bash
# ======================================================
#  STEP 4: Termux-services — crash হলে bot নিজে থেকে restart হবে
#  এটা 24 ঘণ্টা চালানোর সবচেয়ে নিশ্চিত উপায়
# ======================================================

echo "========================================="
echo "  Installing Termux Services (auto-restart)"
echo "========================================="

# termux-services package install
pkg install -y termux-services

# service directory তৈরি
SERVICE_DIR="$PREFIX/var/service/bot"
mkdir -p "$SERVICE_DIR"

# run script লিখুন (bot launch করার জন্য)
cat > "$SERVICE_DIR/run" << 'EOF'
#!/bin/sh
# auto-restart service - bot সবসময় চালু রাখবে
exec 2>&1
cd "$HOME/CA" || exit 1

# wake-lock active করুন
termux-wake-lock

# bot চালান — crash হলে runit নিজে থেকে আবার start করবে
exec python -u bot/main.py
EOF

chmod +x "$SERVICE_DIR/run"

# service enable করুন
sv-enable bot

# service start করুন
sv start bot

echo ""
echo "========================================="
echo "  Service enabled!"
echo ""
echo "  Status দেখতে:   sv status bot"
echo "  Log দেখতে:      tail -f \$PREFIX/var/log/bot/current"
echo "  Stop করতে:      sv stop bot"
echo "  Start করতে:     sv start bot"
echo "  Restart:        sv restart bot"
echo "========================================="
