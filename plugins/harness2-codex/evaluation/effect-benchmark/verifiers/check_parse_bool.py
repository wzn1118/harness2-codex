import sys
sys.path.insert(0, ".")
from ops import parse_bool

assert parse_bool(True) is True
assert parse_bool(False) is False
for value in ("true", " YES ", "1"):
    assert parse_bool(value) is True
for value in ("false", " No ", "0"):
    assert parse_bool(value) is False
for value in ("maybe", ""):
    try:
        parse_bool(value)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid boolean input must raise ValueError")
