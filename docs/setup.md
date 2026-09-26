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
     automatic recording (or an expression like `bool(record_iq)`,
     referencing a Parameter block, if you want that decision made per
     run instead - see [Toggling IQ recording on or off per
     run](adding-satellites.md);
     this specifically requires
     [gr-filerepeater_n6rfm](https://github.com/N6RFM/gr-filerepeater_n6rfm)
     rather than stock upstream `gr-filerepeater`, since upstream's
     `Record On Start` is a fixed Yes/No with no way to reference a
     variable)

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
   Fetches every configured satellite's TLE from SatNOGS in one bulk
   download - including "temporary ID" satellites too new for
   Celestrak/Space-Track's official catalog to have picked up yet.
   Anything SatNOGS doesn't have falls back automatically to an
   individual Celestrak lookup for just that satellite. Nothing to add
   manually - every satellite in `satellites.yaml` is covered by both
   sources on every run, with no flag to remember for a new one.
   Refresh this daily (cron), and re-run `plan_passes.py` after each
   refresh - stale TLEs drift AOS/LOS times and Doppler accuracy.

