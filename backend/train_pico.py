"""Train the half-width "pico" detector for the glasses from scratch on the demo's parts.

    uv run python train_pico.py [imgsz] [epochs]

About a third of the work of YOLO11n per frame (0.8 M parameters). There are no pretrained
weights at this width, so it learns only from our synthetic scenes and labelled photos.
"""
import shutil, sys
from pathlib import Path
from ultralytics import YOLO
from maverick import detector as D
from glasses_export import pico_yaml

kit = D.KITS / "demo"; train = kit / "train"
size = int(sys.argv[1]) if len(sys.argv) > 1 else 416
epochs = int(sys.argv[2]) if len(sys.argv) > 2 else 40
m = YOLO(str(pico_yaml()))
m.train(data=str(train / "dataset" / "data.yaml"), epochs=epochs, imgsz=size, batch=64, device="mps",
        project=str(train / "runs"), name=f"pico{size}", exist_ok=True, workers=4, patience=50,
        pretrained=False, close_mosaic=5, plots=False, verbose=False, amp=False)
shutil.copy(train / "runs" / f"pico{size}" / "weights" / "best.pt", kit / f"model-pico{size}.pt")
print("TRAINED", kit / f"model-pico{size}.pt")
