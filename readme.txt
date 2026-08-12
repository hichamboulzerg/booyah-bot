# YouTube → Facebook Telegram Bot

## What this is

A Telegram bot that runs on your VPS and automates turning your existing
YouTube videos into Facebook posts. Instead of manually downloading a video,
trimming it, and uploading it to your Facebook Page, you just chat with the
bot and it does all three steps for you.

## Who it's for

You have a YouTube channel with a backlog of videos. You want to start
repurposing clips from those videos as Facebook posts, without manually
running screen recorders or video editors every time.

## How it works — the flow

```
You (Telegram) → Bot (on your VPS) → YouTube → ffmpeg → Facebook Page
```

1. **You send the bot a YouTube link** of one of your videos.
2. **The bot asks for a trim range** — e.g. `00:00:10-00:01:30` to grab that
   30–second-to-2-minute clip, or `full` to keep the whole video.
3. **The bot asks for a caption** — whatever text you want on the Facebook
   post.
4. **The bot does the work automatically:**
   - Downloads the video from YouTube using `yt-dlp`
   - Cuts it to your chosen time range using `ffmpeg`
   - Uploads the trimmed video straight to your Facebook Page using the
     Facebook Graph API
5. **The bot replies with a link** to the new Facebook post once it's live.

You never have to touch the VPS directly after setup — everything after
that happens through Telegram chat.

## The three pieces it's built from

| Piece | What it does | Why it's needed |
|---|---|---|
| **Telegram Bot API** | Lets the bot receive your messages and reply | This is your remote control |
| **yt-dlp** | Downloads videos from YouTube | Grabs the source video file |
| **ffmpeg** | Cuts/trims video files | Produces the exact clip you want |
| **Facebook Graph API** | Publishes videos to your Page | Posts the final result |

## What's running where

- **The bot itself (`bot.py`)** runs continuously on your Windows VPS,
  listening for your Telegram messages.
- **Downloaded/trimmed video files** are stored temporarily in a
  `downloads` folder next to the script, then uploaded and left there
  (you can clean these up periodically, or I can add auto-cleanup).
- **Nothing runs on your phone or laptop** — you're just chatting with the
  bot from wherever Telegram is installed. The VPS does all the heavy
  lifting.

## Security notes

- The bot only responds to your Telegram account — its `ALLOWED_USER_IDS`
  list is locked to your user ID, so no one else who finds the bot's
  username can use it to post to your Facebook Page.
- Your Telegram bot token and Facebook Page access token live in `bot.py`
  on your VPS. Treat both like passwords — don't share them or commit them
  to a public repo. If a token ever leaks, regenerate it (BotFather's
  `/revoke` for Telegram, or generate a fresh token in Facebook's Graph API
  Explorer).

## Current limitations / things that can be added later

- **Trimming** currently needs exact `HH:MM:SS-HH:MM:SS` timestamps typed
  in chat. A preview-thumbnail step could be added so you can eyeball the
  clip before it's cut.
- **No automatic intro clip** yet — every post uses just the trimmed
  YouTube footage. A fixed intro (e.g. your channel logo/sting) can be
  spliced onto the front of every clip automatically.
- **No scheduling** — posts go live on Facebook immediately after
  processing. A "post later" option could be added if you want to queue up
  content.
- **Single Facebook Page** — currently configured for one Page. Posting to
  multiple Pages would need a small extension to let you pick a
  destination per post.

## Files in this project

- `bot.py` — the bot itself
- `requirements.txt` — Python packages it depends on
- `SETUP.md` — step-by-step install and configuration instructions
- `README.md` — this file