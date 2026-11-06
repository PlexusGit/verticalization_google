"""Cloud Run Job entry point: runs one pipeline outside the web instance.

Background tasks inside the web service die when Cloud Run shuts an idle
instance down (about 15 minutes without requests), when a new revision is
deployed, or when traffic scales in. A job execution has none of those
limits, so long pipelines are dispatched here (see ``app.services.dispatch``).

Configured through environment overrides set per execution:
``PIPELINE_KIND`` (``process`` or ``finals``), ``PIPELINE_VIDEO_ID``,
``PIPELINE_RESUME`` (``1``/``0``) and, for finals, ``PIPELINE_CHAPTER_IDS``.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Mapping

logger = logging.getLogger("plexus.job")


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s %(message)s")
    # Imported late so a bad environment is reported before Firestore is touched.
    from app.services import processing

    kind = (env.get("PIPELINE_KIND") or "process").strip()
    video_id = (env.get("PIPELINE_VIDEO_ID") or "").strip()
    resume = (env.get("PIPELINE_RESUME") or "0").strip() == "1"
    if not video_id:
        logger.error("PIPELINE_VIDEO_ID não informado")
        return 2
    logger.info("Job de pipeline iniciado kind=%s video=%s resume=%s", kind, video_id, resume)
    if kind == "process":
        processing.process_video(video_id, resume)
    elif kind == "finals":
        chapter_ids = [item.strip() for item in (env.get("PIPELINE_CHAPTER_IDS") or "").split(",") if item.strip()]
        processing.render_final_chapters(video_id, chapter_ids, resume)
    else:
        logger.error("PIPELINE_KIND desconhecido: %s", kind)
        return 2
    logger.info("Job de pipeline concluído kind=%s video=%s", kind, video_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
