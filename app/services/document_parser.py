"""Document parsing service with OCR support."""
import asyncio
import io
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional
from PIL import Image
import pdfplumber
import pypdfium2 as pdfium
import pytesseract
from app.config import settings

logger = logging.getLogger(__name__)

# Protect against decompression bombs
Image.MAX_IMAGE_PIXELS = settings.max_image_megapixels * 1_000_000

# Lazy-loaded local RapidOCR (PaddleOCR ONNX) instance
_rapid_ocr_engine = None


def _get_rapid_ocr():
    """Get or initialize the local RapidOCR (PaddleOCR ONNX) engine."""
    global _rapid_ocr_engine
    if _rapid_ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapid_ocr_engine = RapidOCR()
    return _rapid_ocr_engine

# Thread pool for blocking PDF and OCR operations
_executor = ThreadPoolExecutor(
    max_workers=max(1, settings.ocr_worker_concurrency))


class DocumentParsingError(Exception):
    """Document parsing error."""
    pass


class PageParseResult:
    """Result of parsing a single page."""

    def __init__(
        self,
        page_number: int,
        raw_text: str,
        normalized_text: str,
        extraction_method: str,
        ocr_confidence: Optional[int] = None,
        blocks: Optional[List[Dict]] = None,
        is_readable: bool = True,
        warnings: Optional[List[str]] = None
    ):
        self.page_number = page_number
        self.raw_text = raw_text
        self.normalized_text = normalized_text
        self.extraction_method = extraction_method
        self.ocr_confidence = ocr_confidence
        self.blocks = blocks or []
        self.is_readable = is_readable
        self.warnings = warnings or []


class DocumentParser:
    """Parses documents and extracts text with OCR support."""

    def __init__(self):
        """Initialize parser."""
        self.ocr_language = settings.ocr_language

    async def parse_document(
        self,
        content: bytes,
        content_type: str,
        force_ocr: bool = False
    ) -> List[PageParseResult]:
        """
        Parse document and extract text from all pages.

        Args:
            content: Document bytes
            content_type: MIME type
            force_ocr: Force OCR even if text layer exists

        Returns:
            List of PageParseResult for each page
        """
        if not content:
            raise DocumentParsingError("Document file is empty")

        if content_type == "application/pdf":
            return await self._parse_pdf(content, force_ocr)
        elif content_type.startswith("image/"):
            return await self._parse_image(content)
        elif content_type == "text/plain":
            return self._parse_text(content)
        else:
            raise DocumentParsingError(
                f"Unsupported content type: {content_type}")

    async def _parse_pdf(self, content: bytes, force_ocr: bool = False) -> List[PageParseResult]:
        """Parse PDF document off the main event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _executor,
            self._parse_pdf_sync,
            content,
            force_ocr
        )

    def _parse_pdf_sync(self, content: bytes, force_ocr: bool = False) -> List[PageParseResult]:
        """Synchronously parse PDF pages and run OCR when needed."""
        results: List[PageParseResult] = []
        pdf_file = io.BytesIO(content)

        try:
            with pdfplumber.open(pdf_file) as pdf:
                total_pages = len(pdf.pages)

                if total_pages == 0:
                    raise DocumentParsingError("PDF has 0 pages")

                if total_pages > settings.max_pdf_pages:
                    raise DocumentParsingError(
                        f"PDF has {total_pages} pages, exceeds limit of {settings.max_pdf_pages}"
                    )

                for page_num, page in enumerate(pdf.pages, start=1):
                    try:
                        text = page.extract_text() or ""
                        has_usable_text = len(
                            text.strip()) > 20 and not force_ocr

                        if has_usable_text:
                            blocks = self._create_pdf_blocks(
                                page, text, page_num)
                            result = PageParseResult(
                                page_number=page_num,
                                raw_text=text,
                                normalized_text=self._normalize_text(text),
                                extraction_method="direct",
                                blocks=blocks,
                                is_readable=True,
                                warnings=[]
                            )
                        else:
                            result = self._ocr_pdf_page_sync(content, page_num)

                        results.append(result)

                    except Exception as e:
                        logger.error(
                            f"Failed to parse PDF page {page_num}: {e}")
                        results.append(PageParseResult(
                            page_number=page_num,
                            raw_text="",
                            normalized_text="",
                            extraction_method="failed",
                            is_readable=False,
                            warnings=[
                                f"Parsing failed on page {page_num}: {str(e)}"]
                        ))

        except DocumentParsingError:
            raise
        except Exception as e:
            err_msg = str(e)
            if "password" in err_msg.lower() or "encrypt" in err_msg.lower():
                raise DocumentParsingError(
                    "PDF is password-protected or encrypted and cannot be read")
            logger.error(f"PDF parsing failed: {e}")
            raise DocumentParsingError(f"Failed to parse PDF: {err_msg}")

        return results

    def _ocr_with_rapidocr_sync(self, pil_image: Image.Image, page_number: int = 1) -> Optional[PageParseResult]:
        """Run local offline OCR using RapidOCR (PaddleOCR ONNX Runtime)."""
        try:
            import numpy as np
            engine = _get_rapid_ocr()
            rgb_img = pil_image.convert("RGB")
            img_width, img_height = rgb_img.size
            np_img = np.array(rgb_img)

            ocr_results, _ = engine(np_img)
            if not ocr_results:
                return PageParseResult(
                    page_number=page_number,
                    raw_text="",
                    normalized_text="",
                    extraction_method="ocr",
                    ocr_confidence=0,
                    blocks=[],
                    is_readable=False,
                    warnings=["No readable text detected in image after OCR"]
                )

            # Parse detected boxes and group boxes on the same horizontal row
            raw_items = []
            for box, text, score in ocr_results:
                txt = str(text or "").strip()
                if not txt:
                    continue
                xs = [pt[0] for pt in box]
                ys = [pt[1] for pt in box]
                x0, x1 = float(min(xs)), float(max(xs))
                y0, y1 = float(min(ys)), float(max(ys))
                conf = float(score) * 100.0 if float(score) <= 1.0 else float(score)
                raw_items.append({
                    "text": txt,
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1,
                    "yc": (y0 + y1) / 2.0,
                    "h": max(1.0, y1 - y0),
                    "conf": conf,
                })

            raw_items.sort(key=lambda it: (it["yc"], it["x0"]))
            merged_lines = []
            for item in raw_items:
                if not merged_lines:
                    merged_lines.append([item])
                else:
                    prev_line = merged_lines[-1]
                    prev_yc = sum(x["yc"] for x in prev_line) / len(prev_line)
                    prev_h = max(x["h"] for x in prev_line)
                    if abs(item["yc"] - prev_yc) <= prev_h * 0.55:
                        prev_line.append(item)
                    else:
                        merged_lines.append([item])

            blocks = []
            text_lines = []
            confidences = []
            for idx, line_items in enumerate(merged_lines):
                line_items.sort(key=lambda it: it["x0"])
                line_text = "   ".join(it["text"] for it in line_items)
                lx0 = min(it["x0"] for it in line_items)
                ly0 = min(it["y0"] for it in line_items)
                lx1 = max(it["x1"] for it in line_items)
                ly1 = max(it["y1"] for it in line_items)
                line_conf = int(round(sum(it["conf"] for it in line_items) / len(line_items)))
                confidences.append(line_conf)
                text_lines.append(line_text)
                blocks.append({
                    "block_id": f"p{page_number}_b{idx}",
                    "text": line_text,
                    "bbox": {
                        "x0": round(lx0, 1),
                        "y0": round(ly0, 1),
                        "x1": round(lx1, 1),
                        "y1": round(ly1, 1),
                        "page_width": img_width,
                        "page_height": img_height,
                    },
                    "extraction_method": "ocr",
                    "confidence": line_conf,
                })

            raw_text = "\n".join(text_lines)
            normalized = self._normalize_text(raw_text)
            avg_confidence = int(round(sum(confidences) / len(confidences))) if confidences else 0
            is_readable = len(normalized) > 10 and (avg_confidence >= 30 or len(normalized) > 30)
            warnings = []
            if not is_readable:
                warnings.append("Low or unreadable text content after OCR")
            elif avg_confidence < 50:
                warnings.append(f"Low OCR confidence ({avg_confidence}%)")

            return PageParseResult(
                page_number=page_number,
                raw_text=raw_text,
                normalized_text=normalized,
                extraction_method="ocr",
                ocr_confidence=avg_confidence,
                blocks=blocks,
                is_readable=is_readable,
                warnings=warnings,
            )
        except Exception as e:
            logger.error(f"RapidOCR failed: {e}")
            return None

    def _ocr_pdf_page_sync(self, pdf_content: bytes, page_number: int) -> PageParseResult:
        """Synchronous OCR of PDF page using pypdfium2 and RapidOCR (PaddleOCR ONNX)."""
        pdf_doc = None
        page = None
        bitmap = None
        pil_image = None

        try:
            pdf_doc = pdfium.PdfDocument(pdf_content)
            page = pdf_doc[page_number - 1]
            bitmap = page.render(scale=2.0)
            pil_image = bitmap.to_pil()
            rapid_res = self._ocr_with_rapidocr_sync(pil_image, page_number=page_number)
            if rapid_res is not None:
                return rapid_res

            return PageParseResult(
                page_number=page_number,
                raw_text="",
                normalized_text="",
                extraction_method="ocr",
                ocr_confidence=0,
                blocks=[],
                is_readable=False,
                warnings=["Scanned/image page could not be read by OCR"]
            )
        except Exception as e:
            logger.error(f"OCR failed for PDF page {page_number}: {e}")
            return PageParseResult(
                page_number=page_number,
                raw_text="",
                normalized_text="",
                extraction_method="ocr",
                ocr_confidence=0,
                blocks=[],
                is_readable=False,
                warnings=[f"OCR failed: {str(e)}"]
            )
        finally:
            if pil_image is not None:
                try:
                    pil_image.close()
                except Exception:
                    pass
            if bitmap is not None:
                try:
                    bitmap.close()
                except Exception:
                    pass
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            if pdf_doc is not None:
                try:
                    pdf_doc.close()
                except Exception:
                    pass

    async def _parse_image(self, content: bytes) -> List[PageParseResult]:
        """Parse image file with OCR."""
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _executor,
            self._ocr_image_bytes_sync,
            content
        )
        return [result]

    def _ocr_image_bytes_sync(self, content: bytes) -> PageParseResult:
        """Synchronously validate and OCR an image from bytes using RapidOCR."""
        image = None
        try:
            image = Image.open(io.BytesIO(content))
            width, height = image.size
            megapixels = (width * height) / 1_000_000

            if megapixels > settings.max_image_megapixels:
                raise DocumentParsingError(
                    f"Image is {megapixels:.1f}MP, exceeds limit of {settings.max_image_megapixels}MP"
                )

            rapid_res = self._ocr_with_rapidocr_sync(image, page_number=1)
            if rapid_res is not None:
                return rapid_res

            return PageParseResult(
                page_number=1,
                raw_text="",
                normalized_text="",
                extraction_method="ocr",
                ocr_confidence=0,
                blocks=[],
                is_readable=False,
                warnings=["No readable text detected in image"]
            )
        except DocumentParsingError:
            raise
        except Exception as e:
            logger.error(f"Image parsing failed: {e}")
            raise DocumentParsingError(f"Failed to parse image: {str(e)}")
        finally:
            if image is not None:
                try:
                    image.close()
                except Exception:
                    pass

    def _parse_text(self, content: bytes) -> List[PageParseResult]:
        """Parse plain text file."""
        try:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("latin-1")

            normalized = self._normalize_text(text)
            blocks = self._create_text_blocks(text, page_number=1)
            is_readable = len(normalized) > 0
            warnings = [] if is_readable else ["Text document has no readable text"]

            return [PageParseResult(
                page_number=1,
                raw_text=text,
                normalized_text=normalized,
                extraction_method="direct",
                blocks=blocks,
                is_readable=is_readable,
                warnings=warnings
            )]

        except Exception as e:
            logger.error(f"Text parsing failed: {e}")
            raise DocumentParsingError(f"Failed to parse text: {str(e)}")

    def _normalize_text(self, text: str) -> str:
        """Normalize text with conservative cleaning while preserving line breaks and clinical units."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _create_text_blocks(self, text: str, page_number: int = 1) -> List[Dict]:
        """Create text blocks from plain text."""
        blocks = []
        lines = text.replace("\r\n", "\n").split("\n")

        block_idx = 0
        for line in lines:
            stripped = line.strip()
            if stripped:
                blocks.append({
                    "block_id": f"p{page_number}_b{block_idx}",
                    "text": stripped,
                    "order": block_idx,
                    "bbox": None,
                    "coord_system": "text_line"
                })
                block_idx += 1

        return blocks

    def _create_pdf_blocks(self, page, text: str, page_number: int) -> List[Dict]:
        """Create text blocks with bounding boxes from a pdfplumber page."""
        blocks = []
        try:
            lines = page.extract_text_lines() if hasattr(page, "extract_text_lines") else []
            if lines:
                for idx, line in enumerate(lines):
                    line_text = (line.get("text") or "").strip()
                    if not line_text:
                        continue
                    blocks.append({
                        "block_id": f"p{page_number}_b{len(blocks)}",
                        "text": line_text,
                        "order": len(blocks),
                        "bbox": {
                            "x0": round(float(line.get("x0", 0)), 2),
                            "top": round(float(line.get("top", 0)), 2),
                            "x1": round(float(line.get("x1", 0)), 2),
                            "bottom": round(float(line.get("bottom", 0)), 2),
                            "page_width": round(float(getattr(page, "width", 0) or 0), 2),
                            "page_height": round(float(getattr(page, "height", 0) or 0), 2),
                        },
                        "coord_system": "pdf_points"
                    })
                if blocks:
                    return blocks
        except Exception as e:
            logger.debug(
                f"Could not extract pdf line boxes on page {page_number}: {e}")

        return self._create_text_blocks(text, page_number=page_number)

    def _extract_ocr_blocks_and_text(
        self,
        ocr_data: Dict,
        page_number: int = 1,
        page_width: Optional[int] = None,
        page_height: Optional[int] = None
    ) -> tuple[List[Dict], str, int]:
        """Create text blocks with bounding boxes and confidence from Tesseract OCR output."""
        blocks: List[Dict] = []
        confidences: List[int] = []

        grouped: Dict[tuple, Dict] = {}
        n_items = len(ocr_data.get("text", []))

        for i in range(n_items):
            word = (ocr_data["text"][i] or "").strip()
            if not word:
                continue

            try:
                conf = int(float(ocr_data["conf"][i]))
            except (ValueError, TypeError):
                conf = -1
            if conf >= 0:
                confidences.append(conf)

            block_num = ocr_data.get("block_num", [0] * n_items)[i]
            par_num = ocr_data.get("par_num", [0] * n_items)[i]
            line_num = ocr_data.get("line_num", [0] * n_items)[i]
            key = (block_num, par_num, line_num)

            left = int(ocr_data.get("left", [0] * n_items)[i] or 0)
            top = int(ocr_data.get("top", [0] * n_items)[i] or 0)
            width = int(ocr_data.get("width", [0] * n_items)[i] or 0)
            height = int(ocr_data.get("height", [0] * n_items)[i] or 0)

            if key not in grouped:
                grouped[key] = {
                    "words": [word],
                    "confs": [conf] if conf >= 0 else [],
                    "x0": left,
                    "top": top,
                    "x1": left + width,
                    "bottom": top + height
                }
            else:
                entry = grouped[key]
                entry["words"].append(word)
                if conf >= 0:
                    entry["confs"].append(conf)
                entry["x0"] = min(entry["x0"], left)
                entry["top"] = min(entry["top"], top)
                entry["x1"] = max(entry["x1"], left + width)
                entry["bottom"] = max(entry["bottom"], top + height)

        lines_text = []
        for idx, (_, entry) in enumerate(grouped.items()):
            line_str = " ".join(entry["words"])
            lines_text.append(line_str)
            line_conf = int(
                sum(entry["confs"]) / len(entry["confs"])) if entry["confs"] else None
            blocks.append({
                "block_id": f"p{page_number}_b{idx}",
                "text": line_str,
                "order": idx,
                "confidence": line_conf,
                "bbox": {
                    "x0": entry["x0"],
                    "top": entry["top"],
                    "x1": entry["x1"],
                    "bottom": entry["bottom"],
                    "page_width": page_width,
                    "page_height": page_height
                },
                "coord_system": "image_pixels"
            })

        raw_text = "\n".join(lines_text)
        avg_confidence = int(sum(confidences) /
                             len(confidences)) if confidences else 0
        return blocks, raw_text, avg_confidence


# Singleton instance
document_parser = DocumentParser()
