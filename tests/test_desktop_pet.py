from __future__ import annotations

import io
import json
import shutil
import unittest
import uuid
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image

from app.desktop_pet.assets import PetAssetError, load_sprite_frames
from app.desktop_pet.runner import check_desktop_pet
from app.desktop_pet.state import PetState, clamp_window_position, load_pet_state, save_pet_state


class DesktopPetTests(unittest.TestCase):
    def test_extracts_openpets_zip_and_slices_8_by_9_atlas(self) -> None:
        with _temporary_config() as config:
            asset = ensure_pet_asset_from_bytes(config, _pet_zip_bytes())

            self.assertEqual(asset.pet_id, "fox")
            self.assertEqual(asset.display_name, "Fox")
            self.assertEqual(asset.spritesheet_path.name, "spritesheet.png")

            frames = load_sprite_frames(asset, scale=2)

            self.assertEqual(len(frames), 9)
            self.assertEqual(len(frames[0]), 8)
            self.assertEqual(frames[0][0].size, (4, 4))
            self.assertEqual(frames[0][0].getpixel((0, 0)), (0, 0, 0, 255))
            self.assertEqual(frames[1][0].getpixel((0, 0)), (0, 30, 15, 255))

    def test_rejects_zip_path_traversal(self) -> None:
        with _temporary_config() as config:
            data = _zip_bytes(
                {
                    "pet.json": json.dumps({"id": "fox", "spritesheetPath": "spritesheet.png"}),
                    "../evil.txt": "nope",
                }
            )

            with self.assertRaisesRegex(PetAssetError, "Unsafe path"):
                ensure_pet_asset_from_bytes(config, data)

    def test_check_reports_pet_metadata_without_opening_window(self) -> None:
        with _temporary_config() as config:
            ensure_pet_asset_from_bytes(config, _pet_zip_bytes())

            result = check_desktop_pet(config, pet="fox", scale=1, download=False)

            self.assertEqual(result.pet_id, "fox")
            self.assertEqual(result.display_name, "Fox")
            self.assertEqual(result.rows, 9)
            self.assertEqual(result.columns, 8)
            self.assertEqual(result.frame_size, (2, 2))

    def test_state_persists_and_clamps_to_screen(self) -> None:
        with _temporary_config() as config:
            save_pet_state(config, PetState(x=2000, y=-80, scale=0.75))

            state = load_pet_state(
                config,
                default_scale=0.5,
                window_width=100,
                window_height=120,
                screen_width=800,
                screen_height=600,
            )

            self.assertEqual(state.x, 692)
            self.assertEqual(state.y, 8)
            self.assertEqual(state.scale, 0.75)

    def test_clamp_handles_window_larger_than_screen(self) -> None:
        x, y = clamp_window_position(
            500,
            500,
            window_width=1200,
            window_height=900,
            screen_width=800,
            screen_height=600,
        )

        self.assertEqual((x, y), (8, 8))


def ensure_pet_asset_from_bytes(config: dict[str, Any], data: bytes):
    from app.desktop_pet.assets import extract_pet_zip, load_pet_asset

    extract_pet_zip(data, config, pet="fox")
    return load_pet_asset(config, pet="fox")


def _pet_zip_bytes() -> bytes:
    manifest = json.dumps(
        {
            "id": "fox",
            "displayName": "Fox",
            "description": "Test fox",
            "spritesheetPath": "spritesheet.png",
        },
        ensure_ascii=False,
    )
    return _zip_bytes(
        {
            "fox/pet.json": manifest,
            "fox/spritesheet.png": _spritesheet_png_bytes(),
        }
    )


def _zip_bytes(entries: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return buffer.getvalue()


def _spritesheet_png_bytes() -> bytes:
    image = Image.new("RGBA", (16, 18))
    for row in range(9):
        for column in range(8):
            color = (column * 20, row * 30, (row + column) * 15, 255)
            for x in range(column * 2, column * 2 + 2):
                for y in range(row * 2, row * 2 + 2):
                    image.putpixel((x, y), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class _temporary_config:
    def __enter__(self) -> dict[str, Any]:
        parent = Path("workspace") / "test_desktop_pet"
        parent.mkdir(parents=True, exist_ok=True)
        self.root = parent / uuid.uuid4().hex
        asset_dir = self.root / "data" / "desktop_pet"
        self.config = {
            "desktop_pet": {
                "asset_dir": str(asset_dir),
                "default_pet": "fox",
                "default_asset_url": "https://example.com/fox.zip",
                "default_scale": 0.5,
                "move_speed": 14,
                "attraction_radius": 260,
                "tick_ms": 80,
                "download_timeout": 1,
            },
            "safety": {
                "allowed_dirs": [str(asset_dir)],
                "protected_files": [],
            },
        }
        return self.config

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
