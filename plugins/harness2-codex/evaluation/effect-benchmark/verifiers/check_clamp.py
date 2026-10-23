import sys
sys.path.insert(0, ".")
from ops import clamp

assert clamp(5, 0, 10) == 5
assert clamp(-1, 0, 10) == 0
assert clamp(11, 0, 10) == 10
try:
    clamp(1, 3, 2)
except ValueError:
    pass
else:
    raise AssertionError("reversed bounds must raise ValueError")
