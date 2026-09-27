#!/usr/bin/env python3
"""Translate text-based PDF files while keeping the original page geometry."""

from __future__ import annotations

import argparse
import hashlib
import html
import http.client
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import fitz  # PyMuPDF


LOGGER = logging.getLogger("pdf-translator")
PROMPT_VERSION = "2026-09-26-v2-subsup"

SYSTEM_PROMPT = r"""You are a careful technical-paper translator.
Translate each supplied text block into the target language specified in the
user payload.

Rules:
1. Return exactly one translation for every input id. Keep ids unchanged.
2. Preserve equations, mathematical symbols, variable names, units, numbers,
   citation markers such as [1], URLs, email addresses, DOIs, file paths,
   command lines, and code-like identifiers. Do not invent or remove facts.
3. Translate headings, captions, table labels, body text, and footnotes
   naturally. Use concise academic Chinese rather than word-for-word English.
4. Do not add explanations, translator notes, quotation marks, or commentary.
5. A PDF text block can contain visual line breaks caused by the original
   layout. You may reflow those line breaks; the caller will lay the result out.
6. If a block is only an equation, identifier, URL, email, citation, or number,
   return it unchanged.
7. Text may contain the markers ⟦sup⟧…⟦/sup⟧ and ⟦sub⟧…⟦/sub⟧ around
   superscripts and subscripts of formulas. Keep every marker exactly as it is:
   same marker, same order, still wrapping the same symbol or letters. Never
   drop, add, move or "fix" them, and do not translate what is inside ⟦sup⟧ or
   ⟦sub⟧ unless it is a word that the surrounding text also needs translated.

Return JSON only in this shape:
{"translations":[{"id":"p1b0","text":"..."}]}
"""


class TranslationError(RuntimeError):
    pass


class TranslationCancelled(RuntimeError):
    """用户中途停止：已完成的批次都在缓存里，下次可接着跑。"""


@dataclass(frozen=True)
class TextBlock:
    page_index: int
    block_index: int
    rect: tuple[float, float, float, float]
    source_text: str
    font_size: float
    color: tuple[float, float, float]
    bold: bool
    align: int
    math_ratio: float = 0.0

    @property
    def block_id(self) -> str:
        return f"p{self.page_index + 1}b{self.block_index}"


def clean_extracted_text(text: str) -> str:
    text = text.replace("\u00ad", "").replace("\ufffd", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    return " ".join(line for line in lines if line).strip()


# 上下标判定：字号做门槛、基线定方向。实测 τ 抬升 +3.43、下缀 inflight 下沉 −2.92，
# 而 m_d 的 d 只有 −1.44（0.15×字号 会漏掉它，0.10 才收得住）。
SCRIPT_SIZE_RATIO = 0.85
SCRIPT_OFFSET_RATIO = 0.10
SUP_OPEN, SUP_CLOSE = "⟦sup⟧", "⟦/sup⟧"
SUB_OPEN, SUB_CLOSE = "⟦sub⟧", "⟦/sub⟧"
MARKER_RE = re.compile(r"⟦/?(?:sup|sub)⟧")


def is_script_span(span: dict[str, Any], base_size: float) -> bool:
    """上下标碎片：PyMuPDF 标了上标（flags 的 bit0），或字号明显小于正文。"""
    if int(span.get("flags", 0)) & 1:
        return True
    return float(span.get("size", base_size)) < base_size * SCRIPT_SIZE_RATIO


def dominant_font_size(spans: list[dict[str, Any]]) -> float:
    sizes = Counter(round(float(span.get("size", 10.0)) * 2) / 2 for span in spans)
    return sizes.most_common(1)[0][0] if sizes else 10.0


def dominant_baseline(chars: list[tuple[dict[str, Any], dict[str, Any]]], base_size: float) -> float:
    """该行的正文基线：正文号字符的 origin.y 均值。"""
    values = [
        float(char.get("origin", (0.0, 0.0))[1])
        for char, span in chars
        if abs(float(span.get("size", base_size)) - base_size) < 0.1
    ]
    return sum(values) / len(values) if values else 0.0


def script_verdict(char: dict[str, Any], span: dict[str, Any], base_size: float, base_baseline: float) -> str | None:
    """这个字符是上缀、下缀，还是正文。"""
    if float(span.get("size", base_size)) >= base_size * SCRIPT_SIZE_RATIO:
        return None
    offset = base_baseline - float(char.get("origin", (0.0, base_baseline))[1])
    if offset > SCRIPT_OFFSET_RATIO * base_size:
        return "sup"
    if offset < -SCRIPT_OFFSET_RATIO * base_size:
        return "sub"
    return None


def line_text(line: dict[str, Any], base_size: float) -> str:
    """一行的文字：span 之间按水平间距补空格，并按基线给上下标加标记。

    这些 PDF 里很多空格不是空格字符、只是位置间隔，直接拼接会把单词粘成
    "DisaggregatedLLMinference"；反过来公式里的上下标既不能被塞进空格，还得
    标出来，好让渲染层还原成真正的 <sub>/<sup>。
    """
    spans = [span for span in line.get("spans", []) if str(span.get("text", ""))]
    if not spans:
        return ""
    chars = [(char, span) for span in spans for char in span.get("chars", [])]
    base_baseline = dominant_baseline(chars, base_size)
    text = ""
    state: str | None = None
    previous: dict[str, Any] | None = None
    for span in spans:
        if previous is not None:
            gap = float(span["bbox"][0]) - float(previous["bbox"][2])
            size = min(float(span.get("size", base_size)), float(previous.get("size", base_size)))
            if (
                gap > 0.25 * size
                and not text.endswith(" ")
                and not str(span.get("text", "")).startswith(" ")
                and not is_script_span(span, base_size)
                and not is_script_span(previous, base_size)
            ):
                text += " "
        for char in span.get("chars", []):
            verdict = script_verdict(char, span, base_size, base_baseline)
            if verdict != state:
                if state is not None:
                    text += SUP_CLOSE if state == "sup" else SUB_CLOSE
                if verdict is not None:
                    text += SUP_OPEN if verdict == "sup" else SUB_OPEN
                state = verdict
            text += str(char.get("c", ""))
        previous = span
    if state is not None:
        text += SUP_CLOSE if state == "sup" else SUB_CLOSE
    return text


def block_source_text(lines: list[dict[str, Any]], base_size: float) -> str:
    """块内多"行"拼接：下一行开头是上下标碎片时直接接上，不插空格。

    公式被 Pdf 切成多个"行"（如 "1 + nτ" / "inflight(p)"）时会走到这里。
    """
    parts: list[str] = []
    for line in lines:
        spans = [span for span in line.get("spans", []) if str(span.get("text", ""))]
        if not spans:
            continue
        text = line_text(line, base_size)
        if parts and not is_script_span(spans[0], base_size):
            text = " " + text
        parts.append(text)
    return clean_extracted_text("".join(parts))


def is_translatable(text: str) -> bool:
    """Skip blocks that are clearly only punctuation, numbers, or symbols."""
    text = MARKER_RE.sub("", text)  # 上下标标记不算内容
    if len(text.strip()) < 2 or not re.search(r"[A-Za-z\u3400-\u9fff]", text):
        return False
    if re.search(r"\b(?:Abstract|Introduction|Background|Conclusion|Table|Figure|Fig\.|Section|Appendix|References)\b", text, re.I):
        return True
    word_count = len(re.findall(r"[A-Za-z]{2,}", text))
    if word_count >= 2:
        return True
    # A short identifier/equation such as T = s / B_eff should stay untouched.
    if re.search(r"[=/_^]", text) or re.fullmatch(r"[\w\s./:@?&=#%+\-(),\[\]{}<>]+", text):
        return False
    return word_count > 0


def is_block_translatable(block: TextBlock) -> bool:
    x0, y0, x1, y1 = block.rect
    width, height = x1 - x0, y1 - y0
    if width < 35 and height > width * 3:
        return False
    if re.search(r"\barXiv:", block.source_text, re.I):
        return False
    return is_translatable(block.source_text)


def color_from_int(value: int) -> tuple[float, float, float]:
    return ((value >> 16 & 255) / 255.0, (value >> 8 & 255) / 255.0, (value & 255) / 255.0)


def block_alignment(rect: fitz.Rect, page_rect: fitz.Rect) -> int:
    page_center = (page_rect.x0 + page_rect.x1) / 2
    center = (rect.x0 + rect.x1) / 2
    if rect.width / max(page_rect.width, 1) >= 0.45 and abs(center - page_center) <= page_rect.width * 0.08:
        return 1
    if rect.x0 > page_rect.width * 0.55 and rect.width < page_rect.width * 0.35:
        return 2
    return 0


def iter_text_blocks(doc: fitz.Document, selected_pages: set[int] | None = None) -> Iterator[TextBlock]:
    for page_index, page in enumerate(doc):
        if selected_pages is not None and page_index not in selected_pages:
            continue
        # rawdict 才带每个字符的 origin（基线点），判上下缀要用它。
        page_dict = page.get_text("rawdict", flags=fitz.TEXTFLAGS_TEXT)
        for block_index, block in enumerate(page_dict.get("blocks", [])):
            if block.get("type") != 0:
                continue
            lines = block.get("lines", [])
            for line in lines:
                for span in line.get("spans", []):
                    # rawdict 的 span 不带 text，按字符补出来，后面逻辑照旧。
                    span["text"] = "".join(char.get("c", "") for char in span.get("chars", []))
            spans = [span for line in lines for span in line.get("spans", []) if span.get("text", "").strip()]
            source_text = block_source_text(lines, dominant_font_size(spans))
            if not source_text:
                continue
            rect = fitz.Rect(block["bbox"])
            sizes = [float(span.get("size", 9.0)) for span in spans]
            font_size = max(4.0, min(36.0, sum(sizes) / len(sizes))) if sizes else 9.0
            colors = [color_from_int(int(span.get("color", 0))) for span in spans]
            yield TextBlock(
                page_index=page_index,
                block_index=block_index,
                rect=(rect.x0, rect.y0, rect.x1, rect.y1),
                source_text=source_text,
                font_size=font_size,
                color=colors[0] if colors else (0.0, 0.0, 0.0),
                bold=any("bold" in str(span.get("font", "")).lower() for span in spans),
                align=block_alignment(rect, page.rect),
                math_ratio=math_char_ratio(spans),
            )


TABLE_CAPTION_RE = re.compile(r"^\s*(?:TABLE|Table)\s+[IVXLC0-9]+\s*[:.]")
MATH_FONT_MARKERS = (
    "CMMI",
    "CMSY",
    "CMEX",
    "CMSL",
    "CMBX",
    "MSAM",
    "EUSM",
    "EUFM",
    "Math",
    "Symbol",
    "MTMI",
    "MTSY",
    "STIX",
    "XITS",
)
MATH_SYMBOL_RE = re.compile(r"[=+\-−×·÷^_∑∫√≤≥≠≈∈∀∃∂⟨⟩⊗→←]")
RULE_MIN_WIDTH = 20.0
RULE_MAX_HEIGHT = 3.0
TABLE_MIN_WIDTH = 100.0
RULE_GAP = 120.0
RULE_OVERLAP = 0.6
MATH_CLUSTER_GAP = 14.0
REGION_MARGIN = 3.0
REGION_COVERAGE = 0.5


@dataclass(frozen=True)
class PreservedRegion:
    """一块保持原始排版的页面区域：截图贴回，而不是重排译文。"""

    page_index: int
    rect: tuple[float, float, float, float]
    kind: str  # "table" | "formula"
    label: str

    def as_rect(self) -> fitz.Rect:
        return fitz.Rect(self.rect)


def math_char_ratio(spans: list[dict[str, Any]]) -> float:
    """数学字体（Computer Modern 系列等）字符占该块的比例。"""
    total = 0
    math_chars = 0
    for span in spans:
        text = str(span.get("text", "")).strip()
        if not text:
            continue
        total += len(text)
        font = str(span.get("font", ""))
        if any(marker in font for marker in MATH_FONT_MARKERS):
            math_chars += len(text)
    return math_chars / total if total else 0.0


def is_math_block(block: TextBlock) -> bool:
    """判断一个块是不是（基本）只由公式构成。散文段落一律排除。"""
    text = MARKER_RE.sub("", block.source_text)  # 标记里的 sub/sup 字母不算内容
    words = re.findall(r"[A-Za-z]{2,}", text)
    if len(text) > 160 or len(words) >= 8:
        return False
    if block.math_ratio >= 0.3:
        return True
    return len(words) <= 2 and len(text) <= 60 and block.math_ratio >= 0.1 and bool(MATH_SYMBOL_RE.search(text))


def horizontal_rules(page: fitz.Page) -> list[fitz.Rect]:
    """表格的水平线（booktabs 的 toprule/midrule/bottomrule 等）。"""
    rules: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing["rect"])
        if rect.height <= RULE_MAX_HEIGHT and rect.width >= RULE_MIN_WIDTH:
            rules.append(rect)
    return sorted(rules, key=lambda rect: (rect.y0, rect.x0))


def horizontal_overlap_ratio(a: fitz.Rect, b: fitz.Rect) -> float:
    narrower = min(a.width, b.width)
    if narrower <= 0:
        return 0.0
    return max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0)) / narrower


def covered_ratio(block: TextBlock, rect: fitz.Rect) -> float:
    block_rect = fitz.Rect(block.rect)
    area = block_rect.get_area()
    if area <= 0:
        return 0.0
    return (block_rect & rect).get_area() / area


def union_rect(rects: list[fitz.Rect]) -> fitz.Rect:
    """数值求并。表线矩形高度为 0，PyMuPDF 的 |= 会把这种空矩形当无效值忽略掉。"""
    return fitz.Rect(
        min(rect.x0 for rect in rects),
        min(rect.y0 for rect in rects),
        max(rect.x1 for rect in rects),
        max(rect.y1 for rect in rects),
    )


def rects_intersect(a: fitz.Rect, b: fitz.Rect) -> bool:
    return a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1


def caption_between(upper: fitz.Rect, lower: fitz.Rect, captions: list[TextBlock]) -> bool:
    """两条表线之间夹着表格标题，说明那里是另一张表，不能并成一组。"""
    for caption in captions:
        rect = fitz.Rect(caption.rect)
        if rect.y0 >= upper.y1 - 1.0 and rect.y1 <= lower.y0 + 1.0 and horizontal_overlap_ratio(rect, lower) >= 0.3:
            return True
    return False


def cluster_rules(rules: list[fitz.Rect], captions: list[TextBlock]) -> list[list[fitz.Rect]]:
    """把同一张表的水平线归到一组：列区间对齐、行距不过大、中间没有新表标题。"""
    groups: list[list[fitz.Rect]] = []
    for rule in rules:
        for group in groups:
            last = group[-1]
            if (
                horizontal_overlap_ratio(last, rule) >= RULE_OVERLAP
                and rule.y0 - last.y1 <= RULE_GAP
                and not caption_between(last, rule, captions)
            ):
                group.append(rule)
                break
        else:
            groups.append([rule])
    return [group for group in groups if len(group) >= 2]


def cluster_math_blocks(blocks: list[TextBlock]) -> list[list[TextBlock]]:
    """把被 PDF 切碎的同一个公式合并成一组。"""
    groups: list[list[TextBlock]] = []
    for block in sorted(blocks, key=lambda item: (item.rect[1], item.rect[0])):
        rect = fitz.Rect(block.rect)
        for group in groups:
            last = fitz.Rect(group[-1].rect)
            if -1.0 <= rect.y0 - last.y1 <= MATH_CLUSTER_GAP and horizontal_overlap_ratio(last, rect) >= 0.5:
                group.append(block)
                break
        else:
            groups.append([block])
    return groups


def is_table_body_block(block: TextBlock, rect: fitz.Rect, rules: fitz.Rect) -> bool:
    """表体单元格，或紧贴在末条表线下方、同一横向范围内的短表注。"""
    if TABLE_CAPTION_RE.match(block.source_text):
        return False
    if covered_ratio(block, rect) >= REGION_COVERAGE:
        return True
    return (
        len(block.source_text) <= 90
        and rect.y1 - 1.0 <= block.rect[1] <= rect.y1 + 6.0
        and block.rect[0] >= rules.x0 - 6.0
        and block.rect[2] <= rules.x1 + 6.0
    )


def table_caption_label(page_blocks: list[TextBlock], rect: fitz.Rect) -> str | None:
    for block in page_blocks:
        if not TABLE_CAPTION_RE.match(block.source_text):
            continue
        block_rect = fitz.Rect(block.rect)
        if block_rect.y1 <= rect.y0 + 1.0 and rect.y0 - block_rect.y1 <= 70.0:
            if horizontal_overlap_ratio(block_rect, rect) >= 0.5:
                return block.source_text[:24]
    return None


def detect_table_regions(page: fitz.Page, page_index: int, page_blocks: list[TextBlock]) -> list[PreservedRegion]:
    captions = [block for block in page_blocks if TABLE_CAPTION_RE.match(block.source_text)]
    regions: list[PreservedRegion] = []
    for group in cluster_rules(horizontal_rules(page), captions):
        rules = union_rect(group)
        if rules.height <= 1.0 or rules.width < TABLE_MIN_WIDTH:
            # 又矮又窄的一两条“表线”通常是公式里的分式线，不是表格。
            continue
        rect = fitz.Rect(rules)
        for _ in range(6):
            grown = fitz.Rect(rect)
            for block in page_blocks:
                if is_table_body_block(block, rect, rules):
                    grown = union_rect([grown, fitz.Rect(block.rect)])
            if grown == rect:
                break
            rect = grown
        rect = fitz.Rect(rect.x0 - REGION_MARGIN, rect.y0, rect.x1 + REGION_MARGIN, rect.y1 + REGION_MARGIN)
        if not any(covered_ratio(block, rect) >= REGION_COVERAGE for block in page_blocks):
            # 只有几条短线、里面没有任何文字：多半是公式里的分式线，不是表格。
            continue
        label = table_caption_label(page_blocks, rect) or f"p{page_index + 1} 表格"
        regions.append(PreservedRegion(page_index, (rect.x0, rect.y0, rect.x1, rect.y1), "table", label))
    return regions


def detect_formula_regions(page_index: int, page_blocks: list[TextBlock], skip: set[str]) -> list[PreservedRegion]:
    candidates = [block for block in page_blocks if block.block_id not in skip]
    regions: list[PreservedRegion] = []
    for group in cluster_math_blocks([block for block in candidates if is_math_block(block)]):
        group_ids = {block.block_id for block in group}
        rect = union_rect([fitz.Rect(block.rect) for block in group])
        rect = fitz.Rect(rect.x0 - 2, rect.y0 - 2, rect.x1 + 2, rect.y1 + 2)
        touched = fitz.Rect(rect.x0 - 3, rect.y0 - 3, rect.x1 + 3, rect.y1 + 3)
        for block in candidates:
            if block.block_id in group_ids or is_block_translatable(block) or len(block.source_text) > 30:
                continue
            if rects_intersect(fitz.Rect(block.rect), touched):
                rect = union_rect([rect, fitz.Rect(block.rect)])
        # 被公式字体切成多块的段落，各行 bbox 会互相重叠：整块并进来，别只盖半行。
        for _ in range(3):
            grown = fitz.Rect(rect)
            for block in candidates:
                if block.block_id not in group_ids and covered_ratio(block, rect) >= 0.3:
                    grown = union_rect([grown, fitz.Rect(block.rect)])
            if grown == rect:
                break
            rect = grown
        label = f"公式 {group[0].block_id}"
        regions.append(PreservedRegion(page_index, (rect.x0, rect.y0, rect.x1, rect.y1), "formula", label))
    return regions


def merge_regions(regions: list[PreservedRegion]) -> list[PreservedRegion]:
    """合并相交区域。并集可能又碰到别的区域，所以迭代到不再变化为止。"""
    merged = list(regions)
    while True:
        result: list[PreservedRegion] = []
        changed = False
        for region in merged:
            for index, existing in enumerate(result):
                if existing.page_index != region.page_index:
                    continue
                if not rects_intersect(fitz.Rect(existing.rect), fitz.Rect(region.rect)):
                    continue
                kind = "table" if "table" in (existing.kind, region.kind) else "formula"
                rect = union_rect([fitz.Rect(existing.rect), fitz.Rect(region.rect)])
                label = existing.label if existing.kind == kind else region.label
                result[index] = PreservedRegion(region.page_index, (rect.x0, rect.y0, rect.x1, rect.y1), kind, label)
                changed = True
                break
            else:
                result.append(region)
        merged = result
        if not changed:
            return merged


def detect_preserved_regions(doc: fitz.Document, blocks: list[TextBlock]) -> list[PreservedRegion]:
    by_page: dict[int, list[TextBlock]] = {}
    for block in blocks:
        by_page.setdefault(block.page_index, []).append(block)
    regions: list[PreservedRegion] = []
    for page_index, page_blocks in sorted(by_page.items()):
        page_regions = detect_table_regions(doc[page_index], page_index, page_blocks)
        skip = preserved_block_ids(page_blocks, page_regions)
        page_regions.extend(detect_formula_regions(page_index, page_blocks, skip))
        regions.extend(merge_regions(page_regions))
    return regions


def region_for_block(block: TextBlock, regions: list[PreservedRegion]) -> PreservedRegion | None:
    """块落在哪个保留区域里。表格标题不算，它要照常翻译。"""
    if TABLE_CAPTION_RE.match(block.source_text):
        return None
    for region in regions:
        if region.page_index == block.page_index and covered_ratio(block, fitz.Rect(region.rect)) >= REGION_COVERAGE:
            return region
    return None


def preserved_block_ids(blocks: list[TextBlock], regions: list[PreservedRegion]) -> set[str]:
    if not regions:
        return set()
    return {block.block_id for block in blocks if region_for_block(block, regions) is not None}


def describe_regions(regions: list[PreservedRegion], blocks: list[TextBlock]) -> list[str]:
    lines: list[str] = []
    for region in regions:
        covered = sum(1 for block in blocks if region_for_block(block, [region]) is not None)
        box = ", ".join(f"{value:.0f}" for value in region.rect)
        lines.append(f"第 {region.page_index + 1} 页 {region.kind} {region.label} [{box}] 覆盖 {covered} 个文本块")
    return lines


def parse_page_spec(spec: str | None, page_count: int) -> set[int] | None:
    if not spec:
        return None
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(part)
        if start < 1 or end < start or end > page_count:
            raise ValueError(f"页码范围无效: {part}，PDF 共 {page_count} 页")
        pages.update(range(start - 1, end))
    return pages


def choose_font(font_override: str | None, bold: bool = False) -> str:
    candidates: list[Path] = []
    if font_override:
        candidates.append(Path(font_override))
    windows = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if bold:
        candidates.extend([windows / "simhei.ttf", windows / "Dengb.ttf", windows / "simsunb.ttf"])
    candidates.extend([windows / "simsun.ttf", windows / "simfang.ttf", windows / "Deng.ttf", windows / "NotoSansSC-VF.ttf"])
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("没有找到中文字体，请使用 --font 指定字体文件")


class JsonCache:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data = loaded
            except (OSError, json.JSONDecodeError) as exc:
                LOGGER.warning("缓存读取失败，将重新建立缓存: %s (%s)", path, exc)

    def get(self, key: str) -> str | None:
        value = self.data.get(key)
        return value if isinstance(value, str) else None

    def put(self, key: str, value: str) -> None:
        self.data[key] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.path)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_json_response(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.I)
        content = re.sub(r"\s*```$", "", content)
    start, end = content.find("{"), content.rfind("}")
    if start >= 0 and end > start:
        content = content[start : end + 1]
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("JSON object expected", content, 0)
    return parsed


def collect_translations(translations: list[Any], expected_ids: set[str]) -> dict[str, str]:
    """从接口返回的 translations 数组里挑出我们认识的 id。"""
    collected: dict[str, str] = {}
    for item in translations:
        if not isinstance(item, dict):
            continue
        item_id, text = str(item.get("id", "")), item.get("text")
        if item_id in expected_ids and isinstance(text, str):
            collected[item_id] = text.strip()
    return collected


class OpenAICompatibleTranslator:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: int,
        max_retries: int,
        target_language: str,
        mock: bool = False,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.target_language = target_language
        self.mock = mock

    def translate_batch(self, items: list[dict[str, str]]) -> dict[str, str]:
        if self.mock:
            return {item["id"]: f"【模拟译文】{item['text']}" for item in items}
        if not self.api_key:
            raise TranslationError(
                "没有设置翻译 API 密钥，请设置 TRANSLATE_API_KEY 或 OPENAI_API_KEY；"
                "使用本地兼容服务时可设置任意非空值。"
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"target_language": self.target_language, "items": items},
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            parsed = parse_json_response(self._request(payload))
        except json.JSONDecodeError:
            # Some compatible gateways reject response_format or wrap JSON in
            # Markdown. A second request without response_format handles both.
            payload.pop("response_format", None)
            parsed = parse_json_response(self._request(payload))
        except TranslationError as exc:
            if "HTTP 400" not in str(exc):
                raise
            payload.pop("response_format", None)
            parsed = parse_json_response(self._request(payload))
        translations = parsed.get("translations")
        if not isinstance(translations, list):
            raise TranslationError("翻译接口返回的 JSON 缺少 translations 数组")
        expected_ids = {item["id"] for item in items}
        result = collect_translations(translations, expected_ids)
        missing = expected_ids - result.keys()
        if missing:
            # 模型偶尔会漏一两个 id，只补请求漏掉的那几个，别让整个文件失败。
            LOGGER.warning("翻译接口漏返回 %d 个文本块，单独补一次: %s", len(missing), ", ".join(sorted(missing)))
            payload["messages"][-1] = {
                "role": "user",
                "content": json.dumps(
                    {"target_language": self.target_language, "items": [item for item in items if item["id"] in missing]},
                    ensure_ascii=False,
                ),
            }
            retry = parse_json_response(self._request(payload)).get("translations")
            if isinstance(retry, list):
                result.update(collect_translations(retry, expected_ids))
            missing = expected_ids - result.keys()
            if missing:
                raise TranslationError(f"翻译接口漏返回文本块: {', '.join(sorted(missing))}")
        return result

    def _request(self, payload: dict[str, Any]) -> str:
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "layout-pdf-translator/1.0",
            },
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                if not isinstance(content, str) or not content.strip():
                    raise TranslationError("翻译接口返回空内容")
                return content
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = TranslationError(f"HTTP {exc.code}: {body[:500]}")
                retryable = exc.code == 429 or exc.code >= 500
                if not retryable or attempt >= self.max_retries:
                    break
            except (OSError, http.client.HTTPException, json.JSONDecodeError, KeyError, IndexError) as exc:
                # A connection that is dropped or truncated mid-request raises
                # RemoteDisconnected/IncompleteRead rather than HTTPError; those
                # are as transient as a timeout and must back off and retry.
                last_error = exc
                if attempt >= self.max_retries:
                    break
            delay = min(30.0, 2.0**attempt)
            LOGGER.warning("翻译请求失败，%.1f 秒后重试 (%d/%d): %s", delay, attempt + 1, self.max_retries, last_error)
            time.sleep(delay)
        raise TranslationError(f"翻译请求失败: {last_error}")


def batch_items(items: list[dict[str, str]], max_chars: int) -> Iterator[list[dict[str, str]]]:
    batch: list[dict[str, str]] = []
    size = 0
    for item in items:
        item_size = len(item["text"]) + len(item["id"]) + 32
        if batch and size + item_size > max_chars:
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += item_size
    if batch:
        yield batch


def check_markers(source: str, translated: str, block_id: str) -> str:
    """模型必须把上下标标记原样带回来；数量不对就退回纯文本，别把排版搞坏。"""
    if not MARKER_RE.search(source):
        return MARKER_RE.sub("", translated)
    counts = [
        (token, source.count(token), translated.count(token))
        for token in (SUP_OPEN, SUP_CLOSE, SUB_OPEN, SUB_CLOSE)
    ]
    if all(want == got for _token, want, got in counts):
        return translated
    LOGGER.warning(
        "文本块 %s 的上下标标记没被完整保留（%s），该块按纯文本写入",
        block_id,
        ", ".join(f"{token}:{want}->{got}" for token, want, got in counts if want != got),
    )
    return MARKER_RE.sub("", translated)


def prepare_translations(
    blocks: list[TextBlock],
    cache: JsonCache,
    translator: OpenAICompatibleTranslator,
    *,
    target_language: str,
    max_batch_chars: int,
    force: bool,
    preserved: set[str],
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, str]:
    translated: dict[str, str] = {}
    pending: list[dict[str, str]] = []
    for block in blocks:
        # 表格与公式区域保持原样，稍后由截图贴回，不走翻译接口。
        if block.block_id in preserved:
            continue
        if not is_block_translatable(block):
            translated[block.block_id] = block.source_text
            continue
        key = sha256_text("|".join((PROMPT_VERSION, target_language, translator.model, block.source_text)))
        if not force:
            cached = cache.get(key)
            if cached is not None:
                translated[block.block_id] = cached
                continue
        pending.append({"id": block.block_id, "text": block.source_text, "_cache_key": key})

    if pending:
        batches = list(batch_items(pending, max_batch_chars))
        LOGGER.info("需要翻译 %d 个文本块，共 %d 个请求批次", len(pending), len(batches))
        for index, batch in enumerate(batches, start=1):
            if should_stop is not None and should_stop():
                raise TranslationCancelled(f"已停止：完成 {index - 1}/{len(batches)} 个批次，已翻译的部分留在缓存里")
            LOGGER.info("翻译批次 %d: %d 个文本块", index, len(batch))
            result = translator.translate_batch([{"id": x["id"], "text": x["text"]} for x in batch])
            for item in batch:
                value = check_markers(item["text"], result[item["id"]], item["id"])
                translated[item["id"]] = value
                cache.put(item["_cache_key"], value)
            cache.save()
    return translated


TEXT_ALIGN_NAMES = {0: "left", 1: "center", 2: "right"}
FONT_ARCHIVES: dict[str, fitz.Archive] = {}
# MuPDF 自带的回退字体没有 U+02C6（数学里的帽子 ˆ），逐字回退时会写出空框。
# 这类字符换成人人都有的等价字形；译文缓存里保留原字符，只影响打印效果。
RENDER_SUBSTITUTIONS = str.maketrans({"ˆ": "^"})


def font_archive(fontfile: str) -> fitz.Archive:
    """@font-face 的 src 只能相对 Archive 解析，按目录缓存一个即可。"""
    directory = str(Path(fontfile).parent)
    if directory not in FONT_ARCHIVES:
        FONT_ARCHIVES[directory] = fitz.Archive(directory)
    return FONT_ARCHIVES[directory]


def html_css(fontfile: str, size: float, color: tuple[float, float, float], align: int) -> str:
    red, green, blue = (max(0, min(255, round(value * 255))) for value in color)
    return (
        f"* {{font-family:'cjkfont'; font-size:{size:.2f}px; line-height:1.12;"
        f" text-align:{TEXT_ALIGN_NAMES.get(align, 'left')}; color:#{red:02x}{green:02x}{blue:02x};}}"
        f" sub {{font-size:0.70em; vertical-align:sub;}}"
        f" sup {{font-size:0.70em; vertical-align:super;}}"
        f" @font-face {{font-family:'cjkfont'; src:url('{Path(fontfile).name}');}}"
    )


def to_html(text: str) -> str:
    """转义后把上下标标记换成真标签 —— 顺序不能反，否则标签本身会被转义成文本。"""
    escaped = html.escape(text.translate(RENDER_SUBSTITUTIONS))
    return (
        escaped.replace(SUB_OPEN, "<sub>")
        .replace(SUB_CLOSE, "</sub>")
        .replace(SUP_OPEN, "<sup>")
        .replace(SUP_CLOSE, "</sup>")
    )


def insert_html(
    page: fitz.Page,
    target: fitz.Rect,
    text: str,
    *,
    fontfile: str,
    size: float,
    color: tuple[float, float, float],
    align: int,
    scale_low: float,
) -> tuple[float, float]:
    return page.insert_htmlbox(
        target,
        to_html(text),
        css=html_css(fontfile, size, color, align),
        archive=font_archive(fontfile),
        scale_low=scale_low,
    )


def insert_fitted_text(
    page: fitz.Page,
    rect: fitz.Rect,
    text: str,
    *,
    fontfile: str,
    font_size: float,
    color: tuple[float, float, float],
    align: int,
) -> float:
    """Shrink translated text until it fits inside its original text block.

    这里走 HTML 渲染而不是 insert_textbox：中文字体普遍没有 ℓ、∆、∗ 这类
    数学符号，insert_textbox 只会写出缺字形空框，而 MuPDF 的 HTML 引擎会
    逐字回退到自带字体，符号和中文字都留得住。
    """
    inset = min(1.2, max(0.2, font_size * 0.06))
    target = fitz.Rect(rect.x0 + inset, rect.y0 + inset, rect.x1 - inset, rect.y1 - inset)
    if target.width <= 2 or target.height <= 2:
        target = fitz.Rect(rect)
    size = max(4.0, font_size * 0.96)
    spare, scale = insert_html(
        page, target, text, fontfile=fontfile, size=size, color=color, align=align, scale_low=4.0 / size
    )
    if spare >= 0:
        return size * scale
    LOGGER.warning("文本块无法完全装入原区域，将以 4pt 字号尽量写入: %s", text[:80])
    insert_html(page, target, text, fontfile=fontfile, size=4.0, color=color, align=align, scale_low=0.1)
    return 4.0


def render_translated_pdf(
    source_path: Path,
    output_path: Path,
    blocks: list[TextBlock],
    translations: dict[str, str],
    *,
    font_override: str | None,
    regions: list[PreservedRegion],
    preserved: set[str],
    region_dpi: int,
) -> dict[str, Any]:
    doc = fitz.open(source_path)
    try:
        regions_by_page: dict[int, list[PreservedRegion]] = {}
        for region in regions:
            regions_by_page.setdefault(region.page_index, []).append(region)
        by_page: dict[int, list[TextBlock]] = {}
        preserved_by_page: dict[int, list[TextBlock]] = {}
        for block in blocks:
            if block.block_id in preserved:
                # 区域内的原文会被截图盖回原样，但要先清掉文字层。
                preserved_by_page.setdefault(block.page_index, []).append(block)
            elif is_block_translatable(block):
                # Keep formulas, identifiers, URLs, and symbol-only blocks exactly
                # as authored in the PDF, including their original font styling.
                by_page.setdefault(block.page_index, []).append(block)
        inserted = 0
        min_font_size = 100.0
        for page_index in sorted(set(by_page) | set(preserved_by_page) | set(regions_by_page)):
            page = doc[page_index]
            # 截图必须在遮罩之前取，否则取到的是空白。
            pixmaps = [
                (region.as_rect(), page.get_pixmap(clip=region.as_rect(), dpi=region_dpi))
                for region in regions_by_page.get(page_index, [])
            ]
            page_blocks_for_page = by_page.get(page_index, [])
            for block in preserved_by_page.get(page_index, []) + page_blocks_for_page:
                page.add_redact_annot(fitz.Rect(block.rect), fill=(1, 1, 1))
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
            for rect, pixmap in pixmaps:
                page.insert_image(rect, pixmap=pixmap)
            for block in page_blocks_for_page:
                fontfile = choose_font(font_override, block.bold)
                size = insert_fitted_text(
                    page,
                    fitz.Rect(block.rect),
                    translations.get(block.block_id, block.source_text),
                    fontfile=fontfile,
                    font_size=block.font_size,
                    color=block.color,
                    align=block.align,
                )
                min_font_size = min(min_font_size, size)
                inserted += 1
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        doc.save(temp_path, garbage=4, deflate=True, clean=True)
        doc.close()
        temp_path.replace(output_path)
        return {"blocks": inserted, "regions": len(regions), "min_font_size": min_font_size}
    finally:
        if not doc.is_closed:
            doc.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量翻译 PDF 文本并尽量保持原页面排版。原 PDF 不会被覆盖。")
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).resolve().parent, help="PDF 所在目录，默认是脚本所在目录")
    parser.add_argument("--output-dir", type=Path, default=None, help="输出目录，默认为 source-dir/output")
    parser.add_argument("--input", type=Path, default=None, help="只处理指定的一个 PDF")
    parser.add_argument("--target-language", default="Simplified Chinese", help="目标语言，默认简体中文")
    parser.add_argument("--pages", default=None, help="只处理页码，例如 1-3,5")
    parser.add_argument("--font", default=None, help="中文字体文件路径；默认自动寻找 Windows 中文字体")
    parser.add_argument("--model", default=os.getenv("TRANSLATE_MODEL", "gpt-4o-mini"), help="模型名，也可用 TRANSLATE_MODEL")
    parser.add_argument("--base-url", default=os.getenv("TRANSLATE_BASE_URL", "https://api.openai.com/v1"), help="OpenAI 兼容 API 根地址")
    parser.add_argument("--api-key", default=os.getenv("TRANSLATE_API_KEY", os.getenv("OPENAI_API_KEY", "")), help="API 密钥环境变量名见 README")
    parser.add_argument("--timeout", type=int, default=180, help="单次 API 请求超时秒数")
    parser.add_argument("--max-retries", type=int, default=4, help="请求失败重试次数")
    parser.add_argument("--max-batch-chars", type=int, default=8000, help="单个翻译批次的源文本字符上限")
    parser.add_argument("--region-dpi", type=int, default=200, help="表格/公式截图的分辨率，默认 200")
    parser.add_argument("--no-screenshot", action="store_true", help="关闭表格/公式截图，退回逐块替换文本")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有的译文 PDF；缓存仍然保留")
    parser.add_argument("--force", action="store_true", help="忽略已有翻译缓存，重新请求翻译")
    parser.add_argument("--dry-run", action="store_true", help="只检查 PDF 文本块，不请求 API、不生成 PDF")
    parser.add_argument("--mock", action="store_true", help="使用模拟译文测试流程，不请求 API")
    parser.add_argument("--verbose", action="store_true", help="输出更详细的日志")
    return parser


def discover_inputs(source_dir: Path, output_dir: Path, input_path: Path | None) -> list[Path]:
    if input_path:
        path = input_path if input_path.is_absolute() else source_dir / input_path
        if not path.is_file() or path.suffix.lower() != ".pdf":
            raise FileNotFoundError(f"找不到 PDF: {path}")
        return [path]
    return sorted(
        path
        for path in source_dir.rglob("*.pdf")
        if output_dir not in path.parents and not path.name.endswith(".zh-CN.pdf")
    )


@dataclass(frozen=True)
class TranslationSettings:
    """一次翻译任务的全部参数，命令行与图形界面共用。"""

    target_language: str = "Simplified Chinese"
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    timeout: int = 180
    max_retries: int = 4
    max_batch_chars: int = 8000
    font: str | None = None
    region_dpi: int = 200
    screenshot: bool = True
    pages: str | None = None
    overwrite: bool = False
    force: bool = False
    dry_run: bool = False
    mock: bool = False
    cache_dir: Path | None = None


def settings_from_args(args: argparse.Namespace) -> TranslationSettings:
    return TranslationSettings(
        target_language=args.target_language,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        timeout=args.timeout,
        max_retries=args.max_retries,
        max_batch_chars=args.max_batch_chars,
        font=args.font,
        region_dpi=args.region_dpi,
        screenshot=not args.no_screenshot,
        pages=args.pages,
        overwrite=args.overwrite,
        force=args.force,
        dry_run=args.dry_run,
        mock=args.mock,
    )


def translate_file(
    source_path: Path,
    output_dir: Path,
    settings: TranslationSettings,
    *,
    translator: OpenAICompatibleTranslator | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """翻译一个 PDF，译文写到 output_dir/<原名>.zh-CN.pdf。

    返回 status: "done" | "skipped" | "empty" | "dry-run"。
    should_stop 在批次之间被检查，返回真就抛 TranslationCancelled，
    已完成的批次留在缓存里，下次接着跑。
    """
    if translator is None:
        translator = OpenAICompatibleTranslator(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            timeout=settings.timeout,
            max_retries=settings.max_retries,
            target_language=settings.target_language,
            mock=settings.mock,
        )
    LOGGER.info("处理: %s", source_path)
    doc = fitz.open(source_path)
    try:
        selected_pages = parse_page_spec(settings.pages, len(doc))
        blocks = list(iter_text_blocks(doc, selected_pages))
        if not blocks:
            LOGGER.warning("没有找到可提取的文本，可能是扫描版 PDF: %s", source_path.name)
            return {"status": "empty", "output": None}
        regions: list[PreservedRegion] = []
        if settings.screenshot:
            regions = detect_preserved_regions(doc, blocks)
        preserved = preserved_block_ids(blocks, regions)
        count = sum(1 for block in blocks if block.block_id not in preserved and is_block_translatable(block))
        chars = sum(len(block.source_text) for block in blocks if block.block_id not in preserved)
        LOGGER.info("页数=%d，文本块=%d，需要翻译=%d，字符数=%d", len(doc), len(blocks), count, chars)
        if regions:
            tables = sum(1 for region in regions if region.kind == "table")
            LOGGER.info("保持原样的区域=%d（表格 %d，公式 %d），将以 %d dpi 截图贴回", len(regions), tables, len(regions) - tables, settings.region_dpi)
            for line in describe_regions(regions, blocks):
                LOGGER.info("  %s", line)
        if settings.dry_run:
            return {"status": "dry-run", "output": None}
    finally:
        doc.close()

    output_path = output_dir / f"{source_path.stem}.zh-CN.pdf"
    if output_path.exists() and not settings.overwrite:
        LOGGER.info("输出已存在，跳过（使用 --overwrite 覆盖）: %s", output_path)
        return {"status": "skipped", "output": output_path}
    cache = JsonCache((settings.cache_dir or output_dir / ".cache") / f"{source_path.stem}.json")
    translations = prepare_translations(
        blocks,
        cache,
        translator,
        target_language=settings.target_language,
        max_batch_chars=settings.max_batch_chars,
        force=settings.force,
        preserved=preserved,
        should_stop=should_stop,
    )
    result = render_translated_pdf(
        source_path,
        output_path,
        blocks,
        translations,
        font_override=settings.font,
        regions=regions,
        preserved=preserved,
        region_dpi=settings.region_dpi,
    )
    LOGGER.info(
        "已生成: %s（替换 %d 个文本块，截图 %d 处，最小字号 %.1f）",
        output_path,
        result["blocks"],
        result["regions"],
        result["min_font_size"],
    )
    return {"status": "done", "output": output_path, **result}


def process_one(
    source_path: Path,
    output_dir: Path,
    args: argparse.Namespace,
    translator: OpenAICompatibleTranslator,
) -> None:
    translate_file(source_path, output_dir, settings_from_args(args), translator=translator)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    source_dir = args.source_dir.resolve()
    output_dir = (args.output_dir or source_dir / "output").resolve()
    if not source_dir.is_dir():
        LOGGER.error("输入目录不存在: %s", source_dir)
        return 2
    try:
        inputs = discover_inputs(source_dir, output_dir, args.input)
        if not inputs:
            LOGGER.error("输入目录中没有找到 PDF: %s", source_dir)
            return 2
        translator = OpenAICompatibleTranslator(
            api_key=args.api_key,
            base_url=args.base_url,
            model=args.model,
            timeout=args.timeout,
            max_retries=args.max_retries,
            target_language=args.target_language,
            mock=args.mock,
        )
        LOGGER.info("发现 %d 个 PDF，输出目录: %s", len(inputs), output_dir)
        for source_path in inputs:
            process_one(source_path, output_dir, args, translator)
        return 0
    except (OSError, ValueError, fitz.FileDataError, TranslationError) as exc:
        LOGGER.error("处理失败: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
