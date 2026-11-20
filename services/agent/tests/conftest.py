import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

CASES = Path(__file__).resolve().parents[3] / "contracts" / "examples" / "case_input"
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 19)


def load_case(name: str) -> dict:
    return copy.deepcopy(json.loads((CASES / f"{name}.json").read_text(encoding="utf-8")))


@pytest.fixture
def case_1042() -> dict:
    return load_case("case_1042")


@pytest.fixture
def case_2001() -> dict:
    return load_case("case_2001")


@pytest.fixture
def case_3001() -> dict:
    return load_case("case_3001")
