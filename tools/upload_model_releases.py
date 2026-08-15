#!/usr/bin/env python3
"""Upload Fukuhedrons model packages and previews to GitHub Releases."""

from __future__ import annotations

import argparse
import getpass
import json
import mimetypes
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


DEFAULT_REPOSITORY = "sharkzilla666/Fukuhedrons"
DEFAULT_SOURCE = Path("/Volumes/UE5 1/Fukuhedrons/production/release")
BATCH_SIZE = 400


def github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token

    result = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        capture_output=True,
        check=False,
    )
    values = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line
    )
    token = values.get("password")
    if not token:
        token = getpass.getpass("GitHub token: ").strip()
    if not token:
        raise RuntimeError("A GitHub token is required")
    return token


class GitHub:
    def __init__(self, repository: str, token: str) -> None:
        self.repository = repository
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fukuhedrons-model-uploader",
        }

    def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> dict | list:
        headers = dict(self.headers)
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API returned {error.code}: {detail}") from error

    def release(self, tag: str, start: int, end: int) -> dict:
        encoded_tag = urllib.parse.quote(tag, safe="")
        url = (
            f"https://api.github.com/repos/{self.repository}/releases/tags/"
            f"{encoded_tag}"
        )
        try:
            result = self.request("GET", url)
            assert isinstance(result, dict)
            return result
        except RuntimeError as error:
            if "returned 404" not in str(error):
                raise

        payload = json.dumps(
            {
                "tag_name": tag,
                "name": f"Fukuhedrons 3D Models {start:05d}–{end:05d}",
                "body": (
                    "Community-created model packages and comparison previews "
                    f"for Fukuhedrons {start:05d} through {end:05d}."
                ),
                "draft": False,
                "prerelease": False,
            }
        ).encode()
        result = self.request(
            "POST",
            f"https://api.github.com/repos/{self.repository}/releases",
            data=payload,
            content_type="application/json",
        )
        assert isinstance(result, dict)
        return result

    def assets(self, release_id: int) -> dict[str, dict]:
        result: dict[str, dict] = {}
        page = 1
        while True:
            url = (
                f"https://api.github.com/repos/{self.repository}/releases/"
                f"{release_id}/assets?per_page=100&page={page}"
            )
            rows = self.request("GET", url)
            assert isinstance(rows, list)
            for row in rows:
                result[row["name"]] = row
            if len(rows) < 100:
                return result
            page += 1

    def upload(self, release_id: int, path: Path, name: str) -> dict:
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        url = (
            f"https://uploads.github.com/repos/{self.repository}/releases/"
            f"{release_id}/assets?name={urllib.parse.quote(name)}"
        )
        result = self.request(
            "POST", url, data=path.read_bytes(), content_type=content_type
        )
        assert isinstance(result, dict)
        return result


def release_range(token: int) -> tuple[int, int]:
    start = ((token - 1) // BATCH_SIZE) * BATCH_SIZE + 1
    return start, start + BATCH_SIZE - 1


def package_token(source: Path, token: int, destination: Path) -> tuple[Path, Path]:
    number = f"{token:05d}"
    token_dir = source / number
    blend = token_dir / f"{number}.blend"
    preview = token_dir / f"{number}.comparison.png"
    for path in (blend, preview):
        if not path.is_file():
            raise FileNotFoundError(path)

    archive = destination / f"{number}.zip"
    with zipfile.ZipFile(
        archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as output:
        output.write(blend, f"{number}/{blend.name}")
        output.write(preview, f"{number}/{preview.name}")
    return archive, preview


def load_manifest(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"schema_version": 1, "tokens": {}}


def save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument(
        "--manifest", type=Path, default=Path("3d-models/manifest.json")
    )
    parser.add_argument("--pause", type=float, default=0.5)
    args = parser.parse_args()

    if args.start < 1 or args.end < args.start:
        parser.error("expected 1 <= start <= end")

    github = GitHub(args.repository, github_token())
    manifest = load_manifest(args.manifest)
    current_tag = None
    release = None
    assets: dict[str, dict] = {}

    with tempfile.TemporaryDirectory(prefix="fukuhedrons-release-") as temp:
        temp_dir = Path(temp)
        for token in range(args.start, args.end + 1):
            number = f"{token:05d}"
            start, end = release_range(token)
            tag = f"models-{start:05d}-{end:05d}"
            if tag != current_tag:
                release = github.release(tag, start, end)
                assets = github.assets(release["id"])
                current_tag = tag

            archive_name = f"{number}.zip"
            preview_name = f"{number}.comparison.png"
            archive, preview = package_token(args.source, token, temp_dir)

            if archive_name not in assets:
                print(f"Uploading {archive_name}", flush=True)
                assets[archive_name] = github.upload(
                    release["id"], archive, archive_name
                )
                time.sleep(args.pause)
            else:
                print(f"Skipping existing {archive_name}", flush=True)

            if preview_name not in assets:
                print(f"Uploading {preview_name}", flush=True)
                assets[preview_name] = github.upload(
                    release["id"], preview, preview_name
                )
                time.sleep(args.pause)
            else:
                print(f"Skipping existing {preview_name}", flush=True)

            manifest["tokens"][number] = {
                "release": tag,
                "preview_url": assets[preview_name]["browser_download_url"],
                "download_url": assets[archive_name]["browser_download_url"],
            }
            save_manifest(args.manifest, manifest)
            archive.unlink()


if __name__ == "__main__":
    main()
