#!/usr/bin/env bash
# Package the current HEAD commit into dist/wecom-forward-plus-<commit>.tar.gz
# for server deployment (see README "Package for deployment").
#
# The archive is built with `git archive`, so it contains exactly the
# git-tracked files — never .env, .venv, logs/, or other local state.

set -euo pipefail

cd "$(dirname "$0")/.."

commit=$(git rev-parse --short HEAD)

if ! git diff-index --quiet HEAD --; then
    echo "warning: working tree is dirty; the archive is built from HEAD and will NOT include uncommitted changes" >&2
fi

mkdir -p dist
out="dist/wecom-forward-plus-${commit}.tar.gz"

git archive --format=tar.gz --prefix=wecom-forward-plus/ -o "$out" HEAD

echo "created ${out}"
