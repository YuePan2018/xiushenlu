from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import dashscope
from dashscope import MultiModalConversation
from dotenv import load_dotenv

from app.config import resolve_project_path
from app.safety import safe_write_bytes


MAX_FILENAME_TEXT_LENGTH = 48


@dataclass(frozen=True)
class XhsCoverGenerationResult:
    image_path: Path
    image_url: str
    model: str
    image_count: int
    width: int | None = None
    height: int | None = None
    request_id: str | None = None


def generate_xhs_cover_from_text(text: str, config: dict[str, Any]) -> XhsCoverGenerationResult:
    selected_text = text.strip()
    if not selected_text:
        raise ValueError("草稿正文为空，无法生成封面。")

    load_dotenv()
    llm_config = config.get("llm", {})
    api_key_env = str(llm_config.get("api_key_env", "DASHSCOPE_API_KEY"))
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise RuntimeError(f"Missing {api_key_env}. Set it before generating a cover.")

    xhs_config = config.get("xiaohongshu", {})
    model = str(xhs_config.get("cover_model", "qwen-image-2.0"))
    timeout = float(xhs_config.get("cover_download_timeout", llm_config.get("timeout", 30)))
    prompt = _build_cover_prompt(selected_text)
    messages = [
        {
            "role": "user",
            "content": [
                {"text": prompt},
            ],
        }
    ]

    dashscope.base_http_api_url = "https://dashscope.aliyuncs.com/api/v1"
    response = MultiModalConversation.call(
        api_key=api_key,
        model=model,
        messages=messages,
        result_format="message",
        stream=False,
        n=1,
        watermark=True,
        negative_prompt="",
    )

    image_url = _extract_first_image_url(response)
    usage = _extract_usage(response)
    image_bytes = _download_image_bytes(image_url, timeout)
    image_path = _next_cover_path(selected_text, config)
    safe_write_bytes(image_path, image_bytes, config)

    return XhsCoverGenerationResult(
        image_path=image_path,
        image_url=image_url,
        model=model,
        image_count=usage["image_count"],
        width=usage["width"],
        height=usage["height"],
        request_id=usage["request_id"],
    )


def _build_cover_prompt(text: str) -> str:
    return (
        "请根据以下小红书笔记文字生成一张适合作为小红书封面的图片。"
        "画面要清晰、有主体、有情绪，适合竖屏图文封面；不要出现二维码、网址或联系方式。\n\n"
        f"{text}"
    )


def _extract_first_image_url(response: Any) -> str:
    status_code = _get_value(response, "status_code")
    if status_code not in (None, 200):
        raise RuntimeError(_format_response_error(response))

    output = _get_value(response, "output")
    choices = _get_value(output, "choices") or []
    for choice in choices:
        message = _get_value(choice, "message")
        content = _get_value(message, "content") or []
        if isinstance(content, dict):
            content = [content]
        for item in content:
            image = _get_value(item, "image")
            if isinstance(image, str) and image.strip():
                return image.strip()
    raise RuntimeError(_format_response_error(response))


def _extract_usage(response: Any) -> dict[str, Any]:
    usage = _get_value(response, "usage")
    image_count = _first_int(usage, "image_count")
    if image_count is None or image_count <= 0:
        image_count = 1
    return {
        "image_count": image_count,
        "width": _first_int(usage, "width"),
        "height": _first_int(usage, "height"),
        "request_id": _string_or_none(_get_value(response, "request_id")),
    }


def _download_image_bytes(url: str, timeout: float) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"封面生成返回了无效图片 URL：{url}")

    request = Request(url, headers={"User-Agent": "xiushenlu/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read()
    except (OSError, URLError) as exc:
        raise RuntimeError(f"封面图片下载失败：{exc}") from exc
    if not data:
        raise RuntimeError("封面图片下载失败：返回内容为空。")
    return data


def _next_cover_path(text: str, config: dict[str, Any]) -> Path:
    image_dir = resolve_project_path(config.get("paths", {}).get("post_image_dir", "post/images")).resolve()
    stem = _cover_file_stem(text)
    candidate = image_dir / f"{stem}.png"
    if not candidate.exists():
        return candidate

    for index in range(2, 1000):
        candidate = image_dir / f"{stem}-{index}.png"
        if not candidate.exists():
            return candidate
    raise RuntimeError("封面文件名冲突过多，请调整选中文本第一行后重试。")


def _cover_file_stem(text: str) -> str:
    first_line = _first_non_empty_line(text)
    safe_text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", first_line)
    safe_text = re.sub(r"\s+", " ", safe_text).strip(" .-")
    if not safe_text:
        safe_text = "cover"
    if len(safe_text) > MAX_FILENAME_TEXT_LENGTH:
        safe_text = safe_text[:MAX_FILENAME_TEXT_LENGTH].rstrip(" .-")
    return f"{date.today().isoformat()}_{safe_text or 'cover'}"


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        clean = " ".join(line.strip().split())
        if clean:
            return clean
    return "cover"


def _get_value(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _first_int(source: Any, *keys: str) -> int | None:
    for key in keys:
        value = _get_value(source, key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _format_response_error(response: Any) -> str:
    code = _get_value(response, "code") or _get_value(response, "status_code")
    message = _get_value(response, "message") or _get_value(response, "msg")
    request_id = _get_value(response, "request_id")

    details = []
    if code:
        details.append(f"code={code}")
    if message:
        details.append(f"message={message}")
    if request_id:
        details.append(f"request_id={request_id}")
    if details:
        return "封面生成失败或未返回图片：" + "，".join(details)
    return "封面生成失败或未返回图片。"
