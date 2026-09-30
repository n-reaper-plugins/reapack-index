import os
import zipfile

files = {
    "reapack-index/.github/workflows/build-index.yml": """name: Build ReaPack Index

on:
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: Checkout reapack-index repo
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Install Ruby and reapack-index CLI
        run: |
          sudo gem install reapack-index

      - name: Clone plugin repositories temporarily
        run: |
          mkdir -p temp_repos
          cd temp_repos
          
          git clone --depth 1 https://github.com/n-reaper-plugins/marker-tools.git
          git clone --depth 1 https://github.com/n-reaper-plugins/routing-matrix-helpers.git
          git clone --depth 1 https://github.com/n-reaper-plugins/take-fx-manager.git
          git clone --depth 1 https://github.com/n-reaper-plugins/track-presets.git

      - name: Generate index.xml
        run: |
          reapack-index --name "N-Reaper Plugins" --amend temp_repos/*

      - name: Clean up temporary clones
        if: always()
        run: |
          rm -rf temp_repos

      - name: Commit and push updated index.xml
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add index.xml
          
          if git diff --staged --quiet; then
            echo "No changes detected in index.xml."
          else
            git commit -m "chore: rebuild ReaPack index.xml"
            git push
          fi
""",
    "reapack-index/README.md": """# N-Reaper Plugins Repository

## Import into REAPER

1. Open REAPER -> **Extensions** -> **ReaPack** -> **Import repositories...**
2. Paste: `https://raw.githubusercontent.com/n-reaper-plugins/reapack-index/main/index.xml`
"""
}

with zipfile.ZipFile("reapack-index.zip", "w", zipfile.ZIP_DEFLATED) as zip_file:
    for filepath, content in files.items():
        zip_file.writestr(filepath, content)

print("Created reapack-index.zip successfully.")