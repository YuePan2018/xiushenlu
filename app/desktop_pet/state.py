from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.desktop_pet.assets import asset_root
from app.safety import safe_read_text, safe_write_text


@dataclass(frozen=True)
class PetState:
    x: int
    y: int
    scale: float


def state_path(config: dict[str, Any]) -> Path:
    return asset_root(config) / "state.json"


def clamp_window_position(
    x: float,
    y: float,
    *,
    window_width: int,
    window_height: int,
    screen_width: int,
    screen_height: int,
    margin: int = 8,
) -> tuple[int, int]:
    max_x = max(margin, screen_width - window_width - margin)
    max_y = max(margin, screen_height - window_height - margin)
    clamped_x = min(max(round(x), margin), max_x)
    clamped_y = min(max(round(y), margin), max_y)
    return clamped_x, clamped_y


def load_pet_state(
    config: dict[str, Any],
    *,
    default_scale: float,
    window_width: int,
    window_height: int,
    screen_width: int,
    screen_height: int,
) -> PetState:
    path = state_path(config)
    fallback_x = screen_width - window_width - 80
    fallback_y = screen_height - window_height - 120
    x, y = fallback_x, fallback_y
    scale = default_scale

    if path.exists():
        try:
            data = json.loads(safe_read_text(path, config))
            if isinstance(data, dict):
                x = float(data.get("x", x))
                y = float(data.get("y", y))
                scale = float(data.get("scale", scale))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            x, y, scale = fallback_x, fallback_y, default_scale

    clamped_x, clamped_y = clamp_window_position(
        x,
        y,
        window_width=window_width,
        window_height=window_height,
        screen_width=screen_width,
        screen_height=screen_height,
    )
    return PetState(x=clamped_x, y=clamped_y, scale=scale if scale > 0 else default_scale)


def save_pet_state(config: dict[str, Any], state: PetState) -> None:
    text = json.dumps(
        {"x": state.x, "y": state.y, "scale": state.scale},
        ensure_ascii=False,
        indent=2,
    )
    safe_write_text(state_path(config), text + "\n", config)
