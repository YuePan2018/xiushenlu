from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_VISIBILITIES = {"公开可见", "仅自己可见", "仅互关好友可见"}
TITLE_UNIT_LIMIT = 20
CONTENT_LIMIT = 1000


@dataclass(frozen=True)
class XhsPostPayload:
    title: str
    content: str
    images: list[str]
    tags: list[str]
    visibility: str
    schedule_at: str = ""
    is_original: bool = False
    products: list[str] | None = None

    def to_mcp_arguments(self) -> dict[str, object]:
        args: dict[str, object] = {
            "title": self.title,
            "content": self.content,
            "images": self.images,
            "tags": self.tags,
            "visibility": self.visibility,
        }
        if self.schedule_at:
            args["schedule_at"] = self.schedule_at
        if self.is_original:
            args["is_original"] = True
        if self.products:
            args["products"] = self.products
        return args


def build_xhs_payload(
    *,
    title: str,
    content: str,
    images: list[str],
    tags: list[str] | None = None,
    visibility: str = "仅自己可见",
    schedule_at: str = "",
    is_original: bool = False,
    products: list[str] | None = None,
) -> XhsPostPayload:
    clean_title = _validate_title(title)
    clean_images = _validate_images(images)
    clean_visibility = _validate_visibility(visibility)
    return XhsPostPayload(
        title=clean_title,
        content=_validate_content(content),
        images=clean_images,
        tags=_normalize_tags(tags or []),
        visibility=clean_visibility,
        schedule_at=schedule_at.strip(),
        is_original=is_original,
        products=_normalize_tags(products or []),
    )


def _validate_title(title: str) -> str:
    clean = title.strip()
    if not clean:
        raise ValueError("小红书标题不能为空。")
    if _title_units(clean) > TITLE_UNIT_LIMIT:
        raise ValueError("小红书标题最多约 20 个中文字或英文单词。")
    return clean


def _title_units(title: str) -> int:
    units = 0
    ascii_buffer: list[str] = []
    for char in title:
        if "\u4e00" <= char <= "\u9fff":
            units += _flush_ascii_title_units(ascii_buffer)
            ascii_buffer = []
            units += 1
        elif char.isascii() and (char.isalnum() or char in "_-"):
            ascii_buffer.append(char)
        elif char.strip():
            units += _flush_ascii_title_units(ascii_buffer)
            ascii_buffer = []
            units += 1
        else:
            units += _flush_ascii_title_units(ascii_buffer)
            ascii_buffer = []
    units += _flush_ascii_title_units(ascii_buffer)
    return units


def _flush_ascii_title_units(chars: list[str]) -> int:
    text = "".join(chars).strip()
    if not text:
        return 0
    return len([item for item in re.split(r"[\s_-]+", text) if item])


def _validate_content(content: str) -> str:
    clean = content.strip()
    if not clean:
        raise ValueError("小红书正文不能为空。")
    if len(clean) > CONTENT_LIMIT:
        raise ValueError("小红书正文不能超过 1000 个字。")
    return clean


def _validate_images(images: list[str]) -> list[str]:
    clean = [item.strip() for item in images if item and item.strip()]
    if not clean:
        raise ValueError("小红书图文至少需要 1 张图片。")

    for image in clean:
        parsed = urlparse(image)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            continue
        path = Path(image)
        if not path.is_absolute():
            raise ValueError(f"本地图片必须使用绝对路径：{image}")
        if not path.exists():
            raise ValueError(f"本地图片不存在：{image}")
    return clean


def _normalize_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    clean: list[str] = []
    for tag in tags:
        value = tag.strip().lstrip("#").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        clean.append(value)
    return clean


def _validate_visibility(visibility: str) -> str:
    clean = visibility.strip() or "仅自己可见"
    if clean not in ALLOWED_VISIBILITIES:
        options = "、".join(sorted(ALLOWED_VISIBILITIES))
        raise ValueError(f"小红书可见范围只能是：{options}")
    return clean

