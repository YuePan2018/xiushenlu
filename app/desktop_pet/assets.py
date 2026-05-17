from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from PIL import Image

from app.config import resolve_project_path
from app.safety import safe_read_text, safe_write_bytes


DEFAULT_COLUMNS = 8
DEFAULT_ROWS = 9
MAX_ZIP_FILES = 32
MAX_ZIP_FILE_SIZE = 8 * 1024 * 1024
MAX_ZIP_TOTAL_SIZE = 32 * 1024 * 1024


class PetAssetError(RuntimeError):
    """Raised when a desktop pet asset cannot be loaded safely."""


@dataclass(frozen=True)
class PetAsset:
    pet_id: str
    display_name: str
    description: str
    root: Path
    manifest_path: Path
    spritesheet_path: Path


def desktop_pet_settings(config: dict[str, Any]) -> dict[str, Any]:
    settings = dict(config.get("desktop_pet", {}))
    settings.setdefault("asset_dir", "data/desktop_pet")
    settings.setdefault("default_pet", "fox")
    settings.setdefault("default_asset_url", "https://zip.openpets.dev/pets/fox-openpets/fox.zip")
    settings.setdefault("default_scale", 0.5)
    settings.setdefault("move_speed", 14)
    settings.setdefault("attraction_radius", 260)
    settings.setdefault("tick_ms", 80)
    settings.setdefault("download_timeout", 30)
    return settings


def asset_root(config: dict[str, Any]) -> Path:
    return resolve_project_path(desktop_pet_settings(config)["asset_dir"]).resolve()


def pet_dir(config: dict[str, Any], pet: str | None = None) -> Path:
    pet_name = pet or str(desktop_pet_settings(config)["default_pet"])
    _validate_pet_name(pet_name)
    return asset_root(config) / "pets" / pet_name


def ensure_pet_asset(
    config: dict[str, Any],
    *,
    pet: str | None = None,
    asset_url: str | None = None,
    download: bool = True,
) -> PetAsset:
    settings = desktop_pet_settings(config)
    pet_name = pet or str(settings["default_pet"])
    _validate_pet_name(pet_name)

    try:
        return load_pet_asset(config, pet=pet_name)
    except PetAssetError as first_error:
        if not download:
            raise first_error

    url = asset_url or str(settings["default_asset_url"])
    data = download_pet_zip(url, timeout=float(settings["download_timeout"]))
    extract_pet_zip(data, config, pet=pet_name)
    return load_pet_asset(config, pet=pet_name)


def load_pet_asset(config: dict[str, Any], *, pet: str | None = None) -> PetAsset:
    root = pet_dir(config, pet)
    manifest_path = root / "pet.json"
    if not manifest_path.exists():
        raise PetAssetError(f"Pet manifest not found: {manifest_path}")

    try:
        manifest = json.loads(safe_read_text(manifest_path, config))
    except (OSError, json.JSONDecodeError) as exc:
        raise PetAssetError(f"Pet manifest cannot be read: {manifest_path}") from exc

    if not isinstance(manifest, dict):
        raise PetAssetError("Pet manifest must be a JSON object.")

    pet_id = str(manifest.get("id") or root.name).strip()
    display_name = str(manifest.get("displayName") or pet_id).strip()
    description = str(manifest.get("description") or "").strip()
    spritesheet_value = str(manifest.get("spritesheetPath") or "").strip()
    if not spritesheet_value:
        raise PetAssetError("Pet manifest is missing spritesheetPath.")

    spritesheet_rel = _safe_relative_path(spritesheet_value)
    spritesheet_path = (root / Path(*spritesheet_rel.parts)).resolve()
    if not _is_relative_to(spritesheet_path, root.resolve()):
        raise PetAssetError("Pet spritesheet path escapes the pet directory.")
    if not spritesheet_path.exists():
        raise PetAssetError(f"Pet spritesheet not found: {spritesheet_path}")

    return PetAsset(
        pet_id=pet_id,
        display_name=display_name,
        description=description,
        root=root.resolve(),
        manifest_path=manifest_path.resolve(),
        spritesheet_path=spritesheet_path,
    )


def download_pet_zip(url: str, *, timeout: float) -> bytes:
    request = Request(url, headers={"User-Agent": "xiushenlu-desktop-pet/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_ZIP_TOTAL_SIZE + 1)
    except (OSError, URLError) as exc:
        raise PetAssetError(f"Failed to download pet asset: {url}") from exc

    if len(data) > MAX_ZIP_TOTAL_SIZE:
        raise PetAssetError("Downloaded pet asset is too large.")
    return data


def extract_pet_zip(data: bytes, config: dict[str, Any], *, pet: str | None = None) -> Path:
    target_dir = pet_dir(config, pet)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PetAssetError("Pet asset is not a valid ZIP file.") from exc

    with archive:
        entries = [entry for entry in archive.infolist() if not entry.is_dir()]
        if not entries:
            raise PetAssetError("Pet ZIP does not contain files.")
        if len(entries) > MAX_ZIP_FILES:
            raise PetAssetError("Pet ZIP contains too many files.")

        safe_entries: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        total_size = 0
        for entry in entries:
            rel_path = _safe_relative_path(entry.filename)
            if entry.file_size > MAX_ZIP_FILE_SIZE:
                raise PetAssetError(f"Pet ZIP entry is too large: {entry.filename}")
            total_size += entry.file_size
            if total_size > MAX_ZIP_TOTAL_SIZE:
                raise PetAssetError("Pet ZIP is too large after extraction.")
            safe_entries.append((entry, rel_path))

        manifest_candidates = [path for _, path in safe_entries if path.name == "pet.json"]
        if not manifest_candidates:
            raise PetAssetError("Pet ZIP does not contain pet.json.")
        base = min((path.parent for path in manifest_candidates), key=lambda item: len(item.parts))

        extracted = 0
        for entry, rel_path in safe_entries:
            try:
                output_rel = rel_path.relative_to(base)
            except ValueError:
                continue
            if not output_rel.parts:
                continue
            output_path = target_dir.joinpath(*output_rel.parts)
            with archive.open(entry) as source:
                safe_write_bytes(output_path, source.read(), config)
            extracted += 1

        if extracted == 0:
            raise PetAssetError("Pet ZIP did not extract any usable files.")

    return target_dir.resolve()


def load_sprite_frames(
    asset: PetAsset,
    *,
    scale: float,
    columns: int = DEFAULT_COLUMNS,
    rows: int = DEFAULT_ROWS,
) -> list[list[Image.Image]]:
    if scale <= 0:
        raise PetAssetError("Pet scale must be greater than 0.")

    try:
        with Image.open(asset.spritesheet_path) as source:
            atlas = source.convert("RGBA")
    except OSError as exc:
        raise PetAssetError(f"Pet spritesheet cannot be opened: {asset.spritesheet_path}") from exc

    width, height = atlas.size
    if width % columns != 0 or height % rows != 0:
        raise PetAssetError(
            f"Pet spritesheet size must be divisible by {columns}x{rows}: {width}x{height}"
        )

    frame_width = width // columns
    frame_height = height // rows
    scaled_width = max(1, round(frame_width * scale))
    scaled_height = max(1, round(frame_height * scale))
    frames: list[list[Image.Image]] = []
    for row in range(rows):
        row_frames: list[Image.Image] = []
        for column in range(columns):
            left = column * frame_width
            top = row * frame_height
            frame = atlas.crop((left, top, left + frame_width, top + frame_height))
            if scale != 1:
                frame = frame.resize((scaled_width, scaled_height), Image.Resampling.NEAREST)
            row_frames.append(frame)
        frames.append(row_frames)
    return frames


def _safe_relative_path(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise PetAssetError(f"Unsafe path in pet asset: {value}")
    if ":" in path.parts[0]:
        raise PetAssetError(f"Unsafe path in pet asset: {value}")
    return path


def _validate_pet_name(value: str) -> None:
    if not value:
        raise PetAssetError("Pet name cannot be empty.")
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in value):
        raise PetAssetError(f"Unsafe pet name: {value}")


def _is_relative_to(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents
