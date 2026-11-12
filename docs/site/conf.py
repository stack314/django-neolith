"""Sphinx configuration for the Neolith site (ADR 0024).

Build it with ``python scripts/docs.py build``, which stages the source tree
first; this file is read from ``docs/site/``, the pages from the staged tree.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# The source being documented, the build scripts, and the extension.
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(Path(__file__).parent / "_ext")]

import neolith  # noqa: E402
from scripts import docs  # noqa: E402

project = "Neolith"
copyright = "django-neolith contributors"
version = release = neolith.__version__
development = ".dev" in neolith.__version__

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "neolith_docs",
]
root_doc = "index"
templates_path = ["_templates"]

# Markdown: GitHub's heading anchors, and the strikethrough of withdrawn
# requirements.
myst_heading_anchors = 6
myst_enable_extensions = ["strikethrough"]
# Strikethrough renders only in HTML, which is the only output.
# A mirrored sketch may hold placeholders that a lexer cannot read, such as
# "<Contract>"; it is then shown without highlighting.
suppress_warnings = ["myst.strikethrough", "misc.highlighting_failure"]

# Every reference must resolve (-n), and every warning fails the build (-W).
nitpicky = True
nitpick_ignore = [("py:class", "T"), ("py:class", "F")]

autodoc_typehints = "signature"
autodoc_class_signature = "separated"
autodoc_typehints_format = "short"
autodoc_member_order = "bysource"
autodoc_preserve_defaults = True
python_use_unqualified_type_names = True
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_rtype = False

intersphinx_mapping = {
    name: (base, str(docs.CACHE / f"{name}.inv"))
    for name, (base, _) in docs.INVENTORIES.items()
}
intersphinx_timeout = 30

html_theme = "pydata_sphinx_theme"
html_title = "Neolith"
html_theme_options = {
    "announcement": docs.BANNER if development else "",
    "footer_start": ["neolith-footer"],
    "footer_center": [],
    "footer_end": [],
    "navbar_end": ["theme-switcher"],
    "icon_links": [],
    "use_edit_page_button": False,
    "show_prev_next": False,
    "navigation_with_keys": False,
}
html_show_sourcelink = False
html_copy_source = False
html_show_sphinx = False
html_show_copyright = False
html_use_index = False
html_domain_indices = False
html_static_path: list[str] = []

neolith_root = str(ROOT)
neolith_repo_url = docs.repo_url(ROOT)
neolith_repo_public = False
neolith_development = development
neolith_mirrored = tuple(
    staged.removesuffix(".md") for staged in docs.mirror_table(ROOT).values()
)
