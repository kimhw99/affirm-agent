"""The workers' tools: one case-service endpoint each.

Records worker: official and third-party data (R and K sources).
Evidence worker: merchant evidence (D, B and W sources).
Each worker is bound only to its own set; any other tool name is rejected.
"""

from . import evidence, registry, tin_kyc
from ._common import ToolContext, ToolOutput, ToolSpec

RECORDS_TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in registry.SPECS + tin_kyc.SPECS}
EVIDENCE_TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in evidence.SPECS}

TOOLS_FOR = {"records": RECORDS_TOOLS, "evidence": EVIDENCE_TOOLS}

__all__ = ["EVIDENCE_TOOLS", "RECORDS_TOOLS", "TOOLS_FOR", "ToolContext", "ToolOutput", "ToolSpec"]
