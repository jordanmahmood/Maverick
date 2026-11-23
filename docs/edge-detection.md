# Detection models made for the glasses

Maverick finds the parts on the table with a small trained detector, not a cloud model.
That detector can run in two places, behind one switch:

- **On the Mac hub (default for the demo).** The glasses stream camera frames over USB,
  the hub's full-size model looks at each one (about 23 ms on the Mac), and the boxes go
  back to the lens.
- **On the glasses themselves.** A model built for the glasses' processor runs inside
  `glasses-app/` with ONNX Runtime. The glasses find the part and draw its box without a
  picture leaving the device; only a few hundred bytes of boxes and 2 to 3 small pictures a
  second (for the "is the step done" check) go to the hub.

Switch: "Find parts on the glasses" in the phone app or in the display page's Settings
(`prefs.detect`, `"hub"` or `"glasses"`). On Wi-Fi the glasses always detect for
themselves, because eight pictures a second do not fit through a busy network.

The rest of this page is the pipeline that turns a handful of phone photos into a model
that fits on a 2 GB pair of glasses, and the test bench that decides whether a candidate
is good enough to go live.

## The target

MLVision M6 glasses: Unisoc UWS6152, one Cortex-A75 and three Cortex-A55, 2 GB of memory,
Android 9, 240 mAh battery, no usable GPU or NPU for us. Everything runs on one CPU core,
next to the camera, the on-device box tracker (30 fps) and the display.

## The pipeline

```
phone photos of each part
   │  maverick.train ingest / label     a vision model boxes every part
   │  maverick.train cutouts            SAM cuts each part out of its photo
   │  maverick.train synth              cut-outs pasted onto hundreds of surfaces,
   │                                    every angle, size and lighting, with clutter
   │  maverick.train fit                YOLO11n            -> guides/kits/demo/model.pt   (hub)
   │  maverick.train upgrade            retrain with real camera frames as "not a part";
   │                                    only switches if false boxes drop and parts stay
   ▼
train_glasses.py <size>                fine-tune at the size the glasses will run
   ▼                                   -> guides/kits/demo/model-glasses<size>.pt
glasses_export.py <weights> W H name int8
   │                                   fixed-size ONNX, then 8-bit:
   │                                   calibrated on recorded glasses frames, prepared
   │                                   exactly the way the glasses prepare theirs
   ▼                                   -> guides/kits/demo/glasses/<name>.onnx
glasses_sim.py                         the test bench (below)
   ▼
guides/kits/demo/glasses/meta.json     point it at the winner; the glasses download the
                                       new model on their own, no reinstall
```

### 1. A detector from a handful of photos

`python -m maverick.train all <photos...>` (in `backend/`). Each part is photographed a
few times, boxed by a vision model, cut out with SAM and pasted onto hundreds of different
surfaces with other objects and finger-like shapes in the way. The detector learns the
part, not the table it was photographed on. `upgrade` then adds real frames from the
glasses' camera as negative examples and replaces the model only when it is measurably
better. Six classes: chassis, hood, rear, minifigure, roof, and "foreign" (something on
the build that should not be there).

### 2. Fine-tune at the size the glasses run

The hub's model works at 960 px. Shrinking the input to 320 px at export time alone loses
the small parts, so `train_glasses.py` fine-tunes the same network at a glasses-sized
input first (`model-glasses480.pt`, `model-glasses320.pt`).

### 3. Export and quantize

`glasses_export.py` writes a fixed-size ONNX file (4:3 picture in the top of the input,
grey padding below, the same layout the app uses). With `int8` it quantizes to 8-bit:

- QDQ format, per-channel weights, convolutions only.
- The last block, which turns features into boxes and scores, stays in floating point:
  8-bit there costs accuracy for almost no time.
- Calibration uses frames recorded on the glasses during a real run, not random images.
  Those frames stay on the machine that recorded them (`backend/calib/`, not in git:
  they show people in the room).

A 10.5 MB float model becomes about 3 MB.

### 4. A half-width network

`train_pico.py` trains a network half as wide as YOLO11n (about 0.8 M parameters, a third
of the work per look). There are no pretrained weights at that width, so it learns only
from our own data. It is the fastest thing we measured on the glasses (116 ms) but did not
reach usable accuracy in the time we had.

### 5. The test bench: `glasses_sim.py`

No candidate goes to the glasses on a hunch. The bench replays a recorded run and answers
three questions for every model in `guides/kits/demo/glasses/`:

1. **Does it see the parts?** Every recorded frame goes through exactly what the app does
   (subsampled resize, grey padding, one thread, decode, per-class non-max suppression,
   the thresholds in the model's meta) and the boxes are compared with the hub's
   full-size model on the same frames.
2. **How fast will it be on the glasses?** Time on the Mac, one thread, multiplied by a
   factor measured on the real device (`glasses_calibration.json`, 8-bit and float kept
   apart).
3. **What would the wearer see?** The run is replayed at its real pace with a detector
   that takes that long. It always starts on the newest frame, and its boxes arrive late
   by its own latency. "Live" is the share of moments the box on screen still overlaps
   where the part really is, with no tracking to help.

A model passes with predicted latency of 200 ms or less and "live" of 60% or more on the
build. Results are written to `guides/kits/demo/glasses/sim-results.json`.

### 6. Going live without a reinstall

`meta.json` describes the model completely: file, input size, classes, thresholds,
threads, version. The hub sends the version in its `config` message; when it changes, the
glasses download the new file and swap it in. Trying another model on the device is one
edit and a few seconds.

## What we measured

On the glasses, one thread (`backend/glasses_calibration.json`, 6 to 20 runs each, so
plus or minus 30%):

| Model | Input | Numbers | Per look on the glasses |
|---|---|---|---|
| YOLO11n | 480x384 | float | 900 to 1400 ms |
| YOLO11n | 320x256 | float | 400 ms |
| YOLO11n | 416x320 | 8-bit | 386 ms |
| YOLO11n | 320x256 | 8-bit | 244 ms (175 ms with no upload running) |
| Half-width | 416x320 | 8-bit | 204 ms |
| Half-width | 320x256 | float | 172 ms |
| Half-width | 320x256 | 8-bit | 116 ms |

Smaller input and 8-bit numbers together took the same network from over a second to a
quarter of a second. More threads and XNNPACK were no faster, and this kernel refuses to
pin a thread to the big core alone; plain CPU, one thread, normal priority was best and
leaves the tracker at 30 fps.

On the test bench, against the hub's model on a recorded run
(`guides/kits/demo/glasses/sim-results.json`):

| Candidate | What it is | Predicted | Looks a second | Live on the build | Live on the part |
|---|---|---|---|---|---|
| g320q | fine-tuned at 480, run at 320x256, 8-bit | 244 ms | 4.1 | 68% | 61% |
| h320q | fine-tuned at 480 then 320, run at 320x256, 8-bit | 237 ms | 4.2 | 68% | 60% |
| g416q | fine-tuned at 480, run at 416x320, 8-bit | 382 ms | 2.6 | 57% | 56% |
| h416q | fine-tuned at 480 then 320, run at 416x320, 8-bit | 382 ms | 2.6 | 59% | 56% |

The bigger input sees small parts better frame by frame (rear 72% against 61%) but is so
much slower that the box on screen is right less often. Speed wins: a quick, slightly
weaker look beats a slow, sharper one once the wearer's head is moving.

## Where it stands

- `g320q.onnx` is the model the glasses run. It clears the accuracy bar (68% live on the
  build) and misses the 200 ms latency budget by about 45 ms.
- In the full app, with the camera upload, the tracker and the HUD all running, a look
  took about 530 to 560 ms in the last live session on 2026-11-21. That is why the demo
  defaults to the Mac hub: the hub's model answers in about 23 ms and sees small parts
  the 320 px view misses.
- With detection on the glasses, the hub still looks at the few pictures it receives and
  adds parts the low-resolution view missed, and takes over again by itself if the
  glasses send no detections for 2 seconds.
- Off the cable the glasses have shut down twice within minutes (240 mAh battery with
  camera, detector and display all on). Keep them on power for anything longer than a
  short walk-around.

## Next

- Train the half-width network to usable accuracy: at 116 ms it is the only candidate
  inside the latency budget.
- Give the detector thread the core to itself while the camera upload is off.
- A crop around the build instead of the whole view, so 320 px covers what matters.

## Commands

All from `maverick/backend/`:

```
uv run python -m maverick.train all <photos...>         # the hub's detector, from photos
uv run python train_glasses.py 480 12                    # fine-tune at a glasses size
uv run python glasses_export.py ../guides/kits/demo/model-glasses480.pt 320 256 g320q int8
uv run python glasses_sim.py                             # bench every candidate
uv run python glasses_sim.py g320q --run <recorded run>  # one model, another run
uv run python train_pico.py 416 40                       # the half-width network
```

The 8-bit export needs recorded frames in `backend/calib/*.jpg`: turn on "keep pictures"
in the phone app, do a run, and copy frames from the recording folder.
