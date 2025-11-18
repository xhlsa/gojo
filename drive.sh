#!/data/data/com.termux/files/usr/bin/sh
# Wrapper to hold a wakelock during a drive so Android doesn't suspend sensors.

set -e

termux-wake-lock
trap 'termux-wake-unlock' EXIT

./test_ekf.sh "$@"
