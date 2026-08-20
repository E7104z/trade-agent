"""Extract the mascot's inline JS to a temp file for node --check."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.mascot import _SNIPPET, mascot_embed  # noqa: E402

scripts = re.findall(r"<script>(.*?)</script>", _SNIPPET, re.S)
out = os.path.join(os.environ["TEMP"], "arby_check.js")
with open(out, "w", encoding="utf-8") as f:
    f.write(scripts[-1])
print("extracted", len(scripts[-1]), "chars ->", out)

# also sanity-check boot embedding escapes </script> correctly
sample = mascot_embed({"playbook_bullets": ["a</script><b>x"], "overall": {}})
assert "</script><b>" not in sample.split("<script src")[0].replace("<\\/script>", "")
print("boot embed escape OK")
