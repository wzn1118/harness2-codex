import sys
sys.path.insert(0, ".")
from ops import page_slice

items = list("abcdef")
assert page_slice(items, 1, 2) == ["a", "b"]
assert page_slice(items, 3, 2) == ["e", "f"]
for page, size in ((0, 2), (1, 0), (1, -1)):
    try:
        page_slice(items, page, size)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid page arguments must raise ValueError")
