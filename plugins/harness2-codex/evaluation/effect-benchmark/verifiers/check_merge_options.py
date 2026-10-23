import sys
sys.path.insert(0, ".")
from ops import merge_options

defaults = {"timeout": 10, "mode": "safe"}
overrides = {"timeout": 20}
merged = merge_options(defaults, overrides)
assert merged == {"timeout": 20, "mode": "safe"}
assert defaults == {"timeout": 10, "mode": "safe"}
assert overrides == {"timeout": 20}
assert merged is not defaults
