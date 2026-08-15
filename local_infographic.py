"""Local lesson generation and deterministic notebook-style infographic rendering."""

from __future__ import annotations

import json
import math
import re
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
        "title": {"type": "string", "maxLength": 55},
        "sections": {
            "type": "array",
            "minItems": 4,
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string", "maxLength": 35},
                    "bullets": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 3,
                        "items": {"type": "string", "maxLength": 105},
                    },
                },
                "required": ["heading", "bullets"],
            },
        },
        "example": {"type": "string", "maxLength": 220},
        "caption": {"type": "string", "maxLength": 300},
    },
    "required": ["title", "sections", "example", "caption"],
}


CAROUSEL_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 55},
        "caption": {"type": "string", "maxLength": 500},
        "slides": {
            "type": "array",
            "minItems": 3,
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": 48},
                    "subtitle": {"type": "string", "maxLength": 100},
                    "points": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 2,
                        "items": {
                            "type": "object",
                            "properties": {
                                "heading": {"type": "string", "maxLength": 35},
                                "explanation": {
                                    "type": "string",
                                    "maxLength": 150,
                                },
                            },
                            "required": ["heading", "explanation"],
                        },
                    },
                    "examples": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 2,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "maxLength": 24},
                                "content": {
                                    "type": "string",
                                    "minLength": 10,
                                    "maxLength": 170,
                                },
                            },
                            "required": ["label", "content"],
                        },
                    },
                },
                "required": ["title", "subtitle", "points", "examples"],
            },
        },
    },
    "required": ["title", "caption", "slides"],
}


def _clean(value: object, limit: int) -> str:
    cleaned = " ".join(str(value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    shortened = cleaned[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return (shortened or cleaned[: limit - 1]) + "…"


def generate_lesson(topic: str, endpoint: str, model: str) -> dict:
    prompt = (
        "Create a technically accurate beginner lesson for a vertical educational "
        f"infographic about {topic!r}. Use simple English and explain ideas, not just "
        "name them. Make exactly 4 sections with 2 or 3 useful bullets each. Cover "
        "these four sections in this exact order: What It Is (definition and core "
        "idea), How It Works (process or mechanism), Why It Matters (uses and "
        "benefits), and Tips & Mistakes (practical advice and a misconception to "
        "avoid). Do not put an example in any section; use only the separate example "
        "field for that. Each heading must be under 35 characters and each bullet "
        "under 105 characters. Add one practical example under 220 characters. "
        "Avoid repeated points and temporary facts such as current IP addresses, "
        "prices, dates, or software versions; use a generic placeholder when needed. "
        "Do not use markdown. Carefully verify every fact."
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
        bullets = [_clean(item, 105) for item in section.get("bullets", [])[:3]]
        bullets = [item for item in bullets if item]
        if heading and bullets:
            sections.append({"heading": heading, "bullets": bullets})
    if len(sections) < 4:
        raise RuntimeError("The local model returned too few lesson sections")
    return {
        "title": _clean(payload.get("title") or topic, 55),
        "sections": sections,
        "example": _clean(payload.get("example"), 220),
        "caption": _clean(
            payload.get("caption") or f"Learn {topic} with this visual guide.", 500
        ),
    }


def generate_carousel_lesson(topic: str, endpoint: str, model: str) -> dict:
    prompt = (
        "Plan a valuable, beginner-friendly educational image carousel about "
        f"{topic!r}. This topic can belong to ANY field, so adapt the teaching "
        "structure to the subject instead of assuming it is programming. Choose 3 "
        "to 5 slides based on how much explanation is genuinely useful. The slides "
        "must form one progressive lesson: begin with the foundation, explain the "
        "important ideas or process, show practical meaning or use, include concrete "
        "examples where helpful, and finish with key takeaways, advice, limitations, "
        "or common mistakes. Every slide needs exactly 2 distinct teaching points. "
        "A point heading names the idea; its explanation says what it means and why "
        "it matters in simple English. Every slide must also have exactly 2 different "
        "concrete examples. Examples can be short code samples, worked calculations, "
        "real-life scenarios, mini exercises, comparisons, or demonstrations, chosen "
        "to fit the topic. Give each example a useful short label. Examples must show "
        "how the ideas are applied, not repeat the explanations. Avoid filler, "
        "repeated points, markdown, "
        "unsafe instructions, and "
        "temporary facts such as current prices, live IP addresses, or version "
        "numbers. Keep facts stable and carefully verify them before answering."
        " Write a specific, engaging Facebook caption that summarizes what the "
        "reader will learn; do not use a generic phrase such as educational carousel."
    )
    response = requests.post(
        endpoint.rstrip("/") + "/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": CAROUSEL_SCHEMA,
            "options": {"temperature": 0.18, "num_predict": 2200},
        },
        timeout=(10, 900),
    )
    response.raise_for_status()
    payload = json.loads(response.json()["response"])
    slides = []
    for raw_slide in payload.get("slides", [])[:5]:
        points = []
        for raw_point in raw_slide.get("points", [])[:2]:
            heading = _clean(raw_point.get("heading"), 35)
            explanation = _clean(raw_point.get("explanation"), 150)
            if heading and explanation:
                points.append({"heading": heading, "explanation": explanation})
        if len(points) != 2:
            continue
        examples = []
        for raw_example in raw_slide.get("examples", [])[:2]:
            label = _clean(raw_example.get("label"), 24)
            content = _clean(raw_example.get("content"), 170)
            if label and content:
                examples.append({"label": label, "content": content})
        if len(examples) != 2:
            continue
        slides.append(
            {
                "title": _clean(raw_slide.get("title"), 48),
                "subtitle": _clean(raw_slide.get("subtitle"), 100),
                "points": points,
                "examples": examples,
            }
        )
    if not 3 <= len(slides) <= 5:
        raise RuntimeError("The local model did not return 3 to 5 complete slides")
    all_headings = [
        point["heading"].casefold()
        for slide in slides
        for point in slide["points"]
    ]
    if len(all_headings) != len(set(all_headings)):
        raise RuntimeError("The local model repeated a teaching-point heading")
    all_explanations = [
        point["explanation"].casefold()
        for slide in slides
        for point in slide["points"]
    ]
    if len(all_explanations) != len(set(all_explanations)):
        raise RuntimeError("The local model repeated an explanation")
    all_examples = [
        example["content"].casefold()
        for slide in slides
        for example in slide["examples"]
    ]
    if len(all_examples) != len(set(all_examples)):
        raise RuntimeError("The local model repeated a practical example")
    combined = " ".join(
        all_explanations
        + [
            example["content"].casefold()
            for slide in slides
            for example in slide["examples"]
        ]
    )
    temporary_patterns = (
        r"\bcurrent price\b",
        r"\blatest version\b",
        r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    )
    if any(re.search(pattern, combined) for pattern in temporary_patterns):
        raise RuntimeError("The local model used a temporary fact or live IP address")
    return {
        "title": _clean(payload.get("title") or topic, 55),
        "caption": _clean(
            payload.get("caption") or f"Learn {topic} with this visual guide.", 500
        ),
        "slides": slides,
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
    width, height = 1024, 1792
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
    body_font = _font("comic.ttf", 26)
    code_font = _font("consola.ttf", 23)
    y = 190
    content_left, content_right = 170, 940
    section_gap = 24
    example_top = 1530
    available = example_top - 30 - y
    section_height = max(190, (available - section_gap * (len(lesson["sections"]) - 1)) // len(lesson["sections"]))

    colors = [RED, GREEN, "#7a3db8", "#c06b00"]
    for index, section in enumerate(lesson["sections"]):
        box_top = y
        box_bottom = min(y + section_height, example_top - 30)
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
                line_y += 34
            line_y += 8
            if line_y > box_bottom - 38:
                break
        y = box_bottom + section_gap

    example = lesson.get("example", "")
    if example:
        top = example_top
        draw.rounded_rectangle((170, top, 940, 1740), radius=22, fill="#fff6c7", outline=RED, width=3)
        draw.text((205, top + 15), "Example", font=heading_font, fill=RED)
        lines = _wrapped(draw, example, code_font, 690)
        for index, line in enumerate(lines[:3]):
            draw.text((215, top + 70 + index * 32), line, font=code_font, fill=INK)
        draw.text((790, 1690), "Save & learn!", font=_font("Inkfree.ttf", 28), fill=GREEN)

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


def render_carousel_slide(
    course_title: str,
    slide: dict,
    slide_number: int,
    slide_count: int,
    output: Path,
) -> None:
    width, height = 1024, 1536
    themes = [
        {"paper": "#fffdf4", "accent": "#1746a2", "second": "#d43931", "soft": "#fff2bd"},
        {"paper": "#f3fff8", "accent": "#087f5b", "second": "#e67700", "soft": "#d8f5e8"},
        {"paper": "#fcf7ff", "accent": "#7048b5", "second": "#d6336c", "soft": "#eee2ff"},
        {"paper": "#fff8ef", "accent": "#b45309", "second": "#2563a5", "soft": "#ffe4bd"},
        {"paper": "#f5fbff", "accent": "#176b87", "second": "#c93663", "soft": "#dff3fb"},
    ]
    designs = ["stacked", "split", "example_first", "steps", "sticky"]
    theme = themes[(slide_number - 1) % len(themes)]
    design = designs[(slide_number - 1) % len(designs)]
    image = Image.new("RGB", (width, height), theme["paper"])
    draw = ImageDraw.Draw(image)
    if design in {"stacked", "example_first", "steps"}:
        for y in range(78, height, 54):
            draw.line((0, y, width, y), fill="#c6deec", width=2)
    else:
        for x in range(140, width, 42):
            for y in range(110, height, 42):
                draw.ellipse((x, y, x + 3, y + 3), fill="#cbd9df")
    draw.line((112, 0, 112, height), fill="#e99797", width=3)
    for y in range(28, height, 72):
        draw.ellipse((15, y, 43, y + 28), fill="#363636")
        draw.arc((-15, y - 10, 35, y + 42), 250, 110, fill="#111111", width=6)

    draw.rounded_rectangle((840, 28, 970, 82), radius=22, fill=theme["accent"])
    slide_label = f"{slide_number} / {slide_count}"
    label_font = _font("arialbd.ttf", 25)
    label_x = 905 - draw.textlength(slide_label, font=label_font) / 2
    draw.text((label_x, 40), slide_label, font=label_font, fill="white")

    title_font = _font("Inkfree.ttf", 64)
    while draw.textlength(slide["title"], font=title_font) > 650 and title_font.size > 34:
        title_font = _font("Inkfree.ttf", title_font.size - 4)
    title_x = 160 + (650 - draw.textlength(slide["title"], font=title_font)) / 2
    draw.text((title_x, 35), slide["title"], font=title_font, fill=theme["accent"], stroke_width=1)
    draw.line((205, 125, 920, 125), fill=theme["accent"], width=5)
    subtitle_font = _font("comic.ttf", 25)
    subtitle_lines = _wrapped(draw, slide["subtitle"], subtitle_font, 730)[:2]
    for index, line in enumerate(subtitle_lines):
        line_x = 150 + (810 - draw.textlength(line, font=subtitle_font)) / 2
        draw.text((line_x, 145 + index * 32), line, font=subtitle_font, fill=INK)
    _star(draw, 153, 82, 23)

    heading_font = _font("segoeprb.ttf", 31)
    body_font = _font("comic.ttf", 25)
    example_font = _font("consola.ttf", 22)

    def card(box, heading, text, fill, accent, example=False, number=None):
        left, top, right, bottom = box
        draw.rounded_rectangle(box, radius=22, fill=fill, outline=accent, width=3)
        heading_x = left + 40
        if number is not None:
            draw.ellipse((left + 22, top + 20, left + 70, top + 68), fill=accent)
            nfont = _font("arialbd.ttf", 24)
            draw.text((left + 39, top + 29), str(number), font=nfont, fill="white")
            heading_x = left + 88
        hfont = heading_font
        max_heading = right - heading_x - 25
        while draw.textlength(heading, font=hfont) > max_heading and hfont.size > 23:
            hfont = _font("segoeprb.ttf", hfont.size - 2)
        draw.text((heading_x, top + 22), heading, font=hfont, fill=accent)
        draw.line((heading_x, top + 63, min(right - 25, heading_x + int(draw.textlength(heading, font=hfont))), top + 63), fill=accent, width=4)
        font = example_font if example else body_font
        lines = _wrapped(draw, text, font, right - left - 80)[:6]
        for line_index, line in enumerate(lines):
            draw.text((left + 40, top + 88 + line_index * 34), line, font=font, fill=INK)

    points = slide["points"]
    examples = slide["examples"]
    if design == "stacked":
        card((150, 225, 960, 480), points[0]["heading"], points[0]["explanation"], "#fffef8", theme["accent"])
        card((150, 505, 960, 760), points[1]["heading"], points[1]["explanation"], "#fffef8", theme["second"])
        card((150, 785, 960, 1085), examples[0]["label"], examples[0]["content"], theme["soft"], theme["accent"], True)
        card((150, 1110, 960, 1410), examples[1]["label"], examples[1]["content"], "#ffe9df", theme["second"], True)
    elif design == "split":
        draw.text((155, 215), "KEY IDEAS", font=_font("arialbd.ttf", 23), fill=theme["accent"])
        draw.text((570, 215), "SEE IT IN ACTION", font=_font("arialbd.ttf", 23), fill=theme["second"])
        card((145, 260, 535, 790), points[0]["heading"], points[0]["explanation"], "#fffef8", theme["accent"])
        card((145, 815, 535, 1345), points[1]["heading"], points[1]["explanation"], "#fffef8", theme["accent"])
        card((555, 260, 960, 790), examples[0]["label"], examples[0]["content"], theme["soft"], theme["second"], True)
        card((555, 815, 960, 1345), examples[1]["label"], examples[1]["content"], "#fff0d8", theme["second"], True)
    elif design == "example_first":
        draw.rounded_rectangle((145, 215, 960, 265), radius=18, fill=theme["second"])
        draw.text((375, 224), "START WITH EXAMPLES", font=_font("arialbd.ttf", 23), fill="white")
        card((150, 290, 960, 555), examples[0]["label"], examples[0]["content"], theme["soft"], theme["second"], True)
        card((150, 580, 960, 845), examples[1]["label"], examples[1]["content"], "#ffe9df", theme["second"], True)
        card((150, 875, 960, 1120), points[0]["heading"], points[0]["explanation"], "#fffef8", theme["accent"])
        card((150, 1145, 960, 1390), points[1]["heading"], points[1]["explanation"], "#fffef8", theme["accent"])
    elif design == "steps":
        draw.line((190, 270, 190, 1330), fill=theme["accent"], width=8)
        card((225, 225, 960, 470), points[0]["heading"], points[0]["explanation"], "#fffef8", theme["accent"], number=1)
        card((225, 495, 960, 740), examples[0]["label"], examples[0]["content"], theme["soft"], theme["second"], True, 2)
        card((225, 765, 960, 1010), points[1]["heading"], points[1]["explanation"], "#fffef8", theme["accent"], number=3)
        card((225, 1035, 960, 1325), examples[1]["label"], examples[1]["content"], "#ffe9df", theme["second"], True, 4)
    else:
        positions = [(145, 235, 535, 735), (565, 235, 955, 735), (145, 765, 535, 1345), (565, 765, 955, 1345)]
        card(positions[0], points[0]["heading"], points[0]["explanation"], "#fff6c7", theme["accent"])
        card(positions[1], examples[0]["label"], examples[0]["content"], "#e4f5e9", theme["second"], True)
        card(positions[2], examples[1]["label"], examples[1]["content"], "#f2e7ff", theme["second"], True)
        card(positions[3], points[1]["heading"], points[1]["explanation"], "#ffe7df", theme["accent"])

    footer_font = _font("Inkfree.ttf", 25)
    footer = _clean(course_title, 50)
    draw.text((155, 1493), footer, font=footer_font, fill=theme["accent"])
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG", optimize=True)


def create_carousel(
    topic: str, output_dir: Path, endpoint: str, model: str
) -> tuple[list[Path], str, str]:
    lesson = generate_carousel_lesson(topic, endpoint, model)
    paths = []
    for index, slide in enumerate(lesson["slides"], start=1):
        path = output_dir / f"slide-{index}.png"
        render_carousel_slide(
            lesson["title"], slide, index, len(lesson["slides"]), path
        )
        paths.append(path)
    lesson_text = "\n\n".join(
        [
            f"{index}. {slide['title']}\n"
            + "\n".join(
                f"{point['heading']}: {point['explanation']}"
                for point in slide["points"]
            )
            + "\n"
            + "\n".join(
                f"{example['label']}: {example['content']}"
                for example in slide["examples"]
            )
            for index, slide in enumerate(lesson["slides"], start=1)
        ]
    )
    return paths, lesson["caption"], lesson_text
