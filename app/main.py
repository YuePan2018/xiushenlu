from __future__ import annotations

import sys
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config
from app.llm.qwen_agent_impl import QwenAgentProvider


def main() -> int:
    config = load_config()
    provider = QwenAgentProvider(config)
    reply = provider.chat("你好，请用一句话确认你已经连通。")
    print(reply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

