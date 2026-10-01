# Third-Party Notices — vertical render worker

This directory vendors machine-learning model weights and source code used by
the `autoframe_v10` reframing pipeline. Checksums below are validated during
the Docker image build (`Dockerfile`, `sha256sum -c`).

## YOLOv11n-face weights

- **File:** `models/yolov11n-face.pt` (Git LFS)
- **SHA-256:** `9420a9d4933940d2202f511d45df1750e3c1546dd9efc7a3985caae3bbbf7ed2`
- **Source:** <https://github.com/YapaLab/yolo-face> — release `1.0.0`
- **Weight license:** GPL-3.0
- **Runtime license:** `ultralytics` Python package is AGPL-3.0 (or Ultralytics
  Enterprise). Usage of this worker must comply with the AGPL-3.0 terms —
  accepted by the project owner for this integration.

## LR-ASD (lightweight active-speaker detection)

- **Files:** `third_party/LR-ASD/` — vendored sources
  (`model/{Model,Encoder,Classifier}.py`, `loss.py`) plus weights
  `weight/finetuning_TalkSet.model` (Git LFS)
- **Weight SHA-256:** `6b4ef53694e874e96cf630198dc479c78aebb3993bbf166aee3d926dfe7d9342`
- **Source:** <https://github.com/Junhua-Liao/LR-ASD> @ commit `1b6dcd2`
- **License:** MIT — see `third_party/LR-ASD/LICENSE`

## YuNet face detection (legacy `v3` engine / fallback path)

- **File:** `models/face_detection_yunet_2023mar.onnx`
- **Source:** OpenCV Zoo
- **License:** MIT — see `models/LICENSE`
