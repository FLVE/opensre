"""Evidence source type — a vendor/integration key identifying a data source.

Not a closed enum: each ``integrations/<vendor>/`` package owns its own
source string(s) (the value passed as ``source=`` when registering a tool).
Core only declares the type alias; it must not hardcode the set of vendors.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

EvidenceSource = str

#: Lifts a tool's raw dict output into the canonical report keys the evidence
#: catalog cites. Called as ``mapper(evidence, output, tool_input)`` and mutates
#: ``evidence`` in place. Declared per tool (``@tool(evidence_mapper=...)``) so a
#: vendor's mapping lives with the vendor's tool, never in a shared stage file.
EvidenceMapper = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], None]

#: Key under which mappers accumulate citeable report entries in the evidence dict.
CATALOG_ENTRIES_KEY = "catalog_entries"


def record_evidence_entry(
    evidence: dict[str, Any],
    *,
    source: str,
    label: str,
    summary: str | None = None,
    url: str | None = None,
    snippet: str | None = None,
    key: str | None = None,
) -> None:
    """Record a citeable report entry from inside an evidence mapper.

    The report's evidence catalog turns each entry into a display id (``E1`` …)
    the agent can cite. ``source`` is the claim-facing key; a bespoke catalog
    reader for the same source takes precedence. Lets a tool's output become
    citeable without editing the shared catalog builder.

    ``key`` distinguishes repeated calls to one tool that produce genuinely
    separate evidence — a metrics tool queried once per metric, say. Entries
    sharing a ``(source, key)`` collapse to the first; without a ``key`` the
    whole ``source`` collapses to one entry, which is right for tools that
    yield a single body of evidence per investigation.
    """
    entries = evidence.setdefault(CATALOG_ENTRIES_KEY, [])
    if not isinstance(entries, list):
        return
    entry: dict[str, Any] = {
        "source": source,
        "label": label,
        "summary": summary,
        "url": url,
        "snippet": snippet,
    }
    # Only when supplied, so callers that record one body of evidence per
    # investigation keep the entry shape they already assert on.
    if key:
        entry["key"] = key
    entries.append(entry)
