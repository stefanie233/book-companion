"""BookGraph — knowledge graph extraction for books.

Explorer + Recorder + Orchestrator + ContextSetter, per
BookGraph-Loop-Engineering-Plan-v3.
"""

from bookgraph.orchestrator import GraphOrchestrator, ExtractionResult
from bookgraph.context_setter import run_context_setter

__all__ = [
    "GraphOrchestrator",
    "ExtractionResult",
    "run_context_setter",
]
