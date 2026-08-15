# Setup

## 1. Install prerequisites

Install Python 3.11 or newer. The Python dependencies include a portable ffmpeg
binary, so a separate system ffmpeg installation is not required. Confirm Python works:

```powershell
python --version
```

## 2. Create the Python environment

From this project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade -r requirements.txt
```

## 3. Configure secrets

Edit `.env` and set:

- `TELEGRAM_BOT_TOKEN`: rotate the token exposed in chat using BotFather's `/revoke`, then paste the replacement.
- `ALLOWED_USER_IDS`: Telegram user IDs allowed to use the bot.
- `FACEBOOK_PAGE_ID`: numeric Facebook Page ID.
- `FACEBOOK_PAGE_ACCESS_TOKEN`: long-lived Page access token with the permissions required to publish Page videos.
- `IMAGE_FACEBOOK_PAGE_ID`: ID of the separate education Page used by `/image`.
- `IMAGE_FACEBOOK_PAGE_ACCESS_TOKEN`: access token for that education Page. Keep it separate so images can never be sent to the video Page accidentally.
- `OLLAMA_ENDPOINT`: local Ollama server used by `/create_image`.
- `OLLAMA_MODEL`: local lesson-writing model (default `llama3.1:8b`). The image renderer uses no paid API.
- `DENO_PATH`: Deno executable used by yt-dlp for YouTube JavaScript challenges (default `.tools/deno/deno.exe`). Install Deno 2.3 or newer.

`/create_image` accepts any educational topic and builds an adaptive 3-to-5-slide
notebook-style carousel. Every slide contains two concise teaching points and two
distinct practical examples. Five rotating visual layouts keep the album varied while
preserving the Study Sketch identity. Telegram previews the complete album before
Facebook publishing.

Never commit or share `.env`.

## 4. Start the bot

```powershell
.\.venv\Scripts\python.exe .\bot.py
```

Open the bot in Telegram, send `/start`, then follow the prompts.

## 5. Keep it running on Windows

Use Task Scheduler to run `.venv\Scripts\python.exe` with `bot.py` as the argument and this project directory as **Start in**. Configure restart-on-failure and run whether the user is logged in or not.

## Notes

- Only download and republish videos you own or have permission to use.
- Prepared videos remain cached until publishing succeeds or you press Delete.
- A failed Facebook upload shows a Retry button and does not download or render again.
- Downloads default to Facebook-ready H.264/AAC at 720p for faster trimming and uploading. Change `MAX_VIDEO_HEIGHT` in `.env` if needed.
- Put a transparent logo at `assets/logo.png`. Without one, the text watermark still appears.
- Add `OPENAI_API_KEY` to `.env` to enable AI highlight suggestions and AI captions.
- AI highlights transcribe at most `AI_HIGHLIGHT_MAX_MINUTES` from each source.
- Telegram receives progress by stage and a preview when the processed file fits the configured preview limit.
- Enter multiple comma-separated trim ranges to create a batch from one YouTube download (up to `MAX_BATCH_CLIPS`).
- Every draft can change its caption, headline, trim, layout, or branding template before publishing.
- Layouts: vertical blur, vertical crop, landscape, square, and gaming frame.
- Branding templates: Gaming, Breaking News, Highlights, and Funny.
- Use the Schedule button to queue a draft. Times use `SCHEDULE_TIMEZONE` (UTC by default).
- `/queue` lists upcoming scheduled Facebook posts.
- `/drafts` lists cached drafts with controls to open, edit, publish, schedule, or delete them.
- `/facebook_status` checks the token, Booyah King access, and publishing permission. Add optional `FACEBOOK_APP_ID` and `FACEBOOK_APP_SECRET` for expiry timestamps.
- Facebook uploads show transferred MB, percentage, speed, and ETA.
- Duplicate source/range combinations are blocked across drafts and published history. Prefix a range request with `force:` to intentionally override the warning.
- `/storage` reports cached disk usage; `/cleanup` removes only drafts older than their configured retention. Scheduled and uploading jobs are preserved.
- Automatic cleanup uses `DRAFT_RETENTION_DAYS` and `FAILED_RETENTION_DAYS`.
- `/cmd` shows the complete Telegram command center.
- `/stats`, `/weekly_report`, and `/alerts` monitor both Facebook Pages. Automatic growth checks use `GROWTH_CHECK_INTERVAL_SECONDS` (six hours by default).
- `/content_plan` builds a seven-day plan; `/hooks` creates A/B openings; `/series` shows remembered topics.
- `/quality` checks the latest Study Sketch carousel; `/originality` warns about weakly transformed Booyah drafts.
- `/repurpose` turns the latest Study Sketch carousel into a vertical video. `/comments` prepares Page replies and posts them only after Telegram approval.
- `/cancel` ends the active conversation.
