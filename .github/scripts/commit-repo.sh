#!/bin/bash
set -e

git config --global user.email "Phobos1641+github-bot@proton.me"
git config --global user.name "phobos1641-bot"
git status
if [ -n "$(git status --porcelain)" ]; then
    git add .
    git commit -m "Update extensions repo"
    git push

    curl https://purge.jsdelivr.net/gh/Phobos1641/cosmos-extensions@repo/index.min.json
else
    echo "No changes to commit"
fi
