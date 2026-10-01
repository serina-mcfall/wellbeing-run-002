"""C-02's accessibility requirement registry, reachable from Python.

WHAT THIS IS. protocol/SEVERITY-POLICY.md rule 1 says an accessibility
FAILURE must cite a stated requirement in product/ACCESSIBILITY.md.
control/severity.py::apply_severity_policy accepts such a citation only
against a registry the CALLER supplies, and without one every FAILURE
fails closed as INVALID. C-02 landed that registry as
apparatus/accessibility/requirement-registry.js - a JavaScript module
with no Python counterpart - so until now there was nothing for a Python
caller to supply, and C-05.3b's accessibility adjudication could never
rate a FAILURE as anything but INVALID. That is
C05-3a-SESSION-HANDOVER.md section 36.5 decision D2.

WHAT THIS IS NOT. It is not a second registry and it does not derive
anything. It READS the generated
apparatus/accessibility/requirement-registry.json, which is serialised
from the canonical JS module. Deriving the identifiers here - by parsing
product/ACCESSIBILITY.md a second time - would create exactly the
competing source of truth this avoids: two parsers that can disagree,
with no rule saying which wins.

    product/ACCESSIBILITY.md    imported, frozen, never edited
      -> requirement-registry.js        pinned by requirement-registry.test.js
        -> requirement-registry.json    pinned by requirement-registry-json.test.js
          -> THIS MODULE                reads it; derives nothing

It also changes NO severity policy. control/severity.py is unmodified -
it is a frozen-hash input (manifest.SEVERITY_POLICY_FILES) and editing
it would move a frozen hash. This module only supplies the argument that
function already takes.

READ ONCE, AT IMPORT. C05-3a-SESSION-HANDOVER.md section 31.10 requires
that the registry be reachable as an in-memory collection "without a
filesystem read at call time", because the read must sit in Phase A
observation and never inside the Supervisor's state transaction T1.
Loading at import satisfies that: by the time any tick runs, the
identifiers are already in memory and no call touches the disk.

FAILING LOUDLY IS THE POINT. If the artefact is missing or malformed,
this module raises at import rather than yielding an empty registry. An
empty registry would be fail-CLOSED at the gate - every citation
unrecognised, every FAILURE INVALID, nothing unsafe merged - but it
would be SILENTLY so, and a permanently-shut gate that nobody is told
about is indistinguishable from a working one until a task stalls for a
reason no diagnostic names. A missing generated artefact is a packaging
fault, not a runtime condition to absorb.
"""

from __future__ import annotations

import json
from pathlib import Path

from control import config

REGISTRY_FILE = (config.REPO_ROOT / "apparatus" / "accessibility"
                 / "requirement-registry.json")

# The identifier shape the canonical registry guarantees and its own test
# pins: a group prefix plus a SCREAMING_SNAKE semantic name. Checked here
# too, because this module is what the control plane trusts, and a shape
# check is the cheapest way to notice that the file being read is not the
# file that was meant.
_ID_PREFIXES = ("ACC-DOD-", "ACC-COG-")


class RegistryUnavailable(RuntimeError):
    """The generated registry artefact is missing, unreadable or malformed."""


def _load(path: Path) -> tuple[dict, ...]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryUnavailable(
            f"cannot read the accessibility requirement registry at {path}: "
            f"{exc}. Regenerate it with "
            f"`node apparatus/accessibility/generate-requirement-registry-json.js`."
        ) from exc

    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise RegistryUnavailable(
            f"the accessibility requirement registry at {path} is not valid "
            f"JSON: {exc}"
        ) from exc

    if not isinstance(doc, dict) or not isinstance(doc.get("requirements"), list):
        raise RegistryUnavailable(
            f"the accessibility requirement registry at {path} has no "
            f"'requirements' array"
        )

    entries: list[dict] = []
    seen: set[str] = set()
    for index, entry in enumerate(doc["requirements"]):
        if not isinstance(entry, dict):
            raise RegistryUnavailable(
                f"registry entry {index} is not an object")
        ident = entry.get("id")
        group = entry.get("group")
        phrase = entry.get("source_phrase")
        if not isinstance(ident, str) or not ident.startswith(_ID_PREFIXES):
            raise RegistryUnavailable(
                f"registry entry {index} has identifier {ident!r}, which is "
                f"not of the form ACC-DOD-* or ACC-COG-*")
        if ident in seen:
            raise RegistryUnavailable(
                f"duplicate requirement identifier {ident!r}: a registry that "
                f"names one requirement twice cannot be trusted to name them "
                f"all once")
        if not isinstance(group, str) or not isinstance(phrase, str):
            raise RegistryUnavailable(
                f"registry entry {ident!r} is missing a string group or "
                f"source_phrase")
        seen.add(ident)
        entries.append({"id": ident, "group": group, "source_phrase": phrase})

    if not entries:
        raise RegistryUnavailable(
            f"the accessibility requirement registry at {path} is empty; an "
            f"empty registry would shut the accessibility gate silently")

    return tuple(entries)


REQUIREMENTS: tuple[dict, ...] = _load(REGISTRY_FILE)

# A frozenset, so a caller cannot widen the gate at runtime by appending
# to it - the same property requirement-registry.js gets from
# Object.freeze. This is the value to pass as severity.apply_severity_policy's
# known_requirement_ids.
REQUIREMENT_IDS: frozenset[str] = frozenset(r["id"] for r in REQUIREMENTS)


def requirement_ids() -> frozenset[str]:
    """The canonical accessibility requirement identifiers.

    A function as well as a constant so a caller reads through one named
    entry point rather than reaching for module state, and so the
    docstring travels to the call site.
    """
    return REQUIREMENT_IDS


def prompt_vocabulary() -> str:
    """The canonical identifiers, rendered for the reviewer's prompt.

    WHY THIS IS NOT IN THE PROMPT FILE. C-02a amended the frozen
    prompts/accessibility.md so its one worked example cites a real
    identifier. That fixed the example; it did not tell the reviewer the
    other sixteen. The prompt sends the reviewer to
    product/ACCESSIBILITY.md, which contains the source PROSE ("visible
    focus"), not the canonical form - so a reviewer citing anything but
    the example would have to infer ACC-DOD-*/ACC-COG- from one sample,
    and a wrong inference is a CLASSIFICATION_INVALID hold.

    Widening the frozen file to list all seventeen would have exceeded
    the amendment that was authorised. This list goes into the
    {{evidence}} substitution the Supervisor already owns and already
    fills at dispatch, so:

      * no further frozen-file change, and no new prompt hash;
      * no parser alias - the registry stays the single source of truth
        for what a requirement is called, which is the whole point of
        C-02;
      * the vocabulary is RENDERED FROM THE REGISTRY, so it cannot drift
        from what severity.apply_severity_policy will accept. A list
        typed into a document could.

    Sorted, so the same registry always renders the same block and a
    prompt diff means a real change.
    """
    lines = ["### Canonical accessibility requirement identifiers",
             "",
             "`unmet_requirement` MUST be one of these exact strings. They are "
             "the identifiers derived from `product/ACCESSIBILITY.md`; a "
             "citation outside this list is refused as invalid evidence and "
             "your whole review is discarded, so do not invent, abbreviate or "
             "reword one. If nothing here fits what you found, say so in the "
             "finding's summary rather than inventing an identifier.",
             ""]
    for entry in sorted(REQUIREMENTS, key=lambda r: r["id"]):
        lines.append(f"- `{entry['id']}` — {entry['source_phrase']}")
    return "\n".join(lines)


def is_known_requirement(identifier) -> bool:
    """True iff `identifier` names a requirement in the frozen product spec.

    Recognising a citation is NOT confirming it: per the canonical
    registry's own scope limit, a match proves the reviewer named a real
    requirement, never that the requirement is actually unmet at the
    reviewed SHA. That remains the independent Accessibility Reviewer's
    judgement.

    Non-string input is False rather than an exception, because this
    answers a question about durable JSON, which can carry anything.
    """
    return isinstance(identifier, str) and identifier in REQUIREMENT_IDS
