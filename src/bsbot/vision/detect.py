"""ONNX YOLOv8 detector.

Adapted from PylaAI (https://github.com/PylaAI/PylaAI) — branche `compatibility`.
Licensed under "No Selling" terms. Personal use only.
Original authors: ivanyordanovgt, AngelFireLA, awarzu, Maayan080 (Mac port).

Changes vs original:
- `preferred_device` injected via constructor instead of reading PylaAI's TOML.
- Removed dependency on PylaAI's `utils.suppress_stdout_stderr` — kept output as-is.
- Removed unused PyTorch round-trip; numpy throughout (faster on CoreML).
"""
from __future__ import annotations

import os
from typing import Iterable

import cv2
import numpy as np
import onnxruntime as ort
import torch  # required only for ultralytics NMS
from PIL import Image
from ultralytics.utils.nms import non_max_suppression


class Detect:
    """Wrap an ONNX YOLOv8 model for inference + NMS post-processing."""

    def __init__(
        self,
        model_path: str,
        classes: list[str] | None = None,
        ignore_classes: Iterable[str] | None = None,
        input_size: tuple[int, int] = (640, 640),
        preferred_device: str = "auto",
    ):
        if model_path.endswith(".pt"):
            model_path = model_path.replace(".pt", ".onnx")
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"Model file not found: {os.path.abspath(model_path)}")

        self.model_path = model_path
        self.classes = classes
        self.ignore_classes = set(ignore_classes or ())
        self.preferred_device = preferred_device
        self.session, self.active_provider = self._load_session()
        # Auto-detect input size from model: input shape is (N, C, H, W).
        # Falls back to the user-provided default if shape is dynamic.
        try:
            in_shape = self.session.get_inputs()[0].shape
            h, w = in_shape[2], in_shape[3]
            if isinstance(h, int) and isinstance(w, int):
                self.input_size = (h, w)
            else:
                self.input_size = input_size
        except Exception:
            self.input_size = input_size

    def _load_session(self) -> tuple[ort.InferenceSession, str]:
        available = ort.get_available_providers()
        # Priority order: CoreML (Apple Silicon) > CPU. Other providers ignored
        # on Mac. PylaAI's larger ladder (TensorRT/CUDA/ROCm/DML/OpenVINO) is
        # irrelevant here.
        if self.preferred_device == "cpu":
            providers = ["CPUExecutionProvider"]
        else:
            providers = [p for p in ("CoreMLExecutionProvider", "CPUExecutionProvider") if p in available]

        so = ort.SessionOptions()
        so.log_severity_level = 3
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        try:
            session = ort.InferenceSession(self.model_path, sess_options=so, providers=providers)
            active = session.get_providers()[0]
        except Exception as exc:
            print(f"[detect] acceleration failed ({exc}), falling back to CPU")
            session = ort.InferenceSession(
                self.model_path, sess_options=so, providers=["CPUExecutionProvider"]
            )
            active = "CPUExecutionProvider"
        return session, active

    def _preprocess(self, img: np.ndarray | Image.Image) -> tuple[np.ndarray, int, int]:
        if isinstance(img, Image.Image):
            img = np.array(img)
        h, w = img.shape[:2]
        scale = min(self.input_size[0] / h, self.input_size[1] / w)
        new_w, new_h = int(w * scale), int(h * scale)
        resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        padded = np.full((self.input_size[0], self.input_size[1], 3), 128, dtype=np.uint8)
        padded[:new_h, :new_w, :] = resized
        padded = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
        chw = padded.astype(np.float32, copy=False) / 255.0
        chw = np.transpose(chw, (2, 0, 1))
        return np.expand_dims(chw, axis=0), new_w, new_h

    def _postprocess(
        self,
        preds: np.ndarray,
        orig_shape: tuple[int, int],
        resized_shape: tuple[int, int],
        conf_thresh: float,
    ) -> list[np.ndarray]:
        # Ultralytics NMS expects torch tensors.
        nms_out = non_max_suppression(
            torch.from_numpy(preds), conf_thres=conf_thresh, iou_thres=0.6
        )
        orig_h, orig_w = orig_shape
        resized_w, resized_h = resized_shape
        scale_w, scale_h = orig_w / resized_w, orig_h / resized_h
        results: list[np.ndarray] = []
        for pred in nms_out:
            if len(pred):
                pred[:, 0] *= scale_w
                pred[:, 1] *= scale_h
                pred[:, 2] *= scale_w
                pred[:, 3] *= scale_h
                results.append(pred.cpu().numpy())
        return results

    def detect_objects(
        self, img: np.ndarray | Image.Image, conf_thresh: float = 0.6
    ) -> dict[str, list[list[int]]]:
        """Run inference and return {class_name: [[x1,y1,x2,y2], ...]}."""
        if isinstance(img, Image.Image):
            img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        orig_h, orig_w = img.shape[:2]
        tensor, new_w, new_h = self._preprocess(img)

        outputs = self.session.run(None, {"images": tensor})
        detections = self._postprocess(outputs[0], (orig_h, orig_w), (new_w, new_h), conf_thresh)

        result: dict[str, list[list[int]]] = {}
        if not self.classes:
            # No class map provided -> return raw class indices as keys.
            for det in detections:
                for *xyxy, _conf, cls in det:
                    key = str(int(cls))
                    result.setdefault(key, []).append([int(x) for x in xyxy])
            return result

        for det in detections:
            for *xyxy, _conf, cls in det:
                idx = int(cls)
                if idx < 0 or idx >= len(self.classes):
                    continue
                name = self.classes[idx]
                if name in self.ignore_classes:
                    continue
                result.setdefault(name, []).append([int(x) for x in xyxy])
        return result
