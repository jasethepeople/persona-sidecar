import argparse
import logging

from .config import load_config
from .gateway import SidecarService


def main():
    parser = argparse.ArgumentParser(description="Loopback-only agent delivery and affect sidecar")
    parser.add_argument("--config", default="drives.config.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    SidecarService(config).serve()


if __name__ == "__main__":
    main()
