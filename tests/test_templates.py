"""Compile every template.

A Jinja syntax error only surfaces when its page is requested, so a broken
template can sit unnoticed behind a route no test happens to hit. Compiling
all of them turns that into an immediate, obvious failure.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jinja2 import TemplateSyntaxError

from app.deps import WEB_DIR, templates

ok = True
tpl_dir = WEB_DIR / "templates"
for path in sorted(tpl_dir.glob("*.html")):
    name = path.name
    try:
        templates.env.get_template(name)
        print(f"PASS  {name}")
    except TemplateSyntaxError as e:
        ok = False
        print(f"FAIL  {name}: line {e.lineno}: {e.message}")
    except Exception as e:
        ok = False
        print(f"FAIL  {name}: {type(e).__name__}: {e}")

print("\nRESULT:", "all templates compile" if ok else "TEMPLATE ERRORS")
sys.exit(0 if ok else 1)
