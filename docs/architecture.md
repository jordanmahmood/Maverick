# Architecture

```
glasses (Android app)  ──frames / detections──▶  hub (FastAPI)  ──▶  Azure OpenAI
  lens HUD, box tracker,                          step state machine,   step checks,
  optional on-device detector  ◀──steps, boxes──  detector, memory,     voice, parsing
                                                  voice
                                                       ▲
                          phone app (setup, consent, controls)  ·  display page
```

One shared session. Glasses join `/ws?role=glasses`, display windows `/ws?role=monitor`.

## Hub (`backend/maverick/`)

| Module | Job |
|---|---|
| `main.py` | The session: WebSockets, step state, settling logic, HTTP API. Up to 3 checks run at once, 0.6 s apart. |
| `state.py` | Step state machine. |
| `checker.py` | Shows the model the manual drawings from before and after the step plus the live frame, cropped to the work area. Returns a phase (`no_build`, `finding_part`, `holding_part`, `placing`, `done`, `mistake`), a hint and boxes. |
| `detector.py`, `train.py` | The part detector (YOLO11n) and the photo-to-model training pipeline. |
| `locator.py` | Finds the part to pick up (whole frame) or the spot where it goes (zoomed to the work area); finds the calibration rectangle with OpenCV. |
| `visual.py` | Lens pictures (manual drawing on black), part icons, 3D brick specs. |
| `voice.py` | Spoken steps (TTS, cached), transcription of glasses-mic utterances, and the filter that decides what is a command, a question or room noise. |
| `memory.py` | Remembers where each part was last seen; head movement is measured from the picture. Silent until the wearer asks. |
| `lego.py`, `build_guide.py`, `guide.py` | Kit parser (set number → booklet PDF → one vision call per page → `guides/kits/<set>/guide.json`) and guide loading. |
| `ai.py` | Azure OpenAI client. Box requests always go through `ai.for_boxes()` / `ai.unit_box()`: the photo is resized to short side 768 and the model answers in pixels, which was right 12 of 12 times where fractions were not. |

## How a step finishes

A step is marked done only when the checker says `done` on separate looks at least a second apart: 2 looks if the detector sees the part sitting on the base and holding still, 3 otherwise, and never while the detector sees the part lying apart from the base. The checker is also told what the detector sees, and that a hand still adjusting the part means "placing".

## Glasses app (`glasses-app/`)

Java, no AndroidX. Streams camera frames, draws a thin bottom strip (part icon or rotating 3D brick, one instruction line, one hint line) and a thin rounded AR box with a pill label. `BoxTracker` tracks each box on the device at camera rate, from the frame the hub looked at to now (global motion plus template match); `ArOverlayView` draws it. Optional on-device detector with ONNX Runtime; see [edge-detection.md](edge-detection.md).

Touchpad: tap shows the drawing and speaks the detail, double tap is next, swipe is back.

## Voice

The glasses' microphone sends utterances. Short commands, sentences that name Maverick, and questions that name a part are acted on. "Where is the red rear?" is answered from memory at once and spoken; for 12 s the `ar` message carries either a box on the part or an `away` pointer that the lens draws as an edge arrow. Other questions go to the model with the frame and the memory as context.

## Phone app (`phone-app/`)

A thin Android shell around the hub's `/app` page (`ui/src/phone.tsx`). Setup happens on the phone: what Maverick does, consent (camera, microphone, keep pictures: off by default), fit (dominant eye, brightness, spoken guidance, how much help), then "Start on glasses". While building, the phone follows along and holds the switches (camera, microphone, spoken guidance, help level, dominant eye, keep pictures). "Your glasses" has battery, name, brightness, volume, auto-off, do not disturb, sleep and version info.

## Display page (`ui/`)

Vite, React, Tailwind 4, shadcn. `npm run build` writes `web/dist/`, which the hub serves. It shows only the wearer's live view, the step list and Back/Next; everything for the operator is in the Settings sheet. The look is in [../styleguide](../styleguide/README.md).

## Teaching a new task

Steps are taught with photos: display page, "Demo steps" (capture the part, capture the finished step, "Write it for me"), stored in `guides/kits/demo/`. `python -m maverick.train` turns the photos into a detector (auto-labels, SAM cut-outs, synthetic scenes, YOLO).
