# GNI Vertical Render Worker

A Functions Framework/Cloud Run worker that downloads source media and optional
logo assets from GCS, renders a fixed 1080x1920 MP4, uploads it to GCS, and updates
Firestore render/chapter state.

## API

`GET /health` is unauthenticated. `OPTIONS` supports CORS. `POST /` requires the
`X-API-Key` header to exactly match `API_KEY` (the worker fails closed when unset).
A representative body is:

```json
{
  "video_id": "video-1", "chapter_id": "chapter-1", "render_id": "render-1",
  "source_gcs_path": "gs://bucket/source.mp4",
  "output_gcs_path": "gs://bucket/renders/render-1.mp4",
  "trim_start": 10, "trim_end": 30,
  "video_resolution": {"w": 1920, "h": 1080},
  "mode": "split_stack",
  "keyframes_top": [{"time": 10, "x": 0, "y": 0}],
  "keyframes_bottom": [{"time": 10, "x": 900, "y": 540}],
  "captions": [{"start": 11, "end": 14, "text": "Legenda editável"}],
  "caption_style": {"font": "DejaVu Sans", "size": 64, "color": "#FFFFFF", "margin_bottom": 170},
  "logo": {"gcs_path": "gs://bucket/assets/logo.png", "position": "top_right", "size": 180, "opacity": 0.8},
  "text_overlays": [{"text": "AO VIVO", "position": "top_left", "size": 48, "opacity": 1, "color": "#FF0000"}]
}
```

`center_crop` uses the horizontal center. The documented
`select_automatic_focal_point` function in `renderer.py` is the intended future
active-speaker detector integration point. `split_stack` ports RPP's independent
top/bottom x/y keyframe model, including cosine easing and crop-bound clamping.
Caption times and keyframe times are absolute source-media times.

Firestore paths are `COLLECTION_VIDEOS/{video_id}/renders/{render_id}` and, when
provided, `COLLECTION_VIDEOS/{video_id}/chapters/{chapter_id}`. Statuses are
`PROCESSING`, `COMPLETED`, and `ERROR`.

## Local test and deployment

```bash
python3 -m pip install -r requirements-test.txt
./test.sh
./deploy.sh
```

`deploy.sh` reads `../.env`, pushes to Artifact Registry, and deploys with render-
appropriate Cloud Run limits. The runtime service account needs Storage object
read/write and Firestore write access. For stronger secret handling, configure
Cloud Run/Secret Manager after first deployment instead of retaining `API_KEY` as
a plain environment variable.
