import sys
sys.path.insert(0, ".")
from ops import unique_preserving_order

assert unique_preserving_order(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
assert unique_preserving_order([]) == []
