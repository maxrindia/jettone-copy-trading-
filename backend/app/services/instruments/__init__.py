"""
Canonical instrument identity and cross-broker comparison (R4a).

Pure and deterministic: nothing here calls a broker, Redis, the database or the network, and nothing in the engine
imports it yet. CASH equity only; derivatives are explicitly unmapped until their metadata fields are confirmed.
"""
