# crXte

crXte is a private, localhost exporter for public X posts and X Articles. Paste a supported link, inspect the detected content, and save any combination of original media, clean Markdown, and a print-ready PDF to your computer.

Exports run through a persistent sequential queue with pause/resume, resumable partial media downloads, duplicate-job protection, per-output locking, and media verification before completion.

## Platform

The current release targets **WSL/Linux with Windows browser integration**. The launcher opens crXte in the Windows browser and uses `Windows Downloads/X Media` as the default export root.

## Requirements

- WSL/Linux
- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- `ffmpeg` and `ffprobe`
- Internet access during first-time dependency installation and while exporting posts

On Ubuntu or WSL, install the system requirements:

```bash
sudo apt update
sudo apt install -y ffmpeg python3
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Restart the terminal after installing `uv`, or load the environment change printed by its installer.

## Install and start

Clone the repository and run the launcher:

```bash
git clone https://github.com/051-lab/crXte.git
cd crXte
chmod +x run.sh
./run.sh
```

On first launch, `uv` creates the local `.venv` and installs the exact dependency versions recorded in `uv.lock`. The launcher then starts the localhost server and opens crXte in the Windows browser.

Run without opening a browser:

```bash
./run.sh --no-browser
```

## Export layout

Change the export root from **Settings**. New exports are organized by author and post:

```text
X Media/
└── @handle/
    └── post-id/
        ├── post.md
        ├── post.pdf
        └── media/
```

Only selected outputs are created. Existing flat export files are not moved automatically; queue entries using the old layout are labeled **Legacy flat export**.

## Supported exports

- Public `https://x.com/<account>/status/<id>` links
- Public `twitter.com` status links
- Complete X Articles, including structured text and article images
- Post and article Markdown
- Print-ready post and article PDF
- Original photos, videos, animated GIFs, and mixed-media posts
- Independent output selection, including document-only exports with no media selected
- Video quality selection and optional selected attachments in document exports

Selected media is stored once in the post's `media/` folder. Markdown uses portable relative links to those files. PDF embeds selected images, while selected videos remain separate links to the files in `media/`.

The app intentionally does not load browser cookies or access private, protected, deleted, or DRM-restricted content. Quoted-post media and timelines are excluded. Only export content you own or are authorized to save.

## Troubleshooting

Confirm the external tools and locked Python commands are available:

```bash
ffmpeg -version
ffprobe -version
uv run --locked gallery-dl --version
uv run --locked yt-dlp --version
```

If the browser does not open automatically, start with `./run.sh --no-browser` and visit the localhost address printed in the terminal.

Runtime state lives under the platform user-data directory (`~/.local/share/x-media-downloader` on WSL). Cancelled jobs retain `.part` files so **Retry** can resume them.

## Development

Install development dependencies and run the checks:

```bash
uv sync --locked --dev
uv run --locked ruff check .
uv run --locked pytest
```

The project currently has no repository-level software license. The bundled DejaVu Sans font retains its own license in `src/x_media_downloader/assets/DejaVu-LICENSE.txt`.
