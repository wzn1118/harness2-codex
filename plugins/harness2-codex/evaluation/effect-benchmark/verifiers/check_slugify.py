import sys
sys.path.insert(0, ".")
from ops import slugify

assert slugify("Hello World!") == "hello-world"
assert slugify(" A---B  C ") == "a-b-c"
assert slugify("...") == ""
