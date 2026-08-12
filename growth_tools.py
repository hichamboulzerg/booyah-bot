"""Growth analytics, planning, and community helpers for the two Facebook Pages."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


PAGE_LABELS = {"booyah": "Booyah King", "study": "Study Sketch - Visual Notes"}


def _shorten(text: object, limit: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    shortened = cleaned[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (shortened or cleaned[: limit - 1]) + "…"


def init_growth_db(path: Path) -> None:
    with closing(sqlite3.connect(path, timeout=30)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS growth_snapshots (
                page_key TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                followers INTEGER NOT NULL,
                posts INTEGER NOT NULL,
                reactions INTEGER NOT NULL,
                comments INTEGER NOT NULL,
                shares INTEGER NOT NULL,
                PRIMARY KEY (page_key, collected_at)
            );
            CREATE TABLE IF NOT EXISTS content_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_key TEXT NOT NULL,
                topic TEXT NOT NULL,
                content_type TEXT NOT NULL,
                post_url TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hook_experiments (
                experiment_id TEXT PRIMARY KEY,
                page_key TEXT NOT NULL,
                topic TEXT NOT NULL,
                hook_a TEXT NOT NULL,
                hook_b TEXT NOT NULL,
                winner TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS comment_suggestions (
                suggestion_id TEXT PRIMARY KEY,
                page_key TEXT NOT NULL,
                comment_id TEXT NOT NULL,
                author TEXT NOT NULL,
                original_text TEXT NOT NULL,
                suggested_reply TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            """
        )


def _facebook_error(response: requests.Response) -> RuntimeError:
    try:
        error = response.json().get("error", {})
        message = error.get("message") or response.text[:300]
        return RuntimeError(f"Facebook HTTP {response.status_code}: {message}")
    except ValueError:
        return RuntimeError(f"Facebook HTTP {response.status_code}")


def resolve_page_token(config: dict[str, str]) -> str:
    base = f"https://graph.facebook.com/{config['graph_version']}"
    token = config["access_token"]
    page_id = config["page_id"]
    identity = requests.get(
        f"{base}/me", params={"fields": "id", "access_token": token}, timeout=30
    )
    if identity.ok and str(identity.json().get("id")) == page_id:
        return token
    accounts = requests.get(
        f"{base}/me/accounts",
        params={
            "fields": "id,access_token,tasks",
            "limit": 100,
            "access_token": token,
        },
        timeout=30,
    )
    if not accounts.ok:
        raise _facebook_error(accounts)
    for page in accounts.json().get("data", []):
        if str(page.get("id")) == page_id and page.get("access_token"):
            return page["access_token"]
    raise RuntimeError("The token does not manage the configured Page")


def fetch_page_snapshot(
    page_key: str, config: dict[str, str], days: int = 7
) -> dict:
    token = resolve_page_token(config)
    base = (
        f"https://graph.facebook.com/{config['graph_version']}/{config['page_id']}"
    )
    page_response = requests.get(
        base,
        params={
            "fields": "id,name,fan_count,followers_count",
            "access_token": token,
        },
        timeout=30,
    )
    if not page_response.ok:
        raise _facebook_error(page_response)
    posts_response = requests.get(
        base + "/published_posts",
        params={
            "fields": (
                "id,message,created_time,permalink_url,shares,"
                "comments.limit(0).summary(true),reactions.limit(0).summary(true),"
                "attachments{media_type,type}"
            ),
            "limit": 100,
            "access_token": token,
        },
        timeout=30,
    )
    if not posts_response.ok:
        raise _facebook_error(posts_response)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    posts = []
    for post in posts_response.json().get("data", []):
        created = datetime.fromisoformat(post["created_time"].replace("Z", "+00:00"))
        if created < cutoff:
            continue
        attachments = post.get("attachments", {}).get("data") or [{}]
        posts.append(
            {
                "id": post["id"],
                "message": " ".join(post.get("message", "").split())[:250],
                "created_time": post["created_time"],
                "permalink_url": post.get("permalink_url", ""),
                "reactions": post.get("reactions", {})
                .get("summary", {})
                .get("total_count", 0),
                "comments": post.get("comments", {})
                .get("summary", {})
                .get("total_count", 0),
                "shares": post.get("shares", {}).get("count", 0),
                "type": attachments[0].get("media_type", "unknown"),
            }
        )
    page = page_response.json()
    return {
        "page_key": page_key,
        "name": page.get("name") or PAGE_LABELS[page_key],
        "followers": int(page.get("followers_count") or page.get("fan_count") or 0),
        "days": days,
        "posts": posts,
        "post_count": len(posts),
        "reactions": sum(post["reactions"] for post in posts),
        "comments": sum(post["comments"] for post in posts),
        "shares": sum(post["shares"] for post in posts),
    }


def store_snapshot(state_db: Path, snapshot: dict) -> None:
    with closing(sqlite3.connect(state_db, timeout=30)) as connection, connection:
        connection.execute(
            """
            INSERT INTO growth_snapshots
            (page_key, collected_at, followers, posts, reactions, comments, shares)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot["page_key"],
                datetime.now(timezone.utc).isoformat(),
                snapshot["followers"],
                snapshot["post_count"],
                snapshot["reactions"],
                snapshot["comments"],
                snapshot["shares"],
            ),
        )


def previous_snapshot(state_db: Path, page_key: str) -> dict | None:
    with closing(sqlite3.connect(state_db, timeout=30)) as connection:
        row = connection.execute(
            """
            SELECT collected_at, followers, posts, reactions, comments, shares
            FROM growth_snapshots WHERE page_key = ?
            ORDER BY collected_at DESC LIMIT 1 OFFSET 1
            """,
            (page_key,),
        ).fetchone()
    if not row:
        return None
    return dict(
        zip(
            ("collected_at", "followers", "posts", "reactions", "comments", "shares"),
            row,
        )
    )


def format_stats(snapshots: list[dict]) -> str:
    blocks = ["📊 Page growth — last 7 days"]
    for item in snapshots:
        engagements = item["reactions"] + item["comments"] + item["shares"]
        average = engagements / item["post_count"] if item["post_count"] else 0
        blocks.append(
            f"\n{item['name']}\n"
            f"Followers: {item['followers']}\n"
            f"Posts: {item['post_count']}\n"
            f"Reactions: {item['reactions']} | Comments: {item['comments']} | "
            f"Shares: {item['shares']}\n"
            f"Engagements per post: {average:.1f}"
        )
    blocks.append(
        "\nReach, watch time and follower sources are available in Meta Business Suite; "
        "the bot reports the reliable Page-level fields available to its token."
    )
    return "\n".join(blocks)


def weekly_report(snapshots: list[dict]) -> str:
    lines = ["📈 Weekly growth report"]
    for item in snapshots:
        posts = item["posts"]
        top = max(
            posts,
            key=lambda post: post["reactions"] + 2 * post["comments"] + 3 * post["shares"],
            default=None,
        )
        lines.append(f"\n{item['name']}")
        lines.append(
            f"Published {item['post_count']} post(s); {item['reactions']} reactions, "
            f"{item['comments']} comments and {item['shares']} shares."
        )
        if top:
            score = top["reactions"] + 2 * top["comments"] + 3 * top["shares"]
            lines.append(
                f"Best post: {top['type']} with weighted engagement {score}.\n"
                f"{top['permalink_url']}"
            )
        if item["post_count"] == 0:
            lines.append("Action: publish one strong, on-brand post before adding volume.")
        elif item["comments"] + item["shares"] == 0:
            lines.append(
                "Action: test a stronger opening hook and one natural question or "
                "save/share reason."
            )
        elif top:
            lines.append("Action: make a new Part 2 using the best post's topic and format.")
    lines.append(
        "\nDecision rule: repeat topics that earn comments/shares; reactions alone are "
        "a weaker signal. Review reach and watch time in Meta Business Suite."
    )
    return "\n".join(lines)


def _ollama_json(endpoint: str, model: str, prompt: str, schema: dict, predict=1400):
    response = requests.post(
        endpoint.rstrip("/") + "/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0.2, "num_predict": predict},
        },
        timeout=(10, 900),
    )
    response.raise_for_status()
    return json.loads(response.json()["response"])


def create_content_plan(
    local: dict[str, str], snapshots: list[dict], recent_topics: list[str]
) -> str:
    schema = {
        "type": "object",
        "properties": {
            "days": {
                "type": "array",
                "minItems": 7,
                "maxItems": 7,
                "items": {
                    "type": "object",
                    "properties": {
                        "day": {"type": "string"},
                        "booyah": {"type": "string"},
                        "study": {"type": "string"},
                    },
                    "required": ["day", "booyah", "study"],
                },
            }
        },
        "required": ["days"],
    }
    prompt = (
        "Build a practical seven-day Facebook content calendar for two separate Pages. "
        "Booyah King publishes original or substantially transformed gaming Reels with "
        "commentary, useful analysis, humor, or storytelling. Study Sketch - Visual "
        "Notes publishes accurate visual lessons in programming, technology, science, "
        "or study skills. Give each Page one specific post idea per day, including a "
        "short hook and format. Never mix the brands. Avoid repeating these recent "
        f"topics: {recent_topics[-20:]}. Current summary: "
        + json.dumps(
            [
                {
                    "page": item["name"],
                    "followers": item["followers"],
                    "posts": item["post_count"],
                    "comments": item["comments"],
                    "shares": item["shares"],
                }
                for item in snapshots
            ]
        )
    )
    result = _ollama_json(local["endpoint"], local["model"], prompt, schema, 1500)
    lines = ["🗓 Seven-day content plan"]
    for day in result.get("days", [])[:7]:
        lines.extend(
            [
                f"\n{day.get('day', 'Day')}",
                f"🎮 Booyah: {day.get('booyah', '')}",
                f"📚 Study: {day.get('study', '')}",
            ]
        )
    return "\n".join(lines)[:4000]


def create_hooks(local: dict[str, str], topic: str, page_key: str) -> tuple[str, str]:
    schema = {
        "type": "object",
        "properties": {
            "hook_a": {"type": "string", "maxLength": 100},
            "hook_b": {"type": "string", "maxLength": 100},
        },
        "required": ["hook_a", "hook_b"],
    }
    brand = PAGE_LABELS[page_key]
    result = _ollama_json(
        local["endpoint"],
        local["model"],
        f"Write two substantially different, accurate Facebook opening hooks for "
        f"{brand} about {topic!r}. Hook A should use curiosity. Hook B should promise "
        "a concrete benefit. Each hook must be one complete sentence under 85 "
        "characters. No clickbait, hashtags, or false claims.",
        schema,
        300,
    )
    return _shorten(result["hook_a"], 90), _shorten(result["hook_b"], 90)


def save_hook_experiment(
    state_db: Path, page_key: str, topic: str, hook_a: str, hook_b: str
) -> str:
    experiment_id = uuid.uuid4().hex[:10]
    with closing(sqlite3.connect(state_db, timeout=30)) as connection, connection:
        connection.execute(
            """
            INSERT INTO hook_experiments
            (experiment_id, page_key, topic, hook_a, hook_b, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                experiment_id,
                page_key,
                topic,
                hook_a,
                hook_b,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return experiment_id


def mark_hook_winner(state_db: Path, experiment_id: str, winner: str) -> None:
    if winner not in {"A", "B"}:
        raise RuntimeError("Invalid hook winner")
    with closing(sqlite3.connect(state_db, timeout=30)) as connection, connection:
        cursor = connection.execute(
            "UPDATE hook_experiments SET winner = ? WHERE experiment_id = ?",
            (winner, experiment_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Hook experiment not found")


def create_booyah_companions(local: dict[str, str], job: dict) -> str:
    schema = {
        "type": "object",
        "properties": {
            "question_post": {"type": "string", "maxLength": 300},
            "quote_card": {"type": "string", "maxLength": 120},
            "part_two": {"type": "string", "maxLength": 300},
        },
        "required": ["question_post", "quote_card", "part_two"],
    }
    prompt = (
        "Turn this Booyah King gaming Reel into three original companion ideas: one "
        "natural community question, one short quote-card line, and one genuinely new "
        "Part 2 angle. Add analysis or storytelling; do not claim ownership or facts "
        "not provided. Headline: "
        + str(job.get("headline", ""))
        + ". Caption: "
        + str(job.get("caption", ""))
    )
    result = _ollama_json(local["endpoint"], local["model"], prompt, schema, 600)
    return (
        "🎮 Booyah companion content\n\n"
        f"Question post:\n{_shorten(result['question_post'], 300)}\n\n"
        f"Quote card:\n{_shorten(result['quote_card'], 120)}\n\n"
        f"Part 2 angle:\n{_shorten(result['part_two'], 300)}"
    )


def record_content(
    state_db: Path, page_key: str, topic: str, content_type: str, post_url: str
) -> None:
    with closing(sqlite3.connect(state_db, timeout=30)) as connection, connection:
        connection.execute(
            """
            INSERT INTO content_memory
            (page_key, topic, content_type, post_url, published_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                page_key,
                topic[:200],
                content_type,
                post_url,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def recent_topics(state_db: Path, limit: int = 30) -> list[str]:
    with closing(sqlite3.connect(state_db, timeout=30)) as connection:
        rows = connection.execute(
            "SELECT topic FROM content_memory ORDER BY published_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [row[0] for row in rows]


def format_series(state_db: Path) -> str:
    with closing(sqlite3.connect(state_db, timeout=30)) as connection:
        rows = connection.execute(
            """
            SELECT page_key, topic, content_type, published_at, post_url
            FROM content_memory ORDER BY published_at DESC LIMIT 20
            """
        ).fetchall()
    if not rows:
        return "No tracked published topics yet. New posts will be remembered automatically."
    lines = ["🧠 Recent topic and series memory"]
    for page_key, topic, content_type, published_at, _ in rows:
        lines.append(
            f"• {PAGE_LABELS.get(page_key, page_key)} — {topic} "
            f"({content_type}, {published_at[:10]})"
        )
    return "\n".join(lines)[:4000]


def quality_report(draft: dict, draft_dir: Path) -> str:
    issues = []
    lesson = str(draft.get("lesson", ""))
    filenames = draft.get("image_filenames") or [draft.get("image_filename", "")]
    if not 3 <= len([name for name in filenames if name]) <= 5 and draft.get("generated"):
        issues.append("Generated lessons should contain 3 to 5 slides.")
    missing = [name for name in filenames if name and not (draft_dir / name).is_file()]
    if missing:
        issues.append(f"Missing {len(missing)} cached image file(s).")
    lines = [line.strip().lower() for line in lesson.splitlines() if line.strip()]
    if len(lines) != len(set(lines)):
        issues.append("The lesson contains duplicated lines.")
    if re.search(r"\b(?:current|today's|latest)\s+(?:price|version|ip)\b", lesson.lower()):
        issues.append("The lesson may contain a temporary fact that needs verification.")
    if any(len(line) > 240 for line in lesson.splitlines()):
        issues.append("At least one explanation is too long for comfortable reading.")
    if not lesson and draft.get("generated"):
        issues.append("The generated draft has no stored lesson text.")
    if issues:
        return "⚠️ Quality review\n" + "\n".join(f"• {issue}" for issue in issues)
    return (
        "✅ Automated quality checks passed: slide count, cached files, duplicate text, "
        "temporary-fact wording, and text length. Manually verify factual accuracy."
    )


def originality_report(job: dict) -> str:
    warnings = []
    if job.get("youtube_url"):
        warnings.append("The source is YouTube; confirm you own it or have permission.")
    caption = str(job.get("caption", ""))
    headline = str(job.get("headline", ""))
    if len(caption.split()) < 8:
        warnings.append("The caption adds little context or original analysis.")
    if len(headline.split()) < 3:
        warnings.append("The headline is too generic to show a new creative angle.")
    if not any(
        word in (caption + " " + headline).lower()
        for word in ("why", "how", "tip", "analysis", "lesson", "story", "mistake")
    ):
        warnings.append("Add commentary, analysis, teaching, humor, or a clear storyline.")
    if warnings:
        return "⚠️ Originality review\n" + "\n".join(f"• {item}" for item in warnings)
    return "✅ The metadata shows a meaningful creative angle. Rights still require manual confirmation."


def latest_image_draft(image_root: Path, user_id: int) -> tuple[Path, dict] | None:
    candidates = []
    for meta in image_root.glob("*/image.json"):
        try:
            draft = json.loads(meta.read_text(encoding="utf-8-sig"))
            if draft.get("user_id") == user_id and draft.get("image_filenames"):
                candidates.append((meta.stat().st_mtime, meta.parent, draft))
        except (OSError, ValueError):
            continue
    if not candidates:
        return None
    _, directory, draft = max(candidates, key=lambda item: item[0])
    return directory, draft


def build_slideshow_reel(
    draft_dir: Path, draft: dict, ffmpeg_exe: str, output: Path
) -> Path:
    files = [draft_dir / name for name in draft.get("image_filenames", [])]
    if not files or not all(path.is_file() for path in files):
        raise RuntimeError("The latest carousel files are missing")
    with tempfile.TemporaryDirectory(prefix="study-reel-") as temp_name:
        concat_file = Path(temp_name) / "slides.txt"
        lines = []
        for path in files:
            safe = str(path.resolve()).replace("'", "'\\''")
            lines.extend([f"file '{safe}'", "duration 4"])
        safe_last = str(files[-1].resolve()).replace("'", "'\\''")
        lines.append(f"file '{safe_last}'")
        concat_file.write_text("\n".join(lines), encoding="utf-8")
        command = [
            ffmpeg_exe,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-vf",
            "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=white,format=yuv420p",
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-movflags",
            "+faststart",
            str(output),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
        if result.returncode:
            raise RuntimeError(result.stderr[-1000:])
    return output


def fetch_recent_comments(
    configs: dict[str, dict[str, str]], limit: int = 8
) -> list[dict]:
    comments = []
    for page_key, config in configs.items():
        token = resolve_page_token(config)
        base = (
            f"https://graph.facebook.com/{config['graph_version']}/{config['page_id']}"
        )
        response = requests.get(
            base + "/published_posts",
            params={
                "fields": (
                    "id,comments.limit(10){id,message,from,created_time,parent}"
                ),
                "limit": 15,
                "access_token": token,
            },
            timeout=30,
        )
        if not response.ok:
            raise _facebook_error(response)
        for post in response.json().get("data", []):
            for comment in post.get("comments", {}).get("data", []):
                comments.append(
                    {
                        "page_key": page_key,
                        "comment_id": comment["id"],
                        "post_id": post["id"],
                        "author": comment.get("from", {}).get("name", "Follower"),
                        "message": " ".join(comment.get("message", "").split())[:500],
                        "created_time": comment.get("created_time", ""),
                    }
                )
    return sorted(comments, key=lambda item: item["created_time"], reverse=True)[:limit]


def suggest_comment_replies(
    local: dict[str, str], comments: list[dict]
) -> list[str]:
    if not comments:
        return []
    schema = {
        "type": "object",
        "properties": {
            "replies": {
                "type": "array",
                "minItems": len(comments),
                "maxItems": len(comments),
                "items": {"type": "string", "maxLength": 240},
            }
        },
        "required": ["replies"],
    }
    prompt = (
        "Write one warm, natural Facebook Page reply for each comment in the same "
        "order. Match the comment's language when clear. Do not make factual claims, "
        "promise prizes, ask for personal data, or sound automated. Comments: "
        + json.dumps(
            [
                {
                    "page": PAGE_LABELS[item["page_key"]],
                    "author": item["author"],
                    "comment": item["message"],
                }
                for item in comments
            ],
            ensure_ascii=False,
        )
    )
    result = _ollama_json(local["endpoint"], local["model"], prompt, schema, 700)
    return [str(reply).strip() for reply in result.get("replies", [])]


def save_comment_suggestion(
    state_db: Path, comment: dict, reply: str
) -> str:
    suggestion_id = uuid.uuid4().hex[:12]
    with closing(sqlite3.connect(state_db, timeout=30)) as connection, connection:
        connection.execute(
            """
            INSERT INTO comment_suggestions
            (suggestion_id, page_key, comment_id, author, original_text,
             suggested_reply, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                suggestion_id,
                comment["page_key"],
                comment["comment_id"],
                comment["author"],
                comment["message"],
                reply,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return suggestion_id


def act_on_comment_suggestion(
    state_db: Path,
    suggestion_id: str,
    approve: bool,
    configs: dict[str, dict[str, str]],
) -> str:
    with closing(sqlite3.connect(state_db, timeout=30)) as connection:
        row = connection.execute(
            """
            SELECT page_key, comment_id, suggested_reply, status
            FROM comment_suggestions WHERE suggestion_id = ?
            """,
            (suggestion_id,),
        ).fetchone()
    if not row:
        raise RuntimeError("Comment suggestion not found")
    page_key, comment_id, reply, status = row
    if status != "pending":
        raise RuntimeError(f"Suggestion is already {status}")
    new_status = "skipped"
    if approve:
        config = configs[page_key]
        token = resolve_page_token(config)
        response = requests.post(
            f"https://graph.facebook.com/{config['graph_version']}/{comment_id}/comments",
            data={"message": reply, "access_token": token},
            timeout=30,
        )
        if not response.ok:
            raise _facebook_error(response)
        new_status = "posted"
    with closing(sqlite3.connect(state_db, timeout=30)) as connection, connection:
        connection.execute(
            "UPDATE comment_suggestions SET status = ? WHERE suggestion_id = ?",
            (new_status, suggestion_id),
        )
    return new_status


def growth_alerts(state_db: Path, snapshots: list[dict]) -> list[str]:
    alerts = []
    for item in snapshots:
        previous = previous_snapshot(state_db, item["page_key"])
        if previous and item["followers"] > previous["followers"]:
            alerts.append(
                f"{item['name']} gained {item['followers'] - previous['followers']} "
                "follower(s) since the previous check."
            )
        scores = [
            post["reactions"] + 2 * post["comments"] + 3 * post["shares"]
            for post in item["posts"]
        ]
        if len(scores) >= 3:
            average = sum(scores) / len(scores)
            best_index = max(range(len(scores)), key=scores.__getitem__)
            if average > 0 and scores[best_index] >= max(3, average * 2):
                alerts.append(
                    f"{item['name']} has an outperforming post. Create a related Part 2:\n"
                    f"{item['posts'][best_index]['permalink_url']}"
                )
        if item["post_count"] and item["comments"] + item["shares"] == 0:
            alerts.append(
                f"{item['name']} published {item['post_count']} post(s) but earned no "
                "comments or shares. Test a new hook before increasing volume."
            )
    return alerts
