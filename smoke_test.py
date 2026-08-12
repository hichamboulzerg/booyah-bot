"""Local non-network smoke tests for the bot's media and persistence pipeline."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg

import bot


def main() -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="bot-smoke-") as temporary:
        work = Path(temporary)
        source = work / "source.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=1280x720:rate=30",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:sample_rate=44100",
                "-t",
                "3",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(source),
            ],
            check=True,
        )
        expected_sizes = {
            "vertical_blur": (720, 1280),
            "vertical_crop": (720, 1280),
            "landscape": (1280, 720),
            "square": (720, 720),
            "gaming": (720, 1280),
        }
        rendered = {}
        templates = list(bot.BRAND_TEMPLATES)
        for index, (layout, expected_size) in enumerate(expected_sizes.items()):
            render_dir = work / layout
            render_dir.mkdir()
            output = bot.brand_video(
                source,
                render_dir,
                "TEST HEADLINE",
                "Booyah King",
                work / "missing-logo.png",
                ffmpeg,
                layout,
                templates[index % len(templates)],
            )
            frames = imageio_ffmpeg.read_frames(str(output))
            metadata = next(frames)
            frames.close()
            assert metadata["size"] == expected_size, (layout, metadata)
            assert output.stat().st_size > 0
            rendered[layout] = metadata["size"]

        job_id = "a" * 32
        job_dir = work / job_id
        job_dir.mkdir()
        cached = job_dir / "final.mp4"
        cached.write_bytes((work / "vertical_blur" / "final.mp4").read_bytes())
        bot.write_job(
            job_dir,
            {
                "job_id": job_id,
                "video_filename": cached.name,
                "status": "scheduled",
                "scheduled_at": "2099-01-01T00:00:00+00:00",
            },
        )
        _, loaded, loaded_video = bot.load_job(work, job_id)
        assert loaded["job_id"] == job_id
        assert loaded_video.resolve() == cached.resolve()
        assert len(bot.scheduled_jobs(work)) == 1
        state_db = work / "state.db"
        bot.init_state_db(state_db)
        published_job = {
            "youtube_url": "https://youtu.be/example",
            "trim_text": "00:00:00-00:01:00",
            "fingerprint": bot.clip_fingerprint(
                "https://youtu.be/example", "00:00:00-00:01:00"
            ),
        }
        bot.record_published(state_db, published_job, "https://facebook.com/test")
        reason = bot.duplicate_reason(
            state_db,
            work,
            published_job["youtube_url"],
            published_job["trim_text"],
        )
        assert reason and "published" in reason

        expired_id = "b" * 32
        expired_dir = work / expired_id
        expired_dir.mkdir()
        expired_video = expired_dir / "final.mp4"
        expired_video.write_bytes(b"test")
        bot.write_job(
            expired_dir,
            {
                "job_id": expired_id,
                "video_filename": expired_video.name,
                "status": "ready",
                "created_at": "2000-01-01T00:00:00+00:00",
            },
        )
        removed, _ = bot.cleanup_expired_jobs(work, 14, 30)
        assert removed == 1 and not expired_dir.exists()
        print(json.dumps({"layouts": rendered, "cache": "OK"}))


if __name__ == "__main__":
    main()
