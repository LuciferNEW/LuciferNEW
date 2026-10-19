"""Map/mode pairs recovered from the exact PH client configuration."""
import hashlib
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))


def load_other_ai_settings():
    registry = json.loads((BASE / 'client-modes.json').read_text(encoding='utf-8'))
    return registry.get('other_ai_settings', {'enabled': False})


def load_native_large_ai_settings():
    registry=json.loads((BASE/'client-modes.json').read_text('utf-8'))
    return registry.get('native_large_ai_settings',{'enabled':False})


def load_relaxed_ai_settings():
    registry=json.loads((BASE/'client-modes.json').read_text('utf-8'))
    return registry.get('relaxed_ai_settings',{'enabled':False})


def load_hero_mode_settings():
    registry=json.loads((BASE/'client-modes.json').read_text('utf-8'))
    return registry.get('hero_mode_settings',{'enabled':False})


def load_modes():
    registry = json.loads((BASE / 'client-modes.json').read_text(encoding='utf-8'))
    return {int(mode): {int(entry['id']): entry for entry in info['maps']}
            for mode, info in registry['modes'].items()}
