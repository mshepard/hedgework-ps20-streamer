# Wildlife model training (off-Pi)

Train **two YOLOv8 models** on a GPU workstation, then compile each to Hailo `.hef` for the Pi. **Do not train on the Raspberry Pi.**

| Model | Camera | Dataset | Whitelist |
|-------|--------|---------|-----------|
| `bird_v2` | cam0 — bird feeder | [NABirds](https://dl.allaboutbirds.org/nabirds) | [`ps20_birds.txt`](ps20_birds.txt) |
| `pollinator_v1` | cam1 — pollinator garden | [Georgia Tech Pollinators (Roboflow)](https://universe.roboflow.com/georgia-institute-of-technology-bqtzy/pollinators) | [`ps20_pollinators.txt`](ps20_pollinators.txt) |

This repo does **not** download datasets for you.

---

## 1. Bird model (NABirds → cam0)

### Download NABirds

Request access and download from [https://dl.allaboutbirds.org/nabirds](https://dl.allaboutbirds.org/nabirds). Extract so metadata files sit at `~/data/nabirds/`:

```
nabirds/
  images/
  classes.txt
  images.txt
  bounding_boxes.txt
  image_class_labels.txt
  train_test_split.txt
```

### Edit the PS 20 bird whitelist

[`training/ps20_birds.txt`](ps20_birds.txt) lists species names as **substrings** of NABirds class descriptions. All plumages for a species (Adult Male, Female, Juvenile, …) merge into **one YOLO class** per line — kid-friendly counts on the WordPress page.

Find what's in NABirds:

```bash
python training/scripts/list_nabirds_classes.py ~/data/nabirds --grep "Cardinal"
```

### Build the filtered YOLO dataset

```bash
python training/scripts/prepare_nabirds.py \
  --source ~/data/nabirds \
  --allowlist training/ps20_birds.txt \
  --output training/datasets/birds_v2
```

### Train

```bash
pip install ultralytics pillow
yolo detect train \
  data=training/datasets/birds_v2/data.yaml \
  model=yolov8n.pt \
  epochs=100 \
  imgsz=640 \
  project="$PWD/training/runs" \
  name=bird_v2
```

Using an absolute `project` path prevents Ultralytics from nesting the
run under `runs/detect/training/runs`. Outputs land at:

```
training/runs/bird_v2/weights/best.pt
training/runs/bird_v2/results.csv
```

If you re-run with the same `name`, Ultralytics appends `-2`, `-3`, … (`bird_v2-2`, etc.). Use the latest run directory for export, or pass `exist_ok=True` to overwrite.

### Export labels + Hailo

```bash
# Adjust RUN_DIR if your run was bird_v2-2, etc.
RUN_DIR=training/runs/bird_v2

python training/scripts/export_model_labels.py \
  training/datasets/birds_v2/data.yaml \
  training/models/bird_v2.json

yolo export model="$RUN_DIR/weights/best.pt" format=onnx imgsz=640
# ONNX output: "$RUN_DIR/weights/best.onnx"
# Compile ONNX → HEF with Hailo Dataflow Compiler on x86 Linux
```

Deploy to the Pi:

- `/var/lib/streamer/models/bird_v2.hef`
- `/var/lib/streamer/models/bird_v2.json`

---

## 2. Pollinator model (Georgia Tech Roboflow → cam1)

### Download from Roboflow

1. Open [Georgia Tech Pollinators](https://universe.roboflow.com/georgia-institute-of-technology-bqtzy/pollinators)
2. **Download Dataset** → format **YOLOv8**
3. Extract to e.g. `~/data/pollinators/`

Typical classes: `bee`, `butterfly`, `moth`, `beetle`, `grasshopper`. Confirm in the exported `data.yaml`.

### Edit the PS 20 pollinator whitelist

[`training/ps20_pollinators.txt`](ps20_pollinators.txt) — class names must match the Roboflow export exactly.

### Filter (optional — keeps only PS 20 classes)

```bash
python training/scripts/filter_yolo_dataset.py \
  --source ~/data/pollinators \
  --allowlist training/ps20_pollinators.txt \
  --output training/datasets/pollinators
```

If the Roboflow export already has only the classes you want, you can point training directly at the Roboflow `data.yaml` and skip filtering.

### Train

```bash
yolo detect train \
  data=training/datasets/pollinators/data.yaml \
  model=yolov8n.pt \
  epochs=100 \
  imgsz=640 \
  project="$PWD/training/runs" \
  name=pollinator_v1
```

Output:

```
training/runs/pollinator_v1/weights/best.pt
training/runs/pollinator_v1/results.csv
```

### Export labels + Hailo

```bash
RUN_DIR=training/runs/pollinator_v1

python training/scripts/export_model_labels.py \
  training/datasets/pollinators/data.yaml \
  training/models/pollinator_v1.json

yolo export model="$RUN_DIR/weights/best.pt" format=onnx imgsz=640
# ONNX output: "$RUN_DIR/weights/best.onnx"
# Compile ONNX → HEF
```

Deploy to the Pi:

- `/var/lib/streamer/models/pollinator_v1.hef`
- `/var/lib/streamer/models/pollinator_v1.json`

---

## 3. Enable on the Pi

In `/etc/streamer/streamer.toml`:

```toml
[wildlife]
enabled = true

[camera0.wildlife]
model_path = "/var/lib/streamer/models/bird_v2.hef"
labels_path = "/var/lib/streamer/models/bird_v2.json"

[camera1.wildlife]
model_path = "/var/lib/streamer/models/pollinator_v1.hef"
labels_path = "/var/lib/streamer/models/pollinator_v1.json"
```

Restart: `sudo bash scripts/update.sh`

---

## 4. Fine-tune on PS 20 field images (recommended)

After the mast is live, label 50–200 local images per camera (feeder + garden) and add them to the train split, then re-run `yolo detect train` for each model. This closes the gap between NABirds reference photos / Roboflow garden scenes and your actual Pi Camera 3 mounts.

---

## Kid-friendly display names

Map model class names to labels shown on [ps20.hedgework.net](https://ps20.hedgework.net/) in [`species_display_names.json`](species_display_names.json). The detector uses underscore names from training; WordPress sync can use the display map when you wire it up.
