import os
import uuid
from typing import Dict, Any

import modal
from fastapi import Request

APP_NAME = "trucksense-inference"

# Build a container image with deps.
# Notes:
# - ultralytics pulls in torch and friends; for hackathon MVP this is okay.
# - opencv-python-headless avoids GUI deps.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")  # helps OpenCV decode more video codecs
    .pip_install(
        "ultralytics==8.3.0",
        "opencv-python-headless==4.10.0.84",
        "numpy==1.26.4",
        "fastapi[standard]==0.116.1",
    )
)

app = modal.App(APP_NAME, image=image)

# Persistent storage for uploaded videos (optional but handy).
vol = modal.Volume.from_name("trucksense-videos", create_if_missing=True)
VIDEO_DIR = "/data"


@app.cls(
    gpu="L4",  # good inference GPU to start; change to "A10" / "L40S" if needed
    volumes={VIDEO_DIR: vol},
    scaledown_window=300,  # keep warm briefly for faster demos
)
class CounterService:
    def _load_model(self):
        # Load model once per container (fast subsequent calls).
        from ultralytics import YOLO

        # You can swap to yolov8s.pt if accuracy matters and speed is still OK.
        self.model = YOLO("yolov8n.pt")

        # COCO class names
        self.names = self.model.names

        # Only count traffic-related classes for your pollution proxy.
        self.vehicle_classes = {"car", "truck", "bus", "motorcycle"}

    @modal.enter()
    def load_model(self):
        self._load_model()

    @modal.method()
    def count_video(self, video_path: str) -> Dict[str, Any]:
        """
        Runs YOLOv8 + ByteTrack on the video and returns:
          - unique counts per class (unique track IDs)
          - total unique vehicles
          - peak vehicles per frame (congestion proxy)
        """
        if not hasattr(self, "model"):
            self._load_model()

        # Ensure this container sees latest files committed by the uploader.
        vol.reload()

        from collections import defaultdict, Counter

        # Run tracking. Ultralytics supports ByteTrack via tracker="bytetrack.yaml". :contentReference[oaicite:2]{index=2}
        results = self.model.track(
            source=video_path,
            tracker="bytetrack.yaml",
            persist=True,
            verbose=False,
            conf=0.25,
            iou=0.5,
        )

        # Track-level aggregation
        # We'll:
        # - collect all (track_id -> list of class_names) to do majority vote
        # - track peak number of visible vehicle tracks per frame
        track_to_classes = defaultdict(list)
        peak_vehicles = 0

        for r in results:
            if r.boxes is None or r.boxes.id is None:
                continue

            ids = r.boxes.id.cpu().numpy().astype(int)
            cls = r.boxes.cls.cpu().numpy().astype(int)

            # vehicles visible this frame (unique IDs in this frame, filtered to vehicle classes)
            frame_vehicle_ids = set()

            for tid, c in zip(ids, cls):
                name = self.names[int(c)]
                if name in self.vehicle_classes:
                    track_to_classes[tid].append(name)
                    frame_vehicle_ids.add(tid)

            if len(frame_vehicle_ids) > peak_vehicles:
                peak_vehicles = len(frame_vehicle_ids)

        # Majority vote per track to reduce class flip noise
        counts_by_class = Counter()
        for tid, class_list in track_to_classes.items():
            if not class_list:
                continue
            final_class = Counter(class_list).most_common(1)[0][0]
            counts_by_class[final_class] += 1

        total_unique = sum(counts_by_class.values())

        return {
            "counts_by_class": dict(counts_by_class),
            "total_unique_vehicles": int(total_unique),
            "peak_vehicles_in_frame": int(peak_vehicles),
        }


@app.function(volumes={VIDEO_DIR: vol})
@modal.fastapi_endpoint(method="POST")
async def upload_and_count(request: Request) -> Dict[str, Any]:
    """
    HTTP endpoint:
      POST multipart/form-data with:
        - file: video
        - lat: float (optional)
        - lng: float (optional)
        - timestamp: string (optional)
    Returns JSON counts + echoes metadata.
    """
    form = await request.form()
    file = form.get("file")
    if file is None:
        return {"error": "Missing 'file' in form-data."}
    if not hasattr(file, "read"):
        return {"error": "Invalid 'file' in form-data."}

    lat = form.get("lat")
    lng = form.get("lng")
    timestamp = form.get("timestamp")

    # Save to volume so the GPU method can read it
    vid_id = str(uuid.uuid4())
    save_path = os.path.join(VIDEO_DIR, f"{vid_id}.mp4")

    # Stream upload to disk
    with open(save_path, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)

    # Make sure volume persists
    await vol.commit.aio()

    # Run GPU inference
    svc = CounterService()
    out = await svc.count_video.remote.aio(save_path)

    return {
        "video_id": vid_id,
        "lat": lat,
        "lng": lng,
        "timestamp": timestamp,
        **out,
    }


@app.local_entrypoint()
def main():
    print("Deployed. Use: modal deploy app.py")
