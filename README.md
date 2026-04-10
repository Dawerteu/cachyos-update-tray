# CachyOS Update Tray

Python tray application for CachyOS and other Arch-based systems. It checks `pacman` updates via `checkupdates`, can run `sudo pacman -Syu` in a terminal, and keeps update logs and history.

## Features

- tray icon with state changes
- automatic update check at startup and on a timer
- manual `Check again` action in the tray menu
- icon states for OK / checking / updates / restart / error / running update
- pending package list dialog
- package news and changelog dialog for pending updates
- one update action that runs `sudo pacman -Syu`
- update log viewer with per-date log files
- saved log cleanup by age
- manual saved log removal for selected entries
- pacman log viewer
- completion notifications
- automatic re-check after update completion
- saved timestamps for last check and last update
- timestamped update logs stored separately for each run
- configurable check interval
- ignored package list
- launcher installation for the app menu
- autostart enable/disable
- selective rollback from pacman cache via `sudo pacman -U`
- selective full package removal via `sudo pacman -Rns`
- rollback and removal from pacman transaction history
- improved restart detection using pending core packages, installed kernel versions, pacman reboot hints, and `/run/reboot-required`
- manual clear option for the current restart notification

## Install

```bash
chmod +x setup-deps.sh run.sh
./setup-deps.sh
```

Then start the app from this folder:

```bash
./run.sh
```

Or directly:

```bash
python3 app.py
```

## Requirements

- `pacman-contrib` for `checkupdates`
- a terminal emulator such as `kitty`, `wezterm`, `alacritty`, `konsole`, `xfce4-terminal`, `gnome-terminal`, or `xterm`
- `sudo` for the actual update and rollback
- package files must still exist in `/var/cache/pacman/pkg` for rollback
- read access to `/var/log/pacman.log` for live logging and external transaction rollback history

## Configuration

On first launch, the app creates:

- `~/.config/cachyos-update-tray/config.json`
- `~/.local/state/cachyos-update-tray/state.json`
- `~/.local/state/cachyos-update-tray/last-update.log`
- `~/.local/state/cachyos-update-tray/update-status.json`
- `~/.local/state/cachyos-update-tray/logs/update-YYYY-MM-DD_HH-MM-SS.log`

`config.json` looks like this:

```json
{
  "interval_minutes": 60,
  "reminder_minutes": 180,
  "log_retention_days": 0,
  "ignored_packages": []
}
```

- `interval_minutes: 0` means no periodic checks; the app only runs one startup check after about 3 minutes
- `reminder_minutes: 0` means never repeat the same update reminder
- `log_retention_days: 0` means never auto-remove saved logs

## Folder Usage

Use the project directly from its folder.

- keep the whole folder anywhere you want
- install dependencies with `./setup-deps.sh`
- start it with `./run.sh` or `python3 app.py`
- the app stores its config and state under your home directory, not inside the project folder

## Launcher And Autostart

The tray menu can create or recreate:

- a launcher at `~/.local/share/applications/cachyos-update-tray.desktop`
- an autostart entry at `~/.config/autostart/cachyos-update-tray.desktop`

## Rollback

Rollback:

- it uses pacman transaction history parsed from `/var/log/pacman.log`
- it can include transactions done outside this app
- app-triggered updates are listed under `update`
- it looks for the older package versions in `/var/cache/pacman/pkg`
- for newly installed packages, rollback removes them with `pacman -R`
- the rollback dialog shows a checkbox for each package in the selected transaction
- each package row shows the rollback target version when available
- you can choose only specific packages from a transaction instead of all of them
- the dialog lets you choose either `rollback` or `remove fully`
- `remove fully` uninstalls selected packages with `pacman -Rns`
- if any required package file is missing from the cache, rollback does not start

This is not a full system snapshot. It uses whatever pacman still has in the local cache.

## Update Logs

Each update run writes its own dated log file into `~/.local/state/cachyos-update-tray/logs/`.

- the tray log viewer lets you pick an update date from history
- `Manage saved logs` lets you select specific saved logs and delete them
- logs older than the configured retention are removed automatically
- each history entry points to its own log file
- `last-update.log` is still updated as a copy of the latest run
- the pacman log view reads `/var/log/pacman.log` directly
