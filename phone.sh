#!/bin/sh
# Build the Maverick phone app, install it on the phone and open it on the hub's phone page.
#   ./phone.sh          over the USB cable (works on any network)
#   ./phone.sh wifi     phone untethered: uses this Mac's current Wi-Fi address
# The phone is the first adb device that is not the glasses (model XNM...), or $ANDROID_SERIAL.
cd "$(dirname "$0")"
APK=phone-app/app/build/outputs/apk/debug/app-debug.apk
APP=com.maverick.app
S=$ANDROID_SERIAL
if [ -z "$S" ]; then
  for d in $(adb devices | awk 'NR>1 && $2=="device" {print $1}'); do
    case "$(adb -s "$d" shell getprop ro.product.model | tr -d '\r')" in
      *XNM*) ;;
      *) S=$d; break;;
    esac
  done
fi
if [ -z "$S" ]; then
  echo "No phone on adb. Plug it in, turn on USB debugging and accept the prompt on the phone."
  exit 1
fi
echo "Phone: $(adb -s "$S" shell getprop ro.product.model | tr -d '\r') ($S)"
[ -d /opt/homebrew/opt/openjdk@17 ] && export JAVA_HOME=/opt/homebrew/opt/openjdk@17 PATH=/opt/homebrew/opt/openjdk@17/bin:$PATH
export ANDROID_HOME=${ANDROID_HOME:-$HOME/Library/Android/sdk}
(cd phone-app && ./gradlew assembleDebug -q) || { echo "Build failed."; exit 1; }
# Newer phones (Samsung, Android 14+) refuse cable installs until install checking is
# switched off. Switch it off for this one install only, then put it back.
adb -s "$S" shell settings put global verifier_verify_adb_installs 0 >/dev/null 2>&1
adb -s "$S" shell settings put global package_verifier_enable 0 >/dev/null 2>&1
RESULT=$(adb -s "$S" install -r "$APK" 2>&1 | tail -1)
adb -s "$S" shell settings delete global verifier_verify_adb_installs >/dev/null 2>&1
adb -s "$S" shell settings delete global package_verifier_enable >/dev/null 2>&1
echo "Install: $RESULT"
case "$RESULT" in *Success*) ;; *) echo "Install failed. On Samsung, also try Settings > Security and privacy > Auto Blocker > off."; exit 1;; esac
if [ "$1" = "wifi" ]; then
  URL="http://$(ipconfig getifaddr en0):8000/app"
else
  adb -s "$S" reverse tcp:8000 tcp:8000 >/dev/null
  URL="http://127.0.0.1:8000/app"
fi
adb -s "$S" shell am force-stop $APP
adb -s "$S" shell am start -n $APP/.MainActivity -e url "$URL" | tail -1
echo "Maverick opened on the phone, page at $URL"
