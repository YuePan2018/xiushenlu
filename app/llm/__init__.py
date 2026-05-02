from app.llm.provider import LLMProvider

__all__ = ["LLMProvider", "DashScopeProvider"]


def __getattr__(name: str):
    if name == "DashScopeProvider":
        from app.llm.dashscope_impl import DashScopeProvider

        return DashScopeProvider
    raise AttributeError(name)
