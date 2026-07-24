"""Reserved fixed entrypoint for the later allowlisted analyzer profile."""


def main() -> None:
    """Fail closed until the analyzer egress boundary is implemented."""

    raise RuntimeError("analyzer sandbox jobs are not implemented")


if __name__ == "__main__":
    main()
