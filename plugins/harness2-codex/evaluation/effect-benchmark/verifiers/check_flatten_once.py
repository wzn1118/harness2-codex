import sys
sys.path.insert(0, ".")
from ops import flatten_once

assert flatten_once([1, [2, 3], 4]) == [1, 2, 3, 4]
assert flatten_once([1, [2, [3]], (4, 5)]) == [1, 2, [3], (4, 5)]
