"""New Horizon namespace. Import executable owners from their exact modules.

Read-only History consumers must not import live engines or modem catalogues
as a side effect. Production callers already import the owning modules.
"""

__all__: tuple[str, ...] = ()
