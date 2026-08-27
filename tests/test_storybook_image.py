import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = PROJECT_ROOT / "sandbox" / "storybook-template"


def test_storybook_template_dependencies_are_exact_and_locked() -> None:
    package = json.loads((TEMPLATE_ROOT / "package.json").read_text())
    lock = json.loads((TEMPLATE_ROOT / "package-lock.json").read_text())
    dependencies = package["dependencies"] | package["devDependencies"]

    assert {
        "react",
        "react-dom",
        "vite",
        "typescript",
        "storybook",
        "@storybook/react-vite",
        "@storybook/addon-docs",
        "@storybook/addon-a11y",
        "lucide-react",
    } <= dependencies.keys()
    assert "tailwindcss" not in dependencies
    assert all(version[0].isdigit() for version in dependencies.values())
    assert lock["packages"][""]["dependencies"] == package["dependencies"]
    assert lock["packages"][""]["devDependencies"] == package["devDependencies"]


def test_storybook_packages_use_one_version() -> None:
    package = json.loads((TEMPLATE_ROOT / "package.json").read_text())
    storybook_versions = {
        version
        for name, version in package["devDependencies"].items()
        if name == "storybook" or name.startswith("@storybook/")
    }

    assert storybook_versions == {"10.5.4"}


def test_runtime_image_forces_npm_offline_and_disables_telemetry() -> None:
    dockerfile = (PROJECT_ROOT / "sandbox" / "Dockerfile").read_text()

    assert "npm ci --ignore-scripts --no-audit --no-fund" in dockerfile
    assert "npm_config_offline=true" in dockerfile
    assert "STORYBOOK_DISABLE_TELEMETRY=1" in dockerfile
    assert "npm run typecheck" in dockerfile
    assert "npm run build-storybook" in dockerfile
    assert "ln --symbolic /tmp node_modules/.cache" in dockerfile
