# MRIdian Message.dump conversion

Converts ViewRay MRIdian `Message.dump` files (pre-A3i and A3i) into MetaImage volumes, cine
series over time, tracking-grid points, record tables and MP4 videos.

## Executable (no Python needed)

`dist/mridian-dump` (inside this folder): a standalone executable for Linux x86-64 with glibc 2.31 or newer.

```bash
chmod +x dist/mridian-dump
dist/mridian-dump decode /path/to/Message.dump --out results/
dist/mridian-dump video results/cine --speed 4
```

The full instructions are in [docs/mridian-dump_instructions.pdf](docs/mridian-dump_instructions.pdf).

## Python scripts

```bash
pip install -r install/requirements.txt
python3 decode_message_dump.py /path/to/Message.dump --out results/
python3 cine_to_video.py results/cine --speed 4
```

`python3 decode_message_dump.py --help` documents the reverse-engineered file layout.

## Rebuilding the executable

Building needs Docker:

```bash
bash install/build_executable.sh      # writes dist/mridian-dump
```

## Data protection

The output file `text_messages.jsonl` contains patient identifiers. Never commit dumps or
outputs: `.gitignore` excludes them.
