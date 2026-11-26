# Maverick All-in-One Setup Guide

This guide describes how to set up the Maverick hub, web UI, Android phone/glasses apps,
Azure vision services, local YOLO detection, and optional training.

## 1. What you need

### Required for the hub

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) for Python dependencies and commands
- Node.js 20 or newer
- npm
- An Azure OpenAI resource with a chat-completions model deployed for vision

### Required for Android devices

- Android SDK Platform 34
- Android SDK Build Tools 34.0.0
- JDK 17
- Android Debug Bridge (`adb`)
- An Android 9 or newer device for the glasses app
- A USB data cable and Developer/USB debugging enabled

The glasses app targets Android API 28 and requires arm64-v8a. The phone app targets Android
API 26 and higher. The Android projects are Gradle projects and include the Gradle wrapper,
so a separate Gradle installation is not required.

### Hardware

- For the full setup: MicroLumin MLVision M6 glasses or a compatible Android device
- For initial software testing: a regular Android phone or a browser
- For USB mode: the hub computer and Android device connected by a data cable
- For Wi-Fi mode: the hub computer and Android device on a reachable network

## 2. Get the repository

From a terminal:

```text
git clone <repository-url>
cd Maverick
```

All commands in this guide assume the repository root is the current directory.

## 3. Configure environment variables

Copy the example file:

```text
cp .env.example .env
```

On Windows Command Prompt, use:

```text
copy .env.example .env
```

The backend loads `.env` from the repository root. Do not commit it.

### Required Azure variables

```text
AZURE_OPENAI_ENDPOINT=https://<resource-name>.openai.azure.com/
AZURE_OPENAI_API_KEY=<key>
AZURE_OPENAI_API_VERSION=2024-12-01-preview
```

- `AZURE_OPENAI_ENDPOINT` is the endpoint for the Azure OpenAI resource.
- `AZURE_OPENAI_API_KEY` is a key for that resource.
- `AZURE_OPENAI_API_VERSION` is optional. The code defaults to `2024-12-01-preview`.

### Model/deployment variables

The hub uses these deployment names as configured in Azure:

```text
MAVERICK_CHECK_MODEL=<vision-deployment-for-step-checking>
MAVERICK_PARSE_MODEL=<vision-deployment-for-booklet-pages>
MAVERICK_LOCATE_MODEL=<vision-deployment-for-AR-locations>
```

These default to `gpt-4.1` for checks and parsing and `gpt-5.6-luna` for locating. The supplied
example uses:

```text
MAVERICK_CHECK_MODEL=gpt-5.6-luna
MAVERICK_PARSE_MODEL=gpt-5.5
MAVERICK_LOCATE_MODEL=gpt-5.6-luna
```

The selected deployments should support vision/image input and structured chat-completions
output. Azure may use deployment names rather than public model names; set each variable to
the deployment name configured for your resource.

The repository also has a fixed model used only by the YOLO training labelling tools:
`LABEL_MODEL = "gpt-5.6-luna"` in `backend/maverick/train.py`. It is not controlled by
`.env.example`.

### Optional variables

```text
MAVERICK_MOCK=0
MAVERICK_GUIDE=
MAVERICK_RECORD=
MAVERICK_KEEP_FRAMES=0

MAVERICK_TTS_MODEL=gpt-4o-mini-tts
MAVERICK_STT_MODEL=gpt-4o-mini-transcribe
MAVERICK_VOICE=coral
MAVERICK_VOICE_OFF=0

MAVERICK_DETECT_SIZE=960
MAVERICK_DETECT_CONF=0.30
MAVERICK_LOCATE_SIDE=1280
MAVERICK_LOCATE_CONFIDENCE=0.6
MAVERICK_CALM=0.04
MAVERICK_COLLECT=1
MAVERICK_DETECT_FPS=8
```

- `MAVERICK_MOCK=1` disables AI calls and makes the step checker return `not_yet`.
- `MAVERICK_GUIDE` can point to an alternate guide JSON.
- `MAVERICK_RECORD` enables recording and requires `MAVERICK_KEEP_FRAMES=1` if full frames are needed.
- `MAVERICK_VOICE_OFF=1` disables Azure TTS/STT voice features.
- The TTS and STT variables are Azure deployment/model names.
- The detector variables control YOLO input size and minimum detection confidence.
- Do not put secrets in source files or commit `.env`.

### Optional Anthropic/Claude setup

Claude is only used by the standalone text-to-guide command:

```text
python -m maverick.build_guide manual.txt ../guides/my-guide.json
```

Set `ANTHROPIC_API_KEY` in your environment before running that command. The Anthropic SDK
normally reads this variable automatically:

```text
ANTHROPIC_API_KEY=<key>
```

Claude is not used by the live camera/step-verification pipeline. That pipeline uses Azure
OpenAI.

## 4. Install and build the web UI

From the repository root:

```text
cd ui
npm install
npm run build
cd ..
```

The build writes to `web/dist/`, which the FastAPI hub serves.

For UI development:

```text
cd ui
npm run dev
```

The production hub should normally be used through `http://localhost:8000/`.

## 5. Start the backend hub

macOS or Linux with the included script:

```text
./run.sh
```

The script starts FastAPI on `0.0.0.0:8000` and attempts to open the display page.

Direct command:

```text
uv run --project backend uvicorn maverick.main:app --app-dir backend --host 0.0.0.0 --port 8000
```

On Windows, run the direct command from PowerShell or Command Prompt.

Verify the hub:

- Display: `http://localhost:8000/`
- Fake/browser glasses: `http://localhost:8000/fake`
- Classic monitor: `http://localhost:8000/classic`
- Phone page: `http://localhost:8000/app`
- Health endpoint: `http://localhost:8000/health`

The health response reports mock mode, guide state, configured check model, voice status,
and whether a local detector is loaded.

Without Azure credentials, step checking is in mock mode and will return `not_yet`. The rest
of the hub and browser pages can still start.

## 6. Use it without physical glasses

An ordinary Android phone can act like a see-through lens:

```text
./phone.sh
./device.sh
```

Or open `http://localhost:8000/fake` in a browser. The browser stand-in requires camera
permissions and is useful for validating the UI and hub.

## 7. Set up Android tools

### macOS

```text
brew install --cask android-platform-tools
brew install openjdk@17
brew install --cask android-commandlinetools

export JAVA_HOME=/opt/homebrew/opt/openjdk@17
export ANDROID_HOME=$HOME/Library/Android/sdk
export PATH=$JAVA_HOME/bin:$ANDROID_HOME/platform-tools:$PATH

yes | sdkmanager --sdk_root=$ANDROID_HOME --licenses
sdkmanager --sdk_root=$ANDROID_HOME \
  "platforms;android-34" \
  "build-tools;34.0.0" \
  "platform-tools"
```

### Linux

Install JDK 17 and Android command-line tools using the distribution or Android package
manager. Set:

```text
export JAVA_HOME=/path/to/jdk-17
export ANDROID_HOME=/path/to/android-sdk
export PATH=$JAVA_HOME/bin:$ANDROID_HOME/platform-tools:$PATH
```

Accept licenses and install:

```text
sdkmanager --licenses
sdkmanager "platforms;android-34" "build-tools;34.0.0" "platform-tools"
```

### Windows

Install JDK 17 and Android SDK Platform 34, then set `ANDROID_HOME` and `JAVA_HOME` in
System Environment Variables. Add these to `PATH`:

- `%ANDROID_HOME%\platform-tools`
- `%JAVA_HOME%\bin`

Confirm:

```text
adb version
java -version
```

The repository shell scripts (`run.sh`, `device.sh`, `phone.sh`) are written for macOS/Linux
shells. On Windows, use the Gradle wrappers and direct Python/npm/ADB commands below.

## 8. Build and install the Android apps

Keep the hub running while installing and launching the apps.

### Glasses app

macOS/Linux:

```text
cd glasses-app
./gradlew assembleDebug
cd ..
./device.sh
```

Windows:

```text
cd glasses-app
gradlew.bat assembleDebug
cd ..
adb devices -l
adb install -r glasses-app\app\build\outputs\apk\debug\app-debug.apk
```

The Windows example uses a different APK path from the repository scripts:

```text
glasses-app\app\build\outputs\apk\debug\app-debug.apk
```

### Phone app

macOS/Linux:

```text
./phone.sh
```

Windows:

```text
cd phone-app
gradlew.bat assembleDebug
adb install -r app\build\outputs\apk\debug\app-debug.apk
cd ..
```

The phone app is a WebView shell around `http://<hub-address>:8000/app`. The phone app
handles onboarding, consent, brightness, eye selection, camera, microphone, and sleep
settings.

## 9. Configure USB debugging

1. Enable Developer Options and USB debugging on the Android device.
2. Connect it with a USB data cable.
3. Accept the authorization prompt on the device.
4. Verify:

```text
adb devices -l
```

The device should list as `device`, not `unauthorized` or `offline`.

For the M6 glasses:

1. Pair the glasses through the vendor phone application.
2. Enable Developer Mode on the glasses.
3. Unplug and replug the USB cable.
4. Accept USB debugging.
5. `adb devices -l` should list the M6/model.

The vendor pairing application and vendor confidential SDK are not in this repository.

## 10. Connect an app to the hub

### USB/ADB tunnel

The included scripts use ADB reverse tunneling:

```text
adb reverse tcp:8000 tcp:8000
```

The glasses app normally uses:

```text
ws://127.0.0.1:8000/ws
```

The phone app normally uses:

```text
http://127.0.0.1:8000/app
```

If `device.sh` or `phone.sh` is used:

```text
./device.sh
./phone.sh
./tether.sh &
```

`tether.sh` re-applies USB tunnels if a cable or device reconnects.

### Wi-Fi

The hub must listen on `0.0.0.0`, as `run.sh` and the direct command do.

On macOS:

```text
./device.sh wifi
./phone.sh wifi
```

The scripts use the Mac Wi-Fi address and connect through port 8000.

On any platform, manually launch the app URL or WebSocket URL using the hub computer's LAN
address, for example:

```text
ws://192.168.1.50:8000/ws
http://192.168.1.50:8000/app
```

The host firewall must permit inbound TCP port 8000, and client-to-client isolation on the
Wi-Fi network must be disabled or otherwise allowed.

## 11. Calibrate the M6 or phone lens

In the display page, open Settings and choose **Calibrate lens**.

1. A pink 4:3 rectangle appears.
2. Align the physical lens frame with the pink rectangle.
3. Tap on the lens/device to confirm.
4. The hub detects the rectangle or asks Azure as a fallback.
5. It sends the camera region covered by the lens to the Android app.
6. Verify that AR boxes land on actual objects rather than offset.

The calibration data is stored under `guides/kits/_calib.json`.

## 12. Choose or load a guide

Available guide options include:

- Bundled guides under `guides/kits/`
- The LEGO kit parser
- The demo guide taught with photos
- A guide passed by `MAVERICK_GUIDE`

### Bundled guide

Select a kit in the display/monitor UI. The hub loads it if `guide.json` is available.

### LEGO booklet

The hub can obtain and parse an official LEGO booklet. It:

1. Finds the booklet PDF.
2. Downloads and renders its pages.
3. Sends each page image to Azure `MAVERICK_PARSE_MODEL`.
4. Writes `guides/kits/<kit>/guide.json`.

Azure credentials are required for booklet parsing.

### Demo guide

The demo guide is taught through:

1. Capture the part.
2. Capture the finished state.
3. Run **Write it for me**.

Photos are stored under `guides/kits/demo/`.

## 13. Local YOLO detection

The hub loads `guides/kits/<kit>/model.pt` when present.

```text
MAVERICK_DETECT_SIZE=960
MAVERICK_DETECT_CONF=0.30
```

The detector:

- Runs Ultralytics YOLO locally
- Returns class, confidence, and normalized bounding boxes
- Updates the build/part memory
- Supplies settling, motion, and relevant-part evidence
- Avoids running cloud step checks unless the relevant local parts are visible

The glasses can use an exported ONNX model from:

```text
guides/kits/demo/glasses/
```

The `meta.json` file identifies the model file and detector classes.

## 14. Train a local detector

The basic training commands are described in `docs/edge-detection.md`.

Hub YOLO training:

```text
cd backend
uv run python -m maverick.train all <photos or folders...>
```

This training pipeline uses Azure vision calls to label photos, SAM for segmentation/cut-outs,
synthetic negative/augmented scenes, and Ultralytics to fit YOLO.

Glasses export example:

```text
cd backend
uv run python train_glasses.py 480 12
uv run python glasses_export.py \
  ../guides/kits/demo/model-glasses480.pt \
  320 256 g320q int8
```

Benchmarks:

```text
cd backend
uv run python glasses_sim.py
uv run python glasses_sim.py g320q --run <recorded-run-folder>
```

YOLO training and SAM require their model assets/packages. Azure credentials are needed for
cloud-assisted image labelling.

## 15. Running the complete pipeline

1. Start the hub.
2. Build and open the display UI.
3. Start the glasses or Android phone app.
4. Configure the guide and step.
5. Calibrate the lens if using the glasses app.
6. Let the camera stream frames to `/ws`.
7. Local YOLO/ONNX detection identifies parts and movement.
8. Azure performs the full step check and quick attachment check.
9. The state machine combines repeated votes and tracker stability.
10. A verified step advances in the guide.

Cloud verification calls use:

- `MAVERICK_CHECK_MODEL` for `check_step()` and `confirm_done()`
- `MAVERICK_LOCATE_MODEL` for AR target boxes
- `MAVERICK_PARSE_MODEL` for booklet parsing
- `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY` for Azure access

## 16. Tests

Run backend tests:

```text
cd backend
uv run pytest -q
```

Build the UI:

```text
cd ui
npm run build
```

Build the Android apps:

```text
cd glasses-app
./gradlew assembleDebug

cd ../phone-app
./gradlew assembleDebug
```

## 17. Troubleshooting

### Hub says mock mode

- Check that `.env` exists at the repository root.
- Check `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY`.
- Check `MAVERICK_MOCK`; it must not be `1`.
- Call `http://localhost:8000/health`.

### Azure call fails

- Confirm the endpoint format and API key.
- Confirm the deployment names.
- Confirm the deployment supports vision/image input.
- Confirm that the API version is supported.
- Check that the deployment has quota.

### No device appears in `adb devices`

- Reconnect the USB cable.
- Use a data cable rather than charge-only cable.
- Enable USB debugging.
- Accept the authorization prompt.
- On M6 glasses, enable Developer Mode and replug the cable.
- Try:

```text
adb kill-server
adb start-server
adb devices -l
```

### Device cannot reach the hub over Wi-Fi

- Confirm the hub listens on `0.0.0.0`.
- Confirm port 8000 is allowed through the firewall.
- Confirm both devices are on a network that permits device-to-device traffic.
- Use `adb reverse` or `./tether.sh` as a USB fallback.

### USB app says reconnecting

- Rerun `adb reverse tcp:8000 tcp:8000`.
- Run `./tether.sh &`.
- Reinstall or relaunch the app after the tunnel is established.

### App build fails

- Confirm JDK 17 is selected.
- Confirm Android SDK Platform 34 and Build Tools 34.0.0 are installed.
- Run `java -version`, `adb version`, and `./gradlew --version`.
- On Samsung devices, disable Auto Blocker if it prevents ADB installation.

### AR boxes are shifted

- Re-run lens calibration.
- Confirm the Android camera preview has the expected orientation and zoom.
- Check the saved calibration in `guides/kits/_calib.json`.

### Step verification does not advance

- Confirm Azure check credentials.
- Confirm the `MAVERICK_CHECK_MODEL` deployment supports vision.
- Confirm the camera is actually seeing the build.
- Confirm a local model exists and the target parts are visible if detection gating is enabled.
- Verify that the step has finished and the full and quick checks agree for enough time.

### Voice is unavailable

- Set Azure credentials.
- Confirm TTS/STT deployment names.
- Check that `MAVERICK_VOICE_OFF` is not `1`.
- Grant `RECORD_AUDIO` and network permissions to the Android app.
