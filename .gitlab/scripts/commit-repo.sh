#!/bin/bash
set -e

git config --global user.email "Phobos1641@proton.me"
git config --global user.name "phobos-bot"
git status
if [ -n "$(git status --porcelain)" ]; then
    git add .
    git commit -m "Update extensions repo"
    git push

    curl https://purge.jsdelivr.net/gl/phobos1641/mihon-extensions@repo/index.min.json
else
    echo "No changes to commit"
fi
