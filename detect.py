import cv2
import numpy as np
import torch
from PIL import Image
import onnxruntime as ort
import os
from ultralytics.utils.nms import non_max_suppression
from utils import load_toml_as_dict, suppress_stdout_stderr 

class Detect:
    def __init__(self, model_path, ignore_classes=None, classes=None, input_size=(640, 640)):
        # Auto-correct extension
        if model_path.endswith('.pt'):
            model_path = model_path.replace('.pt', '.onnx')
        
        # Safety check for model path
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"Model file not found at: {os.path.abspath(model_path)}")

        self.preferred_device = load_toml_as_dict("cfg/general_config.toml").get('cpu_or_gpu', 'auto')
        self.model_path = model_path
        self.classes = classes
        self.ignore_classes = ignore_classes if ignore_classes else []
        self.input_size = input_size
        self.model, self.device = self.load_model()

    def load_model(self):
        available_providers = ort.get_available_providers()
        
        # Define priority hierarchy (Fastest to Slowest)
        priority_providers = []

        # 1. NVIDIA TensorRT (Fastest for NVIDIA if engine is cached)
        if "TensorrtExecutionProvider" in available_providers:
            priority_providers.append(('TensorrtExecutionProvider', {
                'device_id': 0,
                'trt_fp16_enable': True,
                'trt_engine_cache_enable': True,
                'trt_engine_cache_path': os.path.join('.', 'models', 'cache')
            }))
            
        # 2. NVIDIA CUDA (Standard for 10-50 Series)
        priority_providers.append('CUDAExecutionProvider')

        # 3. AMD ROCm (Linux Native)
        priority_providers.append('ROCMExecutionProvider')
        priority_providers.append('MIGraphXExecutionProvider')

        # 4. Intel OpenVINO (Optimized for Intel Arc / Integrated GPUs)
        if "OpenVINOExecutionProvider" in available_providers:
            priority_providers.append(('OpenVINOExecutionProvider', {
                'device_type': 'GPU_FP16', # Force GPU usage over CPU
                'num_streams': 1
            }))

        # 5. Windows DirectML (Universal for AMD/Intel/NVIDIA on Windows)
        priority_providers.append('DmlExecutionProvider')

        # 6. Apple Silicon (Metal/MPS)
        priority_providers.append('CoreMLExecutionProvider')

        # 7. Fallback
        priority_providers.append('CPUExecutionProvider')

        # Filter only what's actually installed on this specific machine
        valid_providers = []
        for p in priority_providers:
            p_name = p[0] if isinstance(p, tuple) else p
            if p_name in available_providers:
                valid_providers.append(p)

        # Allow user to override and force CPU mode via config
        if self.preferred_device == "cpu":
            valid_providers = ["CPUExecutionProvider"]

        so = ort.SessionOptions()
        so.log_severity_level = 3 # Silence internal ORT warnings
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        
        active_provider = "CPUExecutionProvider"
        model = None

        with suppress_stdout_stderr():
            try:
                # Initialize session with the best possible hardware provider
                model = ort.InferenceSession(self.model_path, sess_options=so, providers=valid_providers)
                # Query what ORT actually chose
                active_provider = model.get_providers()[0]
            except Exception as e:
                # Absolute fallback if drivers/environment are broken
                print(f"Acceleration failed, falling back to CPU: {e}")
                model = ort.InferenceSession(self.model_path, sess_options=so, providers=["CPUExecutionProvider"])
                active_provider = "CPUExecutionProvider"

        print(f"Model Loaded: {os.path.basename(self.model_path)} | Backend: {active_provider}")
        return model, active_provider

    def preprocess_image(self, img):
        if isinstance(img, Image.Image):
            img = np.array(img)

        h, w, _ = img.shape
        scale = min(self.input_size[0] / h, self.input_size[1] / w)
        new_w, new_h = int(w * scale), int(h * scale)

        resized_img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        padded_img = np.full((self.input_size[0], self.input_size[1], 3), 128, dtype=np.uint8)
        padded_img[:new_h, :new_w, :] = resized_img

        # Efficient conversion for ONNX input format (NCHW)
        padded_img = cv2.cvtColor(padded_img, cv2.COLOR_BGR2RGB)
        padded_img = padded_img.astype(np.float32) / 255.0
        padded_img = np.transpose(padded_img, (2, 0, 1))
        padded_img = np.expand_dims(padded_img, axis=0)

        return torch.from_numpy(padded_img), new_w, new_h

    def postprocess(self, preds, orig_img_shape, resized_shape, conf_tresh=0.6):
        preds = non_max_suppression(
            preds,
            conf_thres=conf_tresh,
            iou_thres=0.6,
            classes=None,
            agnostic=False,
        )

        orig_h, orig_w = orig_img_shape
        resized_w, resized_h = resized_shape
        scale_w, scale_h = orig_w / resized_w, orig_h / resized_h

        results = []
        for pred in preds:
            if len(pred):
                pred[:, 0] *= scale_w
                pred[:, 1] *= scale_h
                pred[:, 2] *= scale_w
                pred[:, 3] *= scale_h
                results.append(pred.cpu().numpy())
        return results

    def detect_objects(self, img, conf_tresh=0.6):
        if isinstance(img, Image.Image):
            img = np.array(img)
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            
        orig_h, orig_w = img.shape[:2]
        preprocessed_img, resized_w, resized_h = self.preprocess_image(img)
        
        # ONNX Inference
        input_data = preprocessed_img.numpy() if torch.is_tensor(preprocessed_img) else preprocessed_img
        outputs = self.model.run(None, {'images': input_data})

        detections = self.postprocess(
            torch.from_numpy(outputs[0]), 
            (orig_h, orig_w), 
            (resized_w, resized_h), 
            conf_tresh
        )

        results = {}
        for detection in detections:
            for *xyxy, conf, cls in detection:
                class_id = int(cls)
                class_name = self.classes[class_id]
                if class_name in self.ignore_classes: continue
                
                if class_name not in results: results[class_name] = []
                results[class_name].append([int(x) for x in xyxy])

        return results