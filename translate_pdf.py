#!/usr/bin/env python3
"""Translate text-based PDF files while keeping the original page geometry."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import fitz  # PyMuPDF


LOGGER = logging.getLogger("pdf-translator")
PROMPT_VERSION = "2026-09-26-v1"

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

Return JSON only in this shape:
{"translations":[{"id":"p1b0","text":"..."}]}
"""


class TranslationError(RuntimeError):
    pass


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

    @property
    def block_id(self) -> str:
        return f"p{self.page_index + 1}b{self.block_index}"


def clean_extracted_text(text: str) -> str:
    text = text.replace("\u00ad", "").replace("\ufffd", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    return " ".join(line for line in lines if line).strip()


def is_translatable(text: str) -> bool:
    """Skip blocks that are clearly only punctuation, numbers, or symbols."""
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
        page_dict = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT)
        for block_index, block in enumerate(page_dict.get("blocks", [])):
            if block.get("type") != 0:
                continue
            lines = block.get("lines", [])
            spans = [span for line in lines for span in line.get("spans", []) if span.get("text", "").strip()]
            raw_text = "\n".join("".join(span.get("text", "") for span in line.get("spans", [])) for line in lines)
            source_text = clean_extracted_text(raw_text)
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
            )


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
        result: dict[str, str] = {}
        expected_ids = {item["id"] for item in items}
        for item in translations:
            if not isinstance(item, dict):
                continue
            item_id, text = str(item.get("id", "")), item.get("text")
            if item_id in expected_ids and isinstance(text, str):
                result[item_id] = text.strip()
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


def prepare_translations(
    blocks: list[TextBlock],
    cache: JsonCache,
    translator: OpenAICompatibleTranslator,
    *,
    target_language: str,
    max_batch_chars: int,
    force: bool,
) -> dict[str, str]:
    translated: dict[str, str] = {}
    pending: list[dict[str, str]] = []
    for block in blocks:
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
            LOGGER.info("翻译批次 %d: %d 个文本块", index, len(batch))
            result = translator.translate_batch([{"id": x["id"], "text": x["text"]} for x in batch])
            for item in batch:
                value = result[item["id"]]
                translated[item["id"]] = value
                cache.put(item["_cache_key"], value)
            cache.save()
    return translated


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
    """Shrink translated text until it fits inside its original text block."""
    inset = min(1.2, max(0.2, font_size * 0.06))
    target = fitz.Rect(rect.x0 + inset, rect.y0 + inset, rect.x1 - inset, rect.y1 - inset)
    if target.width <= 2 or target.height <= 2:
        target = fitz.Rect(rect)
    size = max(4.0, font_size * 0.96)
    while size >= 4.0:
        result = page.insert_textbox(
            target,
            text,
            fontfile=fontfile,
            fontname="cjkfont",
            fontsize=size,
            color=color,
            align=align,
            lineheight=1.12,
            overlay=True,
        )
        if result >= 0:
            return size
        size -= 0.5
    LOGGER.warning("文本块无法完全装入原区域，将以 4pt 字号尽量写入: %s", text[:80])
    page.insert_textbox(
        target,
        text,
        fontfile=fontfile,
        fontname="cjkfont",
        fontsize=4.0,
        color=color,
        align=align,
        lineheight=1.05,
        overlay=True,
    )
    return 4.0


def render_translated_pdf(
    source_path: Path,
    output_path: Path,
    blocks: list[TextBlock],
    translations: dict[str, str],
    *,
    font_override: str | None,
) -> dict[str, Any]:
    doc = fitz.open(source_path)
    try:
        by_page: dict[int, list[TextBlock]] = {}
        for block in blocks:
            # Keep formulas, identifiers, URLs, and symbol-only blocks exactly
            # as authored in the PDF, including their original font styling.
            if is_block_translatable(block):
                by_page.setdefault(block.page_index, []).append(block)
        inserted = 0
        min_font_size = 100.0
        for page_index, page_blocks_for_page in by_page.items():
            page = doc[page_index]
            for block in page_blocks_for_page:
                page.add_redact_annot(fitz.Rect(block.rect), fill=(1, 1, 1))
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
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
        return {"blocks": inserted, "min_font_size": min_font_size}
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


def process_one(
    source_path: Path,
    output_dir: Path,
    args: argparse.Namespace,
    translator: OpenAICompatibleTranslator,
) -> None:
    LOGGER.info("处理: %s", source_path)
    doc = fitz.open(source_path)
    try:
        selected_pages = parse_page_spec(args.pages, len(doc))
        blocks = list(iter_text_blocks(doc, selected_pages))
        if not blocks:
            LOGGER.warning("没有找到可提取的文本，可能是扫描版 PDF: %s", source_path.name)
            return
        count = sum(is_block_translatable(block) for block in blocks)
        chars = sum(len(block.source_text) for block in blocks)
        LOGGER.info("页数=%d，文本块=%d，需要翻译=%d，字符数=%d", len(doc), len(blocks), count, chars)
        if args.dry_run:
            return
    finally:
        doc.close()

    output_path = output_dir / f"{source_path.stem}.zh-CN.pdf"
    if output_path.exists() and not args.overwrite:
        LOGGER.info("输出已存在，跳过（使用 --overwrite 覆盖）: %s", output_path)
        return
    cache = JsonCache(output_dir / ".cache" / f"{source_path.stem}.json")
    translations = prepare_translations(
        blocks,
        cache,
        translator,
        target_language=args.target_language,
        max_batch_chars=args.max_batch_chars,
        force=args.force,
    )
    result = render_translated_pdf(source_path, output_path, blocks, translations, font_override=args.font)
    LOGGER.info("已生成: %s（替换 %d 个文本块，最小字号 %.1f）", output_path, result["blocks"], result["min_font_size"])


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
