from worker.ingest import process_ingest
from worker.normalize import process_normalize
from worker import pipeline

HANDLERS = {
    "INGEST": process_ingest,
    "NORMALIZE": process_normalize,
    "MIX": pipeline.process_mix,
    "TRIM": pipeline.process_trim,
    "BACKGROUND_REMOVE": pipeline.process_background_remove,
    "RENDER_CLEAN": pipeline.process_render_clean,
    "RENDER_SPLIT": pipeline.process_render_split,
    "RENDER_REACT": pipeline.process_render_react,
    "THUMBNAIL": pipeline.process_thumbnail,
    "ZIP": pipeline.process_zip,
}


def dispatch(job: dict):
    handler = HANDLERS.get(job.get("type"))
    if handler is None:
        raise RuntimeError(f"Tipo de job desconhecido: {job.get('type')}")
    return handler(job)
