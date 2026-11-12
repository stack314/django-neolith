"""Sphinx extension for the Neolith site (ADR 0024).

- **Requirement anchors.** Each requirement's bold ID in a spec page,
  ``**CON-12.**``, gets the anchor ``con-12``, registered so that a Markdown
  link to ``01-contracts.md#con-12`` resolves. The IDs found must equal the
  IDs ``scripts/spec_trace.py`` reads from the same document.
- **Citations.** In every page and docstring, each citation that
  ``scripts/citations.py`` finds (``CON-12``, ``ADR 0017``, ``spec 01``)
  becomes a link, outside headings, existing links, code and signatures. A
  citation of an unknown or withdrawn requirement, record or document is a
  warning, so the build fails; one in a heading is checked but not linked.
- **Specified by, Decided in.** Each API entry that cites requirements, spec
  documents or records lists them after its text, with each record's status.
- **Record status.** A record whose status is not ``Accepted`` gets a note
  that only Accepted records bind, and so does the record index; a status
  outside the template's set is a warning.
- **Repository links.** While ``neolith_repo_public`` is false, a link into
  the repository on GitHub renders as its text, since it would not open.
- **Mirrored indexes.** The spec and record indexes are mirrored word for
  word, so their pages are added to the navigation here, not in the text.
- **Aliases.** An exported alias documented by a ``#:`` comment where it is
  defined, in a private module, takes that comment as its text.
- **``{planned}``.** A block for behaviour that does not work yet, naming the
  roadmap step that delivers it; a step the roadmap does not list is a
  warning.
"""

from __future__ import annotations

import functools
import re
import typing
from pathlib import Path
from typing import Any, ClassVar

from docutils import nodes
from docutils.parsers.rst import directives
from sphinx import addnodes
from sphinx.application import Sphinx
from sphinx.ext.autodoc import Options
from sphinx.pycode import ModuleAnalyzer
from sphinx.util import logging
from sphinx.util.docutils import SphinxDirective

from scripts import citations

_log = logging.getLogger(__name__)

_DEFINED = re.compile(r"([A-Z]+-\d+)\.")
_STATUS = re.compile(r"^- \*\*Status:\*\* (.+)$", re.MULTILINE)
_ADMITTED_STATUS = re.compile(r"Accepted|Proposed|Rejected|Superseded by .+")
_STEP = re.compile(r"^\| (\d+) \|", re.MULTILINE)
# Text under these is code or markup: neither checked nor linked.
_CODE = (
    nodes.literal,
    nodes.FixedTextElement,
    nodes.raw,
    nodes.comment,
    nodes.math,
)
# Text under these is checked, not linked.
_NOT_LINKED = (
    nodes.reference,
    nodes.title,
    nodes.target,
    addnodes.desc_signature,
    addnodes.pending_xref,
    addnodes.toctree,
)
_REQUIREMENT_CLASS = "neolith-requirement"


class _Site:
    """What the extension reads from the repository, once per build."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.records = citations.Records(root)
        self.status: dict[str, str] = {}
        for number, stem in self.records.adr_stems.items():
            text = (root / "docs" / "adr" / f"{stem}.md").read_text(encoding="utf-8")
            found = _STATUS.search(text)
            self.status[number] = found[1].strip() if found else ""
        roadmap = (root / "docs" / "roadmap.md").read_text(encoding="utf-8")
        self.steps = set(_STEP.findall(roadmap))

    def docname(self, citation: citations.Citation) -> tuple[str, str]:
        """The page a citation names, and the anchor on it."""
        if citation.kind == citations.REQUIREMENT:
            file = self.records.requirement_files[citation.key]
            return f"spec/{file[: -len('.md')]}", citation.key.lower()
        if citation.kind == citations.RECORD:
            return f"adr/{self.records.adr_stems[citation.key]}", ""
        return f"spec/{self.records.document_stems[citation.key]}", ""


def _site(app: Sphinx) -> _Site:
    return _site_at(app.config.neolith_root)


@functools.cache
def _site_at(root: str) -> _Site:
    return _Site(Path(root))


# Source: navigation for the mirrored indexes, and the record status notes.


def _after_title(text: str, note: str) -> str:
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.startswith("# "):
            return "\n".join([*lines[: index + 1], "", note, *lines[index + 1 :]])
    return note + "\n\n" + text


def _source_read(app: Sphinx, docname: str, source: list[str]) -> None:
    site = _site(app)
    if docname == "spec/index":
        stems = sorted(site.records.document_stems.values())
        source[0] += "\n```{toctree}\n:hidden:\n\n" + "\n".join(stems) + "\n```\n"
    elif docname == "adr/index":
        stems = [site.records.adr_stems[n] for n in sorted(site.records.adr_stems)]
        source[0] = _after_title(
            source[0],
            "```{note}\nOnly records with the status Accepted bind. A Proposed "
            "record is a working assumption.\n```",
        )
        source[0] += "\n```{toctree}\n:hidden:\n\n" + "\n".join(stems) + "\n```\n"
    elif docname.startswith("adr/"):
        number = docname[len("adr/") : len("adr/") + 4]
        status = site.status.get(number, "")
        if not _ADMITTED_STATUS.fullmatch(status):
            _log.warning(
                "record %s: status %r is not one the template admits",
                number,
                status,
                location=docname,
            )
        elif status != "Accepted":
            source[0] = _after_title(
                source[0],
                f"```{{note}}\nThis record's status is {status}. Only records "
                "with the status Accepted bind.\n```",
            )


# Doctree: requirement anchors, and aliases without their value.


def _drop_alias_values(doctree: nodes.document) -> None:
    """Remove autodoc's "alias of" line from each exported ``Annotated`` alias.

    The value of ``neolith.Binary`` and its kin names the private helpers
    that read and write the form; the ``#:`` comment says what it is.
    """
    import neolith

    for desc in doctree.findall(addnodes.desc):
        if desc.get("domain") != "py" or desc.get("objtype") != "data":
            continue
        names = [
            ident.removeprefix("neolith.")
            for signature in desc.findall(addnodes.desc_signature)
            for ident in signature["ids"]
        ]
        if not any(
            name in neolith.__all__
            and typing.get_origin(getattr(neolith, name)) is typing.Annotated
            for name in names
        ):
            continue
        for content in desc.findall(addnodes.desc_content):
            for paragraph in list(content.children):
                if isinstance(paragraph, nodes.paragraph) and (
                    paragraph.astext().startswith("alias of ")
                ):
                    content.remove(paragraph)


def _public_references(doctree: nodes.document) -> None:
    """Point a reference to ``neolith._module.Name`` at ``neolith.Name``.

    An annotation names a class by the private module that defines it; the
    reference documents it under its public name. A private name the package
    does not export stays as it is, so the nitpicky build refuses it.
    """
    import importlib

    import neolith

    for xref in doctree.findall(addnodes.pending_xref):
        target = xref.get("reftarget", "")
        if xref.get("refdomain") != "py" or not target.startswith("neolith._"):
            continue
        module, _, name = target.rpartition(".")
        try:
            found = getattr(importlib.import_module(module), name)
        except (ImportError, AttributeError):
            continue
        if name in neolith.__all__ and getattr(neolith, name) is found:
            xref["reftarget"] = f"neolith.{name}"


def _doctree_read(app: Sphinx, doctree: nodes.document) -> None:
    docname = app.env.docname
    _drop_alias_values(doctree)
    _public_references(doctree)
    if not docname.startswith("spec/") or docname == "spec/index":
        return
    site = _site(app)
    std = app.env.domaindata["std"]
    found = []
    for strong in doctree.findall(nodes.strong):
        defined = _DEFINED.fullmatch(strong.astext())
        if not defined:
            continue
        rid = defined[1]
        anchor = rid.lower()
        found.append(rid)
        strong["ids"].append(anchor)
        strong["classes"].append(_REQUIREMENT_CLASS)
        doctree.ids[anchor] = strong
        std["anonlabels"][anchor] = (docname, anchor)
        std["labels"][anchor] = (docname, anchor, rid)
    file = docname[len("spec/") :] + ".md"
    expected = [
        rid for rid, name in site.records.requirement_files.items() if name == file
    ]
    if sorted(found) != sorted(expected):
        _log.warning(
            "requirement anchors %s differ from the IDs spec_trace reads, %s",
            sorted(found),
            sorted(expected),
            location=docname,
        )


# Resolved doctree: citations, and repository links while private.


def _inside(node: nodes.Node, kinds: tuple[type, ...]) -> bool:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, kinds):
            return True
        if isinstance(parent, nodes.strong) and _REQUIREMENT_CLASS in parent.get(
            "classes", []
        ):
            return True
        parent = parent.parent
    return False


def _link_citations(app: Sphinx, doctree: nodes.document, docname: str) -> None:
    site = _site(app)
    builder = app.builder
    mirrored = docname in app.config.neolith_mirrored
    for text in list(doctree.findall(nodes.Text)):
        if _inside(text, _CODE):
            continue
        found = sorted(citations.find(str(text)), key=lambda c: c.start)
        if not found:
            continue
        if _inside(text, (nodes.reference,)):
            # Already a link: its target, not its words, is what is checked.
            continue
        good = []
        for citation in found:
            if citation.kind == citations.REQUIREMENT and (
                citation.key in citations.NOT_IDS
                or (mirrored and citation.key in site.records.withdrawn)
            ):
                # A mirrored document may name a withdrawn requirement, as
                # its history; the link leads to the struck-through text.
                if citation.key in site.records.withdrawn:
                    good.append(citation)
                continue
            problem = site.records.problem(citation)
            if problem is None:
                good.append(citation)
            else:
                _log.warning("%s", problem, location=text.parent)
        if not good or _inside(text, _NOT_LINKED):
            continue
        source = str(text)
        pieces: list[nodes.Node] = []
        at = 0
        for citation in good:
            if citation.start < at:
                continue
            pieces.append(nodes.Text(source[at : citation.start]))
            target, anchor = site.docname(citation)
            uri = builder.get_relative_uri(docname, target)
            if anchor:
                uri += "#" + anchor
            words = source[citation.start : citation.end]
            pieces.append(nodes.reference(words, words, refuri=uri, internal=True))
            at = citation.end
        pieces.append(nodes.Text(source[at:]))
        text.parent.replace(text, [piece for piece in pieces if piece.astext()])


def _plain_repository_links(app: Sphinx, doctree: nodes.document) -> None:
    if app.config.neolith_repo_public:
        return
    prefix = app.config.neolith_repo_url.rstrip("/") + "/"
    for reference in list(doctree.findall(nodes.reference)):
        if reference.get("refuri", "").startswith(prefix):
            reference.replace_self(nodes.inline("", "", *reference.children))


def _doctree_resolved(app: Sphinx, doctree: nodes.document, docname: str) -> None:
    _plain_repository_links(app, doctree)
    _link_citations(app, doctree, docname)


# Autodoc: aliases' comments, and "Specified by" and "Decided in".


def _alias_comment(name: str, found: object) -> list[str] | None:
    """The ``#:`` comment of the private module that defines ``found``."""
    import neolith

    package = Path(neolith.__file__).parent
    for path in sorted(package.glob("_*.py")):
        module = f"neolith.{path.stem}"
        analyzer = ModuleAnalyzer.for_module(module)
        docs = analyzer.find_attr_docs()
        imported = __import__(module, fromlist=[name])
        if ("", name) in docs and getattr(imported, name, None) is found:
            return list(docs[("", name)])
    return None


def _process_docstring(
    app: Sphinx,
    what: str,
    name: str,
    obj: object,
    options: Options,
    lines: list[str],
) -> None:
    if what == "data" and name.startswith("neolith."):
        comment = _alias_comment(name.rpartition(".")[2], obj)
        if comment is None:
            _log.warning("%s: no #: comment where it is defined", name)
        else:
            lines[:] = comment
    site = _site(app)
    specified: list[str] = []
    decided: list[str] = []
    for citation in citations.find("\n".join(lines)):
        if site.records.problem(citation) is not None:
            continue
        if citation.kind == citations.RECORD:
            entry = f"ADR {citation.key} ({site.status.get(citation.key, '')})"
            if entry not in decided:
                decided.append(entry)
        elif citation.key not in citations.NOT_IDS:
            entry = (
                f"spec {citation.key}"
                if citation.kind == citations.DOCUMENT
                else citation.key
            )
            if entry not in specified:
                specified.append(entry)
    if specified or decided:
        lines.append("")
        if specified:
            lines.append(":Specified by: " + ", ".join(specified))
        if decided:
            lines.append(":Decided in: " + ", ".join(decided))


# The {planned} block.


class Planned(SphinxDirective):
    """Behaviour that does not work yet, with the roadmap step that delivers it."""

    required_arguments = 1
    has_content = True
    option_spec: ClassVar[dict[str, Any]] = {"class": directives.class_option}

    def run(self) -> list[nodes.Node]:
        """Build the block; warn if the roadmap lists no such step."""
        step = self.arguments[0]
        if step not in _site_at(self.config.neolith_root).steps:
            _log.warning(
                "{planned} names roadmap step %s, which the roadmap does not list",
                step,
                location=self.get_location(),
            )
        block = nodes.admonition(classes=["planned"])
        block += nodes.title("", f"Planned: roadmap step {step}")
        self.state.nested_parse(self.content, self.content_offset, block)
        return [block]


def _page_context(
    app: Sphinx,
    pagename: str,
    templatename: str,
    context: dict[str, Any],
    doctree: nodes.document | None,
) -> None:
    if app.config.neolith_development:
        context["metatags"] = (
            context.get("metatags", "")
            + '\n    <meta name="robots" content="noindex, nofollow">'
        )


def setup(app: Sphinx) -> dict[str, Any]:
    """Register the extension."""
    app.add_config_value("neolith_root", "", "env", str)
    app.add_config_value("neolith_repo_url", "", "env", str)
    app.add_config_value("neolith_repo_public", False, "env", bool)
    app.add_config_value("neolith_development", True, "env", bool)
    app.add_config_value("neolith_mirrored", (), "env", tuple)
    app.add_directive("planned", Planned)
    app.connect("source-read", _source_read)
    app.connect("doctree-read", _doctree_read)
    app.connect("doctree-resolved", _doctree_resolved)
    app.connect("autodoc-process-docstring", _process_docstring)
    app.connect("html-page-context", _page_context)
    return {"version": "1", "parallel_read_safe": False, "parallel_write_safe": True}
