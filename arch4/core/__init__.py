"""Core package. Modules are imported from their owner files. Importing this package does
not load the composition root, because importing a small contract must not start the
command stack.
"""

__all__: tuple[str, ...] = ()
