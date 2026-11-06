"""Describe the supervisor package without enabling it."""
import argparse


def main():
    parser = argparse.ArgumentParser(description="RealStop supervisor primitives (experimental; not enabled automatically).")
    parser.parse_args()
    parser.print_help()


if __name__ == "__main__":
    main()
