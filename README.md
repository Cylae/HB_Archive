# 🚀 Honorbuddy Archive System

Welcome to the **Honorbuddy Archive System — Final Edition**! 🎉

This is a high-performance, fully asynchronous Python web scraper and archiver designed to help you back up and map the entire Honorbuddy ecosystem. It replaces the legacy, slow, and sequential PowerShell scripts with a blazing-fast, robust engine built using `asyncio` and `aiohttp`.

Whether you are looking to preserve history or just grab your favorite profiles, you're in the right place!

## ✨ Features

- **Blazing Fast**: Powered by `asyncio` and `aiohttp` for true parallel I/O.
- **Smart BFS Crawling**: Intelligently crawls URLs by depth to map out repositories and forums.
- **Resilient**: Real checkpointing allows you to stop and resume (`--resume`) without losing progress.
- **Graceful Shutdown**: Safely catches `Ctrl+C` (SIGINT) to save your current state before exiting.
- **Streamed Downloads**: Efficiently downloads large files directly to your disk without bloating your RAM.
- **Atomic Renaming**: Ensures files are completely downloaded before marking them as finished.
- **Aggressive Deduplication**: Normalizes URLs to ensure we don't fetch the same asset twice.
- **Clean UI**: Features an inline, non-spammy progress bar to keep you updated.

## 🛠 Installation

You don't need much to get started. Just clone this repository and make sure you have Python 3 installed. The script is smart enough to auto-install its required dependencies (`aiohttp` and `aiofiles`) on its first run!

## 💻 Usage

Running the script is super simple. It is now fully **interactive**! If you don't provide an output directory or a GitHub token via arguments, it will kindly prompt you for them.

For cross-platform compatibility, especially on Windows, use the `python` command:

```bash
python honorbuddy_archive.py --help
```

### Examples

**Standard Mode:** (Balanced, takes ~20-40 mins)
```bash
python honorbuddy_archive.py --mode standard
```

**Aggressive Mode:** (Wider coverage, takes ~40-70 mins)
```bash
python honorbuddy_archive.py --mode aggressive
```

**Ultimate Mode:** (Leave no stone unturned! takes ~70-120 mins)
```bash
python honorbuddy_archive.py --mode ultimate
```

### Pro-Tip: GitHub Token
To avoid getting rate-limited by GitHub's search API, it is highly recommended to provide a personal access token:
```bash
export GITHUB_TOKEN=ghp_your_token_here
python honorbuddy_archive.py --mode standard
```
*Or pass it directly:*
```bash
python honorbuddy_archive.py --github-token ghp_your_token_here
```

## ⚙️ Advanced Options

Feel free to customize your crawl by skipping elements you don't need:

- `--skip-private`: Exclude private servers.
- `--skip-addons`: Exclude addons.
- `--skip-meshes`: Exclude meshes.
- `--skip-crawl`: Disable deep crawling (fast discovery only).
- `--skip-download`: Only discover and map, but don't download anything.
- `--resume`: Pick up right where you left off.

## 🤝 Contributing

Got ideas? We'd love to hear them! Feel free to open an issue or submit a pull request. We use `flake8` for linting and `mypy` for static type checking.

Happy Archiving! 💾
