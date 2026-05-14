from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import load_config
from app.logger import EventLogger
from app.posting.drafts import read_post_draft, summarize_content
from app.posting.validation import XhsPostPayload, build_xhs_payload
from app.posting.xhs_mcp import XhsMcpClient, XhsMcpError, XhsToolResult


@dataclass(frozen=True)
class XhsPublishResult:
    approved: bool
    draft_path: Path
    payload: XhsPostPayload
    requested_event: dict[str, Any]
    status_result: XhsToolResult | None = None
    publish_result: XhsToolResult | None = None


def publish_xhs_from_draft(
    *,
    draft: str | Path,
    title: str,
    images: list[str],
    tags: list[str] | None = None,
    visibility: str = "仅自己可见",
    approve: bool = False,
    schedule_at: str = "",
    is_original: bool = False,
    products: list[str] | None = None,
    config: dict[str, Any] | None = None,
    logger: EventLogger | None = None,
    client: XhsMcpClient | None = None,
) -> XhsPublishResult:
    cfg = config or load_config()
    event_logger = logger or EventLogger(config=cfg)
    draft_path, content = read_post_draft(draft, cfg)
    payload = build_xhs_payload(
        title=title,
        content=content,
        images=images,
        tags=tags,
        visibility=visibility,
        schedule_at=schedule_at,
        is_original=is_original,
        products=products,
    )

    requested_event = event_logger.append_event(
        "post_publish_requested",
        "请求发布小红书图文",
        {
            "platform": "xiaohongshu",
            "draft_path": str(draft_path),
            "title": payload.title,
            "content_summary": summarize_content(payload.content),
            "images_count": len(payload.images),
            "tags": payload.tags,
            "visibility": payload.visibility,
            "schedule_at": payload.schedule_at or None,
            "approved": approve,
        },
    )

    if not approve:
        return XhsPublishResult(
            approved=False,
            draft_path=draft_path,
            payload=payload,
            requested_event=requested_event,
        )

    mcp_client = client or _build_client(cfg)
    try:
        publish_result = mcp_client.publish_content(payload.to_mcp_arguments())
        if publish_result.is_error:
            raise XhsMcpError(publish_result.text or "小红书发布失败。")
    except XhsMcpError as exc:
        event_logger.append_event(
            "post_failed",
            "小红书图文发布失败",
            {
                "platform": "xiaohongshu",
                "draft_path": str(draft_path),
                "title": payload.title,
                "error": str(exc),
            },
        )
        raise

    event_logger.append_event(
        "post_published",
        "小红书图文发布成功",
        {
            "platform": "xiaohongshu",
            "draft_path": str(draft_path),
            "title": payload.title,
            "result": publish_result.text,
        },
    )

    return XhsPublishResult(
        approved=True,
        draft_path=draft_path,
        payload=payload,
        requested_event=requested_event,
        publish_result=publish_result,
    )


def _build_client(config: dict[str, Any]) -> XhsMcpClient:
    settings = config.get("xiaohongshu", {})
    url = settings.get("mcp_url", "http://localhost:18060/mcp")
    timeout = float(settings.get("timeout", 30))
    return XhsMcpClient(url=url, timeout=timeout)
