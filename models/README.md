# Models

The detection process expects the YOLO weights at:

```
models/yolov8n.pt
```

The file is gitignored (`models/*.pt`) and is not part of the repository.

## Download

From the project folder:

```bat
cd models
..\.venv\Scripts\python.exe -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
cd ..
```

Ultralytics downloads `yolov8n.pt` into the current folder, which is why the command runs inside `models`. You can also download `yolov8n.pt` from the Ultralytics assets releases on GitHub and place it here.

The detection process itself never downloads a model. If the file is missing it stops with exit code 4 and prints the command above.

## Using another model

Any Ultralytics detection model trained on COCO (class 0 = person) works, for example `yolov8s.pt` for better accuracy at lower speed on the CPU.

- For one run: `scripts\run_detection.bat --model models\yolov8s.pt`
- Permanently: set `detection.model_path` in `config/config.yaml`, or `TPD_MODEL_PATH` in `.env`

Relative paths are resolved against the project root. Only the person class is used; all other classes are ignored.
