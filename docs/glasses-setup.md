# Setting up the M6 glasses

Hardware: MicroLumin MLVision M6 glasses (Android 9, arm64, 640x480 lens, one Cortex-A75, 2 GB, no GPU).

The glasses run our own Android app, installed over `adb`. That is the only route that lets us draw our own HUD on the lens: the vendor's iOS SDK only drives the glasses' built-in apps. The vendor's phone app is needed once, to pair the glasses and switch on Developer Mode. The vendor kit is confidential and is not part of this repository.

## 1. Toolchain

```bash
brew install --cask android-platform-tools         # adb
brew install openjdk@17
brew install --cask android-commandlinetools
export JAVA_HOME=/opt/homebrew/opt/openjdk@17 PATH=$JAVA_HOME/bin:$PATH ANDROID_HOME=$HOME/Library/Android/sdk
yes | sdkmanager --sdk_root=$ANDROID_HOME --licenses
sdkmanager --sdk_root=$ANDROID_HOME "platforms;android-28" "build-tools;34.0.0" "platform-tools"
```

## 2. Developer Mode (once)

1. Pair the glasses with the vendor's phone app.
2. On the glasses: Developer, then **Developer Mode** on.
3. Unplug and replug the USB cable into a data-capable port. Accept "Allow USB debugging" on the glasses.
4. `adb devices -l` should list `model:XNM_Glass`.

## 3. Build and install

```bash
cd glasses-app && ./gradlew assembleDebug && cd ..
./device.sh                 # installs over USB and points the app at the hub through `adb reverse`
./device.sh wifi            # untethered: uses this Mac's Wi-Fi address instead
```

The hub must listen on `0.0.0.0`, not `localhost`, for the Wi-Fi route (`run.sh` does). If the network blocks client-to-client traffic, the USB route tunnels through the cable instead.

`./device.sh` also works on an ordinary Android phone, which then behaves like a see-through lens (camera preview behind the HUD). `ZOOM=0.5 ./device.sh` asks the phone for its ultrawide camera, close to the glasses' field of view.

Logs: `adb logcat -s MaverickHUD`.

## 4. Lens calibration

In the display page's Settings, "Calibrate lens" shows a pink 4:3 rectangle. The wearer fits the lens frame onto it and taps. The hub finds the part of the camera frame the lens covers and sends it to the app so boxes land on the real object.

## Gotchas

- The glasses ship with the lens at about 10% brightness; `device.sh` raises it.
- Newer Android phones refuse cable installs until install checking is off; `device.sh` switches it off for the one install and restores it.
- The glasses do not appear in `ioreg` or `adb` until Developer Mode is on and the cable has been replugged.
