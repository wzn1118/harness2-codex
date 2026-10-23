import sys
sys.path.insert(0, ".")
from ops import net_price

assert net_price(100, .2) == 80
assert net_price(50, 0) == 50
assert net_price(10, 1) == 0
