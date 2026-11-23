#!/bin/sh
# Install the Maverick app on whatever is plugged in over USB and point it at the hub.
#   ./device.sh          phone or glasses over the USB cable (works on any network)
#   ./device.sh wifi     glasses untethered: uses this Mac's current Wi-Fi address
#   ./device.sh mirror   also mirror a phone's screen on the Mac (needs scrcpy)
# Wireless adb, so the cable can come out and installs still work:
#   adb tcpip 5555 && adb connect <glasses-ip>:5555     (glasses-ip: adb shell ip addr show wlan0)
cd "$(dirname "$0")"
APK=glasses-app/app/build/outputs/apk/debug/app-debug.apk
APP=com.maverick.glasses
# With the glasses reachable both by cable and by wireless adb there are two "devices":
# pick one (the first listed, which is the cable when it is plugged in).
export ANDROID_SERIAL=${ANDROID_SERIAL:-$(adb devices | awk 'NR>1 && $2=="device"{print $1; exit}')}
if ! adb get-state >/dev/null 2>&1; then
  echo "No device on USB. Plug it in, turn on USB debugging and accept the prompt on the device."
  exit 1
fi
echo "Device: $(adb shell getprop ro.product.model | tr -d '\r') (Android $(adb shell getprop ro.build.version.release | tr -d '\r'))"
# Newer phones (Samsung, Android 14+) refuse cable installs until install checking is
# switched off. Switch it off for this one install only, then put it back.
adb shell settings put global verifier_verify_adb_installs 0 >/dev/null 2>&1
adb shell settings put global package_verifier_enable 0 >/dev/null 2>&1
RESULT=$(adb install -r "$APK" 2>&1 | tail -1)
adb shell settings delete global verifier_verify_adb_installs >/dev/null 2>&1
adb shell settings delete global package_verifier_enable >/dev/null 2>&1
echo "Install: $RESULT"
case "$RESULT" in *Success*) ;; *) echo "Install failed. On Samsung, also try Settings > Security and privacy > Auto Blocker > off."; exit 1;; esac
adb shell pm grant $APP android.permission.CAMERA
adb shell pm grant $APP android.permission.RECORD_AUDIO
adb shell pm grant $APP android.permission.ACCESS_FINE_LOCATION 2>/dev/null   # lets the app read the Wi-Fi network's name
if [ "$1" = "wifi" ]; then
  URL="ws://$(ipconfig getifaddr en0):8000/ws"
else
  adb reverse tcp:8000 tcp:8000 >/dev/null
  URL="ws://127.0.0.1:8000/ws"
fi
# Camera 0 is the rear camera on phones and the only camera on the glasses.
# ZOOM=0.5 ./device.sh asks a phone for its ultrawide (0.5x) lens, close to the glasses' view.
CAM=${CAMERA:-0}
# On the glasses, turn the lens up to full brightness (it ships at about 10%).
case "$(adb shell getprop ro.product.model)" in *XNM*)
  adb shell settings put system screen_brightness_mode 0
  adb shell settings put system screen_brightness 255;;
esac
adb shell am force-stop $APP
adb shell am start -n $APP/.MainActivity -e url "$URL" --ei camera "$CAM" --ef zoom "${ZOOM:-1}" | tail -1
echo "App launched, hub at $URL, camera $CAM"
if [ "$1" = "mirror" ]; then
  scrcpy --window-title "Maverick phone" --max-size 1280 >/dev/null 2>&1 &
fi
