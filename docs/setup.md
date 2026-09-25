# groundtrack: Setup

[← back to README](../README.md)

## One-time setup

1. **Clone the repo and create your personal config:**
   ```
   git clone git@github.com:n6rfm/groundtrack.git
   cd groundtrack
   cp satellites.example.yaml satellites.yaml
   ```
   `satellites.yaml` is gitignored on purpose - it holds your ground
   station's coordinates and is yours alone; `satellites.example.yaml` is
   the version-controlled template everyone starts from.

2. **System packages** (Debian/Ubuntu):
   ```
   sudo apt install libhamlib-utils
   ```
   This provides `rigctld`/`rigctl` and `rotctld`/`rotctl`. Confirm with
   `rigctld --version`.

3. **Python packages** - these are NOT apt packages, install with pip:
   ```
   pip install skyfield pyyaml --break-system-packages
   ```
   (or use a venv if you'd rather not pass `--break-system-packages`).

4. **Generate the flowgraph scripts.** The `.grc` files in this repo were
   authored outside GNU Radio Companion, so the runnable `.py` files don't
   exist yet. On your machine, with GNU Radio and your SDR driver
   installed, open each `.grc` and generate it (or `./regen_all.sh` for
   all of them at once). Also check, per file:
   - decode-and-relay satellites: the decoder block
     (`satellites_satellite_decoder`) points `file:` at your real decoder
     definition, and `network_socket_pdu` goes through `kiss_encode_pdu`
     (see "Why every `.grc` needs a `kiss_encode_pdu` block" below)
   - the `AdvFileSink` block's `basedir` points somewhere sensible for IQ
     output, and `recordOnStart` is `True` if you want unattended
     automatic recording

5. **Run the station setup wizard** to fill in `satellites.yaml`'s
   ground-station section:
   ```
   python3 setup_station.py
   ```
   Asks for latitude/longitude/altitude, your TLE source, and the shared
   rig port. It can also fetch the TLE file for you at the end.

6. **Fetch a TLE file** if you skipped that in step 5:
   ```
   python3 update_tle.py
   ```
   Fetches a base of Celestrak's `cubesat` and `amateur` groups by
   default - between them, covering most satellites this kind of station
   is likely to track - merges them, and validates that every satellite
   currently in `satellites.yaml` is actually covered. If one isn't (too
   new, uncoordinated, or simply in a different group), add it
   individually by NORAD ID rather than needing a whole separate TLE
   source:
   ```
   python3 update_tle.py --extra-catnr 69880
   ```
   Refresh this daily (cron), and re-run `plan_passes.py` after each
   refresh - stale TLEs drift AOS/LOS times and Doppler accuracy.

