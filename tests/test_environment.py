def test_core_dependencies_import() -> None:
    import docker
    import ollama
    import playwright
    import pydantic
    import pydantic_settings
    import typer

    assert all((docker, ollama, playwright, pydantic, pydantic_settings, typer))
