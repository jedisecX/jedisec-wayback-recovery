Jedi-Sec Wayback Recovery

A resumable Wayback Machine inventory, metadata indexing, and file recovery tool written in Python.

Jedi-Sec Wayback Recovery indexes archived URLs through the Internet Archive's CDX API, records capture information in SQLite, optionally downloads recoverable files, and extracts metadata from archived HTML pages.

The tool is designed for long-running recovery jobs that can resume after interruptions.

Features

- Wayback Machine indexing: Queries CDX for archived URLs and capture records.
- Resumable processing: Stores per-root pagination checkpoints in SQLite.
- File recovery: Optionally downloads supported archived file types.
- Metadata extraction: Collects HTML titles, descriptions, keywords, canonical URLs, Open Graph metadata, robots directives, and language attributes.
- SHA-256 verification: Calculates hashes for downloaded files.
- CSV exports: Produces searchable inventories and metadata reports.
- Live crawling: Follows same-host directory links beneath a configured root.
- Multiple sources: Supports multiple Wayback roots in a single run.
- Safe handling: Does not execute, extract, or import recovered files into a live database.

Requirements

- Python 3.10 or newer recommended.
- Internet connectivity.
- Access to the Internet Archive CDX API for Wayback mode.

The script uses Python's standard library and does not require third-party packages.

Installation

Clone the repository:

git clone https://github.com/jedisecX/jedisec-wayback-recovery.git
cd jedisec-wayback-recovery

Ensure the Python script is present in the repository directory.

Usage

1. Inventory archived URLs

python3 jedisec_wayback_recovery_fullmeta-1.py \
  --source wayback \
  --out ./jedi-recovery

2. Inventory and download recoverable files

python3 jedisec_wayback_recovery_fullmeta-1.py \
  --source wayback \
  --download \
  --out ./jedi-recovery

3. Extract archived HTML metadata

python3 jedisec_wayback_recovery_fullmeta-1.py \
  --source wayback \
  --meta-index \
  --out ./jedi-recovery

4. Run inventory, downloads, and metadata extraction

python3 jedisec_wayback_recovery_fullmeta-1.py \
  --source wayback \
  --download \
  --meta-index \
  --out ./jedi-recovery

5. Specify individual archive roots

python3 jedisec_wayback_recovery_fullmeta-1.py \
  --source wayback \
  --root https://jedi-sec.us/ \
  --root http://jedi-sec.space/ \
  --out ./jedi-recovery

6. Crawl a live website

python3 jedisec_wayback_recovery_fullmeta-1.py \
  --source live \
  --root https://example.org/downloads/ \
  --out ./jedi-recovery

Only use live crawling on websites you are authorized to access.

Command-Line Options

Option| Description
"--source wayback"| Index archived URLs through CDX.
"--source live"| Crawl a live website.
"--root URL"| Add a source root; repeat for multiple roots.
"--out DIR"| Set the output directory.
"--download"| Download discovered supported files.
"--meta-index"| Extract metadata from archived HTML captures.
"--delay SECONDS"| Set the delay between requests; default is 1 second.
"--limit N"| Limit downloads and metadata extraction per run; "0" means unlimited.

Output Files

The output directory contains the following files and directories:

Path| Purpose
"recovery_state.sqlite3"| Persistent job state, checkpoints, and indexes.
"inventory.csv"| Downloadable file inventory and recovery status.
"wayback_url_index.csv"| Archived URL and capture inventory.
"page_metadata.csv"| Extracted HTML metadata.
"recovered/"| Downloaded files, organized by source and capture timestamp.

Resume and Checkpointing

The SQLite database preserves CDX pagination progress and the status of discovered files and pages. Re-running the script against the same output directory allows it to reuse existing state.

The script records errors and checkpoints after CDX pages are processed. Failed downloads can be retried on subsequent runs.

Important: The current implementation marks a live-crawl page as failed after a request error. It does not automatically reset failed live pages to pending, so a separate recovery step may be necessary to retry those pages.

Data Integrity and Safety

- Downloaded files receive SHA-256 hashes.
- Original URLs, capture timestamps, and source roots are stored where available.
- Existing nonempty local downloads can be reused.
- Recovered content is treated as untrusted data.
- The tool does not execute or unpack downloaded files.

Always inspect recovered files before opening or processing them.

Limitations

- CDX availability, pagination behavior, and rate limits are controlled by the Internet Archive.
- A missing capture does not necessarily mean that a file never existed.
- The default archived roots are configured in the Python source and can be overridden with "--root".
- The current CDX query uses host-wide URL patterns. When multiple roots share a host, the inventory may include URLs outside an individual root's intended path.
- This tool does not guarantee complete recovery of every historical resource.

Responsible Use

Use this tool for lawful archival research, recovery of your own content, and authorized investigations. Respect the policies and rate limits of archive services and live websites.

License

No license is specified in this repository yet. Add a "LICENSE" file before distributing the project under an open-source license.

---

Jedi-Sec Wayback Recovery
Repository: https://github.com/jedisecX/jedisec-wayback-recovery
