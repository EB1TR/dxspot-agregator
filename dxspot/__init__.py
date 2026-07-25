"""DXSpot Agregator."""

from .application import DXSpotAgregator
from .config import AppConfig, load_config

__all__ = ["AppConfig", "DXSpotAgregator", "load_config"]
