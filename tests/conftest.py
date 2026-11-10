from pathlib import Path

import pytest
import yaml


@pytest.fixture
def cfg():
    return yaml.safe_load(Path("config/scoring.yaml").read_text())
