"""C-UAS C2 reference node (c2-core).

A minimal, runnable C2 node that demonstrates the government-owned interfaces:
it builds the common operating picture from the pub/sub track stream, tasks
sensors, and orders engagements subject to authority/ROE gates. It is a reference
scaffold proving the interfaces, not a fielded weapons system.
"""

__all__ = ["SCHEMA_VERSION"]

SCHEMA_VERSION = "1.0.0"
