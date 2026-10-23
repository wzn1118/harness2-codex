import sys
sys.path.insert(0, ".")
from ops import parse_int_list

assert parse_int_list("1, 2; -3") == [1, 2, -3]
assert parse_int_list(" 4 ") == [4]
for value in ("1,,2", "1; ;2", "1,x"):
    try:
        parse_int_list(value)
    except ValueError:
        pass
    else:
        raise AssertionError("empty or invalid integer fields must raise ValueError")
