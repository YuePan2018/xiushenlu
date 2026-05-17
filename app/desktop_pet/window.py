from __future__ import annotations

import math
import random
import tkinter as tk
from tkinter import TclError
from typing import Any

from PIL import ImageTk

from app.desktop_pet.assets import PetAsset, load_sprite_frames
from app.desktop_pet.state import PetState, clamp_window_position, load_pet_state, save_pet_state


class DesktopPetWindow:
    def __init__(
        self,
        *,
        asset: PetAsset,
        config: dict[str, Any],
        scale: float,
        move_speed: float,
        attraction_radius: float,
        tick_ms: int,
    ) -> None:
        self.asset = asset
        self.config = config
        self.scale = scale
        self.move_speed = move_speed
        self.attraction_radius = attraction_radius
        self.tick_ms = max(30, tick_ms)
        self.paused = False
        self.dragging = False
        self.drag_offset = (0, 0)
        self.drag_start = (0, 0)
        self.frame_index = 0
        self.current_row = 0
        self.poke_ticks = 0
        self.idle_ticks = 0
        self.idle_vector = (0.0, 0.0)

        self.root = tk.Tk()
        self.root.title(self.asset.display_name)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.transparent_color = "#00ff00"
        self.root.configure(bg=self.transparent_color)
        try:
            self.root.wm_attributes("-transparentcolor", self.transparent_color)
        except TclError:
            pass

        pil_frames = load_sprite_frames(asset, scale=scale)
        self.frames = [[ImageTk.PhotoImage(frame) for frame in row] for row in pil_frames]
        self.frame_width = self.frames[0][0].width()
        self.frame_height = self.frames[0][0].height()

        self.label = tk.Label(self.root, bd=0, bg=self.transparent_color, image=self.frames[0][0])
        self.label.pack()
        self.menu = tk.Menu(self.root, tearoff=False)
        self.menu.add_command(label="Pause", command=self.toggle_pause)
        self.menu.add_command(label="Reload Asset", command=self.reload_asset)
        self.menu.add_separator()
        self.menu.add_command(label="Exit", command=self.close)

        self.label.bind("<ButtonPress-1>", self.on_left_press)
        self.label.bind("<B1-Motion>", self.on_left_drag)
        self.label.bind("<ButtonRelease-1>", self.on_left_release)
        self.label.bind("<Button-3>", self.on_right_click)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        state = load_pet_state(
            config,
            default_scale=scale,
            window_width=self.frame_width,
            window_height=self.frame_height,
            screen_width=screen_width,
            screen_height=screen_height,
        )
        self.x = float(state.x)
        self.y = float(state.y)
        self.root.geometry(f"{self.frame_width}x{self.frame_height}+{state.x}+{state.y}")

    def run(self) -> None:
        self.root.after(self.tick_ms, self.tick)
        self.root.mainloop()

    def tick(self) -> None:
        self.update_behavior()
        self.update_frame()
        self.root.after(self.tick_ms, self.tick)

    def update_behavior(self) -> None:
        if self.paused:
            self.current_row = 6
            return
        if self.dragging:
            self.current_row = 4
            return
        if self.poke_ticks > 0:
            self.current_row = 3
            self.poke_ticks -= 1
            return

        pointer_x, pointer_y = self.root.winfo_pointerxy()
        center_x = self.x + self.frame_width / 2
        center_y = self.y + self.frame_height / 2
        dx = pointer_x - center_x
        dy = pointer_y - center_y
        distance = math.hypot(dx, dy)

        if 18 < distance < self.attraction_radius:
            step = min(self.move_speed, max(2.0, distance * 0.08))
            self.x += dx / distance * step
            self.y += dy / distance * step
            self.current_row = 1 if dx >= 0 else 2
            self.apply_position()
            return
        if distance <= 18:
            self.current_row = 3
            return

        self.current_row = 0
        self.idle_ticks += 1
        if self.idle_ticks % 24 == 0:
            self.idle_vector = (random.uniform(-1.5, 1.5), random.uniform(-0.8, 0.8))
        if self.idle_vector != (0.0, 0.0):
            self.x += self.idle_vector[0]
            self.y += self.idle_vector[1]
            self.apply_position()

    def update_frame(self) -> None:
        row = self.current_row if 0 <= self.current_row < len(self.frames) else 0
        if not self.frames[row]:
            row = 0
        self.frame_index = (self.frame_index + 1) % len(self.frames[row])
        self.label.configure(image=self.frames[row][self.frame_index])

    def apply_position(self) -> None:
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x, y = clamp_window_position(
            self.x,
            self.y,
            window_width=self.frame_width,
            window_height=self.frame_height,
            screen_width=screen_width,
            screen_height=screen_height,
        )
        self.x = float(x)
        self.y = float(y)
        self.root.geometry(f"{self.frame_width}x{self.frame_height}+{x}+{y}")

    def on_left_press(self, event: tk.Event) -> None:
        self.dragging = True
        self.drag_offset = (event.x, event.y)
        self.drag_start = (event.x_root, event.y_root)
        self.current_row = 4

    def on_left_drag(self, event: tk.Event) -> None:
        self.x = float(event.x_root - self.drag_offset[0])
        self.y = float(event.y_root - self.drag_offset[1])
        self.apply_position()

    def on_left_release(self, event: tk.Event) -> None:
        self.dragging = False
        moved = math.hypot(event.x_root - self.drag_start[0], event.y_root - self.drag_start[1])
        if moved < 6:
            self.poke_ticks = 10
        self.persist_state()

    def on_right_click(self, event: tk.Event) -> None:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.menu.entryconfigure(0, label="Resume" if self.paused else "Pause")

    def reload_asset(self) -> None:
        pil_frames = load_sprite_frames(self.asset, scale=self.scale)
        self.frames = [[ImageTk.PhotoImage(frame) for frame in row] for row in pil_frames]
        self.frame_index = 0
        self.update_frame()

    def persist_state(self) -> None:
        save_pet_state(
            self.config,
            PetState(x=round(self.x), y=round(self.y), scale=self.scale),
        )

    def close(self) -> None:
        self.persist_state()
        self.root.destroy()
