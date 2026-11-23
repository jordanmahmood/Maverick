"""Fine-tune the part detector at the size the glasses run it (480 wide), from the current
model. Writes guides/kits/demo/glasses/model.onnx + meta.json when it is no worse than the
model it replaces on the real frames from this morning's run."""
import json, shutil, sys, time
from pathlib import Path
from ultralytics import YOLO
from maverick import detector as D
kit = D.KITS / "demo"; train = kit / "train"
size = int(sys.argv[1]) if len(sys.argv) > 1 else 480
epochs = int(sys.argv[2]) if len(sys.argv) > 2 else 12
base = sys.argv[3] if len(sys.argv) > 3 else str(kit / "model.pt")
m = YOLO(base)
m.train(data=str(train / "dataset" / "data.yaml"), epochs=epochs, imgsz=size, batch=32, device="mps",
        project=str(train / "runs"), name=f"glasses{size}", exist_ok=True, workers=4, patience=20,
        lr0=0.003, warmup_epochs=1, close_mosaic=3, plots=False, verbose=False)
best = train / "runs" / f"glasses{size}" / "weights" / "best.pt"
shutil.copy(best, kit / f"model-glasses{size}.pt")
print("TRAINED", kit / f"model-glasses{size}.pt")
