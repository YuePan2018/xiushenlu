from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class XhsMcpError(RuntimeError):
    """Raised when the xiaohongshu MCP service cannot complete a request."""


@dataclass(frozen=True)
class XhsToolResult:
    name: str
    text: str
    raw: dict[str, Any]
    is_error: bool = False


class XhsMcpClient:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._next_id = 1
        self._initialized = False
        self._session_id: str | None = None

    def check_login_status(self) -> XhsToolResult:
        return self.call_tool("check_login_status", {})

    def publish_content(self, arguments: dict[str, object]) -> XhsToolResult:
        return self.call_tool("publish_content", arguments)

    def get_my_profile_username(self) -> str:
        response = self._get_json(_sibling_api_url(self.url, "/api/v1/user/me"))
        return _extract_my_profile_username(response)

    def call_tool(self, name: str, arguments: dict[str, object]) -> XhsToolResult:
        self._ensure_initialized()
        response = self._rpc(
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
            },
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise XhsMcpError(f"MCP 工具返回格式异常：{response}")
        is_error = bool(result.get("isError"))
        text = _extract_text(result)
        return XhsToolResult(name=name, text=text, raw=result, is_error=is_error)

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "xiushenlu",
                    "version": "0.1.0",
                },
            },
        )
        self._notify("notifications/initialized", {})
        self._initialized = True

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        response = self._post_json(payload)
        if "error" in response:
            raise XhsMcpError(f"MCP 请求失败：{response['error']}")
        return response

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._post_json(payload, allow_empty=True)

    def _post_json(self, payload: dict[str, Any], *, allow_empty: bool = False) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self._session_id = session_id
                raw = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise XhsMcpError(f"无法连接 xiaohongshu-mcp：{exc}") from exc

        if not raw.strip() and allow_empty:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise XhsMcpError(f"MCP 返回不是 JSON：{raw[:200]}") from exc

    def _get_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise XhsMcpError(f"无法读取小红书当前用户信息：{exc}") from exc

        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise XhsMcpError(f"小红书当前用户信息返回不是 JSON：{raw[:200]}") from exc
        if not isinstance(body, dict):
            raise XhsMcpError(f"小红书当前用户信息返回格式异常：{raw[:200]}")
        return body


def _extract_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts).strip()


def _sibling_api_url(mcp_url: str, api_path: str) -> str:
    parsed = urllib.parse.urlparse(mcp_url)
    return urllib.parse.urlunparse(parsed._replace(path=api_path, params="", query="", fragment=""))


def _extract_my_profile_username(response: dict[str, Any]) -> str:
    candidates = [
        response,
        response.get("data") if isinstance(response.get("data"), dict) else None,
    ]
    first_data = candidates[1]
    if isinstance(first_data, dict):
        candidates.append(first_data.get("data") if isinstance(first_data.get("data"), dict) else None)

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        basic_info = candidate.get("userBasicInfo")
        if not isinstance(basic_info, dict):
            continue
        for key in ("nickname", "nickName"):
            value = basic_info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""
