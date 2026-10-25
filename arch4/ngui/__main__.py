from multiprocessing import freeze_support

from .application import launch


def main() -> int:
    freeze_support()
    return int(launch())


if __name__ == "__main__":
    raise SystemExit(main())
