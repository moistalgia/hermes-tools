# QMD — Installation

## Node.js >= 22 (required)

```bash
# Check version
node --version  # must be >= 22

# macOS — install or upgrade via Homebrew
brew install node@22

# Linux — use NodeSource or nvm
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
# or with nvm:
nvm install 22 && nvm use 22
```

## SQLite with Extension Support (macOS only)

macOS system SQLite lacks extension loading. Install via Homebrew:

```bash
brew install sqlite
```

## Install qmd

```bash
npm install -g @tobilu/qmd
# or with Bun:
bun install -g @tobilu/qmd
```

First run auto-downloads 3 local GGUF models (~2GB total):

| Model | Purpose | Size |
|-------|---------|------|
| embeddinggemma-300M-Q8_0 | Vector embeddings | ~300MB |
| qwen3-reranker-0.6b-q8_0 | Result reranking | ~640MB |
| qmd-query-expansion-1.7B | Query expansion | ~1.1GB |

## Verify Installation

```bash
qmd --version
qmd status
```

## Data Storage

- **Index & vectors:** `~/.cache/qmd/index.sqlite`
- **Models:** Auto-downloaded to local cache on first run
- **No cloud dependencies** — everything runs locally

## See Also

- [GitHub: tobi/qmd](https://github.com/tobi/qmd)
- [QMD Changelog](https://github.com/tobi/qmd/blob/main/CHANGELOG.md)
