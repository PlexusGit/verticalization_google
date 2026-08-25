import json
from pathlib import Path
from unittest.mock import Mock, call

import pytest

import processor


def test_gpu_available_requires_gpu_and_encoder():
    runner = Mock(side_effect=[
        Mock(returncode=0),
        Mock(returncode=0, stdout=" V..... h264_nvenc NVIDIA NVENC H.264 encoder", stderr=""),
    ])
    assert processor.gpu_available(runner) is True
    assert runner.call_count == 2


def test_gpu_unavailable_short_circuits_encoder_probe():
    runner = Mock(return_value=Mock(returncode=1))
    assert processor.gpu_available(runner) is False
    runner.assert_called_once()


def test_video_command_has_high_quality_codecs_and_scale():
    command = processor.build_video_command("in.mov", "out.mp4", 1080, nvenc=False)
    assert "libx264" in command
    assert "aac" in command
    assert "192k" in command
    assert "scale=-2:min(1080\\,ih)" in command
    assert "+faststart" in command


def test_nvenc_command_uses_hardware_encoder():
    command = processor.build_video_command("in.mov", "out.mp4", 1080, nvenc=True)
    assert "h264_nvenc" in command
    assert "libx264" not in command


def test_audio_command_is_16k_mono_flac():
    command = processor.build_audio_command("in.mov", "chunk_%03d.flac", 600)
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "16000"
    assert command[command.index("-c:a") + 1] == "flac"
    assert command[command.index("-segment_time") + 1] == "600"


def test_probe_media_parses_duration_and_resolution(monkeypatch):
    completed = Mock(stdout=json.dumps({
        "streams": [{"width": 1920, "height": 1080}],
        "format": {"duration": "12.3456"},
    }))
    monkeypatch.setattr(processor.subprocess, "run", Mock(return_value=completed))
    assert processor.probe_media("video.mp4") == {
        "duration_seconds": 12.346, "width": 1920, "height": 1080
    }


def test_processor_uploads_outputs_and_cleans_up(monkeypatch, tmp_path):
    settings = processor.Settings("bucket", temp_root=str(tmp_path), use_nvenc=False)
    source_blob, high_blob, chunk_blob = Mock(), Mock(), Mock()
    source_blob.exists.return_value = True
    source_blob.download_to_filename.side_effect = lambda filename: Path(filename).write_bytes(b"source")
    bucket = Mock()
    bucket.blob.side_effect = lambda name: {
        "input/video.mov": source_blob,
        "v1/v1_high.mp4": high_blob,
        "v1/audio/chunk_000.flac": chunk_blob,
    }[name]
    client = Mock()
    client.bucket.return_value = bucket
    monkeypatch.setattr(processor, "gpu_available", lambda: False)
    monkeypatch.setattr(processor, "probe_media", Mock(side_effect=[
        {"duration_seconds": 5.0, "width": 1920, "height": 1080},
        {"duration_seconds": 5.0, "width": 1080, "height": 608},
    ]))

    def fake_run(command, timeout, label):
        if "FLAC" in label:
            Path(command[-1].replace("%03d", "000")).write_bytes(b"audio")
        else:
            Path(command[-1]).write_bytes(b"video")

    monkeypatch.setattr(processor, "_run", fake_run)
    result = processor.TranscodeProcessor(settings, client).process("v1", "input/video.mov")

    assert result["resolution"] == {"width": 1080, "height": 608}
    assert result["duration_seconds"] == 5.0
    assert result["gcs_audio_chunks"] == [{"path": "v1/audio/chunk_000.flac", "offset_seconds": 0}]
    high_blob.upload_from_filename.assert_called_once()
    chunk_blob.upload_from_filename.assert_called_once()
    assert list(tmp_path.iterdir()) == []


def test_processor_falls_back_to_cpu(monkeypatch, tmp_path):
    settings = processor.Settings("bucket", temp_root=str(tmp_path), use_nvenc=True)
    source_blob, high_blob, chunk_blob = Mock(), Mock(), Mock()
    source_blob.exists.return_value = True
    source_blob.download_to_filename.side_effect = lambda filename: Path(filename).write_bytes(b"source")
    bucket = Mock()
    bucket.blob.side_effect = [source_blob, high_blob, chunk_blob]
    client = Mock(bucket=Mock(return_value=bucket))
    monkeypatch.setattr(processor, "gpu_available", lambda: True)
    monkeypatch.setattr(processor, "probe_media", Mock(return_value={
        "duration_seconds": 1.0, "width": 100, "height": 100,
    }))
    labels = []

    def fake_run(command, timeout, label):
        labels.append(label)
        if label.startswith("NVENC"):
            raise processor.TranscodeError("GPU unavailable")
        if "FLAC" in label:
            Path(command[-1].replace("%03d", "000")).write_bytes(b"audio")
        else:
            Path(command[-1]).write_bytes(b"video")

    monkeypatch.setattr(processor, "_run", fake_run)
    result = processor.TranscodeProcessor(settings, client).process("v1", "input.mov")
    assert labels[:2] == ["NVENC video transcode", "CPU fallback video transcode"]
    assert result["video_encoder"] == "libx264"


@pytest.mark.parametrize("value", ["../secret", "gs://bucket/object", ""])
def test_rejects_unsafe_gcs_paths(value):
    with pytest.raises(ValueError):
        processor._validate_object_path(value, "path")
