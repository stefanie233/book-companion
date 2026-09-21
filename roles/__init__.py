"""Role-play kernel package (cache RETIRED, avatars only).

Role-play kernels are generated as a pure-function projection of the
graph via ``bookgraph.formatter.format_role_kernel`` - no LLM
pre-translation, no on-disk cache. The kernel refreshes with the graph
each turn and lives in the system prompt static region for prefix
caching.

The legacy ``bookgraph.role_card`` pipeline (LLM-generated
``graph_{book_id}_{name}_{mode}.md`` caches) was removed in f9743b3;
the stale files were purged from this directory afterwards. Only
``avatars/`` (character portrait images, still consumed by the reader
frontend) remains.
"""
