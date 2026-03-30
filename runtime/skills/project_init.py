"""
project-init skill — Initializes a game project directory structure.
For Godot: creates project.godot, directories, main scene, autoloads.
For Pygame: creates main.py, requirements.txt, directory layout.
Tier 0 (pure Python — no LLM needed).
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import STATE_DIR

log = logging.getLogger(__name__)
GAMEDEV_DIR = STATE_DIR / "gamedev"
PROJECT_DIR = GAMEDEV_DIR / "project"
GDD_FILE = GAMEDEV_DIR / "gdd.json"
MANIFEST_FILE = PROJECT_DIR / "manifest.json"


def _load_gdd() -> dict:
    """Load the GDD for project title and genre."""
    if GDD_FILE.exists():
        try:
            with open(GDD_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load gdd.json: %s", e)
    return {}


def _load_manifest() -> dict:
    """Load existing manifest or return empty."""
    if MANIFEST_FILE.exists():
        try:
            with open(MANIFEST_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"files": [], "targets": {}}


def _save_manifest(manifest: dict) -> None:
    """Persist the project manifest."""
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def _write_file(path: Path, content: str) -> None:
    """Write a file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _init_godot(
    project_name: str,
    window_width: int,
    window_height: int,
) -> tuple[list[str], list[str]]:
    """
    Create a Godot 4 project structure.
    Returns (created_files, created_dirs).
    """
    root = PROJECT_DIR / "godot"
    created_files = []
    created_dirs = []

    # -- Directories --
    dirs = [
        root / "scenes",
        root / "scripts" / "autoload",
        root / "assets" / "sprites",
        root / "assets" / "audio" / "sfx",
        root / "assets" / "audio" / "music",
        root / "assets" / "ui",
        root / "tests",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        created_dirs.append(str(d.relative_to(PROJECT_DIR)))

    # -- project.godot --
    project_godot = f"""; Engine configuration file.
; It's best edited using the editor UI and not directly.

[application]

config/name="{project_name}"
run/main_scene="res://scenes/main.tscn"
config/features=PackedStringArray("4.2", "Forward Plus")

[display]

window/size/viewport_width={window_width}
window/size/viewport_height={window_height}
window/stretch/mode="canvas_items"

[autoload]

GameManager="*res://scripts/autoload/game_manager.gd"
AudioManager="*res://scripts/autoload/audio_manager.gd"

[rendering]

renderer/rendering_method="forward_plus"
"""
    _write_file(root / "project.godot", project_godot)
    created_files.append("godot/project.godot")

    # -- default_env.tres --
    default_env = """[gd_resource type="Environment" format=3]

[resource]
background_mode = 1
background_color = Color(0.12, 0.12, 0.14, 1)
"""
    _write_file(root / "default_env.tres", default_env)
    created_files.append("godot/default_env.tres")

    # -- icon.svg (minimal placeholder) --
    icon_svg = """<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128">
  <rect width="128" height="128" rx="16" fill="#478cbf"/>
  <text x="64" y="76" font-size="48" font-family="sans-serif"
        text-anchor="middle" fill="white">G</text>
</svg>
"""
    _write_file(root / "icon.svg", icon_svg)
    created_files.append("godot/icon.svg")

    # -- scenes/main.tscn --
    main_tscn = f"""[gd_scene load_steps=2 format=3 uid="uid://main_scene"]

[ext_resource type="Script" path="res://scripts/main.gd" id="1"]

[node name="Main" type="Node2D"]
script = ExtResource("1")

[node name="Camera2D" type="Camera2D" parent="."]
position = Vector2({window_width // 2}, {window_height // 2})
"""
    _write_file(root / "scenes" / "main.tscn", main_tscn)
    created_files.append("godot/scenes/main.tscn")

    # -- scripts/main.gd --
    main_gd = f"""extends Node2D
## Main scene script for {project_name}.
## Entry point — manages top-level game flow.


func _ready() -> void:
\tprint("{project_name} — ready")


func _process(delta: float) -> void:
\tpass  # Per-frame logic here


func _unhandled_input(event: InputEvent) -> void:
\tif event.is_action_pressed("ui_cancel"):
\t\tget_tree().quit()
"""
    _write_file(root / "scripts" / "main.gd", main_gd)
    created_files.append("godot/scripts/main.gd")

    # -- scripts/autoload/game_manager.gd --
    game_manager_gd = f"""extends Node
## GameManager autoload — global game state and scene transitions.

signal scene_changed(scene_name: String)
signal game_paused(paused: bool)

enum GameState {{ MENU, PLAYING, PAUSED, GAME_OVER }}

var current_state: GameState = GameState.MENU
var score: int = 0
var player_data: Dictionary = {{}}


func _ready() -> void:
\tprocess_mode = Node.PROCESS_MODE_ALWAYS
\tprint("GameManager initialized")


func change_scene(scene_path: String) -> void:
\t\"\"\"Transition to a new scene by resource path.\"\"\"
\tvar err := get_tree().change_scene_to_file(scene_path)
\tif err == OK:
\t\tscene_changed.emit(scene_path.get_file().get_basename())
\telse:
\t\tpush_error("Failed to change scene to: " + scene_path)


func toggle_pause() -> void:
\t\"\"\"Toggle the game pause state.\"\"\"
\tvar paused := not get_tree().paused
\tget_tree().paused = paused
\tcurrent_state = GameState.PAUSED if paused else GameState.PLAYING
\tgame_paused.emit(paused)


func reset_game() -> void:
\t\"\"\"Reset all game state for a new run.\"\"\"
\tscore = 0
\tplayer_data.clear()
\tcurrent_state = GameState.MENU
"""
    _write_file(root / "scripts" / "autoload" / "game_manager.gd", game_manager_gd)
    created_files.append("godot/scripts/autoload/game_manager.gd")

    # -- scripts/autoload/audio_manager.gd --
    audio_manager_gd = """extends Node
## AudioManager autoload — handles SFX and music playback.

var _music_player: AudioStreamPlayer
var _sfx_players: Array[AudioStreamPlayer] = []
var _sfx_pool_size: int = 8

var music_volume_db: float = 0.0
var sfx_volume_db: float = 0.0


func _ready() -> void:
\tprocess_mode = Node.PROCESS_MODE_ALWAYS
\t_music_player = AudioStreamPlayer.new()
\t_music_player.bus = "Music"
\tadd_child(_music_player)
\t# Pre-allocate SFX player pool
\tfor i in _sfx_pool_size:
\t\tvar player := AudioStreamPlayer.new()
\t\tplayer.bus = "SFX"
\t\tadd_child(player)
\t\t_sfx_players.append(player)
\tprint("AudioManager initialized with %d SFX channels" % _sfx_pool_size)


func play_music(stream: AudioStream, fade_in: float = 0.5) -> void:
\t\"\"\"Play background music, cross-fading if something is already playing.\"\"\"
\t_music_player.stream = stream
\t_music_player.volume_db = music_volume_db
\t_music_player.play()


func stop_music(fade_out: float = 0.5) -> void:
\t\"\"\"Stop the current music track.\"\"\"
\t_music_player.stop()


func play_sfx(stream: AudioStream) -> void:
\t\"\"\"Play a one-shot sound effect using the next available pool slot.\"\"\"
\tfor player in _sfx_players:
\t\tif not player.playing:
\t\t\tplayer.stream = stream
\t\t\tplayer.volume_db = sfx_volume_db
\t\t\tplayer.play()
\t\t\treturn
\t# All channels busy — steal the first one
\t_sfx_players[0].stream = stream
\t_sfx_players[0].volume_db = sfx_volume_db
\t_sfx_players[0].play()
"""
    _write_file(root / "scripts" / "autoload" / "audio_manager.gd", audio_manager_gd)
    created_files.append("godot/scripts/autoload/audio_manager.gd")

    # -- export_presets.cfg (empty template) --
    _write_file(root / "export_presets.cfg", "")
    created_files.append("godot/export_presets.cfg")

    return created_files, created_dirs


def _init_pygame(
    project_name: str,
    window_width: int,
    window_height: int,
) -> tuple[list[str], list[str]]:
    """
    Create a Pygame project structure.
    Returns (created_files, created_dirs).
    """
    root = PROJECT_DIR / "pygame"
    created_files = []
    created_dirs = []

    # -- Directories --
    dirs = [
        root / "game" / "scenes",
        root / "game" / "entities",
        root / "game" / "assets" / "sprites",
        root / "game" / "assets" / "audio",
        root / "game" / "assets" / "ui",
        root / "tests",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        created_dirs.append(str(d.relative_to(PROJECT_DIR)))

    # -- settings.py --
    settings_py = f'''"""Game-wide constants and configuration for {project_name}."""

# Display
SCREEN_WIDTH: int = {window_width}
SCREEN_HEIGHT: int = {window_height}
FPS: int = 60
TITLE: str = "{project_name}"

# Colors (R, G, B)
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
RED = (220, 50, 50)
GREEN = (50, 200, 50)
BLUE = (50, 100, 220)
GRAY = (128, 128, 128)
DARK_GRAY = (40, 40, 44)
'''
    _write_file(root / "settings.py", settings_py)
    created_files.append("pygame/settings.py")

    # -- main.py --
    main_py = f'''"""
{project_name} — main entry point.
Initializes Pygame, runs the game loop, and handles clean shutdown.
"""

import sys
import pygame
from settings import SCREEN_WIDTH, SCREEN_HEIGHT, FPS, TITLE, DARK_GRAY
from game.manager import GameManager


def main() -> None:
    pygame.init()
    screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
    pygame.display.set_caption(TITLE)
    clock = pygame.time.Clock()
    manager = GameManager(screen)

    running = True
    while running:
        dt = clock.tick(FPS) / 1000.0  # delta time in seconds

        # --- Event handling ---
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False
            else:
                manager.handle_event(event)

        # --- Update ---
        manager.update(dt)

        # --- Draw ---
        screen.fill(DARK_GRAY)
        manager.draw(screen)
        pygame.display.flip()

    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()
'''
    _write_file(root / "main.py", main_py)
    created_files.append("pygame/main.py")

    # -- requirements.txt --
    _write_file(root / "requirements.txt", "pygame>=2.5.0\n")
    created_files.append("pygame/requirements.txt")

    # -- game/__init__.py --
    _write_file(root / "game" / "__init__.py", f'"""{project_name} game package."""\n')
    created_files.append("pygame/game/__init__.py")

    # -- game/manager.py --
    manager_py = f'''"""
GameManager — central game state and scene management for {project_name}.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

import pygame

if TYPE_CHECKING:
    from game.scenes.base_scene import BaseScene


class GameManager:
    """Manages the active scene and global game state."""

    def __init__(self, screen: pygame.Surface) -> None:
        self.screen = screen
        self.current_scene: BaseScene | None = None
        self.running: bool = True
        self.score: int = 0
        self.game_data: dict = {{}}

    def change_scene(self, scene: BaseScene) -> None:
        """Transition to a new scene."""
        if self.current_scene is not None:
            self.current_scene.on_exit()
        self.current_scene = scene
        self.current_scene.on_enter()

    def handle_event(self, event: pygame.event.Event) -> None:
        """Forward events to the active scene."""
        if self.current_scene is not None:
            self.current_scene.handle_event(event)

    def update(self, dt: float) -> None:
        """Update the active scene."""
        if self.current_scene is not None:
            self.current_scene.update(dt)

    def draw(self, screen: pygame.Surface) -> None:
        """Draw the active scene."""
        if self.current_scene is not None:
            self.current_scene.draw(screen)

    def reset(self) -> None:
        """Reset all game state for a new run."""
        self.score = 0
        self.game_data.clear()
'''
    _write_file(root / "game" / "manager.py", manager_py)
    created_files.append("pygame/game/manager.py")

    # -- game/scenes/__init__.py --
    _write_file(root / "game" / "scenes" / "__init__.py", '"""Scene modules."""\n')
    created_files.append("pygame/game/scenes/__init__.py")

    # -- game/scenes/base_scene.py --
    base_scene_py = '''"""
BaseScene — abstract base class for all game scenes.
Subclass this and override the lifecycle methods.
"""

from abc import ABC, abstractmethod
import pygame


class BaseScene(ABC):
    """Abstract scene with standard lifecycle hooks."""

    def on_enter(self) -> None:
        """Called when the scene becomes active."""

    def on_exit(self) -> None:
        """Called when the scene is being replaced."""

    @abstractmethod
    def handle_event(self, event: pygame.event.Event) -> None:
        """Process a single pygame event."""

    @abstractmethod
    def update(self, dt: float) -> None:
        """Update scene logic. dt is seconds since last frame."""

    @abstractmethod
    def draw(self, screen: pygame.Surface) -> None:
        """Render the scene to the screen surface."""
'''
    _write_file(root / "game" / "scenes" / "base_scene.py", base_scene_py)
    created_files.append("pygame/game/scenes/base_scene.py")

    # -- game/entities/__init__.py --
    _write_file(root / "game" / "entities" / "__init__.py", '"""Game entity modules."""\n')
    created_files.append("pygame/game/entities/__init__.py")

    # -- tests/__init__.py --
    _write_file(root / "tests" / "__init__.py", "")
    created_files.append("pygame/tests/__init__.py")

    # -- tests/conftest.py --
    conftest_py = f'''"""Pytest fixtures for {project_name}."""

import pytest
import pygame


@pytest.fixture(autouse=True)
def init_pygame():
    """Initialize and teardown Pygame for each test."""
    pygame.init()
    yield
    pygame.quit()


@pytest.fixture
def screen():
    """Provide a test display surface."""
    from settings import SCREEN_WIDTH, SCREEN_HEIGHT
    surface = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT))
    return surface
'''
    _write_file(root / "tests" / "conftest.py", conftest_py)
    created_files.append("pygame/tests/conftest.py")

    return created_files, created_dirs


def run(**kwargs) -> dict:
    """
    Initialize a game project directory structure.

    kwargs:
        target (str):          "godot" or "pygame". Default "godot".
        project_name (str):    Name for the project. Falls back to GDD title.
        window_width (int):    Viewport width. Default 1280.
        window_height (int):   Viewport height. Default 720.
    """
    GAMEDEV_DIR.mkdir(parents=True, exist_ok=True)

    target = kwargs.get("target", "godot")
    window_width = int(kwargs.get("window_width", 1280))
    window_height = int(kwargs.get("window_height", 720))

    if target not in ("godot", "pygame"):
        target = "godot"

    # Resolve project name: explicit kwarg > GDD title > default
    project_name = kwargs.get("project_name", "")
    if not project_name:
        gdd = _load_gdd()
        project_name = gdd.get("title", "")
    if not project_name:
        project_name = "Untitled Game"

    # Check if target directory already exists with files
    target_dir = PROJECT_DIR / target
    if target_dir.exists() and any(target_dir.rglob("*")):
        existing_count = sum(1 for _ in target_dir.rglob("*") if _.is_file())
        log.info("Target directory %s already has %d files — reinitializing.", target, existing_count)

    # Create the project structure
    if target == "godot":
        created_files, created_dirs = _init_godot(project_name, window_width, window_height)
    else:
        created_files, created_dirs = _init_pygame(project_name, window_width, window_height)

    # Update manifest
    manifest = _load_manifest()
    existing_paths = {fe.get("path") for fe in manifest.get("files", [])}
    for fpath in created_files:
        if fpath not in existing_paths:
            manifest.setdefault("files", []).append({
                "path": fpath,
                "target": target,
                "generated_by": "project-init",
            })
    manifest.setdefault("targets", {})[target] = True
    manifest["project_name"] = project_name
    manifest["initialized_at"] = datetime.now(timezone.utc).isoformat()
    _save_manifest(manifest)

    summary = (
        f"Initialized {target} project '{project_name}' — "
        f"{len(created_files)} file(s) and {len(created_dirs)} director(ies) created "
        f"at state/gamedev/project/{target}/."
    )

    return {
        "status": "success",
        "summary": summary,
        "metrics": {
            "files_created": len(created_files),
            "directories_created": len(created_dirs),
            "target": target,
            "project_name": project_name,
            "window_size": f"{window_width}x{window_height}",
        },
        "escalate": False,
        "escalation_reason": "",
        "action_items": [],
    }
