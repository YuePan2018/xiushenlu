from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.desktop_pet.assets import desktop_pet_settings, ensure_pet_asset, load_sprite_frames
from app.desktop_pet.window import DesktopPetWindow


@dataclass(frozen=True)
class PetCheckResult:
    pet_id: str
    display_name: str
    spritesheet_path: str
    rows: int
    columns: int
    frame_size: tuple[int, int]


def check_desktop_pet(
    config: dict[str, Any],
    *,
    pet: str | None = None,
    asset_url: str | None = None,
    scale: float | None = None,
    download: bool = True,
) -> PetCheckResult:
    settings = desktop_pet_settings(config)
    effective_scale = float(scale if scale is not None else settings["default_scale"])
    asset = ensure_pet_asset(config, pet=pet, asset_url=asset_url, download=download)
    frames = load_sprite_frames(asset, scale=effective_scale)
    first = frames[0][0]
    return PetCheckResult(
        pet_id=asset.pet_id,
        display_name=asset.display_name,
        spritesheet_path=str(asset.spritesheet_path),
        rows=len(frames),
        columns=len(frames[0]) if frames else 0,
        frame_size=first.size,
    )


def launch_desktop_pet(
    config: dict[str, Any],
    *,
    pet: str | None = None,
    asset_url: str | None = None,
    scale: float | None = None,
    download: bool = True,
) -> None:
    settings = desktop_pet_settings(config)
    effective_scale = float(scale if scale is not None else settings["default_scale"])
    asset = ensure_pet_asset(config, pet=pet, asset_url=asset_url, download=download)
    window = DesktopPetWindow(
        asset=asset,
        config=config,
        scale=effective_scale,
        move_speed=float(settings["move_speed"]),
        attraction_radius=float(settings["attraction_radius"]),
        tick_ms=int(settings["tick_ms"]),
    )
    window.run()
