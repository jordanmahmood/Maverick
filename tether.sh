#!/bin/sh
# Keeps the USB tunnel to the hub alive for every device on adb (glasses and phone).
# `adb reverse` is forgotten whenever a cable is re-plugged or a device restarts, and then
# the app on that device shows "Reconnecting" / "Glasses not connected" until it is set
# again. Run this once and leave it running:   ./tether.sh &
PORT=${PORT:-8000}
while true; do
  for serial in $(adb devices | awk 'NR>1 && $2=="device" && $1 !~ /:/ {print $1}'); do
    adb -s "$serial" reverse --list 2>/dev/null | grep -q "tcp:$PORT" || {
      adb -s "$serial" reverse tcp:$PORT tcp:$PORT >/dev/null 2>&1 &&
        echo "$(date +%H:%M:%S) tunnel set for $serial"
    }
  done
  sleep 3
done
