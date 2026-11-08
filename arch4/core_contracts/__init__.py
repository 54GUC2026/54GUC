"""Transport-neutral Core contract package.

The package initializer intentionally exports no executable contract graph.
Every caller must import the exact contract from its owning module.  Eager
barrel imports are forbidden here because they hide dependency ownership and
can create circular startup paths between command, observation, and accepted
root contracts.
"""

__all__: tuple[str, ...] = ()
