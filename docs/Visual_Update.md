# BioDataQC — rotating DNA visual update

A navy header with a slowly rotating, decorative DNA double helix; a dark
sidebar; teal accents; and refreshed metric cards and tabs. Motion stays in the
header so the results remain easy to read. The helix is an illustration, not a
visualization of the selected sample or its genes.

## 1. Download and stop the app

This update is for the working BioDataQC app **after the FastQC integration**.
Download `BioDataQC_Visual_Update.zip` into your Downloads folder.
In the terminal running Streamlit, press **Ctrl+C**.

## 2. Back up and install

Run these commands in a terminal:

```bash
conda activate biodataqc
cd ~/Precursion/BioDataQC
mkdir -p backups/before_visual_update
cp -n biodataqc/ui.py backups/before_visual_update/ui.py
if [ -f .streamlit/config.toml ]; then
    cp -n .streamlit/config.toml backups/before_visual_update/config.toml
fi
python -m zipfile -e ~/Downloads/BioDataQC_Visual_Update.zip .
```

The archive replaces `biodataqc/ui.py` and installs a project theme in
`.streamlit/config.toml`. If you had added your own server settings to that
configuration, copy those settings from the backup into the new configuration.
The new `[theme]` and `[theme.sidebar]` sections control the appearance.

The other files are `biodataqc/appearance.py`,
`biodataqc/assets/dna_header.html`, and this guide. There are no new packages
to install. Your existing `app.py` remains the entry point.

## 3. Restart and view

From the same project folder:

```bash
python -m streamlit run app.py
```

Open the local address printed by Streamlit, usually `http://localhost:8501`.
You should see the navy DNA header, a navy sidebar and teal accents. If the old
appearance remains, refresh with **Ctrl+Shift+R**. In Streamlit's Settings,
select the custom theme if you previously chose a built-in Light or Dark theme.

The project directory matters: launch from `~/Precursion/BioDataQC` so
Streamlit finds `.streamlit/config.toml`.

## 4. Control the animation

At the bottom of the left sidebar, expand **Appearance** and turn **Animate DNA**
off to freeze the helix. The choice lasts for the current Streamlit session.
The header also respects your operating system/browser's reduced-motion
preference and pauses while off-screen or in a hidden browser tab.

The drawing runs locally in your browser. No online images, external animation
service, JavaScript library, GPU-specific setup or API key is required.

## What changes in the application?

Only presentation and the new appearance toggle change. The update contains no
datasets or database. Validation, SQL history, run comparison, FastQC, reports
and package verification keep their existing behavior. Pausing the helix does
not launch validation or add a SQL run.

For a quick check, open an existing run from **History**, then toggle the
animation. The same run should stay selected. Reports keep their existing
report design; this update styles the Streamlit interface.

## Verification

The 48 existing Streamlit interface regression tests pass with Python 3.12 and
Streamlit 1.67.0. A separate JavaScript/canvas check exercised animation,
disabled animation, reduced motion, visibility changes and drawing at three
widths. A Streamlit AppTest check confirmed that the appearance toggle preserves
the selected evidence and SQL run count. These checks do not replace viewing
the final layout in your browser; a full browser screenshot could not be
captured in the build environment.

The header uses `st.iframe` when available, with a fallback to
`streamlit.components.v1.html`. The theme was checked against Streamlit 1.67.0;
older versions may not support every theme option.

## Restore the previous appearance

Stop Streamlit with **Ctrl+C**, then run:

```bash
cd ~/Precursion/BioDataQC
cp backups/before_visual_update/ui.py biodataqc/ui.py
if [ -f backups/before_visual_update/config.toml ]; then
    cp backups/before_visual_update/config.toml .streamlit/config.toml
else
    mv .streamlit/config.toml .streamlit/config.visual-update.toml
fi
python -m streamlit run app.py
```

The extra appearance module and HTML asset can stay in place: the restored
interface no longer imports them. Your datasets, saved runs and analyses stay
available.
