#!/bin/bash
# ======================================================
#  STEP 1: Termux-এ bot চালানোর জন্য base setup
#  Termux app-এ এই কোডটি paste করুন এবং run করুন
# ======================================================

echo "========================================="
echo "  Termux Setup - Package Install"
echo "========================================="

# Termux update + upgrade (প্রথমবার)
pkg update -y
pkg upgrade -y

# প্রয়োজনীয় package গুলো install
# python  -> bot চালানোর জন্য
# clang   -> tgcrypto / cryptography compile করতে
# rust    -> cryptography 42+ build করতে (Termux-এ wheel নেই)
# openssl -> cryptography dependency
# libffi  -> cffi dependency
# git     -> code pull করতে
# nano    -> .env edit করতে
# termux-api -> wake-lock (24 ঘণ্টা জাগিয়ে রাখা)
pkg install -y python python-pip clang rust binutils openssl libffi git nano termux-api

# pip latest এ upgrade
pip install --upgrade pip

echo "========================================="
echo "  Base setup complete!"
echo "  এখন STEP 2 চালান"
echo "========================================="
