"""Telegram workflow for turning YouTube clips into branded Facebook videos."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import textwrap
import time
import uuid
import warnings
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import imageio_ffmpeg
import requests
from local_infographic import create_infographic
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

(
    ASK_URL,
    CHOOSE_SOURCE_ACTION,
    ASK_TRIM,
    ASK_CAPTION,
    ASK_HEADLINE,
    CHOOSE_LAYOUT,
    CHOOSE_TEMPLATE,
    CONFIRM,
) = range(8)
EDIT_VALUE, SCHEDULE_VALUE = range(20, 22)
IMAGE_WAIT_PHOTO, IMAGE_WAIT_CAPTION = range(30, 32)
GENERATE_IMAGE_TOPIC = 40
TIME_RANGE_RE = re.compile(
    r"^(?P<start>\d{1,3}:\d{2}:\d{2})-(?P<end>\d{1,3}:\d{2}:\d{2})$"
)
JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
LAYOUTS = {
    "vertical_blur": "Vertical blur",
    "vertical_crop": "Vertical crop",
    "landscape": "Landscape",
    "square": "Square",
    "gaming": "Gaming frame",
}
BRAND_TEMPLATES = {
    "gaming": {"name": "Gaming", "color": "0x4422aa", "accent": "0x00d9ff"},
    "breaking": {"name": "Breaking News", "color": "0xb00020", "accent": "white"},
    "highlights": {"name": "Highlights", "color": "0x9a6700", "accent": "0xffdf5d"},
    "funny": {"name": "Funny", "color": "0xa00078", "accent": "0xff9de2"},
}

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
LOGGER = logging.getLogger(__name__)
warnings.filterwarnings(
    "ignore",
    message=r"If 'per_message=False', 'CallbackQueryHandler'.*",
)


def required_setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required setting: {name}")
    return value


def parse_allowed_users(raw: str) -> set[int]:
    try:
        return {int(item.strip()) for item in raw.split(",") if item.strip()}
    except ValueError as exc:
        raise RuntimeError("ALLOWED_USER_IDS must contain comma-separated numbers") from exc


def is_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme in {"http", "https"} and parsed.hostname in YOUTUBE_HOSTS
    except ValueError:
        return False


def timestamp_seconds(value: str) -> int:
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    if minutes > 59 or seconds > 59:
        raise ValueError("Minutes and seconds must be between 00 and 59")
    return hours * 3600 + minutes * 60 + seconds


def seconds_timestamp(value: float) -> str:
    total = max(0, int(value))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def parse_trim(value: str, max_seconds: int) -> tuple[int, int] | None:
    if value.strip().lower() == "full":
        return None
    match = TIME_RANGE_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("Use HH:MM:SS-HH:MM:SS, for example 00:00:10-00:01:30")
    start = timestamp_seconds(match.group("start"))
    end = timestamp_seconds(match.group("end"))
    if end <= start:
        raise ValueError("The end time must be after the start time")
    if end - start > max_seconds:
        raise ValueError(f"The clip cannot be longer than {max_seconds} seconds")
    return start, end


def parse_trim_ranges(
    value: str, max_seconds: int, max_batch_clips: int
) -> list[tuple[tuple[int, int] | None, str]]:
    parts = [part.strip() for part in re.split(r"[,\n]+", value) if part.strip()]
    if not parts:
        raise ValueError("Send at least one trim range")
    if len(parts) > max_batch_clips:
        raise ValueError(f"A batch can contain at most {max_batch_clips} clips")
    if any(part.lower() == "full" for part in parts) and len(parts) > 1:
        raise ValueError("full cannot be combined with other trim ranges")
    return [(parse_trim(part, max_seconds), part.lower()) for part in parts]


def clip_fingerprint(url: str, trim_text: str) -> str:
    value = f"{url.strip()}\n{trim_text.strip().lower()}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def init_state_db(path: Path) -> None:
    with closing(sqlite3.connect(path, timeout=30)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS published_clips (
                fingerprint TEXT PRIMARY KEY,
                youtube_url TEXT NOT NULL,
                trim_text TEXT NOT NULL,
                post_url TEXT NOT NULL,
                published_at TEXT NOT NULL
            )
            """
        )


def record_published(state_db: Path, job: dict, post_url: str) -> None:
    fingerprint = job.get("fingerprint") or clip_fingerprint(
        job.get("youtube_url", ""), job.get("trim_text", "full")
    )
    with closing(sqlite3.connect(state_db, timeout=30)) as connection, connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO published_clips
            (fingerprint, youtube_url, trim_text, post_url, published_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                job.get("youtube_url", ""),
                job.get("trim_text", "full"),
                post_url,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def duplicate_reason(
    state_db: Path, download_root: Path, url: str, trim_text: str
) -> str | None:
    fingerprint = clip_fingerprint(url, trim_text)
    with closing(sqlite3.connect(state_db, timeout=30)) as connection, connection:
        row = connection.execute(
            "SELECT published_at FROM published_clips WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
    if row:
        return f"already published on {row[0][:10]}"
    for job_file in download_root.glob("*/job.json"):
        try:
            job = json.loads(job_file.read_text(encoding="utf-8-sig"))
            job_fingerprint = job.get("fingerprint") or clip_fingerprint(
                job.get("youtube_url", ""), job.get("trim_text", "full")
            )
            if job_fingerprint == fingerprint:
                return f"already exists as a {job.get('status', 'cached')} draft"
        except (OSError, KeyError, ValueError):
            continue
    return None


def make_headline(caption: str) -> str:
    words = caption.replace("\n", " ").split()
    return " ".join(words[:8])[:80] or "New video"


def image_draft_path(image_root: Path, image_id: str) -> Path:
    if not JOB_ID_RE.fullmatch(image_id):
        raise RuntimeError("Invalid image draft ID")
    root = image_root.resolve()
    draft = (root / image_id).resolve()
    if root not in draft.parents:
        raise RuntimeError("Invalid image draft path")
    return draft


async def image_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "Send one educational image as a Telegram photo or image file. "
        "It will use the separate education Facebook Page, never Booyah King."
    )
    return IMAGE_WAIT_PHOTO


async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    message = update.message
    telegram_file = None
    suffix = ".jpg"
    if message.photo:
        telegram_file = await message.photo[-1].get_file()
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        telegram_file = await message.document.get_file()
        suffix = Path(message.document.file_name or "image.jpg").suffix.lower() or ".jpg"
    if telegram_file is None:
        await message.reply_text("Please send a JPG, PNG or WebP image.")
        return IMAGE_WAIT_PHOTO
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        await message.reply_text("Supported image types: JPG, PNG and WebP.")
        return IMAGE_WAIT_PHOTO

    image_id = uuid.uuid4().hex
    draft_dir = image_draft_path(context.application.bot_data["image_root"], image_id)
    draft_dir.mkdir(parents=True, exist_ok=False)
    image_path = draft_dir / f"image{suffix}"
    try:
        await telegram_file.download_to_drive(custom_path=image_path)
    except Exception:
        shutil.rmtree(draft_dir, ignore_errors=True)
        raise
    context.user_data["image_id"] = image_id
    context.user_data["image_filename"] = image_path.name
    await message.reply_text("Image received. Now send the Facebook caption.")
    return IMAGE_WAIT_CAPTION


async def receive_image_caption(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    caption = update.message.text.strip()
    if not caption:
        await update.message.reply_text("The caption cannot be empty.")
        return IMAGE_WAIT_CAPTION
    image_id = context.user_data.get("image_id", "")
    draft_dir = image_draft_path(context.application.bot_data["image_root"], image_id)
    image_path = draft_dir / context.user_data["image_filename"]
    draft = {
        "image_id": image_id,
        "user_id": update.effective_user.id,
        "image_filename": image_path.name,
        "caption": caption,
        "status": "ready",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (draft_dir / "image.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Publish to education Page", callback_data=f"image_publish:{image_id}"),
        InlineKeyboardButton("Cancel", callback_data=f"image_cancel:{image_id}"),
    ]])
    with image_path.open("rb") as handle:
        await update.message.reply_photo(
            photo=handle,
            caption=(f"Image preview\n\n{caption}")[:1024],
            reply_markup=keyboard,
            read_timeout=120,
            write_timeout=120,
        )
    context.user_data.clear()
    return ConversationHandler.END


def generate_education_image(
    topic: str,
    draft_dir: Path,
    local_config: dict,
) -> tuple[Path, str, str]:
    image_path = draft_dir / "generated.png"
    caption, lesson = create_infographic(
        topic,
        image_path,
        local_config["endpoint"],
        local_config["model"],
    )
    return image_path, caption, lesson


def image_generation_error_message(exc: Exception) -> str:
    text = str(exc)
    if "connection" in text.lower() or "11434" in text:
        return (
            "The local Ollama service is unavailable. Start Ollama, then run "
            "/create_image again."
        )
    if "404" in text or "not found" in text.lower():
        return "The configured local Ollama model is not installed."
    return f"Local image generation failed: {text[:500]}"


async def create_image_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "Send an educational topic, for example: Python lists, DNS explained, "
        "or 20 useful Windows shortcuts."
    )
    return GENERATE_IMAGE_TOPIC


async def receive_generated_image_topic(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    topic = update.message.text.strip()[:160]
    if not topic:
        await update.message.reply_text("The topic cannot be empty.")
        return GENERATE_IMAGE_TOPIC
    progress = await update.message.reply_text(
        "Writing the lesson and generating the notebook-style image…"
    )
    image_id = uuid.uuid4().hex
    draft_dir = image_draft_path(context.application.bot_data["image_root"], image_id)
    draft_dir.mkdir(parents=True, exist_ok=False)
    try:
        image_path, caption, lesson = await asyncio.to_thread(
            generate_education_image,
            topic,
            draft_dir,
            context.application.bot_data["local_image"],
        )
    except Exception as exc:
        shutil.rmtree(draft_dir, ignore_errors=True)
        LOGGER.exception("Education image generation failed")
        await progress.edit_text(image_generation_error_message(exc))
        return ConversationHandler.END
    draft = {
        "image_id": image_id,
        "user_id": update.effective_user.id,
        "image_filename": image_path.name,
        "caption": caption,
        "topic": topic,
        "lesson": lesson,
        "generated": True,
        "status": "ready",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (draft_dir / "image.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Publish to education Page", callback_data=f"image_publish:{image_id}"
            ),
            InlineKeyboardButton(
                "Regenerate", callback_data=f"image_regenerate:{image_id}"
            ),
        ],
        [InlineKeyboardButton("Cancel", callback_data=f"image_cancel:{image_id}")],
    ])
    with image_path.open("rb") as handle:
        await update.message.reply_photo(
            photo=handle,
            caption=(f"Generated preview: {topic}\n\n{caption}")[:1024],
            reply_markup=keyboard,
            read_timeout=300,
            write_timeout=300,
        )
    await progress.edit_text(
        "Preview ready. Check every word before publishing; regenerate if needed."
    )
    context.user_data.clear()
    return ConversationHandler.END


async def authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    allowed_users: set[int] = context.application.bot_data["allowed_users"]
    if user and user.id in allowed_users:
        return True
    LOGGER.warning("Rejected request from Telegram user %s", user.id if user else "unknown")
    if update.callback_query:
        await update.callback_query.answer("This bot is private.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("This bot is private.")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "Send a YouTube video link. Use /cancel at any time to stop."
    )
    return ASK_URL


async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    value = update.message.text.strip()
    if not is_youtube_url(value):
        await update.message.reply_text("That doesn't look like a YouTube URL. Try again.")
        return ASK_URL
    context.user_data["youtube_url"] = value
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✂ Choose trim", callback_data="source:trim"),
            InlineKeyboardButton("✨ AI highlights", callback_data="source:highlights"),
        ]]
    )
    await update.message.reply_text("How should I select the clip?", reply_markup=keyboard)
    return CHOOSE_SOURCE_ACTION


async def choose_source_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    if query.data == "source:trim":
        await query.edit_message_text(
            "Send one trim range as HH:MM:SS-HH:MM:SS, or send several ranges "
            "separated by commas to create a batch. You can also send full."
        )
        return ASK_TRIM

    ai = context.application.bot_data["openai"]
    if not ai["api_key"]:
        await query.edit_message_text(
            "AI highlights need OPENAI_API_KEY in .env.\n\n"
            "For now, send the trim range as HH:MM:SS-HH:MM:SS, or full."
        )
        return ASK_TRIM

    await query.edit_message_text(
        "🎧 AI highlights: downloading and transcribing audio (10%).\n"
        "This can take a few minutes."
    )
    try:
        suggestions = await asyncio.to_thread(
            find_highlights,
            context.user_data["youtube_url"],
            context.application.bot_data["download_root"],
            ai,
        )
    except Exception:
        LOGGER.exception("AI highlight analysis failed")
        await query.edit_message_text(
            "AI highlight analysis failed. Send a manual trim range instead."
        )
    else:
        await query.edit_message_text(
            "✨ Suggested highlights:\n\n"
            f"{suggestions[:3500]}\n\n"
            "Send one range as HH:MM:SS-HH:MM:SS."
        )
    return ASK_TRIM


async def receive_trim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    raw_value = update.message.text.strip()
    force = raw_value.lower().startswith("force:")
    if force:
        raw_value = raw_value.split(":", 1)[1].strip()
    try:
        trims = parse_trim_ranges(
            raw_value,
            context.application.bot_data["max_clip_seconds"],
            context.application.bot_data["max_batch_clips"],
        )
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return ASK_TRIM
    if not force:
        duplicates = []
        for _, trim_text in trims:
            reason = duplicate_reason(
                context.application.bot_data["state_db"],
                context.application.bot_data["download_root"],
                context.user_data["youtube_url"],
                trim_text,
            )
            if reason:
                duplicates.append(f"{trim_text}: {reason}")
        if duplicates:
            await update.message.reply_text(
                "⚠ Duplicate protection stopped this request:\n"
                + "\n".join(duplicates)
                + "\n\nUse different ranges, or prefix the same request with force:"
            )
            return ASK_TRIM
    context.user_data["trims"] = trims
    await update.message.reply_text(
        "Send the Facebook caption. You can generate another AI caption from the preview."
    )
    return ASK_CAPTION


async def receive_caption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    caption = update.message.text.strip()
    if not caption:
        await update.message.reply_text("The caption cannot be empty.")
        return ASK_CAPTION
    context.user_data["caption"] = caption
    await update.message.reply_text(
        "Send the headline to show at the top of the vertical video, or send auto."
    )
    return ASK_HEADLINE


async def receive_headline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    value = update.message.text.strip()
    headline = make_headline(context.user_data["caption"]) if value.lower() == "auto" else value
    if not headline:
        await update.message.reply_text("The headline cannot be empty.")
        return ASK_HEADLINE
    context.user_data["headline"] = headline[:100]
    await update.message.reply_text(
        "Choose the video layout:", reply_markup=layout_keyboard("setup")
    )
    return CHOOSE_LAYOUT


def layout_keyboard(prefix: str, job_id: str = "") -> InlineKeyboardMarkup:
    rows = []
    for key, label in LAYOUTS.items():
        data = f"{prefix}_layout:{key}" if not job_id else f"{prefix}_layout:{job_id}:{key}"
        rows.append([InlineKeyboardButton(label, callback_data=data)])
    return InlineKeyboardMarkup(rows)


def template_keyboard(prefix: str, job_id: str = "") -> InlineKeyboardMarkup:
    rows = []
    for key, template in BRAND_TEMPLATES.items():
        data = f"{prefix}_template:{key}" if not job_id else f"{prefix}_template:{job_id}:{key}"
        rows.append([InlineKeyboardButton(template["name"], callback_data=data)])
    return InlineKeyboardMarkup(rows)


async def choose_layout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    layout = query.data.split(":", 1)[1]
    if layout not in LAYOUTS:
        await query.answer("Unknown layout", show_alert=True)
        return CHOOSE_LAYOUT
    context.user_data["layout"] = layout
    await query.edit_message_text(
        f"Layout: {LAYOUTS[layout]}\nChoose a branding template:",
        reply_markup=template_keyboard("setup"),
    )
    return CHOOSE_TEMPLATE


async def choose_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    template = query.data.split(":", 1)[1]
    if template not in BRAND_TEMPLATES:
        await query.answer("Unknown template", show_alert=True)
        return CHOOSE_TEMPLATE
    context.user_data["template"] = template
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🎬 Prepare preview", callback_data="setup:prepare"),
            InlineKeyboardButton("Cancel", callback_data="setup:cancel"),
        ]]
    )
    trim_summary = ", ".join(text for _, text in context.user_data["trims"])
    await query.edit_message_text(
        "Ready to prepare:\n"
        f"Trim(s): {trim_summary}\n"
        f"Layout: {LAYOUTS[context.user_data['layout']]}\n"
        f"Template: {BRAND_TEMPLATES[template]['name']}\n"
        f"Headline: {context.user_data['headline']}\n"
        f"Caption: {context.user_data['caption']}",
        reply_markup=keyboard,
    )
    return CONFIRM


async def setup_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    if query.data == "setup:cancel":
        context.user_data.clear()
        await query.edit_message_text("Cancelled. Send /start when ready.")
        return ConversationHandler.END

    data = dict(context.user_data)
    download_root = context.application.bot_data["download_root"]
    batch_dir = download_root / f"batch-{uuid.uuid4().hex}"
    batch_dir.mkdir(parents=True, exist_ok=False)
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        await query.edit_message_text("⬇️ Downloading Facebook-ready video — 10%")
        source, metadata = await asyncio.to_thread(
            download_video,
            data["youtube_url"],
            batch_dir,
            ffmpeg_exe,
            context.application.bot_data["max_video_height"],
        )
        total = len(data["trims"])
        for index, (trim, trim_text) in enumerate(data["trims"], start=1):
            job_id = uuid.uuid4().hex
            job_dir = download_root / job_id
            job_dir.mkdir(parents=True, exist_ok=False)
            linked_source = job_dir / source.name
            await asyncio.to_thread(link_or_copy, source, linked_source)
            headline = data["headline"]
            if total > 1:
                headline = f"{headline} • Part {index}"
            await query.edit_message_text(
                f"✂️ Batch clip {index}/{total}: trimming {trim_text} — 40%"
            )
            clip = await asyncio.to_thread(
                trim_video, linked_source, trim, job_dir, ffmpeg_exe
            )
            await query.edit_message_text(
                f"🎨 Batch clip {index}/{total}: rendering {LAYOUTS[data['layout']]} — 65%"
            )
            final_video = await render_with_progress(
                query,
                brand_video,
                clip,
                job_dir,
                headline,
                context.application.bot_data["watermark_text"],
                context.application.bot_data["logo_path"],
                ffmpeg_exe,
                data["layout"],
                data["template"],
            )
            await query.edit_message_text(
                f"🎞 Batch clip {index}/{total}: preparing preview — 89%"
            )
            preview_video = await asyncio.to_thread(
                create_preview_video,
                final_video,
                job_dir,
                context.application.bot_data["preview_max_bytes"],
                ffmpeg_exe,
            )
            job = {
                "job_id": job_id,
                "user_id": update.effective_user.id,
                "youtube_url": data["youtube_url"],
                "fingerprint": clip_fingerprint(data["youtube_url"], trim_text),
                "trim": list(trim) if trim else None,
                "trim_text": trim_text,
                "caption": data["caption"],
                "headline": headline,
                "title": metadata.get("title", ""),
                "description": metadata.get("description", "")[:3000],
                "source_filename": linked_source.name,
                "clip_filename": clip.name,
                "video_filename": final_video.name,
                "layout": data["layout"],
                "template": data["template"],
                "status": "ready",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            write_job(job_dir, job)
            await send_job_preview(
                query.message,
                job,
                preview_video,
                final_video,
                context.application.bot_data["preview_max_bytes"],
            )
        await query.edit_message_text(
            f"✅ {total} preview{'s' if total != 1 else ''} ready — 100%"
        )
    except Exception:
        LOGGER.exception("Preview preparation failed")
        await query.edit_message_text(
            "Preview preparation failed. Check the VPS log for details."
        )
    finally:
        shutil.rmtree(batch_dir, ignore_errors=True)
        context.user_data.clear()
    return ConversationHandler.END


def link_or_copy(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


async def render_with_progress(query, function, *args):
    """Run a blocking render while keeping the Telegram progress message alive."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    elapsed = 0
    while True:
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=15)
        except TimeoutError:
            elapsed += 15
            percent = min(88, 65 + (elapsed // 15) * 2)
            try:
                await query.edit_message_text(
                    "🎨 Rendering vertical video with blur, headline, logo and "
                    f"watermark — {percent}%\nElapsed: {elapsed // 60}m {elapsed % 60:02d}s"
                )
            except Exception:
                LOGGER.debug("Could not refresh rendering progress", exc_info=True)


def run_command(args: list[str]) -> None:
    LOGGER.info("Running %s", args[0])
    completed = subprocess.run(args, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise RuntimeError(f"{args[0]} failed: {detail}")


def download_video(
    url: str, job_dir: Path, ffmpeg_exe: str, max_video_height: int
) -> tuple[Path, dict]:
    output_template = str(job_dir / "source.%(ext)s")
    format_selector = (
        f"bv*[height<={max_video_height}][vcodec^=avc1]+ba[acodec^=mp4a]/"
        f"b[height<={max_video_height}][ext=mp4]/b[height<={max_video_height}]"
    )
    run_command(
        [
            "yt-dlp", "--no-playlist", "--restrict-filenames",
            "--merge-output-format", "mp4", "--write-info-json",
            "--ffmpeg-location", ffmpeg_exe, "-f", format_selector,
            "-o", output_template, url,
        ]
    )
    matches = [
        path for path in job_dir.glob("source.*")
        if path.is_file() and not path.name.endswith(".info.json")
    ]
    if not matches:
        raise RuntimeError("yt-dlp finished but no video file was found")
    info_files = list(job_dir.glob("source.info.json"))
    metadata = json.loads(info_files[0].read_text(encoding="utf-8")) if info_files else {}
    return max(matches, key=lambda path: path.stat().st_size), metadata


def trim_video(
    source: Path, trim: tuple[int, int] | None, job_dir: Path, ffmpeg_exe: str
) -> Path:
    if trim is None:
        return source
    start, end = trim
    output = job_dir / "clip.mp4"
    run_command(
        [
            ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(start), "-i", str(source), "-t", str(end - start),
            "-c", "copy", "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart", str(output),
        ]
    )
    return output


def filter_path(path: Path) -> str:
    return path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")


def brand_video(
    source: Path,
    job_dir: Path,
    headline: str,
    watermark: str,
    logo_path: Path,
    ffmpeg_exe: str,
    layout: str = "vertical_blur",
    template_key: str = "gaming",
) -> Path:
    headline_file = job_dir / "headline.txt"
    watermark_file = job_dir / "watermark.txt"
    headline_file.write_text(
        "\n".join(textwrap.wrap(headline, width=28)[:2]), encoding="utf-8"
    )
    watermark_file.write_text(watermark, encoding="utf-8")
    output = job_dir / "final.mp4"
    font = Path(r"C:\Windows\Fonts\arialbd.ttf")
    if not font.exists():
        font = Path(r"C:\Windows\Fonts\arial.ttf")

    template = BRAND_TEMPLATES.get(template_key, BRAND_TEMPLATES["gaming"])
    if layout == "vertical_crop":
        layout_filter = (
            "[0:v]fps=30,scale=720:1280:force_original_aspect_ratio=increase,"
            "crop=720:1280[composite];"
        )
        header_height, font_size, headline_y = 180, 40, 42
    elif layout == "landscape":
        layout_filter = (
            "[0:v]fps=30,scale=1280:720:force_original_aspect_ratio=decrease,"
            "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black[composite];"
        )
        header_height, font_size, headline_y = 120, 34, 26
    elif layout == "square":
        layout_filter = (
            "[0:v]fps=30,split=2[bgsrc][fgsrc];"
            "[bgsrc]scale=180:180:force_original_aspect_ratio=increase,"
            "crop=180:180,boxblur=8:2,scale=720:720[bg];"
            "[fgsrc]scale=720:720:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[composite];"
        )
        header_height, font_size, headline_y = 140, 34, 30
    elif layout == "gaming":
        layout_filter = (
            "[0:v]fps=30,scale=720:900:force_original_aspect_ratio=decrease,"
            f"pad=720:1280:(ow-iw)/2:190:color={template['color']}[composite];"
        )
        header_height, font_size, headline_y = 180, 40, 42
    else:
        layout_filter = (
            "[0:v]fps=30,split=2[bgsrc][fgsrc];"
            "[bgsrc]scale=180:320:force_original_aspect_ratio=increase,"
            "crop=180:320,boxblur=8:2,scale=720:1280[bg];"
            "[fgsrc]scale=720:1280:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[composite];"
        )
        header_height, font_size, headline_y = 180, 40, 42

    base_filter = (
        layout_filter
        + f"[composite]drawbox=x=0:y=0:w=iw:h={header_height}:"
        f"color={template['color']}@0.82:t=fill,"
        f"drawtext=fontfile='{filter_path(font)}':textfile='{filter_path(headline_file)}':"
        f"expansion=none:fontcolor={template['accent']}:fontsize={font_size}:line_spacing=8:"
        f"x=(w-text_w)/2:y={headline_y}:"
        "shadowcolor=black:shadowx=2:shadowy=2,"
        f"drawtext=fontfile='{filter_path(font)}':textfile='{filter_path(watermark_file)}':"
        "expansion=none:fontcolor=white@0.85:fontsize=26:"
        "x=w-text_w-24:y=h-text_h-24"
        "[branded]"
    )
    args = [ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
    if logo_path.exists():
        args.extend(["-loop", "1", "-i", str(logo_path)])
        filter_complex = (
            base_filter
            + ";[branded]setsar=1[branded_sar];[1:v]scale=110:-1[logo];"
            "[branded_sar][logo]overlay=24:H-h-24[outv]"
        )
    else:
        filter_complex = base_filter + ";[branded]setsar=1[outv]"
    args.extend(
        [
            "-filter_complex", filter_complex, "-map", "[outv]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-threads", "0",
            "-crf", "19", "-maxrate", "4500k", "-bufsize", "9000k",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-shortest", str(output),
        ]
    )
    run_command(args)
    return output


def create_preview_video(
    final_video: Path,
    job_dir: Path,
    max_bytes: int,
    ffmpeg_exe: str,
) -> Path:
    """Return the master when it fits, otherwise make a full-quality sample."""
    if final_video.stat().st_size <= max_bytes:
        return final_video
    preview = job_dir / "preview.mp4"
    for seconds in (30, 15):
        run_command(
            [
                ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(final_video), "-t", str(seconds), "-c", "copy",
                "-movflags", "+faststart", str(preview),
            ]
        )
        if preview.stat().st_size <= max_bytes:
            return preview
    return final_video


def write_job(job_dir: Path, job: dict) -> None:
    (job_dir / "job.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def migrate_job(job_dir: Path, job: dict) -> dict:
    changed = False
    if "created_at" not in job:
        job["created_at"] = datetime.fromtimestamp(
            (job_dir / "job.json").stat().st_mtime, tz=timezone.utc
        ).isoformat()
        changed = True
    if "fingerprint" not in job and job.get("youtube_url"):
        job["fingerprint"] = clip_fingerprint(
            job["youtube_url"], job.get("trim_text", "full")
        )
        changed = True
    if "layout" not in job:
        job["layout"] = "vertical_blur"
        changed = True
    if "template" not in job:
        job["template"] = "gaming"
        changed = True
    if "source_filename" not in job:
        sources = [
            path for path in job_dir.glob("source.*")
            if path.is_file() and not path.name.endswith(".info.json")
        ]
        if sources:
            job["source_filename"] = max(
                sources, key=lambda path: path.stat().st_size
            ).name
            changed = True
    if "clip_filename" not in job and job.get("source_filename"):
        clip = job_dir / "clip.mp4"
        job["clip_filename"] = clip.name if clip.exists() else job["source_filename"]
        changed = True
    if "trim" not in job and job.get("trim_text"):
        try:
            trim = parse_trim(job["trim_text"], 24 * 3600)
            job["trim"] = list(trim) if trim else None
            changed = True
        except ValueError:
            pass
    if changed:
        write_job(job_dir, job)
    return job


def load_job(download_root: Path, job_id: str) -> tuple[Path, dict, Path]:
    if not JOB_ID_RE.fullmatch(job_id):
        raise RuntimeError("Invalid job ID")
    root = download_root.resolve()
    job_dir = (root / job_id).resolve()
    if root not in job_dir.parents:
        raise RuntimeError("Invalid job path")
    job_file = job_dir / "job.json"
    if not job_file.exists():
        raise RuntimeError("This cached job is no longer available")
    job = migrate_job(
        job_dir, json.loads(job_file.read_text(encoding="utf-8-sig"))
    )
    video = job_dir / job["video_filename"]
    if not video.exists():
        raise RuntimeError("The cached video is missing")
    return job_dir, job, video


def cached_jobs(download_root: Path) -> list[tuple[Path, dict, Path]]:
    jobs = []
    for path in download_root.iterdir():
        if not path.is_dir() or not JOB_ID_RE.fullmatch(path.name):
            continue
        try:
            jobs.append(load_job(download_root, path.name))
        except Exception:
            LOGGER.exception("Could not load cached job %s", path)
    return sorted(
        jobs,
        key=lambda item: item[1].get("created_at", ""),
        reverse=True,
    )


async def drafts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    jobs = [
        item for item in cached_jobs(context.application.bot_data["download_root"])
        if item[1].get("user_id") == update.effective_user.id
    ]
    if not jobs:
        await update.message.reply_text("There are no cached drafts.")
        return
    lines = []
    rows = []
    for index, (_, job, video) in enumerate(jobs[:20], start=1):
        size_mb = video.stat().st_size / (1024 * 1024)
        lines.append(
            f"{index}. [{job.get('status', 'ready')}] {job.get('headline', 'Untitled')} "
            f"— {size_mb:.1f} MB"
        )
        rows.append(
            [InlineKeyboardButton(
                f"Open draft {index}", callback_data=f"draft_open:{job['job_id']}"
            )]
        )
    await update.message.reply_text(
        "📂 Cached drafts:\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def open_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    query = update.callback_query
    await query.answer()
    job_id = query.data.split(":", 1)[1]
    try:
        _, job, video = load_job(
            context.application.bot_data["download_root"], job_id
        )
    except Exception as exc:
        await query.answer(str(exc), show_alert=True)
        return
    if job.get("user_id") != update.effective_user.id:
        await query.answer("This draft belongs to another user.", show_alert=True)
        return
    size_mb = video.stat().st_size / (1024 * 1024)
    await query.message.reply_text(
        f"📄 {job.get('headline', 'Untitled')}\n"
        f"Status: {job.get('status', 'ready')}\n"
        f"Trim: {job.get('trim_text', 'full')}\n"
        f"Layout: {LAYOUTS.get(job.get('layout'), job.get('layout', 'unknown'))}\n"
        f"Size: {size_mb:.1f} MB",
        reply_markup=job_keyboard(job_id, retry=job.get("status") == "upload_failed"),
    )


def directory_size(path: Path) -> int:
    total = 0
    for file in path.rglob("*"):
        if file.is_file():
            try:
                total += file.stat().st_size
            except OSError:
                pass
    return total


def cleanup_expired_jobs(
    download_root: Path, ready_days: int, failed_days: int
) -> tuple[int, int]:
    removed_count = 0
    removed_bytes = 0
    now = datetime.now(timezone.utc)
    for job_dir, job, _ in cached_jobs(download_root):
        status = job.get("status", "ready")
        if status in {"scheduled", "uploading"}:
            continue
        try:
            created = datetime.fromisoformat(job["created_at"])
        except (KeyError, ValueError):
            created = datetime.fromtimestamp(job_dir.stat().st_mtime, tz=timezone.utc)
        retention = failed_days if status == "upload_failed" else ready_days
        if now - created < timedelta(days=retention):
            continue
        size = directory_size(job_dir)
        root = download_root.resolve()
        resolved = job_dir.resolve()
        if root in resolved.parents:
            shutil.rmtree(resolved, ignore_errors=True)
            removed_count += 1
            removed_bytes += size
    return removed_count, removed_bytes


async def storage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    root = context.application.bot_data["download_root"]
    jobs = cached_jobs(root)
    total = directory_size(root)
    counts: dict[str, int] = {}
    for _, job, _ in jobs:
        status = job.get("status", "ready")
        counts[status] = counts.get(status, 0) + 1
    status_text = ", ".join(f"{key}: {value}" for key, value in sorted(counts.items()))
    await update.message.reply_text(
        f"💾 Storage: {total / (1024 * 1024):.1f} MB\n"
        f"Drafts: {len(jobs)} ({status_text or 'none'})\n"
        f"Ready retention: {context.application.bot_data['draft_retention_days']} days\n"
        f"Failed retention: {context.application.bot_data['failed_retention_days']} days\n"
        "Scheduled jobs are never removed automatically."
    )


async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    count, size = await asyncio.to_thread(
        cleanup_expired_jobs,
        context.application.bot_data["download_root"],
        context.application.bot_data["draft_retention_days"],
        context.application.bot_data["failed_retention_days"],
    )
    await update.message.reply_text(
        f"🧹 Cleanup removed {count} expired draft(s), "
        f"freeing {size / (1024 * 1024):.1f} MB."
    )


def job_keyboard(job_id: str, retry: bool = False) -> InlineKeyboardMarkup:
    publish_label = "🔁 Retry upload" if retry else "🚀 Publish"
    action = "retry" if retry else "publish"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(publish_label, callback_data=f"{action}:{job_id}"),
                InlineKeyboardButton("🗓 Schedule", callback_data=f"schedule:{job_id}"),
            ],
            [
                InlineKeyboardButton("✨ AI caption", callback_data=f"ai_caption:{job_id}"),
                InlineKeyboardButton("✏ Edit", callback_data=f"edit_menu:{job_id}"),
            ],
            [
                InlineKeyboardButton("▣ Layout", callback_data=f"edit_layout_menu:{job_id}"),
                InlineKeyboardButton("🎨 Template", callback_data=f"edit_template_menu:{job_id}"),
            ],
            [InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{job_id}")],
        ]
    )


async def send_job_preview(
    message, job: dict, preview: Path, master: Path, max_bytes: int
) -> None:
    preview_note = (
        "\nPreview: full video"
        if preview == master
        else "\nPreview: first full-quality section; Facebook receives the complete master"
    )
    caption = (
        f"Preview ready\n\nHeadline: {job['headline']}\n"
        f"Layout: {LAYOUTS.get(job.get('layout'), job.get('layout', ''))}\n"
        f"Template: {BRAND_TEMPLATES.get(job.get('template'), {}).get('name', '')}\n"
        f"Post caption: {job['caption']}{preview_note}"
    )[:1000]
    keyboard = job_keyboard(job["job_id"])
    if preview.stat().st_size <= max_bytes:
        with preview.open("rb") as handle:
            await message.reply_video(
                video=handle,
                caption=caption,
                supports_streaming=True,
                reply_markup=keyboard,
                read_timeout=300,
                write_timeout=300,
            )
    else:
        size_mb = master.stat().st_size / (1024 * 1024)
        await message.reply_text(
            f"Preview prepared ({size_mb:.1f} MB), but it is too large to send through "
            "Telegram. You can still publish or generate an AI caption.",
            reply_markup=keyboard,
        )


def resolve_page_access_token(config: dict[str, str]) -> str:
    base = f"https://graph.facebook.com/{config['graph_version']}"
    supplied_token = config["access_token"]
    page_id = config["page_id"]
    identity = requests.get(
        f"{base}/me", params={"fields": "id", "access_token": supplied_token}, timeout=30
    )
    if identity.ok and identity.json().get("id") == page_id:
        return supplied_token
    accounts = requests.get(
        f"{base}/me/accounts",
        params={
            "fields": "id,name,access_token,tasks", "limit": 100,
            "access_token": supplied_token,
        },
        timeout=30,
    )
    if not accounts.ok:
        raise facebook_error(accounts)
    for page in accounts.json().get("data", []):
        if page.get("id") == page_id and page.get("access_token"):
            if "CREATE_CONTENT" not in page.get("tasks", []):
                raise RuntimeError("The Facebook account cannot create Page content")
            return page["access_token"]
    raise RuntimeError("The configured Facebook Page is not managed by this token")


def publish_image_to_facebook(image: Path, caption: str, config: dict[str, str]) -> str:
    if not config.get("page_id") or not config.get("access_token"):
        raise RuntimeError(
            "Education Page is not configured. Add IMAGE_FACEBOOK_PAGE_ID and "
            "IMAGE_FACEBOOK_PAGE_ACCESS_TOKEN to .env."
        )
    page_token = resolve_page_access_token(config)
    endpoint = (
        f"https://graph.facebook.com/{config['graph_version']}/"
        f"{config['page_id']}/photos"
    )
    with image.open("rb") as handle:
        response = requests.post(
            endpoint,
            data={
                "access_token": page_token,
                "caption": caption,
                "published": "true",
            },
            files={"source": (image.name, handle, "application/octet-stream")},
            timeout=(30, 300),
        )
    if not response.ok:
        raise facebook_error(response)
    result = response.json()
    object_id = str(result.get("post_id") or result.get("id") or "")
    if not object_id:
        raise RuntimeError("Facebook accepted the image but returned no post ID")
    lookup_id = str(result.get("id") or object_id)
    lookup = requests.get(
        f"https://graph.facebook.com/{config['graph_version']}/{lookup_id}",
        params={"fields": "permalink_url", "access_token": page_token},
        timeout=30,
    )
    if lookup.ok and lookup.json().get("permalink_url"):
        return "https://www.facebook.com" + lookup.json()["permalink_url"]
    return f"https://www.facebook.com/{object_id}"


async def image_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    query = update.callback_query
    action, image_id = query.data.split(":", 1)
    await query.answer()
    try:
        draft_dir = image_draft_path(
            context.application.bot_data["image_root"], image_id
        )
        draft = json.loads(
            (draft_dir / "image.json").read_text(encoding="utf-8-sig")
        )
        image_path = draft_dir / draft["image_filename"]
    except Exception as exc:
        await query.answer(f"Image draft unavailable: {exc}", show_alert=True)
        return
    if draft.get("user_id") != update.effective_user.id:
        await query.answer("This image belongs to another user.", show_alert=True)
        return
    if action == "image_cancel":
        shutil.rmtree(draft_dir, ignore_errors=True)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Image post cancelled and cached image deleted.")
        return
    if action == "image_regenerate":
        if not draft.get("generated") or not draft.get("topic"):
            await query.answer("Only AI-generated drafts can be regenerated.", show_alert=True)
            return
        progress = await query.message.reply_text("Generating a new visual version…")
        try:
            image_path, caption, lesson = await asyncio.to_thread(
                generate_education_image,
                draft["topic"],
                draft_dir,
                context.application.bot_data["local_image"],
            )
        except Exception as exc:
            LOGGER.exception("Education image regeneration failed")
            await progress.edit_text(image_generation_error_message(exc))
            return
        draft.update(
            image_filename=image_path.name,
            caption=caption,
            lesson=lesson,
            status="ready",
            last_error="",
        )
        (draft_dir / "image.json").write_text(
            json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "Publish to education Page",
                    callback_data=f"image_publish:{image_id}",
                ),
                InlineKeyboardButton(
                    "Regenerate", callback_data=f"image_regenerate:{image_id}"
                ),
            ],
            [InlineKeyboardButton("Cancel", callback_data=f"image_cancel:{image_id}")],
        ])
        with image_path.open("rb") as handle:
            await query.message.reply_photo(
                photo=handle,
                caption=f"New generated preview: {draft['topic']}",
                reply_markup=keyboard,
                read_timeout=300,
                write_timeout=300,
            )
        await query.edit_message_reply_markup(reply_markup=None)
        await progress.edit_text("New version ready. Check every word before publishing.")
        return
    if draft.get("status") == "published":
        await query.answer("This image is already published.", show_alert=True)
        return
    progress = await query.message.reply_text("Uploading image to education Page…")
    try:
        post_url = await asyncio.to_thread(
            publish_image_to_facebook,
            image_path,
            draft["caption"],
            context.application.bot_data["image_facebook"],
        )
    except Exception as exc:
        LOGGER.exception("Facebook image upload failed")
        draft["status"] = "upload_failed"
        draft["last_error"] = str(exc)
        (draft_dir / "image.json").write_text(
            json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await progress.edit_text(
            f"Image upload failed: {exc}\n\nThe image is cached; press Publish again after fixing the Page settings."
        )
        return
    draft["status"] = "published"
    draft["post_url"] = post_url
    (draft_dir / "image.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    await query.edit_message_reply_markup(reply_markup=None)
    await progress.edit_text(f"Image published successfully.\n{post_url}")


def facebook_error(response: requests.Response) -> RuntimeError:
    try:
        error = response.json().get("error", {})
        message = error.get("message", "Facebook rejected the request")
        code = error.get("code", "unknown")
        subcode = error.get("error_subcode", "none")
    except (ValueError, AttributeError):
        message, code, subcode = "Facebook rejected the request", "unknown", "none"
    return RuntimeError(
        f"Facebook error {code}/{subcode} (HTTP {response.status_code}): {message}"
    )


class ProgressReader:
    def __init__(self, handle, total: int, callback=None):
        self.handle = handle
        self.total = total
        self.callback = callback
        self.sent = 0

    def __len__(self) -> int:
        return self.total

    def read(self, size: int = -1) -> bytes:
        data = self.handle.read(size)
        self.sent += len(data)
        if self.callback:
            self.callback(self.sent, self.total)
        return data


def publish_resumable(
    video: Path,
    caption: str,
    page_token: str,
    config: dict[str, str],
    progress_callback=None,
) -> tuple[requests.Response, str]:
    """Upload a long Page video with Facebook's native chunked protocol."""
    base = f"https://graph.facebook.com/{config['graph_version']}"
    video_endpoint = f"{base}/{config['page_id']}/videos"
    start = requests.post(
        video_endpoint,
        data={
            "upload_phase": "start",
            "file_size": str(video.stat().st_size),
            "access_token": page_token,
        },
        timeout=30,
    )
    if not start.ok:
        raise facebook_error(start)
    start_data = start.json()
    session_id = start_data.get("upload_session_id")
    video_id = str(start_data.get("video_id", ""))
    try:
        start_offset = int(start_data["start_offset"])
        end_offset = int(start_data["end_offset"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Facebook returned invalid resumable upload offsets") from exc
    if not session_id or not video_id:
        raise RuntimeError("Facebook returned an incomplete video upload session")

    total = video.stat().st_size
    with video.open("rb") as handle:
        while start_offset < end_offset:
            if not (0 <= start_offset < end_offset <= total):
                raise RuntimeError("Facebook requested an invalid video byte range")
            handle.seek(start_offset)
            chunk = handle.read(end_offset - start_offset)
            if len(chunk) != end_offset - start_offset:
                raise RuntimeError("Could not read the requested video chunk")
            transfer = requests.post(
                f"https://graph-video.facebook.com/{config['graph_version']}/"
                f"{config['page_id']}/videos",
                data={
                    "upload_phase": "transfer",
                    "upload_session_id": session_id,
                    "start_offset": str(start_offset),
                    "access_token": page_token,
                },
                files={
                    "video_file_chunk": (
                        video.name, chunk, "application/octet-stream"
                    )
                },
                timeout=(30, 3600),
            )
            if not transfer.ok:
                raise facebook_error(transfer)
            transfer_data = transfer.json()
            try:
                next_start = int(transfer_data["start_offset"])
                next_end = int(transfer_data["end_offset"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("Facebook returned invalid next-chunk offsets") from exc
            if next_start <= start_offset:
                raise RuntimeError("Facebook did not advance the video upload offset")
            start_offset, end_offset = next_start, next_end
            if progress_callback:
                progress_callback(min(start_offset, total), total)

    finish = requests.post(
        video_endpoint,
        data={
            "upload_phase": "finish",
            "upload_session_id": session_id,
            "access_token": page_token,
            "description": caption,
        },
        timeout=(30, 300),
    )
    return finish, video_id


def normalize_for_facebook(video: Path) -> Path:
    """Losslessly normalize H.264 pixel aspect and MP4 indexing for Facebook."""
    normalized = video.parent / "facebook-upload.mp4"
    if normalized.exists() and normalized.stat().st_mtime >= video.stat().st_mtime:
        return normalized
    run_command(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
            "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
            "-bsf:v", "h264_metadata=sample_aspect_ratio=1/1",
            "-movflags", "+faststart", str(normalized),
        ]
    )
    return normalized


def publish_to_facebook(
    video: Path,
    caption: str,
    config: dict[str, str],
    progress_callback=None,
) -> str:
    video = normalize_for_facebook(video)
    page_token = resolve_page_access_token(config)
    # Large multipart requests are rejected by Meta's edge with HTTP 413 on
    # some Pages. Go straight to the official resumable flow instead of
    # uploading the entire file once before falling back.
    if video.stat().st_size >= 100 * 1024 * 1024:
        response, video_id = publish_resumable(
            video, caption, page_token, config, progress_callback
        )
    else:
        response = None
        video_id = ""
    endpoint = (
        f"https://graph-video.facebook.com/{config['graph_version']}/"
        f"{config['page_id']}/videos"
    )
    if response is None:
        with video.open("rb") as handle:
            encoder = MultipartEncoder(
                fields={
                    "description": caption,
                    "access_token": page_token,
                    "source": (video.name, handle, "video/mp4"),
                }
            )
            monitor = MultipartEncoderMonitor(
                encoder,
                (lambda item: progress_callback(item.bytes_read, item.len))
                if progress_callback
                else None,
            )
            response = requests.post(
                endpoint,
                data=monitor,
                headers={"Content-Type": monitor.content_type},
                timeout=(30, 3600),
            )
        if response.status_code == 413:
            response, video_id = publish_resumable(
                video, caption, page_token, config, progress_callback
            )
    if not response.ok:
        raise facebook_error(response)
    video_id = video_id or response.json().get("id")
    if not video_id:
        raise RuntimeError("Facebook accepted the upload but returned no video ID")
    lookup = requests.get(
        f"https://graph.facebook.com/{config['graph_version']}/{video_id}",
        params={"fields": "permalink_url", "access_token": page_token}, timeout=30,
    )
    if lookup.ok and lookup.json().get("permalink_url"):
        return "https://www.facebook.com" + lookup.json()["permalink_url"]
    return f"https://www.facebook.com/{video_id}"


async def publish_with_progress(
    video: Path,
    caption: str,
    config: dict[str, str],
    progress_message,
) -> str:
    stats = {"sent": 0, "total": max(1, video.stat().st_size)}
    started = time.monotonic()

    def update(sent: int, total: int) -> None:
        stats["sent"] = sent
        stats["total"] = max(1, total)

    task = asyncio.create_task(
        asyncio.to_thread(publish_to_facebook, video, caption, config, update)
    )
    while True:
        done, _ = await asyncio.wait({task}, timeout=4)
        if task in done:
            return task.result()
        else:
            sent = stats["sent"]
            total = stats["total"]
            percent = min(99, int(sent * 100 / total))
            elapsed = max(0.1, time.monotonic() - started)
            speed = sent / elapsed / (1024 * 1024)
            remaining = max(0, total - sent)
            eta = int(remaining / max(1, sent / elapsed))
            try:
                await progress_message.edit_text(
                    f"🚀 Uploading to Facebook — {percent}%\n"
                    f"{sent / (1024 * 1024):.1f} / {total / (1024 * 1024):.1f} MB\n"
                    f"Speed: {speed:.2f} MB/s · ETA: {eta // 60}m {eta % 60:02d}s"
                )
            except Exception:
                LOGGER.debug("Could not refresh upload progress", exc_info=True)


def openai_client(config: dict[str, str]):
    if not config["api_key"]:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    from openai import OpenAI
    return OpenAI(api_key=config["api_key"])


def generate_caption(job: dict, config: dict[str, str]) -> str:
    client = openai_client(config)
    prompt = (
        "Write one engaging Facebook video caption. Keep it under 500 characters, "
        "use a strong hook, one call to action, and 3-5 relevant hashtags. "
        "Return only the caption.\n\n"
        f"Video title: {job.get('title', '')}\n"
        f"Headline: {job.get('headline', '')}\n"
        f"Current caption: {job.get('caption', '')}\n"
        f"Description: {job.get('description', '')[:1500]}"
    )
    response = client.responses.create(model=config["text_model"], input=prompt)
    caption = response.output_text.strip()
    if not caption:
        raise RuntimeError("The AI returned an empty caption")
    return caption[:1000]


def download_audio_chunks(url: str, work_dir: Path, ffmpeg_exe: str, max_minutes: int) -> list[Path]:
    template = str(work_dir / "audio.%(ext)s")
    run_command(
        [
            "yt-dlp", "--no-playlist", "--restrict-filenames", "-f", "bestaudio/best",
            "--ffmpeg-location", ffmpeg_exe, "-o", template, url,
        ]
    )
    sources = [path for path in work_dir.glob("audio.*") if path.is_file()]
    if not sources:
        raise RuntimeError("No audio was downloaded for highlight analysis")
    source = max(sources, key=lambda path: path.stat().st_size)
    chunk_pattern = str(work_dir / "chunk%03d.mp3")
    run_command(
        [
            ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-t", str(max_minutes * 60), "-vn", "-ac", "1", "-ar", "16000",
            "-b:a", "48k", "-f", "segment", "-segment_time", "1200",
            "-reset_timestamps", "1", chunk_pattern,
        ]
    )
    return sorted(work_dir.glob("chunk*.mp3"))


def find_highlights(
    url: str, download_root: Path, config: dict[str, str]
) -> str:
    client = openai_client(config)
    work_dir = download_root / f"analysis-{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=False)
    try:
        chunks = download_audio_chunks(
            url, work_dir, imageio_ffmpeg.get_ffmpeg_exe(), config["max_minutes"]
        )
        transcript_lines: list[str] = []
        for index, chunk in enumerate(chunks):
            with chunk.open("rb") as handle:
                transcript = client.audio.transcriptions.create(
                    model=config["transcribe_model"],
                    file=handle,
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                )
            for segment in transcript.segments or []:
                start = float(getattr(segment, "start", 0)) + index * 1200
                end = float(getattr(segment, "end", start)) + index * 1200
                text = str(getattr(segment, "text", "")).strip()
                if text:
                    transcript_lines.append(
                        f"{seconds_timestamp(start)}-{seconds_timestamp(end)} {text}"
                    )
        if not transcript_lines:
            raise RuntimeError("No speech could be transcribed")
        prompt = (
            "Select the 5 most engaging standalone moments from this timestamped video "
            "transcript. Prefer moments 30-120 seconds long. Return exactly five lines in "
            "this format: HH:MM:SS-HH:MM:SS — short reason. Do not add other text.\n\n"
            + "\n".join(transcript_lines)
        )
        response = client.responses.create(model=config["text_model"], input=prompt)
        result = response.output_text.strip()
        if not result:
            raise RuntimeError("The AI returned no highlight suggestions")
        return result
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def edit_menu_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Caption", callback_data=f"edit_caption:{job_id}"),
                InlineKeyboardButton("Headline", callback_data=f"edit_headline:{job_id}"),
                InlineKeyboardButton("Trim", callback_data=f"edit_trim:{job_id}"),
            ],
            [InlineKeyboardButton("Back", callback_data=f"edit_back:{job_id}")],
        ]
    )


def trim_from_job(job: dict) -> tuple[int, int] | None:
    value = job.get("trim")
    return (int(value[0]), int(value[1])) if value else None


def rerender_job(
    job_dir: Path,
    job: dict,
    ffmpeg_exe: str,
    watermark: str,
    logo_path: Path,
    preview_max_bytes: int,
    retrim: bool = False,
) -> tuple[Path, Path]:
    source = job_dir / job["source_filename"]
    if retrim:
        clip = trim_video(source, trim_from_job(job), job_dir, ffmpeg_exe)
        job["clip_filename"] = clip.name
    else:
        clip = job_dir / job["clip_filename"]
    master = brand_video(
        clip,
        job_dir,
        job["headline"],
        watermark,
        logo_path,
        ffmpeg_exe,
        job.get("layout", "vertical_blur"),
        job.get("template", "gaming"),
    )
    preview = create_preview_video(
        master, job_dir, preview_max_bytes, ffmpeg_exe
    )
    job["video_filename"] = master.name
    job["status"] = "ready"
    job.pop("last_error", None)
    write_job(job_dir, job)
    return preview, master


async def show_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    query = update.callback_query
    await query.answer()
    job_id = query.data.split(":", 1)[1]
    try:
        _, job, _ = load_job(context.application.bot_data["download_root"], job_id)
    except Exception as exc:
        await query.answer(str(exc), show_alert=True)
        return
    if job.get("user_id") != update.effective_user.id:
        await query.answer("This job belongs to another user.", show_alert=True)
        return
    await query.message.reply_text(
        "What do you want to edit?", reply_markup=edit_menu_keyboard(job_id)
    )


async def edit_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    query = update.callback_query
    await query.answer()
    job_id = query.data.split(":", 1)[1]
    await query.edit_message_text(
        "Draft controls:", reply_markup=job_keyboard(job_id)
    )


async def show_layout_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    query = update.callback_query
    await query.answer()
    job_id = query.data.split(":", 1)[1]
    await query.message.reply_text(
        "Choose the replacement layout:",
        reply_markup=layout_keyboard("edit", job_id),
    )


async def show_template_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    query = update.callback_query
    await query.answer()
    job_id = query.data.split(":", 1)[1]
    await query.message.reply_text(
        "Choose the replacement branding template:",
        reply_markup=template_keyboard("edit", job_id),
    )


async def apply_style_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    query = update.callback_query
    await query.answer()
    kind, job_id, value = query.data.split(":", 2)
    try:
        job_dir, job, _ = load_job(
            context.application.bot_data["download_root"], job_id
        )
    except Exception as exc:
        await query.answer(str(exc), show_alert=True)
        return
    if job.get("user_id") != update.effective_user.id:
        await query.answer("This job belongs to another user.", show_alert=True)
        return
    if kind == "edit_layout" and value in LAYOUTS:
        job["layout"] = value
    elif kind == "edit_template" and value in BRAND_TEMPLATES:
        job["template"] = value
    else:
        await query.answer("Unknown style", show_alert=True)
        return
    progress = await query.message.reply_text(
        "🎨 Re-rendering cached draft. No download is needed..."
    )
    try:
        preview, master = await asyncio.to_thread(
            rerender_job,
            job_dir,
            job,
            imageio_ffmpeg.get_ffmpeg_exe(),
            context.application.bot_data["watermark_text"],
            context.application.bot_data["logo_path"],
            context.application.bot_data["preview_max_bytes"],
        )
        await send_job_preview(
            query.message,
            job,
            preview,
            master,
            context.application.bot_data["preview_max_bytes"],
        )
        await progress.edit_text("✅ Updated preview ready")
    except Exception:
        LOGGER.exception("Draft style edit failed")
        await progress.edit_text("Draft re-render failed. Check the VPS log.")


async def begin_text_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    prefix, job_id = query.data.split(":", 1)
    context.user_data["edit_job_id"] = job_id
    context.user_data["edit_field"] = prefix.removeprefix("edit_")
    prompts = {
        "caption": "Send the replacement Facebook caption.",
        "headline": "Send the replacement video headline.",
        "trim": "Send one replacement range as HH:MM:SS-HH:MM:SS, or full.",
    }
    await query.message.reply_text(prompts[context.user_data["edit_field"]])
    return EDIT_VALUE


async def receive_text_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    job_id = context.user_data.get("edit_job_id", "")
    field = context.user_data.get("edit_field", "")
    try:
        job_dir, job, _ = load_job(
            context.application.bot_data["download_root"], job_id
        )
    except Exception as exc:
        await update.message.reply_text(str(exc))
        return ConversationHandler.END
    if job.get("user_id") != update.effective_user.id:
        await update.message.reply_text("This job belongs to another user.")
        return ConversationHandler.END
    value = update.message.text.strip()
    if not value:
        await update.message.reply_text("The value cannot be empty.")
        return EDIT_VALUE
    retrim = False
    try:
        if field == "caption":
            job["caption"] = value
            write_job(job_dir, job)
            await update.message.reply_text(
                "✅ Caption updated.", reply_markup=job_keyboard(job_id)
            )
            return ConversationHandler.END
        if field == "headline":
            job["headline"] = value[:100]
        elif field == "trim":
            trim = parse_trim(value, context.application.bot_data["max_clip_seconds"])
            job["trim"] = list(trim) if trim else None
            job["trim_text"] = value.lower()
            retrim = True
        else:
            raise ValueError("Unknown edit field")
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return EDIT_VALUE

    progress = await update.message.reply_text(
        "🎨 Re-rendering cached draft. No download is needed..."
    )
    try:
        preview, master = await asyncio.to_thread(
            rerender_job,
            job_dir,
            job,
            imageio_ffmpeg.get_ffmpeg_exe(),
            context.application.bot_data["watermark_text"],
            context.application.bot_data["logo_path"],
            context.application.bot_data["preview_max_bytes"],
            retrim,
        )
        await send_job_preview(
            update.message,
            job,
            preview,
            master,
            context.application.bot_data["preview_max_bytes"],
        )
        await progress.edit_text("✅ Updated preview ready")
    except Exception:
        LOGGER.exception("Draft text edit failed")
        await progress.edit_text("Draft re-render failed. Check the VPS log.")
    finally:
        context.user_data.pop("edit_job_id", None)
        context.user_data.pop("edit_field", None)
    return ConversationHandler.END


async def begin_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    job_id = query.data.split(":", 1)[1]
    try:
        _, job, _ = load_job(context.application.bot_data["download_root"], job_id)
    except Exception as exc:
        await query.answer(str(exc), show_alert=True)
        return ConversationHandler.END
    if job.get("user_id") != update.effective_user.id:
        await query.answer("This job belongs to another user.", show_alert=True)
        return ConversationHandler.END
    context.user_data["schedule_job_id"] = job_id
    timezone_name = context.application.bot_data["schedule_timezone"]
    await query.message.reply_text(
        "Send the publishing date and time as YYYY-MM-DD HH:MM\n"
        f"Timezone: {timezone_name}\nExample: 2026-08-10 18:30"
    )
    return SCHEDULE_VALUE


async def receive_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await authorized(update, context):
        return ConversationHandler.END
    job_id = context.user_data.get("schedule_job_id", "")
    try:
        job_dir, job, _ = load_job(
            context.application.bot_data["download_root"], job_id
        )
        local_zone = ZoneInfo(context.application.bot_data["schedule_timezone"])
        local_time = datetime.strptime(
            update.message.text.strip(), "%Y-%m-%d %H:%M"
        ).replace(tzinfo=local_zone)
        scheduled_utc = local_time.astimezone(timezone.utc)
        if scheduled_utc <= datetime.now(timezone.utc):
            raise ValueError("The scheduled time must be in the future")
    except (ValueError, RuntimeError) as exc:
        await update.message.reply_text(
            f"{exc}\nUse YYYY-MM-DD HH:MM and try again."
        )
        return SCHEDULE_VALUE
    job["status"] = "scheduled"
    job["scheduled_at"] = scheduled_utc.isoformat()
    write_job(job_dir, job)
    context.user_data.pop("schedule_job_id", None)
    await update.message.reply_text(
        f"🗓 Scheduled for {local_time.strftime('%Y-%m-%d %H:%M %Z')}.\n"
        "The cached master will publish automatically."
    )
    return ConversationHandler.END


def scheduled_jobs(download_root: Path) -> list[tuple[Path, dict, Path]]:
    jobs = []
    for job_file in download_root.glob("*/job.json"):
        try:
            job = json.loads(job_file.read_text(encoding="utf-8-sig"))
            video = job_file.parent / job["video_filename"]
            if video.exists() and job.get("status") == "scheduled":
                jobs.append((job_file.parent, job, video))
        except (OSError, KeyError, ValueError):
            LOGGER.exception("Could not read scheduled job %s", job_file)
    return jobs


def check_facebook_health(config: dict[str, str]) -> dict:
    base = f"https://graph.facebook.com/{config['graph_version']}"
    token = config["access_token"]
    page_id = config["page_id"]
    result = {
        "valid": False,
        "identity": "",
        "page_name": "",
        "can_publish": False,
        "expires_at": None,
        "message": "Unknown Facebook token error",
    }
    identity = requests.get(
        f"{base}/me",
        params={"fields": "id,name", "access_token": token},
        timeout=30,
    )
    if not identity.ok:
        error = identity.json().get("error", {}) if identity.content else {}
        result["message"] = error.get("message", "Token validation failed")
        return result
    identity_data = identity.json()
    result["identity"] = identity_data.get("name", identity_data.get("id", ""))
    if identity_data.get("id") == page_id:
        page = requests.get(
            f"{base}/{page_id}",
            params={"fields": "id,name", "access_token": token},
            timeout=30,
        )
        result["valid"] = page.ok
        result["can_publish"] = page.ok
        result["page_name"] = page.json().get("name", "") if page.ok else ""
    else:
        accounts = requests.get(
            f"{base}/me/accounts",
            params={
                "fields": "id,name,tasks", "limit": 100,
                "access_token": token,
            },
            timeout=30,
        )
        if not accounts.ok:
            error = accounts.json().get("error", {}) if accounts.content else {}
            result["message"] = error.get("message", "Page access check failed")
            return result
        target = next(
            (item for item in accounts.json().get("data", []) if item.get("id") == page_id),
            None,
        )
        result["valid"] = bool(target)
        result["page_name"] = target.get("name", "") if target else ""
        result["can_publish"] = bool(
            target and "CREATE_CONTENT" in target.get("tasks", [])
        )
    app_id = config.get("app_id", "")
    app_secret = config.get("app_secret", "")
    if app_id and app_secret:
        debug = requests.get(
            f"{base}/debug_token",
            params={
                "input_token": token,
                "access_token": f"{app_id}|{app_secret}",
            },
            timeout=30,
        )
        if debug.ok:
            debug_data = debug.json().get("data", {})
            expires_at = int(debug_data.get("expires_at", 0) or 0)
            if expires_at:
                result["expires_at"] = datetime.fromtimestamp(
                    expires_at, tz=timezone.utc
                ).isoformat()
            result["valid"] = result["valid"] and bool(
                debug_data.get("is_valid", True)
            )
    if result["valid"] and result["can_publish"]:
        result["message"] = "Token and Page publishing permissions are healthy"
    elif result["valid"]:
        result["message"] = "Token is valid but CREATE_CONTENT is missing"
    else:
        result["message"] = "The configured Page is not accessible"
    return result


async def facebook_status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await authorized(update, context):
        return
    status = await asyncio.to_thread(
        check_facebook_health, context.application.bot_data["facebook"]
    )
    expiry = "Unavailable (add FACEBOOK_APP_ID and FACEBOOK_APP_SECRET)"
    if status.get("expires_at"):
        expiry = datetime.fromisoformat(status["expires_at"]).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    await update.message.reply_text(
        f"Facebook status: {'✅ Healthy' if status['valid'] and status['can_publish'] else '⚠ Problem'}\n"
        f"Identity: {status.get('identity') or 'unknown'}\n"
        f"Page: {status.get('page_name') or 'not accessible'}\n"
        f"Can publish: {'yes' if status['can_publish'] else 'no'}\n"
        f"Expires: {expiry}\n"
        f"Details: {status['message']}"
    )


async def maintenance_worker(application: Application) -> None:
    last_token_check = 0.0
    last_cleanup = 0.0
    last_health: bool | None = None
    while True:
        now = time.monotonic()
        if now - last_token_check >= application.bot_data["token_check_interval"]:
            try:
                status = await asyncio.to_thread(
                    check_facebook_health, application.bot_data["facebook"]
                )
                healthy = bool(status["valid"] and status["can_publish"])
                if not healthy and last_health is not False:
                    for user_id in application.bot_data["allowed_users"]:
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=f"⚠ Facebook token alert: {status['message']}",
                        )
                if status.get("expires_at"):
                    expires = datetime.fromisoformat(status["expires_at"])
                    if expires - datetime.now(timezone.utc) <= timedelta(days=3):
                        for user_id in application.bot_data["allowed_users"]:
                            await application.bot.send_message(
                                chat_id=user_id,
                                text=f"⚠ Facebook token expires soon: {status['expires_at']}",
                            )
                last_health = healthy
                application.bot_data["facebook_health"] = status
            except Exception:
                LOGGER.exception("Facebook health check failed")
            last_token_check = now
        if now - last_cleanup >= application.bot_data["cleanup_interval"]:
            try:
                count, size = await asyncio.to_thread(
                    cleanup_expired_jobs,
                    application.bot_data["download_root"],
                    application.bot_data["draft_retention_days"],
                    application.bot_data["failed_retention_days"],
                )
                if count:
                    LOGGER.info(
                        "Automatic cleanup removed %d drafts and %d bytes", count, size
                    )
            except Exception:
                LOGGER.exception("Automatic storage cleanup failed")
            last_cleanup = now
        await asyncio.sleep(60)


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    jobs = [
        job for _, job, _ in scheduled_jobs(context.application.bot_data["download_root"])
        if job.get("user_id") == update.effective_user.id
    ]
    if not jobs:
        await update.message.reply_text("The publishing queue is empty.")
        return
    timezone_name = context.application.bot_data["schedule_timezone"]
    local_zone = ZoneInfo(timezone_name)
    lines = []
    for job in sorted(jobs, key=lambda item: item["scheduled_at"]):
        when = datetime.fromisoformat(job["scheduled_at"]).astimezone(local_zone)
        lines.append(
            f"• {when.strftime('%Y-%m-%d %H:%M')} — {job.get('headline', 'Untitled')}"
        )
    await update.message.reply_text(
        f"🗓 Scheduled posts ({timezone_name}):\n" + "\n".join(lines[:30])
    )


async def scheduled_worker(application: Application) -> None:
    while True:
        now = datetime.now(timezone.utc)
        for job_dir, job, video in scheduled_jobs(application.bot_data["download_root"]):
            try:
                due = datetime.fromisoformat(job["scheduled_at"])
                if due > now:
                    continue
                job["status"] = "uploading"
                write_job(job_dir, job)
                post_url = await asyncio.to_thread(
                    publish_to_facebook,
                    video,
                    job["caption"],
                    application.bot_data["facebook"],
                )
            except Exception as exc:
                LOGGER.exception("Scheduled Facebook upload failed")
                job["status"] = "upload_failed"
                job["last_error"] = str(exc)
                write_job(job_dir, job)
                await application.bot.send_message(
                    chat_id=job["user_id"],
                    text="Scheduled upload failed. The master is still cached.",
                    reply_markup=job_keyboard(job["job_id"], retry=True),
                )
            else:
                record_published(
                    application.bot_data["state_db"], job, post_url
                )
                await application.bot.send_message(
                    chat_id=job["user_id"],
                    text=f"✅ Scheduled post published:\n{post_url}",
                )
                shutil.rmtree(job_dir, ignore_errors=True)
        await asyncio.sleep(30)


async def post_init(application: Application) -> None:
    application.bot_data["background_tasks"] = [
        asyncio.create_task(
            scheduled_worker(application), name="scheduled-publisher"
        ),
        asyncio.create_task(
            maintenance_worker(application), name="maintenance-worker"
        ),
    ]


async def post_shutdown(application: Application) -> None:
    tasks = application.bot_data.get("background_tasks", [])
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


async def job_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    query = update.callback_query
    action, job_id = query.data.split(":", 1)
    await query.answer()
    try:
        job_dir, job, video = load_job(
            context.application.bot_data["download_root"], job_id
        )
    except Exception as exc:
        await query.answer(str(exc), show_alert=True)
        return
    if job.get("user_id") != update.effective_user.id:
        await query.answer("This job belongs to another user.", show_alert=True)
        return

    if action in {"publish", "retry"} and job.get("status") == "published":
        await query.answer("This video is already published.", show_alert=True)
        return

    if action == "delete":
        shutil.rmtree(job_dir, ignore_errors=True)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Cached video deleted.")
        return

    if action == "ai_caption":
        ai = context.application.bot_data["openai"]
        if not ai["api_key"]:
            await query.answer("Add OPENAI_API_KEY to .env first.", show_alert=True)
            return
        progress = await query.message.reply_text("✨ Generating AI caption...")
        try:
            caption = await asyncio.to_thread(generate_caption, job, ai)
        except Exception:
            LOGGER.exception("AI caption generation failed")
            await progress.edit_text("AI caption generation failed. Check the VPS log.")
            return
        job["caption"] = caption
        write_job(job_dir, job)
        await progress.edit_text(
            f"✨ New caption:\n\n{caption}", reply_markup=job_keyboard(job_id)
        )
        return

    progress = await query.message.reply_text(
        "🚀 Uploading cached video to Booyah King — 95%"
    )
    try:
        post_url = await publish_with_progress(
            video,
            job["caption"],
            context.application.bot_data["facebook"],
            progress,
        )
    except Exception as exc:
        LOGGER.exception("Facebook upload failed")
        job["status"] = "upload_failed"
        job["last_error"] = str(exc)
        write_job(job_dir, job)
        await progress.edit_text(
            "Upload failed, but the processed video is cached. Retry will not download "
            "or render it again.",
            reply_markup=job_keyboard(job_id, retry=True),
        )
    else:
        job["status"] = "published"
        record_published(context.application.bot_data["state_db"], job, post_url)
        await progress.edit_text(f"✅ Published successfully — 100%\n{post_url}")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        shutil.rmtree(job_dir, ignore_errors=True)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.effective_message:
        await update.effective_message.reply_text("Cancelled. Send /start when ready.")
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    LOGGER.error(
        "Unhandled Telegram error",
        exc_info=(type(error), error, error.__traceback__) if error else None,
    )


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    load_dotenv(base_dir / ".env")
    asyncio.set_event_loop(asyncio.new_event_loop())
    token = required_setting("TELEGRAM_BOT_TOKEN")
    allowed_users = parse_allowed_users(required_setting("ALLOWED_USER_IDS"))
    download_root = base_dir / os.getenv("DOWNLOAD_DIR", "downloads")
    download_root.mkdir(parents=True, exist_ok=True)
    image_root = download_root / "images"
    image_root.mkdir(parents=True, exist_ok=True)
    state_db = base_dir / os.getenv("STATE_DB", "bot_state.db")
    init_state_db(state_db)

    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data.update(
        allowed_users=allowed_users,
        max_clip_seconds=int(os.getenv("MAX_CLIP_SECONDS", "7200")),
        max_video_height=int(os.getenv("MAX_VIDEO_HEIGHT", "720")),
        max_batch_clips=int(os.getenv("MAX_BATCH_CLIPS", "6")),
        preview_max_bytes=int(os.getenv("TELEGRAM_PREVIEW_MAX_MB", "45")) * 1024 * 1024,
        download_root=download_root,
        image_root=image_root,
        state_db=state_db,
        schedule_timezone=os.getenv("SCHEDULE_TIMEZONE", "UTC").strip(),
        draft_retention_days=int(os.getenv("DRAFT_RETENTION_DAYS", "14")),
        failed_retention_days=int(os.getenv("FAILED_RETENTION_DAYS", "30")),
        cleanup_interval=int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600")),
        token_check_interval=int(os.getenv("TOKEN_CHECK_INTERVAL_SECONDS", "21600")),
        logo_path=base_dir / os.getenv("LOGO_PATH", "assets/logo.png"),
        watermark_text=os.getenv("WATERMARK_TEXT", "Booyah King").strip(),
        facebook={
            "page_id": os.getenv("FACEBOOK_PAGE_ID", "").strip(),
            "access_token": os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip(),
            "graph_version": os.getenv("FACEBOOK_GRAPH_VERSION", "v23.0").strip(),
            "app_id": os.getenv("FACEBOOK_APP_ID", "").strip(),
            "app_secret": os.getenv("FACEBOOK_APP_SECRET", "").strip(),
        },
        image_facebook={
            "page_id": os.getenv("IMAGE_FACEBOOK_PAGE_ID", "").strip(),
            "access_token": os.getenv(
                "IMAGE_FACEBOOK_PAGE_ACCESS_TOKEN", ""
            ).strip(),
            "graph_version": os.getenv("FACEBOOK_GRAPH_VERSION", "v23.0").strip(),
        },
        local_image={
            "endpoint": os.getenv(
                "OLLAMA_ENDPOINT", "http://127.0.0.1:11434"
            ).strip(),
            "model": os.getenv("OLLAMA_MODEL", "llama3.1:8b").strip(),
        },
        openai={
            "api_key": os.getenv("OPENAI_API_KEY", "").strip(),
            "text_model": os.getenv("OPENAI_TEXT_MODEL", "gpt-4.1-mini").strip(),
            "transcribe_model": os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1").strip(),
            "max_minutes": int(os.getenv("AI_HIGHLIGHT_MAX_MINUTES", "60")),
        },
    )

    conversation = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url)],
            CHOOSE_SOURCE_ACTION: [
                CallbackQueryHandler(choose_source_action, pattern=r"^source:")
            ],
            ASK_TRIM: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_trim)],
            ASK_CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_caption)],
            ASK_HEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_headline)],
            CHOOSE_LAYOUT: [
                CallbackQueryHandler(choose_layout, pattern=r"^setup_layout:")
            ],
            CHOOSE_TEMPLATE: [
                CallbackQueryHandler(choose_template, pattern=r"^setup_template:")
            ],
            CONFIRM: [CallbackQueryHandler(setup_action, pattern=r"^setup:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(conversation)
    image_conversation = ConversationHandler(
        entry_points=[CommandHandler("image", image_start)],
        states={
            IMAGE_WAIT_PHOTO: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_image)
            ],
            IMAGE_WAIT_CAPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_image_caption)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(image_conversation)
    generated_image_conversation = ConversationHandler(
        entry_points=[CommandHandler("create_image", create_image_start)],
        states={
            GENERATE_IMAGE_TOPIC: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_generated_image_topic
                )
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(generated_image_conversation)
    edit_conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                begin_text_edit,
                pattern=r"^edit_(caption|headline|trim):[a-f0-9]{32}$",
            )
        ],
        states={
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text_edit)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    schedule_conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(begin_schedule, pattern=r"^schedule:[a-f0-9]{32}$")
        ],
        states={
            SCHEDULE_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_schedule)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(edit_conversation)
    application.add_handler(schedule_conversation)
    application.add_handler(
        CallbackQueryHandler(show_edit_menu, pattern=r"^edit_menu:[a-f0-9]{32}$")
    )
    application.add_handler(
        CallbackQueryHandler(edit_back, pattern=r"^edit_back:[a-f0-9]{32}$")
    )
    application.add_handler(
        CallbackQueryHandler(
            show_layout_menu, pattern=r"^edit_layout_menu:[a-f0-9]{32}$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            show_template_menu, pattern=r"^edit_template_menu:[a-f0-9]{32}$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            apply_style_edit,
            pattern=r"^edit_(layout|template):[a-f0-9]{32}:[a-z_]+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            job_action, pattern=r"^(publish|retry|ai_caption|delete):[a-f0-9]{32}$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            image_action,
            pattern=r"^image_(publish|regenerate|cancel):[a-f0-9]{32}$",
        )
    )
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("queue", queue_command))
    application.add_handler(CommandHandler("drafts", drafts_command))
    application.add_handler(CommandHandler("storage", storage_command))
    application.add_handler(CommandHandler("cleanup", cleanup_command))
    application.add_handler(CommandHandler("facebook_status", facebook_status_command))
    application.add_handler(
        CallbackQueryHandler(open_draft, pattern=r"^draft_open:[a-f0-9]{32}$")
    )
    application.add_error_handler(error_handler)
    LOGGER.info("Starting bot for %d authorized user(s)", len(allowed_users))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
