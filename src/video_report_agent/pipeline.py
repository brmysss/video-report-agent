"""One sequential URL → ASR → Pi → report pipeline."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import traceback
import uuid
from pathlib import Path

from .asr import AsrError, transcribe_audio
from .audio import AudioExtractionError, extract_audio
from .failures import asr_failure
from .ingest import UrlIngestError, download_bilibili_video, validate_bilibili_url
from .media_config import resolve_media_config
from .paraformer import CloudAsrError
from .pi import PiError, PiRunner
from .report_image import render_report_image
from .retention import cleanup_media
from .reuse import reuse_asr, reuse_download, reuse_transcript
from .trace import RunTrace
from .transcript import build_transcript
from .transcript_foundation import parse_roi


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.replace(path)


def create_run(
    root: Path,
    url: str,
    *,
    report_mode="standard",
    transcript_mode="asr-only",
    ocr_mode="off",
    ocr_roi=None,
    subtitle_file=None,
    model_selection=None,
    model_recovery=None,
    asr_backend=None,
    asr_model=None,
    ocr_backend=None,
) -> Path:
    if report_mode not in ("standard", "brief"):
        raise ValueError("report_mode must be standard or brief")
    if transcript_mode not in {"asr-only", "fused"}:
        raise ValueError("transcript_mode must be asr-only or fused")
    if ocr_mode == "on":
        ocr_mode = "roi"
    if ocr_mode not in {"off", "auto", "roi"}:
        raise ValueError("ocr_mode must be off, auto or roi")
    if transcript_mode == "fused" and ocr_mode == "roi":
        parse_roi(ocr_roi)
    media_config = resolve_media_config(asr_backend, asr_model, ocr_backend)
    source = validate_bilibili_url(url)
    run = root.resolve() / uuid.uuid4().hex
    run.mkdir(parents=True)
    write_json(
        run / "input.json",
        {
            **({"model_selection": model_selection} if model_selection else {}),
            **({"model_recovery": model_recovery} if model_recovery else {}),
            **media_config,
            "url": source.canonical_url,
            "video_id": source.video_id,
            "report_mode": report_mode,
            "transcript_mode": transcript_mode,
            "ocr_mode": ocr_mode,
            "ocr_roi": ocr_roi,
            "subtitle_file": "subtitle" + Path(subtitle_file).suffix if subtitle_file else None,
        },
    )
    write_json(
        run / "status.json",
        {
            "run_id": run.name,
            "state": "QUEUED",
            "stage": "QUEUED",
            "started_at": time.time(),
            **({"model_name": model_recovery.get("selected", {}).get("name")}
               if model_recovery else {}),
        },
    )
    if subtitle_file:
        import shutil

        shutil.copy2(subtitle_file, run / ("subtitle" + Path(subtitle_file).suffix))
    return run


def generate(run: Path) -> dict:
    metadata = json.loads((run / "input.json").read_text())
    status = json.loads((run / "status.json").read_text())
    status.update(video_id=metadata["video_id"])

    def update(stage: str, **fields):
        if stage in {"RENDERED", "FAILED"}:
            fields["finished_at"] = time.time()
        status.update(state=stage, stage=stage, **fields)
        write_json(run / "status.json", status)

    trace = RunTrace(run)
    try:
        transcript_source = reuse_transcript(run, metadata)
        status["download_reused_from"] = None
        if transcript_source is None:
            update("DOWNLOADING")
            with trace.span("download", input={"video_id": metadata["video_id"]}) as detail:
                source = validate_bilibili_url(metadata["url"])
                request_subtitles = metadata.get("transcript_mode") == "fused"
                audio_only = metadata.get("transcript_mode", "asr-only") == "asr-only"
                downloaded, download_source = reuse_download(
                    source,
                    run,
                    request_subtitles=request_subtitles,
                    audio_only=audio_only,
                )
                if downloaded is None:
                    downloaded = download_bilibili_video(
                        source,
                        run,
                        request_subtitles=request_subtitles,
                        audio_only=audio_only,
                    )
                detail.update(
                    output={"media": downloaded.media_path.name}, reused_from=download_source,
                )
            status["download_reused_from"] = download_source
            metadata.update(
                download_audio_only=audio_only,
                title=downloaded.title,
                uploader=downloaded.uploader,
                attribution=downloaded.attribution,
            )
        write_json(run / "input.json", metadata)
        status["title"] = metadata.get("title")
        status["transcript_reused_from"] = transcript_source
        if transcript_source is None:
            update("TRANSCRIBING")
            with trace.span("ffmpeg", input={"media": downloaded.media_path.name}) as detail:
                audio = extract_audio(downloaded.media_path, run / "audio.wav")
                detail.update(output={
                    "audio": audio.path.name, "duration_ms": audio.probe.duration_ms,
                })
            with trace.span(
                "asr", backend=metadata["asr_backend"], model=metadata["asr_model"],
                input={"audio": audio.path.name},
            ) as detail:
                asr_source = reuse_asr(run, metadata)
                status["asr_reused_from"] = asr_source
                if asr_source is None:
                    asr = transcribe_audio(
                        audio.path,
                        model=metadata["asr_model"],
                        backend=metadata["asr_backend"],
                        base_url=metadata["asr_base_url"],
                        language=metadata["asr_language"],
                        parameters=metadata["asr_parameters"],
                        task_path=run / "asr-task.json",
                    )
                    write_json(run / "asr.json", asr.to_dict())
                    segments = len(asr.segments)
                else:
                    segments = len(json.loads((run / "asr.json").read_text())["segments"])
                detail.update(
                    output={"artifact": "asr.json", "segments": segments},
                    reused_from=asr_source,
                )
            with trace.span("transcript") as detail:
                options = argparse.Namespace(
                    transcript_mode=metadata.get("transcript_mode", "asr-only"),
                    ocr_mode=metadata.get("ocr_mode", "off"),
                    ocr_roi=metadata.get("ocr_roi"),
                    ocr_backend=metadata["ocr_backend"],
                    subtitle_file=(
                        run / metadata["subtitle_file"] if metadata.get("subtitle_file") else None
                    ),
                )
                build, _, _ = build_transcript(
                    options,
                    source_path=downloaded.media_path,
                    artifact_root=run,
                    asr_path=run / "asr.json",
                    source_duration_ms=audio.probe.duration_ms,
                    manifest=metadata,
                )
                transcript = [f"# {downloaded.title}", downloaded.attribution, ""]
                transcript.extend(
                    f"[{u.unit_id} | {u.start_ms / 1000:.3f}–{u.end_ms / 1000:.3f}s] "
                    f"{u.canonical_text}"
                    for u in build.canonical_units
                )
                (run / "transcript.md").write_text("\n".join(transcript))
                detail.update(output={
                    "artifact": "transcript.md", "units": len(build.canonical_units),
                })
        else:
            with trace.span("transcript") as detail:
                detail.update(reused_from=transcript_source, output={"artifact": "transcript.md"})
        update("GENERATING")
        runner_options = dict(metadata.get("model_selection", {}))
        if metadata.get("model_recovery"):
            runner_options["model_recovery"] = metadata["model_recovery"]
        runner = PiRunner(**runner_options)
        with trace.span("agent", backend=runner.provider, model=runner.model,
                        input={"artifact": "transcript.md"}) as detail:
            asyncio.run(runner.run(run))
            detail.update(output={"artifact": "report.html"})
        update("GENERATING_IMAGE")
        try:
            with trace.span("render", input={"artifact": "report.html"}) as detail:
                render_report_image(run)
                detail.update(output={"artifact": "report.png"})
        except Exception as exc:
            status["image_error"] = str(exc)
        update("RENDERED")
    except Exception as exc:
        if isinstance(exc, CloudAsrError):
            write_json(run / "asr-error.json", exc.to_dict())
            (run / "failure.log").write_text(str(exc))
        else:
            (run / "failure.log").write_text(traceback.format_exc())
        if isinstance(exc, CloudAsrError):
            category = "EXTERNAL_API_FAILURE"
        elif isinstance(exc, PiError):
            category = exc.category
        elif isinstance(exc, UrlIngestError):
            category = (
                "ENVIRONMENT_FAILURE" if "unavailable" in str(exc) else "EXTERNAL_API_FAILURE"
            )
        elif isinstance(exc, (AsrError, AudioExtractionError, OSError)):
            category = "ENVIRONMENT_FAILURE"
        else:
            category = "IMPLEMENTATION_FAILURE"
        code, message = category, str(exc)
        diagnostics = {}
        if isinstance(exc, CloudAsrError):
            code, message = asr_failure(exc)
            diagnostics = {"http_status": exc.http_status, "provider_code": exc.provider_code,
                           "failed_stage": exc.stage}
        elif isinstance(exc, UrlIngestError):
            code = exc.category
            if code in {"VIDEO_DURATION_INVALID", "URL_INVALID"}:
                category = "INPUT_REJECTED"
            elif code == "DOWNLOAD_ERROR" and str(exc) == "yt-dlp failed":
                message = "视频下载失败，可能是视频不可访问或下载服务异常，请稍后重试。"
        update("FAILED", error_category=category, error_code=code, error=message, **diagnostics)
    cleanup_media(run.parent)
    return status
