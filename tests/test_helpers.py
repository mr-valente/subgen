"""Tests for pure helper functions in subgen.py."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import subgen
from language_code import LanguageCode


class TestConvertToBool:
    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "on", "ON", "1", "y", "yes", "YES"])
    def test_truthy_values(self, value):
        assert subgen.convert_to_bool(value) is True

    @pytest.mark.parametrize("value", ["false", "False", "FALSE", "off", "OFF", "0", "n", "no", "NO"])
    def test_falsy_values(self, value):
        assert subgen.convert_to_bool(value) is False

    def test_bool_true_input(self):
        assert subgen.convert_to_bool(True) is True

    def test_bool_false_input(self):
        assert subgen.convert_to_bool(False) is False


class TestGenerateAudioHash:
    def test_deterministic(self):
        data = b"fake audio bytes"
        h1 = subgen.generate_audio_hash(data, "transcribe", "en")
        h2 = subgen.generate_audio_hash(data, "transcribe", "en")
        assert h1 == h2

    def test_different_content_different_hash(self):
        assert (
            subgen.generate_audio_hash(b"audio1")
            != subgen.generate_audio_hash(b"audio2")
        )

    def test_different_task_different_hash(self):
        data = b"same audio"
        assert (
            subgen.generate_audio_hash(data, "transcribe")
            != subgen.generate_audio_hash(data, "translate")
        )

    def test_different_language_different_hash(self):
        data = b"same audio"
        assert (
            subgen.generate_audio_hash(data, language="en")
            != subgen.generate_audio_hash(data, language="fr")
        )

    def test_returns_16_char_string(self):
        h = subgen.generate_audio_hash(b"data")
        assert isinstance(h, str) and len(h) == 16

    def test_no_task_no_language(self):
        h = subgen.generate_audio_hash(b"data")
        assert isinstance(h, str) and len(h) == 16


class TestGetEnvWithFallback:
    def test_new_name_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("NEW_VAR", "new_value")
        monkeypatch.setenv("OLD_VAR", "old_value")
        result = subgen.get_env_with_fallback("NEW_VAR", "OLD_VAR", "default")
        assert result == "new_value"

    def test_falls_back_to_old_name(self, monkeypatch):
        monkeypatch.delenv("NEW_VAR", raising=False)
        monkeypatch.setenv("OLD_VAR", "old_value")
        result = subgen.get_env_with_fallback("NEW_VAR", "OLD_VAR", "default")
        assert result == "old_value"

    def test_uses_default_when_neither_set(self, monkeypatch):
        monkeypatch.delenv("NEW_VAR", raising=False)
        monkeypatch.delenv("OLD_VAR", raising=False)
        result = subgen.get_env_with_fallback("NEW_VAR", "OLD_VAR", "default")
        assert result == "default"

    def test_convert_func_applied(self, monkeypatch):
        monkeypatch.setenv("NEW_VAR", "true")
        result = subgen.get_env_with_fallback("NEW_VAR", "OLD_VAR", False, subgen.convert_to_bool)
        assert result is True

    def test_convert_func_not_applied_to_none_default(self, monkeypatch):
        monkeypatch.delenv("NEW_VAR", raising=False)
        monkeypatch.delenv("OLD_VAR", raising=False)
        result = subgen.get_env_with_fallback("NEW_VAR", "OLD_VAR", None, subgen.convert_to_bool)
        assert result is None


class TestPathMapping:
    def test_disabled_returns_original(self, monkeypatch):
        monkeypatch.setattr(subgen, "use_path_mapping", False)
        monkeypatch.setattr(subgen, "path_mapping_from", "/tv")
        monkeypatch.setattr(subgen, "path_mapping_to", "/Volumes/TV")
        assert subgen.path_mapping("/tv/show.mkv") == "/tv/show.mkv"

    def test_enabled_replaces_prefix(self, monkeypatch):
        monkeypatch.setattr(subgen, "use_path_mapping", True)
        monkeypatch.setattr(subgen, "path_mapping_from", "/tv")
        monkeypatch.setattr(subgen, "path_mapping_to", "/Volumes/TV")
        assert subgen.path_mapping("/tv/show.mkv") == "/Volumes/TV/show.mkv"

    def test_enabled_no_match_returns_original(self, monkeypatch):
        monkeypatch.setattr(subgen, "use_path_mapping", True)
        monkeypatch.setattr(subgen, "path_mapping_from", "/tv")
        monkeypatch.setattr(subgen, "path_mapping_to", "/Volumes/TV")
        assert subgen.path_mapping("/movies/film.mkv") == "/movies/film.mkv"


class TestFileExtensions:
    @pytest.mark.parametrize("fname", ["movie.mkv", "movie.mp4", "movie.avi", "movie.MOV"])
    def test_has_video_extension(self, fname):
        assert subgen.has_video_extension(fname) is True

    @pytest.mark.parametrize("fname", ["subtitle.srt", "text.txt", "audio.mp3"])
    def test_has_no_video_extension(self, fname):
        assert subgen.has_video_extension(fname) is False

    @pytest.mark.parametrize("fname", ["track.mp3", "track.flac", "track.WAV"])
    def test_has_audio_extension(self, fname):
        assert subgen.has_audio_extension(fname) is True

    @pytest.mark.parametrize("fname", ["movie.mkv", "subtitle.srt"])
    def test_has_no_audio_extension(self, fname):
        assert subgen.has_audio_extension(fname) is False

    def test_is_audio_file_extension_case_insensitive(self):
        assert subgen.is_audio_file_extension(".MP3") is True
        assert subgen.is_audio_file_extension(".mp3") is True
        assert subgen.is_audio_file_extension(".Flac") is True


class TestRemoteAudioHelpers:
    def test_prepare_upload_wraps_unknown_encode_false_pcm_as_wav(self):
        pcm_bytes = b"\0\0" * 16000

        audio_content, remote_name = subgen.prepare_upload_for_remote(
            pcm_bytes,
            encode=False,
            upload_filename="audio_file",
            upload_content_type=None,
        )

        assert remote_name == "audio_file.wav"
        assert audio_content[:4] == b"RIFF"
        assert subgen.wav_duration_seconds(audio_content) == 1.0

    def test_prepare_upload_slices_unknown_encode_false_pcm(self):
        pcm_bytes = b"\0\0" * 16000 * 3

        audio_content, remote_name = subgen.prepare_upload_for_remote(
            pcm_bytes,
            encode=False,
            upload_filename="audio_file",
            upload_content_type=None,
            start_time=1,
            duration=1,
        )

        assert remote_name == "audio_file.wav"
        assert audio_content[:4] == b"RIFF"
        assert subgen.wav_duration_seconds(audio_content) == 1.0

    def test_upload_source_name_adds_extension_from_content_type(self):
        assert subgen.upload_source_name("audio_file", content_type="video/mp4") == "audio_file.mp4"

    def test_openai_audio_url_uses_base_url(self, monkeypatch):
        monkeypatch.setattr(subgen, "openai_base_url", "http://asr.local/v1")
        monkeypatch.setattr(subgen, "openai_transcriptions_url", "")
        monkeypatch.setattr(subgen, "openai_translations_url", "")

        assert subgen.openai_audio_url("transcribe") == "http://asr.local/v1/audio/transcriptions"
        assert subgen.openai_audio_url("translate") == "http://asr.local/v1/audio/translations"

    def test_openai_audio_url_rewrites_full_endpoint_base(self, monkeypatch):
        monkeypatch.setattr(subgen, "openai_base_url", "http://asr.local/v1/audio/transcriptions")
        monkeypatch.setattr(subgen, "openai_transcriptions_url", "")
        monkeypatch.setattr(subgen, "openai_translations_url", "")

        assert subgen.openai_audio_url("translate") == "http://asr.local/v1/audio/translations"

    def test_shift_srt_timestamps(self):
        srt = "1\n00:00:01,000 --> 00:00:02,500\nHello\n"
        shifted = subgen.shift_srt_timestamps(srt, 3.25)

        assert "00:00:04,250 --> 00:00:05,750" in shifted

    def test_segments_to_srt(self):
        result = subgen.segments_to_srt([
            {"start": 1.2, "end": 2.4, "text": "Hello"},
        ])

        assert "00:00:01,200 --> 00:00:02,400" in result
        assert "Hello" in result

    def test_wav_duration_seconds(self):
        import io
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\0\0" * 16000)

        assert subgen.wav_duration_seconds(buf.getvalue()) == 1.0

    def test_prepare_upload_prefers_accessible_video_file(self, monkeypatch):
        extracted = []
        transcoded = []

        monkeypatch.setattr(subgen.os.path, "isfile", lambda path: path == "/media/show.mp4")
        monkeypatch.setattr(
            subgen,
            "extract_audio_for_remote",
            lambda *args, **kwargs: extracted.append((args, kwargs)) or (b"wav-bytes", "show.wav"),
        )
        monkeypatch.setattr(
            subgen,
            "transcode_upload_for_remote",
            lambda *args, **kwargs: transcoded.append((args, kwargs)) or (b"bad", "bad.wav"),
        )

        audio_content, remote_name = subgen.prepare_upload_for_remote(
            b"video-bytes",
            encode=False,
            upload_filename="upload.mp4",
            upload_content_type="video/mp4",
            video_file="/media/show.mp4",
            language=LanguageCode.ENGLISH,
        )

        assert audio_content == b"wav-bytes"
        assert remote_name == "show.wav"
        assert len(extracted) == 1
        assert extracted[0][0][0] == "/media/show.mp4"
        assert extracted[0][1]["language"] == LanguageCode.ENGLISH
        assert transcoded == []

    def test_prepare_upload_transcodes_video_upload_even_when_encode_false(self, monkeypatch):
        transcoded = []

        monkeypatch.setattr(subgen.os.path, "isfile", lambda path: False)
        monkeypatch.setattr(
            subgen,
            "transcode_upload_for_remote",
            lambda *args, **kwargs: transcoded.append((args, kwargs)) or (b"wav-bytes", "show.wav"),
        )

        audio_content, remote_name = subgen.prepare_upload_for_remote(
            b"video-bytes",
            encode=False,
            upload_filename="show.mp4",
            upload_content_type="video/mp4",
            video_file="/missing/show.mp4",
        )

        assert audio_content == b"wav-bytes"
        assert remote_name == "show.wav"
        assert len(transcoded) == 1
        assert transcoded[0][0][1] == "show.mp4"

    def test_prepare_upload_passthrough_saves_debug_payload(self, monkeypatch):
        saved = []

        monkeypatch.setattr(subgen.os.path, "isfile", lambda path: False)
        monkeypatch.setattr(subgen, "audio_debug_save", True)
        monkeypatch.setattr(
            subgen,
            "save_audio_debug_payload",
            lambda *args, **kwargs: saved.append((args, kwargs)),
        )

        audio_content, remote_name = subgen.prepare_upload_for_remote(
            b"audio-bytes",
            encode=False,
            upload_filename="clip.mp3",
            upload_content_type="audio/mpeg",
        )

        assert audio_content == b"audio-bytes"
        assert remote_name == "clip.mp3"
        assert len(saved) == 1
        assert saved[0][0][1] == "clip.mp3"

    def test_transcode_upload_retries_via_temp_file(self, monkeypatch):
        removed = []

        monkeypatch.setattr(
            subgen,
            "transcode_upload_pipe_for_remote",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pipe failed")),
        )
        monkeypatch.setattr(subgen, "write_upload_payload_to_tempfile", lambda *args, **kwargs: "/tmp/subgen-upload.mp4")
        monkeypatch.setattr(
            subgen,
            "transcode_upload_file_for_remote",
            lambda *args, **kwargs: (b"wav-bytes", "show.wav"),
        )
        monkeypatch.setattr(subgen.os, "unlink", lambda path: removed.append(path))

        audio_content, remote_name = subgen.transcode_upload_for_remote(b"video-bytes", "show.mp4")

        assert audio_content == b"wav-bytes"
        assert remote_name == "show.wav"
        assert removed == ["/tmp/subgen-upload.mp4"]

    def test_prepare_upload_raises_when_video_transcode_fails(self, monkeypatch):
        monkeypatch.setattr(subgen.os.path, "isfile", lambda path: False)
        monkeypatch.setattr(
            subgen,
            "transcode_upload_for_remote",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad upload")),
        )

        with pytest.raises(ValueError, match="bad upload"):
            subgen.prepare_upload_for_remote(
                b"video-bytes",
                encode=False,
                upload_filename="audio_file",
                upload_content_type="video/mp4",
            )

    def test_prepare_upload_falls_back_to_passthrough_for_audio_when_transcode_fails(self, monkeypatch):
        passed = []

        monkeypatch.setattr(subgen.os.path, "isfile", lambda path: False)
        monkeypatch.setattr(
            subgen,
            "transcode_upload_for_remote",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad transcode")),
        )
        monkeypatch.setattr(
            subgen,
            "passthrough_upload_for_remote",
            lambda *args, **kwargs: passed.append((args, kwargs)) or (b"audio-bytes", "clip.mp3"),
        )

        audio_content, remote_name = subgen.prepare_upload_for_remote(
            b"audio-bytes",
            encode=True,
            upload_filename="clip.mp3",
            upload_content_type="audio/mpeg",
        )

        assert audio_content == b"audio-bytes"
        assert remote_name == "clip.mp3"
        assert len(passed) == 1
