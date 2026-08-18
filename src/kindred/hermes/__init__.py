"""Hermes-specific retained integration assets."""

from pathlib import Path

HERMES_MOUTH_PLUGIN_DIR = Path(__file__).parent / "mouth_plugin"

__all__ = ["HERMES_MOUTH_PLUGIN_DIR"]
