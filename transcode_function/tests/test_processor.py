import io
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


def write_fake_audio(command):
    """Emulate ffmpeg: one 5 s MP3 chunk plus the CSV segment list it reports."""
    chunk = Path(command[-1].replace("%03d", "000"))
    chunk.write_bytes(b"audio")
    Path(command[command.index("-segment_list") + 1]).write_text(f"{chunk.name},0.000000,5.000000\n")


def test_audio_command_is_16k_mono_mp3_with_segment_list():
    command = processor.build_audio_command("in.mov", "chunk_%03d.mp3", 600, "segments.csv", "48k")
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "16000"
    assert command[command.index("-c:a") + 1] == "libmp3lame"
    assert command[command.index("-b:a") + 1] == "48k"
    assert command[command.index("-segment_time") + 1] == "600"
    assert command[command.index("-segment_list") + 1] == "segments.csv"
    assert command[command.index("-segment_list_type") + 1] == "csv"


def test_audio_bitrate_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("BUCKET_NAME", "bucket")
    monkeypatch.delenv("AUDIO_BITRATE", raising=False)
    assert processor.Settings.from_env().audio_bitrate == "64k"
    monkeypatch.setenv("AUDIO_BITRATE", "96k")
    assert processor.Settings.from_env().audio_bitrate == "96k"


def test_read_audio_segments_uses_exact_offsets_and_drops_short_tails(tmp_path):
    for name in ("chunk_000.mp3", "chunk_001.mp3", "chunk_002.mp3"):
        (tmp_path / name).write_bytes(b"audio")
    listing = tmp_path / "segments.csv"
    listing.write_text(
        "chunk_001.mp3,600.026122,1200.026122\n"
        "chunk_000.mp3,0.000000,600.026122\n"
        "chunk_002.mp3,1200.026122,1200.3\n"
        "chunk_003.mp3,1300.0,1400.0\n"
    )
    chunks = processor.read_audio_segments(str(listing), tmp_path)
    assert [(path.name, start) for path, start, _ in chunks] == [
        ("chunk_000.mp3", 0.0),
        ("chunk_001.mp3", 600.026122),
    ]


def test_read_audio_segments_reports_a_missing_list(tmp_path):
    with pytest.raises(processor.TranscodeError, match="segment list"):
        processor.read_audio_segments(str(tmp_path / "missing.csv"), tmp_path)


def test_probe_media_parses_duration_and_resolution(monkeypatch):
    completed = Mock(stdout=json.dumps({
        "streams": [{
            "width": 1920, "height": 1080, "codec_name": "h264", "pix_fmt": "yuv420p",
            "has_b_frames": 1, "avg_frame_rate": "30000/1001",
            "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}],
        }],
        "format": {"duration": "12.3456"},
    }))
    monkeypatch.setattr(processor.subprocess, "run", Mock(return_value=completed))
    assert processor.probe_media("video.mp4") == {
        "duration_seconds": 12.346, "width": 1920, "height": 1080,
        "codec": "h264", "pix_fmt": "yuv420p", "has_b_frames": 1,
        "avg_frame_rate": "30000/1001", "rotation": -90,
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
        "v1/audio/chunk_000.mp3": chunk_blob,
    }[name]
    client = Mock()
    client.bucket.return_value = bucket
    monkeypatch.setattr(processor, "gpu_available", lambda: False)
    monkeypatch.setattr(processor, "probe_media", Mock(side_effect=[
        {"duration_seconds": 5.0, "width": 1920, "height": 1080},
        {"duration_seconds": 5.0, "width": 1080, "height": 608},
    ]))

    def fake_run(command, timeout, label, progress_total_seconds=None):
        if "MP3" in label:
            write_fake_audio(command)
        else:
            Path(command[-1]).write_bytes(b"video")

    monkeypatch.setattr(processor, "_run", fake_run)
    result = processor.TranscodeProcessor(settings, client).process("v1", "input/video.mov")

    assert result["resolution"] == {"width": 1080, "height": 608}
    assert result["duration_seconds"] == 5.0
    assert result["gcs_audio_chunks"] == [
        {"path": "v1/audio/chunk_000.mp3", "offset_seconds": 0.0, "end_seconds": 5.0}
    ]
    assert chunk_blob.upload_from_filename.call_args.kwargs["content_type"] == "audio/mpeg"
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

    def fake_run(command, timeout, label, progress_total_seconds=None):
        labels.append(label)
        if label.startswith("NVENC"):
            raise processor.TranscodeError("GPU unavailable")
        if "MP3" in label:
            write_fake_audio(command)
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


def test_video_command_uses_the_configured_preset_and_crf():
    command = processor.build_video_command("in.mov", "out.mp4", 1080, False, "veryfast", 20)
    assert command[command.index("-preset") + 1] == "veryfast"
    assert command[command.index("-crf") + 1] == "20"
    nvenc = processor.build_video_command("in.mov", "out.mp4", 1080, True, "veryfast", 20)
    assert nvenc[nvenc.index("-preset") + 1] == "p5"


def test_transcode_env_overrides_and_validation(monkeypatch):
    monkeypatch.setenv("BUCKET_NAME", "bucket")
    for name in ("TRANSCODE_PRESET", "TRANSCODE_CRF", "TRANSCODE_REMUX"):
        monkeypatch.delenv(name, raising=False)
    defaults = processor.Settings.from_env()
    assert (defaults.x264_preset, defaults.x264_crf, defaults.remux_if_compatible) == ("medium", 18, True)
    monkeypatch.setenv("TRANSCODE_PRESET", "bogus")
    assert processor.Settings.from_env().x264_preset == "medium"
    monkeypatch.setenv("TRANSCODE_PRESET", "veryfast")
    monkeypatch.setenv("TRANSCODE_CRF", "20")
    tuned = processor.Settings.from_env()
    assert (tuned.x264_preset, tuned.x264_crf) == ("veryfast", 20)
    monkeypatch.setenv("TRANSCODE_CRF", "60")
    with pytest.raises(ValueError, match="TRANSCODE_CRF"):
        processor.Settings.from_env()
    monkeypatch.setenv("TRANSCODE_CRF", "20")
    monkeypatch.setenv("TRANSCODE_REMUX", "0")
    assert processor.Settings.from_env().remux_if_compatible is False


def test_remux_command_copies_video_and_normalizes_audio():
    command = processor.build_remux_command("in.mkv", "out.mp4")
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "aac"
    assert "-movflags" in command and "+faststart" in command


def _settings(**overrides):
    return processor.Settings("bucket", **overrides)


def _info(**overrides):
    base = {
        "duration_seconds": 100.0, "width": 1920, "height": 1080,
        "codec": "h264", "pix_fmt": "yuv420p", "rotation": 0,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "info,settings,expected_reason",
    [
        (_info(), _settings(), None),
        (_info(), _settings(remux_if_compatible=False), "disabled"),
        (_info(codec="vp9"), _settings(), "codec"),
        (_info(codec="hevc"), _settings(), "codec"),
        (_info(pix_fmt="yuv420p10le"), _settings(), "pixel format"),
        (_info(height=2160), _settings(), "height"),
        (_info(height=720), _settings(), None),
        (_info(rotation=90), _settings(), "rotation"),
        (_info(rotation=-90), _settings(), "rotation"),
        (_info(codec="", pix_fmt=""), _settings(), "codec"),
    ],
)
def test_remux_blocker(info, settings, expected_reason):
    blocker = processor.remux_blocker(info, settings)
    if expected_reason is None:
        assert blocker is None
    else:
        assert expected_reason in blocker


def test_measure_max_keyframe_gap_parses_flags(monkeypatch):
    packets = "0.000000,K__\n5.000000,____\n10.200000,K__\n15.400000,____\n20.000000,K__\n"
    completed = Mock(stdout=packets)
    monkeypatch.setattr(processor.subprocess, "run", Mock(return_value=completed))
    assert processor.measure_max_keyframe_gap("v.mp4") == pytest.approx(10.2)


def test_measure_max_keyframe_gap_single_keyframe_is_rejected(monkeypatch):
    completed = Mock(stdout="0.000000,K__\n1.000000,____\n")
    monkeypatch.setattr(processor.subprocess, "run", Mock(return_value=completed))
    assert processor.measure_max_keyframe_gap("v.mp4") == float("inf")


def test_measure_max_keyframe_gap_returns_none_on_probe_failure(monkeypatch):
    monkeypatch.setattr(
        processor.subprocess, "run", Mock(side_effect=processor.subprocess.SubprocessError())
    )
    assert processor.measure_max_keyframe_gap("v.mp4") is None


def _wire_process(monkeypatch, tmp_path, probe_results, keyframe_gap):
    settings = processor.Settings("bucket", temp_root=str(tmp_path), use_nvenc=False)
    source_blob, high_blob, chunk_blob = Mock(), Mock(), Mock()
    source_blob.exists.return_value = True
    source_blob.download_to_filename.side_effect = lambda f: Path(f).write_bytes(b"source")
    bucket = Mock()
    bucket.blob.side_effect = lambda name: {
        "input/video.mov": source_blob,
        "v1/v1_high.mp4": high_blob,
        "v1/audio/chunk_000.mp3": chunk_blob,
    }[name]
    client = Mock()
    client.bucket.return_value = bucket
    monkeypatch.setattr(processor, "gpu_available", lambda: False)
    monkeypatch.setattr(processor, "probe_media", Mock(side_effect=probe_results))
    monkeypatch.setattr(processor, "measure_max_keyframe_gap", Mock(return_value=keyframe_gap))
    labels = []

    def fake_run(command, timeout, label, progress_total_seconds=None):
        labels.append(label)
        if "MP3" in label:
            write_fake_audio(command)
        else:
            Path(command[-1]).write_bytes(b"video")

    monkeypatch.setattr(processor, "_run", fake_run)
    return settings, client, labels


def test_process_remuxes_a_compliant_source(monkeypatch, tmp_path):
    settings, client, labels = _wire_process(
        monkeypatch, tmp_path,
        [
            _info(duration_seconds=5.0),
            {"duration_seconds": 5.0, "width": 1920, "height": 1080},
        ],
        keyframe_gap=4.2,
    )
    result = processor.TranscodeProcessor(settings, client).process("v1", "input/video.mov")
    assert labels == ["Video remux", "MP3 audio extraction"]
    assert result["video_encoder"] == "copy"


def test_process_reencodes_when_keyframes_are_too_sparse(monkeypatch, tmp_path):
    settings, client, labels = _wire_process(
        monkeypatch, tmp_path,
        [_info(duration_seconds=5.0), {"duration_seconds": 5.0, "width": 1920, "height": 1080}],
        keyframe_gap=60.0,
    )
    result = processor.TranscodeProcessor(settings, client).process("v1", "input/video.mov")
    assert labels == ["CPU video transcode", "MP3 audio extraction"]
    assert result["video_encoder"] == "libx264"


def test_process_reencodes_non_h264_without_measuring_keyframes(monkeypatch, tmp_path):
    settings, client, labels = _wire_process(
        monkeypatch, tmp_path,
        [_info(codec="vp9", duration_seconds=5.0), {"duration_seconds": 5.0, "width": 1920, "height": 1080}],
        keyframe_gap=1.0,  # must never be consulted
    )
    result = processor.TranscodeProcessor(settings, client).process("v1", "input/video.mov")
    assert labels == ["CPU video transcode", "MP3 audio extraction"]
    assert result["video_encoder"] == "libx264"


def test_with_progress_inserts_ffmpeg_progress_flags():
    command = processor.build_remux_command("in.mp4", "out.mp4")
    progressed = processor._with_progress(command)
    index = progressed.index("-progress")
    assert progressed[index + 1] == "pipe:1"
    assert "-nostats" in progressed
    assert progressed.index("-progress") < progressed.index("-i")
    assert "-progress" not in command  # the original command is unchanged


def test_run_with_progress_logs_and_finishes(monkeypatch, caplog):
    lines = iter(["out_time_us=250000000\n", "speed=1.8x\n", "progress=end\n"])

    class Reader:
        def __init__(self):
            self.exhausted = False

        def readline(self):
            try:
                return next(lines)
            except StopIteration:
                self.exhausted = True
                return ""

        def __iter__(self):
            return iter(())

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.stdout = Reader()
            self.stderr = io.StringIO("")
            self.returncode = 0

        def poll(self):
            return 0 if self.stdout.exhausted else None

        def kill(self):
            self.returncode = -9

        def wait(self):
            return self.returncode

    monkeypatch.setattr(processor.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(processor.select, "select", lambda r, w, x, t: (r, [], []))
    monkeypatch.setattr(processor, "PROGRESS_LOG_INTERVAL_SECONDS", 0)
    with caplog.at_level("INFO", processor.LOGGER.name):
        processor._run(["ffmpeg", "-i", "in", "out"], 60, "CPU video transcode", 500.0)
    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("CPU video transcode started") for m in messages)
    assert any("50%" in m and "speed 1.8x" in m for m in messages)
    assert any("CPU video transcode completed" in m for m in messages)


def test_run_with_progress_enforces_the_timeout(monkeypatch):
    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")
            self.returncode = None
            self.killed = False

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self):
            return self.returncode

    monkeypatch.setattr(processor.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(processor.select, "select", lambda r, w, x, t: ([], [], []))
    with pytest.raises(processor.TranscodeError, match="timed out"):
        processor._run(["ffmpeg", "-i", "in", "out"], 0, "CPU video transcode", 500.0)


def test_run_without_progress_still_logs_start_and_end(monkeypatch, caplog):
    completed = Mock(returncode=0, stderr="", stdout="")
    monkeypatch.setattr(processor.subprocess, "run", Mock(return_value=completed))
    with caplog.at_level("INFO", processor.LOGGER.name):
        processor._run(["echo", "hi"], 60, "step")
    messages = [record.getMessage() for record in caplog.records]
    assert messages[0] == "step started"
    assert messages[-1].startswith("step completed")
