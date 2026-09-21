"""Memory pipeline package: L0-L3 automatic memory extraction.

L0 (capture)   - conversation messages buffered per-turn
L1 (extract)   - typed atomic memories extracted via single LLM call
L2 (scene)     - narrative scene blocks consolidating L1 memories
L3 (persona)   - auto-generated user portrait

Design adapted from TencentDB Agent Memory 2.0.
"""
