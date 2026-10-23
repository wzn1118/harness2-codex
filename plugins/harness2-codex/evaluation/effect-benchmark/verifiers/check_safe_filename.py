import sys
sys.path.insert(0, ".")
from ops import safe_filename

assert safe_filename("../A report?.txt") == "A_report_.txt"
assert safe_filename("  .draft  ") == "draft"
assert safe_filename("///") == "file"
