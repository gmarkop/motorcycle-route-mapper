import sys
from pathlib import Path

# Make the package importable without installing it, so `pytest` works straight
# from a fresh clone.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture
def data_dir() -> Path:
    return DATA_DIR


@pytest.fixture
def read_fixture():
    def _read(name: str) -> bytes:
        return (DATA_DIR / name).read_bytes()
    return _read
