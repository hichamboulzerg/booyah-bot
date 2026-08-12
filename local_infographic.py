"""Local lesson generation and deterministic notebook-style infographic rendering."""

from __future__ import annotations

import json
import math
import textwrap
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont


BLUE = "#123ca0"
RED = "#c7352d"
GREEN = "#18834b"
YELLOW = "#f6c928"
INK = "#202124"
PAPER = "#fffdf4"


LESSON_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "sections": {
            "type": "array",
            "minItems": 3,
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "bullets": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 2,
                        "items": {"type": "string"},
                    },
                },
                "required": ["heading", "bullets"],
            },
        },
        "example": {"type": "string"},
        "caption": {"type": "string"},
    },
    "required": ["title", "sections", "example", "caption"],
}


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def generate_lesson(topic: str, endpoint: str, model: str) -> dict:
    prompt = (
        "Create a technically accurate beginner lesson for a vertical educational "
        f"infographic about {topic!r}. Use simple English. Make 3 or 4 sections, "
        "each with 1 or 2 short bullets. Each heading must be under 35 characters; "
        "each bullet under 90 characters. Add one concise example under 180 characters. "
        "Do not use markdown. Verify facts before answering."
    )
    response = requests.post(
        endpoint.rstrip("/") + "/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": LESSON_SCHEMA,
            "options": {"temperature": 0.2, "num_predict": 700},
        },
        timeout=(10, 600),
    )
    response.raise_for_status()
    payload = json.loads(response.json()["response"])
    sections = []
    for section in payload.get("sections", [])[:4]:
        heading = _clean(section.get("heading"), 35)
        bullets = [_clean(item, 90) for item in section.get("bullets", [])[:2]]
        bullets = [item for item in bullets if item]
        if heading and bullets:
            sections.append({"heading": heading, "bullets": bullets})
    if len(sections) < 3:
        raise RuntimeError("The local model returned too few lesson sections")
    return {
        "title": _clean(payload.get("title") or topic, 55),
        "sections": sections,
        "example": _clean(payload.get("example"), 180),
        "caption": _clean(
            payload.get("caption") or f"Learn {topic} with this visual guide.", 500
        ),
    }


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    path = Path("C:/Windows/Fonts") / name
    return ImageFont.truetype(str(path), size=size)


def _wrapped(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or draw.textlength(candidate, font=font) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _star(draw: ImageDraw.ImageDraw, cx: int, cy: int, radius: int = 24) -> None:
    points = []
    for index in range(10):
        angle = -math.pi / 2 + index * math.pi / 5
        distance = radius if index % 2 == 0 else radius * 0.45
        points.append((cx + math.cos(angle) * distance, cy + math.sin(angle) * distance))
    draw.polygon(points, fill=YELLOW, outline=INK, width=4)


def _bulb(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    draw.ellipse((x, y, x + 62, y + 62), fill="#ffe56b", outline=INK, width=4)
    draw.line((x + 20, y + 60, x + 42, y + 60), fill=INK, width=7)
    draw.line((x + 24, y + 70, x + 38, y + 70), fill=INK, width=6)
    for dx, dy in ((31, -16), (-15, 16), (77, 16)):
        draw.line((x + 31, y + 31, x + dx, y + dy), fill=YELLOW, width=5)


def render_notebook(lesson: dict, output: Path) -> None:
    width, height = 1024, 1536
    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)

    for y in range(78, height, 54):
        draw.line((0, y, width, y), fill="#bddbf1", width=2)
    draw.line((128, 0, 128, height), fill="#e99797", width=3)
    for y in range(28, height, 72):
        draw.ellipse((18, y, 46, y + 28), fill="#363636")
        draw.arc((-12, y - 10, 38, y + 42), 250, 110, fill="#111111", width=6)

    title_font = _font("Inkfree.ttf", 76)
    while draw.textlength(lesson["title"], font=title_font) > 760 and title_font.size > 48:
        title_font = _font("Inkfree.ttf", title_font.size - 4)
    title_x = 150 + (820 - draw.textlength(lesson["title"], font=title_font)) / 2
    draw.text((title_x, 34), lesson["title"], font=title_font, fill=BLUE, stroke_width=1)
    title_bottom = 34 + title_font.size + 15
    draw.line((230, title_bottom, 915, title_bottom), fill=BLUE, width=6)
    draw.line((300, title_bottom + 12, 840, title_bottom + 12), fill=BLUE, width=3)
    _star(draw, 172, 82, 26)
    _bulb(draw, 900, 54)

    heading_font = _font("segoeprb.ttf", 37)
    body_font = _font("comic.ttf", 29)
    code_font = _font("consola.ttf", 25)
    y = 190
    content_left, content_right = 170, 940
    section_gap = 24
    example_space = 220 if lesson.get("example") else 60
    available = 1325 - y - example_space
    section_height = max(190, (available - section_gap * (len(lesson["sections"]) - 1)) // len(lesson["sections"]))

    colors = [RED, GREEN, "#7a3db8", "#c06b00"]
    for index, section in enumerate(lesson["sections"]):
        box_top = y
        box_bottom = min(y + section_height, 1300 - example_space)
        draw.rounded_rectangle(
            (150, box_top, 960, box_bottom), radius=22,
            fill="#fffef8", outline="#31559a", width=3,
        )
        _star(draw, 178, box_top + 40, 20)
        draw.text((215, box_top + 18), section["heading"], font=heading_font, fill=BLUE)
        underline_end = min(900, 215 + int(draw.textlength(section["heading"], font=heading_font)))
        draw.line((215, box_top + 66, underline_end, box_top + 66), fill=colors[index], width=4)
        line_y = box_top + 82
        for bullet in section["bullets"]:
            lines = _wrapped(draw, bullet, body_font, content_right - content_left - 45)
            arrow_y = line_y + 17
            draw.line(
                (content_left, arrow_y, content_left + 24, arrow_y),
                fill=colors[index],
                width=4,
            )
            draw.polygon(
                [
                    (content_left + 24, arrow_y),
                    (content_left + 15, arrow_y - 7),
                    (content_left + 15, arrow_y + 7),
                ],
                fill=colors[index],
            )
            for line_index, line in enumerate(lines[:2]):
                draw.text((content_left + 42, line_y), line, font=body_font, fill=INK)
                line_y += 38
            line_y += 8
            if line_y > box_bottom - 38:
                break
        y = box_bottom + section_gap

    example = lesson.get("example", "")
    if example:
        top = 1300
        draw.rounded_rectangle((170, top, 940, 1480), radius=22, fill="#fff6c7", outline=RED, width=3)
        draw.text((205, top + 15), "Example", font=heading_font, fill=RED)
        lines = _wrapped(draw, example, code_font, 690)
        for index, line in enumerate(lines[:3]):
            draw.text((215, top + 70 + index * 34), line, font=code_font, fill=INK)
        draw.text((790, 1430), "Save & learn!", font=_font("Inkfree.ttf", 28), fill=GREEN)

    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG", optimize=True)


def create_infographic(
    topic: str, output: Path, endpoint: str, model: str
) -> tuple[str, str]:
    lesson = generate_lesson(topic, endpoint, model)
    render_notebook(lesson, output)
    lesson_text = "\n".join(
        [lesson["title"]]
        + [
            section["heading"] + ": " + "; ".join(section["bullets"])
            for section in lesson["sections"]
        ]
        + (["Example: " + lesson["example"]] if lesson.get("example") else [])
    )
    return lesson["caption"], lesson_text
