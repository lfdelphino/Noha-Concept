#!/usr/bin/env python3
"""Create the same small, credential-free source asset locally and in GitHub Actions."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tomllib
from zipfile import ZipFile, ZIP_DEFLATED


def build(root, output, tag=None, requires_installer=False):
    package_version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", package_version) or (tag and tag != "v" + package_version):
        raise ValueError("O tag precisa ser v<versão de pyproject.toml>, por exemplo v1.1.0")
    # 1.2.0 introduces the privileged helper, ngrok and Avahi system installation.
    requires_installer = requires_installer or package_version == "1.2.0"
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for directory in ("noha", "config", "scripts", "systemd", "tests"):
            for path in sorted((root / directory).rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}:
                    if path.is_symlink():
                        raise ValueError("Não inclua links simbólicos no release")
                    archive.write(path, "noha-pi/" + path.relative_to(root).as_posix())
        for name in ("README.md", "pyproject.toml", ".gitignore"):
            archive.write(root / name, "noha-pi/" + name)
        archive.writestr("noha-pi/release.json", json.dumps({
            "format": 1, "version": package_version, "requires_installer": requires_installer}))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag")
    parser.add_argument("--output", type=Path, default=Path("dist/noha-pi.zip"))
    parser.add_argument("--requires-installer", action="store_true")
    args = parser.parse_args()
    print(build(Path(__file__).resolve().parent.parent, args.output, args.tag, args.requires_installer))


if __name__ == "__main__":
    main()
