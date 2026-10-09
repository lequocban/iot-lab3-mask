"""YOLO11 detect ONNX FP32, static NCHW, raw output (1, 7, anchors)."""
import ast
import os
import time

import numpy as np
import onnxruntime as ort
from PIL import Image

NAMES = ["with_mask", "without_mask", "mask_weared_incorrect"]


def letterbox(image, size):
    width, height = image.size
    ratio = min(size / width, size / height)
    nw, nh = round(width * ratio), round(height * ratio)
    left, top = (size - nw) // 2, (size - nh) // 2
    padded = Image.new("RGB", (size, size), (114, 114, 114))
    padded.paste(image.resize((nw, nh), Image.Resampling.BILINEAR), (left, top))
    x = np.asarray(padded, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
    return np.ascontiguousarray(x), ratio, left, top


def nms(boxes, scores, threshold=0.45, limit=100):
    """Class-agnostic: a face should have exactly one of the three labels."""
    order = scores.argsort()[::-1]
    keep = []
    while order.size and len(keep) < limit:
        i = int(order[0])
        keep.append(i)
        other = order[1:]
        a = np.maximum(boxes[i, :2], boxes[other, :2])
        b = np.minimum(boxes[i, 2:], boxes[other, 2:])
        wh = np.maximum(b - a, 0)
        intersection = wh[:, 0] * wh[:, 1]
        area_i = np.prod(np.maximum(boxes[i, 2:] - boxes[i, :2], 0))
        area_o = np.prod(np.maximum(boxes[other, 2:] - boxes[other, :2], 0), axis=1)
        iou = intersection / np.maximum(area_i + area_o - intersection, 1e-9)
        order = other[iou <= threshold]
    return keep


def decode(raw, width, height, ratio, left, top, confidence=0.4, iou=0.45):
    if raw.ndim != 3 or raw.shape[0] != 1 or raw.shape[1] != 7:
        raise ValueError(f"Expected raw YOLO11 (1,7,N), got {raw.shape}")
    rows = raw[0].T
    cls = rows[:, 4:].argmax(axis=1)
    scores = rows[np.arange(len(rows)), cls + 4]
    valid = np.isfinite(rows).all(axis=1) & (scores >= confidence)
    rows, scores, cls = rows[valid], scores[valid], cls[valid]
    if len(rows) == 0:
        return []
    # Bound the work for crowded images before quadratic NMS.
    order = scores.argsort()[::-1][:1000]
    rows, scores, cls = rows[order], scores[order], cls[order]
    boxes = np.column_stack((rows[:, :2] - rows[:, 2:4] / 2,
                             rows[:, :2] + rows[:, 2:4] / 2))
    boxes[:, [0, 2]] = (boxes[:, [0, 2]] - left) / ratio
    boxes[:, [1, 3]] = (boxes[:, [1, 3]] - top) / ratio
    boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, width)
    boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, height)
    keep = nms(boxes, scores, iou)
    return [{"class_id": int(cls[i]), "label": NAMES[int(cls[i])],
             "confidence": round(float(scores[i]), 5),
             "box": [round(float(v), 2) for v in boxes[i]]}
            for i in keep if boxes[i, 2] > boxes[i, 0] and boxes[i, 3] > boxes[i, 1]]


class Detector:
    def __init__(self, path):
        options = ort.SessionOptions()
        options.intra_op_num_threads = int(os.getenv("ORT_THREADS", "1"))
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), options,
                                            providers=["CPUExecutionProvider"])
        inputs = self.session.get_inputs()
        if len(inputs) != 1:
            raise ValueError("Model must have one input")
        spec = inputs[0]
        shape = spec.shape
        if (spec.type != "tensor(float)" or len(shape) != 4 or shape[:2] != [1, 3]
                or not isinstance(shape[2], int) or shape[2] != shape[3]):
            raise ValueError(f"Expected static FP32 [1,3,S,S], got {spec.type} {shape}")
        self.input_name, self.size = spec.name, shape[2]
        metadata = self.session.get_modelmeta().custom_metadata_map
        names = ast.literal_eval(metadata.get("names", "{}"))
        ordered = [names.get(i, names.get(str(i))) for i in range(3)] if isinstance(names, dict) else names
        if ordered != NAMES:
            raise ValueError(f"Wrong model labels/order: {ordered}. Expected {NAMES}")
        result = self.session.run(None, {self.input_name: np.zeros(shape, np.float32)})[0]
        if result.ndim != 3 or result.shape[:2] != (1, 7):
            raise ValueError(f"Unsupported output {result.shape}; export nms=False, dynamic=False")

    def predict(self, image, confidence=0.4, iou=0.45):
        start = time.perf_counter()
        tensor, ratio, left, top = letterbox(image, self.size)
        ready = time.perf_counter()
        raw = self.session.run(None, {self.input_name: tensor})[0]
        inferred = time.perf_counter()
        detections = decode(raw, *image.size, ratio, left, top, confidence, iou)
        done = time.perf_counter()
        return detections, {"preprocess_ms": (ready - start) * 1000,
                            "inference_ms": (inferred - ready) * 1000,
                            "postprocess_ms": (done - inferred) * 1000}
