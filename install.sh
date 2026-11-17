#!/bin/bash
set -euo pipefail

case "$(uname -s)" in Darwin | Linux) ;; *) exit 0 ;; esac

src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
    echo "wamove: skipped, uv is not installed (see https://docs.astral.sh/uv/)" >&2
    exit 0
fi

echo "Installing wamove..."
uv tool install --force --quiet --python 3.13 --editable "$src"
echo "wamove is installed: run wamove doctor to start"
