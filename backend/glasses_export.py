"""Export detector variants for the glasses and (optionally) quantize them to 8-bit.

    uv run python glasses_export.py <weights.pt | pico> <width> <height> <name> [int8]

Writes guides/kits/demo/glasses/<name>.onnx. `pico` builds an untrained half-width network
(for timing only). int8 uses real frames from a recorded run for calibration, prepared the
way the glasses prepare theirs (frame resized into the top of the input, grey padding).
"""
import glob, json, shutil, sys, time
from pathlib import Path
import cv2, numpy as np
from ultralytics import YOLO
from maverick import detector as D

KIT = D.KITS / "demo"; OUT = KIT / "glasses"; OUT.mkdir(exist_ok=True)
PICO = Path(__file__).parent / "yolo11p.yaml"
CALIB = sorted(glob.glob(str(Path(sys.argv[0]).resolve().parent / "calib" / "*.jpg")))


def pico_yaml() -> Path:
    import ultralytics, yaml
    src = Path(ultralytics.__file__).parent / "cfg" / "models" / "11" / "yolo11.yaml"
    cfg = yaml.safe_load(src.read_text())
    # Half the width of "n": about a quarter of the work. It has to be the only scale in
    # the file, because the loader picks the scale from the file name (n/s/m/l/x only).
    cfg["scales"] = {"n": [0.50, 0.125, 1024]}
    cfg["nc"] = 6
    PICO.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return PICO


def prep(path: str, w: int, h: int) -> np.ndarray:
    img = cv2.imread(path)
    ch = round(w * 3 / 4)
    small = cv2.resize(img, (w, ch), interpolation=cv2.INTER_AREA)
    canvas = np.full((h, w, 3), 114, np.uint8)
    canvas[:ch] = small
    return (canvas[..., ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0)


def export(weights: str, w: int, h: int, name: str, int8: bool) -> Path:
    model = YOLO(str(pico_yaml())) if weights == "pico" else YOLO(weights)
    path = Path(model.export(format="onnx", imgsz=(h, w), opset=17, simplify=True, dynamic=False,
                             device="cpu"))
    out = OUT / f"{name}.onnx"
    if not int8:
        shutil.move(path, out)
        return out
    from onnxruntime.quantization import (CalibrationDataReader, QuantFormat, QuantType,
                                          quantize_static)
    from onnxruntime.quantization.shape_inference import quant_pre_process
    pre = path.with_suffix(".pre.onnx")
    quant_pre_process(str(path), str(pre), skip_symbolic_shape=True)

    class Reader(CalibrationDataReader):
        def __init__(self):
            self.items = iter(CALIB[:80])
        def get_next(self):
            item = next(self.items, None)
            return None if item is None else {"images": prep(item, w, h)}

    import onnx
    graph = onnx.load(str(pre)).graph
    # The last block turns features into boxes and scores; 8-bit there costs accuracy for
    # almost no time, so it stays in floating point.
    head = [n.name for n in graph.node if "/model.23/" in n.name and
            any(k in n.name for k in ("dfl", "Sigmoid", "Concat", "Sub", "Add", "Div", "Mul", "Slice",
                                      "Reshape", "Transpose", "Softmax", "cv2.0/cv2.0.2", "cv2.1/cv2.1.2",
                                      "cv2.2/cv2.2.2", "cv3.0/cv3.0.2", "cv3.1/cv3.1.2", "cv3.2/cv3.2.2"))]
    quantize_static(str(pre), str(out), Reader(), quant_format=QuantFormat.QDQ, per_channel=True,
                    activation_type=QuantType.QUInt8, weight_type=QuantType.QInt8,
                    op_types_to_quantize=["Conv"], nodes_to_exclude=head)
    path.unlink(missing_ok=True); pre.unlink(missing_ok=True)
    return out


if __name__ == "__main__":
    weights, w, h, name = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    out = export(weights, w, h, name, "int8" in sys.argv[5:])
    import onnxruntime as ort
    so = ort.SessionOptions(); so.intra_op_num_threads = 1
    s = ort.InferenceSession(str(out), so, providers=["CPUExecutionProvider"])
    x = np.random.rand(1, 3, h, w).astype(np.float32)
    s.run(None, {"images": x}); t = time.perf_counter()
    for _ in range(20): s.run(None, {"images": x})
    print(f"EXPORTED {out.name}: {out.stat().st_size // 1024} KB, output {s.get_outputs()[0].shape}, "
          f"mac 1 thread {(time.perf_counter() - t) / 20 * 1000:.1f} ms")
