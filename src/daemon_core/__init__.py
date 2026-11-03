__all__ = ["DaemonCore"]


def __getattr__(name: str):
    if name == "DaemonCore":
        from .loop import DaemonCore

        return DaemonCore
    raise AttributeError(name)
