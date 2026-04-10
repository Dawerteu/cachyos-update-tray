#!/usr/bin/env python3
from __future__ import annotations

import glob
import json
import platform
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "CachyOS Update Tray"
APP_ID = "cachyos-update-tray"
BASE_DIR = Path(__file__).resolve().parent
HOME_DIR = Path.home()
LOCAL_BIN_DIR = HOME_DIR / ".local" / "bin"
CONFIG_DIR = HOME_DIR / ".config" / APP_ID
STATE_DIR = HOME_DIR / ".local" / "state" / APP_ID
LOGS_DIR = STATE_DIR / "logs"
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = STATE_DIR / "state.json"
LOG_PATH = STATE_DIR / "last-update.log"
STATUS_PATH = STATE_DIR / "update-status.json"
LAUNCHER_PATH = HOME_DIR / ".local" / "share" / "applications" / f"{APP_ID}.desktop"
AUTOSTART_PATH = HOME_DIR / ".config" / "autostart" / f"{APP_ID}.desktop"
INSTALLED_RUNNER_PATH = LOCAL_BIN_DIR / APP_ID
REBOOT_FLAG_PATH = Path("/run/reboot-required")
PACMAN_CACHE_DIR = Path("/var/cache/pacman/pkg")
PACMAN_LOG_PATH = Path("/var/log/pacman.log")
MODULES_DIR = Path("/usr/lib/modules")
DEFAULT_INTERVAL_MINUTES = 60
DEFAULT_IGNORE_PACKAGES: list[str] = []
PACMAN_LOG_TRANSACTION_LIMIT = 40
PACMAN_CHANGELOG_LINES = 32
TERMINALS = [
    "kitty",
    "wezterm",
    "alacritty",
    "konsole",
    "xfce4-terminal",
    "gnome-terminal",
    "xterm",
]
REBOOT_PACKAGES = {
    "amd-ucode",
    "glibc",
    "intel-ucode",
    "linux",
    "linux-api-headers",
    "linux-cachyos",
    "linux-cachyos-bore",
    "linux-cachyos-lto",
    "linux-cachyos-server",
    "linux-cachyos-lts",
    "linux-headers",
    "linux-zen",
    "mkinitcpio",
    "nvidia",
    "nvidia-dkms",
    "systemd",
}

PACMAN_EVENT_RE = re.compile(
    r"\[(?P<timestamp>[^\]]+)\] \[ALPM\] (?P<action>upgraded|downgraded|installed|removed|reinstalled) "
    r"(?P<package>[^\s]+) \((?P<versions>.+)\)"
)


@dataclass
class AppConfig:
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    log_retention_days: int = 30
    ignored_packages: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_PACKAGES))


@dataclass
class PersistedState:
    last_check_at: str = ""
    last_check_status: str = "never"
    last_error: str = ""
    last_update_started_at: str = ""
    last_update_finished_at: str = ""
    last_update_exit_code: int | None = None
    last_update_packages: list[str] = field(default_factory=list)
    pending_update_packages: list[str] = field(default_factory=list)
    reboot_recommended: bool = False
    restart_notification_cleared: bool = False
    update_history: list[dict[str, object]] = field(default_factory=list)


@dataclass
class UpdateState:
    packages: list[str]
    ignored_packages: list[str]
    reboot_recommended: bool
    reboot_reason: str
    last_check_at: str

    @property
    def update_count(self) -> int:
        return len(self.packages)


class CheckUpdatesWorker(QThread):
    completed = Signal(object)
    error = Signal(str)

    def __init__(self, ignored_packages: list[str]) -> None:
        super().__init__()
        self.ignored_packages = set(ignored_packages)

    def run(self) -> None:
        command = shutil.which("checkupdates")
        if not command:
            self.error.emit("Missing `checkupdates`. Install `pacman-contrib`.")
            return

        try:
            result = subprocess.run(
                [command],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except Exception as exc:
            self.error.emit(f"Failed to run checkupdates: {exc}")
            return

        if result.returncode not in (0, 2):
            message = result.stderr.strip() or result.stdout.strip() or "Unknown checkupdates error."
            self.error.emit(message)
            return

        raw_packages = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        packages = [line for line in raw_packages if self._package_name(line) not in self.ignored_packages]
        reboot_recommended, reboot_reason = self._detect_reboot_required(packages)
        self.completed.emit(
            UpdateState(
                packages=packages,
                ignored_packages=sorted(self.ignored_packages),
                reboot_recommended=reboot_recommended,
                reboot_reason=reboot_reason,
                last_check_at=self._now(),
            )
        )

    def _package_name(self, package_line: str) -> str:
        return package_line.split()[0]

    def _detect_reboot_required(self, packages: list[str]) -> tuple[bool, str]:
        if REBOOT_FLAG_PATH.exists():
            return True, "reboot-required marker exists"

        package_names = {self._package_name(package_line) for package_line in packages}
        reboot_packages = sorted(package_names & REBOOT_PACKAGES)
        if reboot_packages:
            return True, f"pending core package updates: {', '.join(reboot_packages[:5])}"

        running_kernel = platform.release()
        module_versions = [path.name for path in MODULES_DIR.iterdir() if path.is_dir()] if MODULES_DIR.exists() else []
        if module_versions:
            latest_installed_kernel = max(module_versions, key=self._kernel_sort_key)
            if self._kernel_sort_key(latest_installed_kernel) > self._kernel_sort_key(running_kernel):
                return True, f"newer installed kernel detected: {latest_installed_kernel}"

        pacman_hint = self._recent_pacman_reboot_hint()
        if pacman_hint:
            return True, pacman_hint

        return False, ""

    def _recent_pacman_reboot_hint(self) -> str:
        if not PACMAN_LOG_PATH.exists():
            return ""
        try:
            lines = PACMAN_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        except Exception:
            return ""
        for line in reversed(lines):
            if "Reboot is recommended" in line:
                return "pacman hook reported that a reboot is recommended"
        return ""

    def _kernel_sort_key(self, version: str) -> tuple[object, ...]:
        parts = re.split(r"([0-9]+)", version)
        key: list[object] = []
        for part in parts:
            if not part:
                continue
            key.append(int(part) if part.isdigit() else part)
        return tuple(key)

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class LogDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.log_path = LOG_PATH
        self.title = "Saved Update Log"
        self.setWindowTitle(self.title)
        self.resize(900, 600)

        layout = QVBoxLayout(self)
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        self.editor = QPlainTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setFont(QFont("monospace"))
        layout.addWidget(self.editor)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.reload)
        buttons.addWidget(self.refresh_button)

        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.reload)
        self.timer.start(2000)
        self.reload()

    def reload(self) -> None:
        self.setWindowTitle(self.title)
        if self.log_path.exists():
            self.editor.setPlainText(self.log_path.read_text(encoding="utf-8", errors="replace"))
        else:
            self.editor.setPlainText("No log data is available yet.")

        self.info_label.setText(f"Log file: {self.log_path}")

    def set_log_path(self, log_path: Path, title: str) -> None:
        self.log_path = log_path
        self.title = title
        self.reload()


class SettingsDialog(QDialog):
    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(520, 220)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.interval_input = QLineEdit(str(config.interval_minutes))
        form.addRow("Check interval (min):", self.interval_input)

        self.log_retention_input = QLineEdit(str(config.log_retention_days))
        self.log_retention_input.setPlaceholderText("0 disables automatic cleanup")
        form.addRow("Delete logs older than (days):", self.log_retention_input)

        self.ignore_input = QLineEdit(", ".join(config.ignored_packages))
        self.ignore_input.setPlaceholderText("for example firefox, thunderbird")
        form.addRow("Ignored packages:", self.ignore_input)
        layout.addLayout(form)

        help_label = QLabel(
            "Separate ignored packages with commas. Saved update logs older than the configured number of days are removed automatically. Use 0 to keep them."
        )
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        buttons = QHBoxLayout()
        save_button = QPushButton("Save")
        save_button.clicked.connect(self.accept)
        buttons.addWidget(save_button)

        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

    def result_config(self) -> AppConfig:
        interval = max(5, int(self.interval_input.text().strip() or DEFAULT_INTERVAL_MINUTES))
        retention = max(0, int(self.log_retention_input.text().strip() or 0))
        ignored = [item.strip() for item in self.ignore_input.text().split(",") if item.strip()]
        return AppConfig(interval_minutes=interval, log_retention_days=retention, ignored_packages=ignored)


class LogManagementDialog(QDialog):
    def __init__(self, log_entries: list[dict[str, object]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Manage Saved Logs")
        self.resize(760, 560)
        self._entry_checkboxes: list[tuple[dict[str, object], QCheckBox]] = []

        layout = QVBoxLayout(self)
        header = QLabel("Select the saved update logs you want to delete.")
        header.setWordWrap(True)
        layout.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)

        for entry in log_entries:
            checkbox = QCheckBox(self._entry_label(entry))
            checkbox.setChecked(False)
            self._entry_checkboxes.append((entry, checkbox))
            content_layout.addWidget(checkbox)

        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)

        buttons = QHBoxLayout()
        select_all = QPushButton("Select all")
        select_all.clicked.connect(lambda: self._set_all(True))
        buttons.addWidget(select_all)

        clear_all = QPushButton("Clear all")
        clear_all.clicked.connect(lambda: self._set_all(False))
        buttons.addWidget(clear_all)

        delete_button = QPushButton("Delete selected")
        delete_button.clicked.connect(self._confirm)
        buttons.addWidget(delete_button)

        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

    def _entry_label(self, entry: dict[str, object]) -> str:
        finished_at = str(entry.get("finished_at", "unknown date"))
        log_path = entry.get("log_path")
        log_name = Path(str(log_path)).name if log_path else "missing"
        packages = entry.get("packages")
        package_events = entry.get("package_events")
        count = len(packages) if isinstance(packages, list) else len(package_events) if isinstance(package_events, list) else 0
        return f"{finished_at} ({count} items) [{log_name}]"

    def _set_all(self, checked: bool) -> None:
        for _, checkbox in self._entry_checkboxes:
            checkbox.setChecked(checked)

    def _confirm(self) -> None:
        if not any(checkbox.isChecked() for _, checkbox in self._entry_checkboxes):
            QMessageBox.warning(self, APP_NAME, "Select at least one log.")
            return
        self.accept()

    def selected_entries(self) -> list[dict[str, object]]:
        return [entry for entry, checkbox in self._entry_checkboxes if checkbox.isChecked()]


def find_cached_package_files(package_name: str, version: str) -> list[str]:
    pattern = str(PACMAN_CACHE_DIR / f"{package_name}-{version}-*.pkg.tar*")
    matches = sorted(glob.glob(pattern))
    return [match for match in matches if not match.endswith(".sig")]


def find_previous_cached_package_files(package_name: str, current_version: str) -> list[str]:
    pattern = str(PACMAN_CACHE_DIR / f"{package_name}-*.pkg.tar*")
    matches = sorted(glob.glob(pattern), key=lambda path: Path(path).stat().st_mtime)
    package_matches = [match for match in matches if not match.endswith(".sig")]
    previous: list[str] = []
    current_marker = f"{package_name}-{current_version}-"
    for match in package_matches:
        if current_marker in Path(match).name:
            continue
        previous.append(match)
    return previous


def previous_cached_version(package_name: str, current_version: str) -> str:
    matches = find_previous_cached_package_files(package_name, current_version)
    if not matches:
        return ""
    filename = Path(matches[-1]).name
    prefix = f"{package_name}-"
    if not filename.startswith(prefix):
        return ""
    remainder = filename[len(prefix):]
    parts = remainder.split("-")
    if len(parts) < 3:
        return ""
    return "-".join(parts[:-2])


class RollbackSelectionDialog(QDialog):
    def __init__(self, transaction: dict[str, object], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Rollback And Removal")
        self.resize(760, 560)
        self._event_checkboxes: list[tuple[dict[str, object], QCheckBox]] = []

        layout = QVBoxLayout(self)
        header = QLabel(
            "Select the packages you want to manage. Each row shows the package and the target version for rollback."
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        transaction_label = QLabel(f"Transaction: {transaction.get('finished_at', 'unknown date')}")
        transaction_label.setWordWrap(True)
        layout.addWidget(transaction_label)

        action_row = QHBoxLayout()
        self.rollback_checkbox = QCheckBox("Rollback selected packages")
        self.remove_checkbox = QCheckBox("Remove selected packages fully")
        self.rollback_checkbox.setChecked(True)
        self.rollback_checkbox.toggled.connect(self._sync_action_checkboxes)
        self.remove_checkbox.toggled.connect(self._sync_action_checkboxes)
        action_row.addWidget(self.rollback_checkbox)
        action_row.addWidget(self.remove_checkbox)
        layout.addLayout(action_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)

        for event in transaction.get("package_events", []):
            if not isinstance(event, dict):
                continue
            label = self._event_label(event)
            checkbox = QCheckBox(label)
            checkbox.setChecked(True)
            self._event_checkboxes.append((event, checkbox))
            content_layout.addWidget(checkbox)

        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)

        buttons = QHBoxLayout()
        select_all = QPushButton("Select all")
        select_all.clicked.connect(lambda: self._set_all(True))
        buttons.addWidget(select_all)

        clear_all = QPushButton("Clear all")
        clear_all.clicked.connect(lambda: self._set_all(False))
        buttons.addWidget(clear_all)

        confirm = QPushButton("Continue")
        confirm.clicked.connect(self._confirm)
        buttons.addWidget(confirm)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def _sync_action_checkboxes(self) -> None:
        sender = self.sender()
        if sender is self.rollback_checkbox and self.rollback_checkbox.isChecked():
            self.remove_checkbox.blockSignals(True)
            self.remove_checkbox.setChecked(False)
            self.remove_checkbox.blockSignals(False)
        elif sender is self.remove_checkbox and self.remove_checkbox.isChecked():
            self.rollback_checkbox.blockSignals(True)
            self.rollback_checkbox.setChecked(False)
            self.rollback_checkbox.blockSignals(False)

    def _set_all(self, checked: bool) -> None:
        for _, checkbox in self._event_checkboxes:
            checkbox.setChecked(checked)

    def _confirm(self) -> None:
        if not any(checkbox.isChecked() for _, checkbox in self._event_checkboxes):
            QMessageBox.warning(self, APP_NAME, "Select at least one package.")
            return
        if self.rollback_checkbox.isChecked() == self.remove_checkbox.isChecked():
            QMessageBox.warning(self, APP_NAME, "Choose exactly one action: rollback or remove fully.")
            return
        self.accept()

    def selected_events(self) -> list[dict[str, object]]:
        return [event for event, checkbox in self._event_checkboxes if checkbox.isChecked()]

    def selected_action(self) -> str:
        return "rollback" if self.rollback_checkbox.isChecked() else "remove"

    def _event_label(self, event: dict[str, object]) -> str:
        package = str(event.get("package", "unknown"))
        action = str(event.get("action", ""))
        old_version = str(event.get("old_version", ""))
        new_version = str(event.get("new_version", ""))
        if action in {"upgraded", "downgraded"}:
            target = old_version or "unknown version"
            return f"{package}: rollback to {target} (current {new_version})"
        if action == "installed":
            previous = previous_cached_version(package, new_version)
            if previous:
                return f"{package}: rollback to {previous} (installed {new_version})"
            return f"{package}: no previous cached version found (installed {new_version})"
        if action == "reinstalled":
            return f"{package}: reinstall target {new_version}"
        return f"{package}: remove package"


class TrayApp:
    def __init__(self) -> None:
        self._ensure_dirs()
        self.config = self._load_config()
        self.persisted = self._load_persisted_state()
        self._cleanup_old_logs()
        reboot_recommended, reboot_reason = self._runtime_reboot_state()
        visible_reboot = (reboot_recommended or self.persisted.reboot_recommended) and not self.persisted.restart_notification_cleared
        self.state = UpdateState(
            packages=list(self.persisted.pending_update_packages),
            ignored_packages=list(self.config.ignored_packages),
            reboot_recommended=visible_reboot,
            reboot_reason=reboot_reason if visible_reboot else "",
            last_check_at=self.persisted.last_check_at,
        )
        self.worker: CheckUpdatesWorker | None = None
        self.log_dialog: LogDialog | None = None
        self.live_log_dialog: LogDialog | None = None

        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setApplicationName(APP_NAME)
        self.app.setApplicationDisplayName(APP_NAME)

        self.update_watch_timer = QTimer(self.app)
        self.update_watch_timer.timeout.connect(self._poll_update_status)
        self.timer = QTimer(self.app)
        self.timer.timeout.connect(self.check_for_updates)
        self.startup_check_timer = QTimer(self.app)
        self.startup_check_timer.setSingleShot(True)
        self.startup_check_timer.timeout.connect(self._startup_check)

        self.tray = QSystemTrayIcon()
        self.tray.setContextMenu(self._build_menu())
        self.tray.activated.connect(self._handle_tray_activation)
        self._set_state_icon(self._current_icon_state())
        self._refresh_menu_labels()
        self.tray.show()

        self._reset_check_timer()
        self.startup_check_timer.start(1200)

        if self.persisted.last_update_started_at and not self.persisted.last_update_finished_at:
            self.update_watch_timer.start(2000)

    def _ensure_dirs(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

    def _load_config(self) -> AppConfig:
        if not CONFIG_PATH.exists():
            config = AppConfig()
            self._save_config(config)
            return config
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return AppConfig(
                interval_minutes=max(5, int(data.get("interval_minutes", DEFAULT_INTERVAL_MINUTES))),
                log_retention_days=max(0, int(data.get("log_retention_days", 30))),
                ignored_packages=list(data.get("ignored_packages", DEFAULT_IGNORE_PACKAGES)),
            )
        except Exception:
            return AppConfig()

    def _save_config(self, config: AppConfig) -> None:
        CONFIG_PATH.write_text(json.dumps(asdict(config), indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_persisted_state(self) -> PersistedState:
        if not STATE_PATH.exists():
            state = PersistedState()
            self._save_persisted_state(state)
            return state
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return PersistedState(
                last_check_at=data.get("last_check_at", ""),
                last_check_status=data.get("last_check_status", "never"),
                last_error=data.get("last_error", ""),
                last_update_started_at=data.get("last_update_started_at", ""),
                last_update_finished_at=data.get("last_update_finished_at", ""),
                last_update_exit_code=data.get("last_update_exit_code"),
                last_update_packages=list(data.get("last_update_packages", [])),
                pending_update_packages=list(data.get("pending_update_packages", [])),
                reboot_recommended=bool(data.get("reboot_recommended", False)),
                restart_notification_cleared=bool(data.get("restart_notification_cleared", False)),
                update_history=list(data.get("update_history", [])),
            )
        except Exception:
            return PersistedState()

    def _save_persisted_state(self, state: PersistedState) -> None:
        STATE_PATH.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False), encoding="utf-8")

    def _build_menu(self) -> QMenu:
        menu = QMenu()

        self.status_action = QAction()
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)

        self.last_check_action = QAction()
        self.last_check_action.setEnabled(False)
        menu.addAction(self.last_check_action)

        menu.addSeparator()

        self.check_action = QAction("Check again")
        self.check_action.triggered.connect(self.check_for_updates)
        menu.addAction(self.check_action)

        self.show_updates_action = QAction("Show pending updates")
        self.show_updates_action.triggered.connect(self.show_updates)
        menu.addAction(self.show_updates_action)

        self.news_action = QAction("Show package news")
        self.news_action.triggered.connect(self.show_package_news)
        menu.addAction(self.news_action)

        self.update_action = QAction("Download updates")
        self.update_action.triggered.connect(self.run_system_update)
        menu.addAction(self.update_action)

        self.rollback_action = QAction("Manage rollback and removal")
        self.rollback_action.triggered.connect(self.run_rollback)
        menu.addAction(self.rollback_action)

        self.log_action = QAction("Show saved update log")
        self.log_action.triggered.connect(self.show_saved_log_dialog)
        menu.addAction(self.log_action)

        self.manage_logs_action = QAction("Manage saved logs")
        self.manage_logs_action.triggered.connect(self.manage_saved_logs)
        menu.addAction(self.manage_logs_action)

        self.live_log_action = QAction("Show live pacman log")
        self.live_log_action.triggered.connect(self.show_live_pacman_log)
        menu.addAction(self.live_log_action)

        self.clear_restart_action = QAction("Clear restart notification")
        self.clear_restart_action.triggered.connect(self.clear_restart_notification)
        menu.addAction(self.clear_restart_action)

        menu.addSeparator()

        self.settings_action = QAction("Settings")
        self.settings_action.triggered.connect(self.open_settings)
        menu.addAction(self.settings_action)

        self.launcher_action = QAction("Install app launcher")
        self.launcher_action.triggered.connect(self.install_launcher)
        menu.addAction(self.launcher_action)

        self.autostart_action = QAction("Enable autostart")
        self.autostart_action.triggered.connect(self.enable_autostart)
        menu.addAction(self.autostart_action)

        self.disable_autostart_action = QAction("Disable autostart")
        self.disable_autostart_action.triggered.connect(self.disable_autostart)
        menu.addAction(self.disable_autostart_action)

        menu.addSeparator()

        self.quit_action = QAction("Quit")
        self.quit_action.triggered.connect(self.app.quit)
        menu.addAction(self.quit_action)
        return menu

    def _reset_check_timer(self) -> None:
        self.timer.start(self.config.interval_minutes * 60 * 1000)

    def _startup_check(self) -> None:
        self.check_for_updates(silent=True)

    def _handle_tray_activation(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._handle_left_click()

    def _handle_left_click(self) -> None:
        if not self.state.packages:
            self.show_updates()
            return

        dialog = QMessageBox()
        dialog.setWindowTitle(APP_NAME)
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setText(f"{self.state.update_count} pending updates found.")

        info_lines = ["Select an action."]
        if self.state.reboot_recommended:
            info_lines.append("")
            info_lines.append("A system restart is also recommended.")
            if self.state.reboot_reason:
                info_lines.append(f"Reason: {self.state.reboot_reason}")
        dialog.setInformativeText("\n".join(info_lines))

        download_button = dialog.addButton("Download updates", QMessageBox.ButtonRole.AcceptRole)
        news_button = dialog.addButton("Check news", QMessageBox.ButtonRole.ActionRole)
        list_button = dialog.addButton("Show list", QMessageBox.ButtonRole.ActionRole)
        dialog.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(download_button)
        dialog.exec()

        clicked = dialog.clickedButton()
        if clicked is download_button:
            self.run_system_update()
        elif clicked is news_button:
            self.show_package_news()
        elif clicked is list_button:
            self.show_updates()

    def _set_state_icon(self, state: str) -> None:
        colors = {
            "idle": ("#115e59", "#ecfdf5", "#34d399"),
            "checking": ("#0f766e", "#ccfbf1", "#14b8a6"),
            "updates": ("#1d4ed8", "#dbeafe", "#60a5fa"),
            "reboot": ("#c2410c", "#ffedd5", "#fb923c"),
            "error": ("#b91c1c", "#fee2e2", "#ef4444"),
            "running": ("#6d28d9", "#f3e8ff", "#a78bfa"),
        }
        bg_color, fg_color, accent_color = colors[state]
        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.setBrush(QColor(bg_color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(4, 4, 56, 56)

        painter.setBrush(QColor(fg_color))
        painter.drawEllipse(10, 10, 44, 44)

        painter.setPen(QColor(accent_color))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if state == "checking":
            painter.setPen(QColor(accent_color))
            painter.drawArc(16, 16, 32, 32, 35 * 16, 250 * 16)
            painter.setBrush(QColor(accent_color))
            painter.drawEllipse(39, 17, 8, 8)
        elif state == "updates":
            painter.setBrush(QColor(accent_color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(22, 14, 20, 24, 5, 5)
            painter.drawRect(30, 38, 4, 10)
            painter.drawRect(25, 34, 14, 5)
            painter.drawRect(27, 29, 10, 5)
            badge_text = self._update_badge_text()
            self._draw_badge(painter, badge_text, "#0f172a", "#f8fafc")
        elif state == "reboot":
            painter.setPen(QColor(accent_color))
            painter.drawArc(16, 16, 32, 32, 45 * 16, 270 * 16)
            painter.drawLine(36, 15, 44, 18)
            painter.drawLine(44, 18, 38, 24)
        elif state == "error":
            painter.setPen(QColor(accent_color))
            painter.drawLine(22, 22, 42, 42)
            painter.drawLine(42, 22, 22, 42)
        elif state == "running":
            painter.setBrush(QColor(accent_color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(18, 27, 7, 7)
            painter.drawEllipse(29, 27, 7, 7)
            painter.drawEllipse(40, 27, 7, 7)
            badge_text = self._update_badge_text()
            if self.state.update_count:
                self._draw_badge(painter, badge_text, "#581c87", "#faf5ff")
        else:
            painter.setPen(QColor(accent_color))
            painter.drawLine(22, 32, 29, 39)
            painter.drawLine(29, 39, 42, 24)

        painter.end()
        self.tray.setIcon(QIcon(pixmap))

    def _draw_badge(self, painter: QPainter, text: str, bg_color: str, fg_color: str) -> None:
        painter.setBrush(QColor(bg_color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(38, 4, 22, 22)
        painter.setPen(QColor(fg_color))
        font = painter.font()
        font.setBold(True)
        font.setPointSize(9 if len(text) < 3 else 7)
        painter.setFont(font)
        painter.drawText(38, 4, 22, 22, int(Qt.AlignmentFlag.AlignCenter), text)

    def _update_badge_text(self) -> str:
        if self.state.update_count <= 0:
            return ""
        if self.state.update_count > 99:
            return "99+"
        return str(self.state.update_count)

    def _runtime_reboot_state(self) -> tuple[bool, str]:
        worker = CheckUpdatesWorker(self.config.ignored_packages)
        return worker._detect_reboot_required(list(self.persisted.pending_update_packages))

    def _current_icon_state(self) -> str:
        if self.update_watch_timer.isActive():
            return "running"
        if self.persisted.last_check_status == "error":
            return "error"
        if self.state.update_count:
            return "updates"
        if self.state.reboot_recommended:
            return "reboot"
        return "idle"

    def _refresh_menu_labels(self) -> None:
        if self.update_watch_timer.isActive():
            status_text = "Status: update is running"
        elif self.state.reboot_recommended:
            reason = f" ({self.state.reboot_reason})" if self.state.reboot_reason else ""
            status_text = f"Status: restart recommended{reason}"
        elif self.state.update_count:
            status_text = f"Status: {self.state.update_count} updates available"
        elif self.persisted.last_check_status == "error":
            status_text = "Status: last check failed"
        else:
            status_text = "Status: system is up to date"

        last_check = self.state.last_check_at or "never"
        self.status_action.setText(status_text)
        self.last_check_action.setText(f"Last check: {last_check}")
        self.tray.setToolTip(f"{APP_NAME}\n{status_text}\nLast check: {last_check}")

        self.show_updates_action.setText(f"Show pending updates ({self.state.update_count})")
        rollback_count = len(self._rollback_transaction_history())
        self.rollback_action.setEnabled(rollback_count > 0)
        self.rollback_action.setText(f"Manage rollback and removal ({rollback_count})")
        log_count = len(self._log_history_entries())
        self.log_action.setEnabled(log_count > 0)
        self.manage_logs_action.setEnabled(log_count > 0)
        self.manage_logs_action.setText(f"Manage saved logs ({log_count})")
        self.news_action.setEnabled(bool(self.state.packages))
        self.clear_restart_action.setEnabled(self.state.reboot_recommended)
        self.disable_autostart_action.setEnabled(AUTOSTART_PATH.exists())
        self.autostart_action.setEnabled(not AUTOSTART_PATH.exists())

    def _write_desktop_file(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        exec_command = str(INSTALLED_RUNNER_PATH if INSTALLED_RUNNER_PATH.exists() else Path(sys.argv[0]).resolve())
        desktop_text = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            "Comment=Tray app for pacman update checks\n"
            f'Exec="{exec_command}"\n'
            f"Path={BASE_DIR}\n"
            f"Icon={BASE_DIR / 'icon.svg'}\n"
            "Terminal=false\n"
            "Categories=System;Utility;\n"
            "StartupNotify=false\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
        target.write_text(desktop_text, encoding="utf-8")

    def install_launcher(self) -> None:
        self._write_desktop_file(LAUNCHER_PATH)
        self.tray.showMessage(APP_NAME, f"Launcher created at {LAUNCHER_PATH}", QSystemTrayIcon.MessageIcon.Information, 4000)

    def enable_autostart(self) -> None:
        self._write_desktop_file(AUTOSTART_PATH)
        self._refresh_menu_labels()
        self.tray.showMessage(APP_NAME, "Autostart enabled.", QSystemTrayIcon.MessageIcon.Information, 4000)

    def disable_autostart(self) -> None:
        if AUTOSTART_PATH.exists():
            AUTOSTART_PATH.unlink()
        self._refresh_menu_labels()
        self.tray.showMessage(APP_NAME, "Autostart disabled.", QSystemTrayIcon.MessageIcon.Information, 4000)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.config)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.config = dialog.result_config()
        except ValueError:
            QMessageBox.warning(None, APP_NAME, "Interval and log retention must be numbers.")
            return
        self._save_config(self.config)
        self._cleanup_old_logs()
        self.state.ignored_packages = list(self.config.ignored_packages)
        self._reset_check_timer()
        self.tray.showMessage(APP_NAME, "Settings saved.", QSystemTrayIcon.MessageIcon.Information, 3000)
        self.check_for_updates(silent=True)

    def check_for_updates(self, silent: bool = False) -> None:
        if self.worker is not None and self.worker.isRunning():
            return

        self._set_state_icon("checking")
        self.status_action.setText("Status: checking for updates...")
        self.persisted.last_check_status = "checking"
        self._save_persisted_state(self.persisted)

        self.worker = CheckUpdatesWorker(self.config.ignored_packages)
        self.worker.completed.connect(lambda state: self._handle_check_result(state, silent))
        self.worker.error.connect(lambda message: self._handle_check_error(message, silent))
        self.worker.finished.connect(self._clear_worker)
        self.worker.start()

    def _clear_worker(self) -> None:
        self.worker = None

    def _handle_check_result(self, state: UpdateState, silent: bool) -> None:
        if not state.reboot_recommended:
            self.persisted.restart_notification_cleared = False
        visible_reboot = state.reboot_recommended and not self.persisted.restart_notification_cleared
        self.state = UpdateState(
            packages=list(state.packages),
            ignored_packages=list(state.ignored_packages),
            reboot_recommended=visible_reboot,
            reboot_reason=state.reboot_reason if visible_reboot else "",
            last_check_at=state.last_check_at,
        )
        self.persisted.last_check_at = state.last_check_at
        self.persisted.last_check_status = "ok"
        self.persisted.last_error = ""
        self.persisted.pending_update_packages = list(state.packages)
        self.persisted.reboot_recommended = state.reboot_recommended
        self._save_persisted_state(self.persisted)
        self._set_state_icon(self._current_icon_state())
        self._refresh_menu_labels()

        if state.update_count and not silent:
            self.tray.showMessage(APP_NAME, f"Found {state.update_count} updates.", QSystemTrayIcon.MessageIcon.Information, 4000)
        elif not state.update_count and not silent:
            self.tray.showMessage(APP_NAME, "System is up to date.", QSystemTrayIcon.MessageIcon.Information, 3500)

    def _handle_check_error(self, message: str, silent: bool) -> None:
        self.persisted.last_check_status = "error"
        self.persisted.last_error = message
        self.persisted.last_check_at = self._now()
        self._save_persisted_state(self.persisted)
        self._set_state_icon("error")
        self._refresh_menu_labels()
        if not silent:
            self.tray.showMessage(APP_NAME, f"Update check failed: {message}", QSystemTrayIcon.MessageIcon.Warning, 6000)

    def show_updates(self) -> None:
        ignored_note = ""
        if self.config.ignored_packages:
            ignored_note = f"\n\nIgnored packages: {', '.join(self.config.ignored_packages)}"
        reboot_note = ""
        if self.state.reboot_recommended:
            reboot_note = "\n\nSystem restart is recommended."
            if self.state.reboot_reason:
                reboot_note += f"\nReason: {self.state.reboot_reason}"
        if not self.state.packages:
            QMessageBox.information(None, APP_NAME, f"No pending updates.{reboot_note}{ignored_note}")
            return

        packages = "\n".join(self.state.packages)
        QMessageBox.information(None, APP_NAME, f"Pending updates ({self.state.update_count}):\n\n{packages}{reboot_note}{ignored_note}")

    def show_package_news(self) -> None:
        if not self.state.packages:
            QMessageBox.information(None, APP_NAME, "No pending updates are available.")
            return

        sections: list[str] = []
        for package_line in self.state.packages[:12]:
            package_name = self._package_name(package_line)
            changelog = self._read_package_changelog(package_name)
            sections.append(f"== {package_name} ==\n{changelog}")

        if len(self.state.packages) > 12:
            sections.append(f"... {self.state.update_count - 12} more packages are not shown.")

        self._show_text_dialog("Package Changes", "\n\n".join(sections))

    def show_saved_log_dialog(self) -> None:
        entries = self._log_history_entries()
        if not entries:
            QMessageBox.information(None, APP_NAME, "No saved update logs are available.")
            return

        selected_entry = self._select_history_entry("Open update log:", entries)
        if selected_entry is None:
            return

        self._show_log_path(self._log_path_from_entry(selected_entry), f"Saved Update Log: {self._history_label(selected_entry)}", live=False)

    def manage_saved_logs(self) -> None:
        entries = self._log_history_entries()
        if not entries:
            QMessageBox.information(None, APP_NAME, "No saved update logs are available.")
            return

        dialog = LogManagementDialog(list(reversed(entries)))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        selected_entries = dialog.selected_entries()
        removed_count = self._delete_selected_logs(selected_entries)
        if removed_count:
            self.tray.showMessage(APP_NAME, f"Deleted {removed_count} saved logs.", QSystemTrayIcon.MessageIcon.Information, 4000)
        else:
            QMessageBox.information(None, APP_NAME, "No saved logs were deleted.")

    def show_live_pacman_log(self) -> None:
        self._show_log_path(PACMAN_LOG_PATH, "Live Pacman Log", live=True)

    def clear_restart_notification(self) -> None:
        if not self.state.reboot_recommended:
            QMessageBox.information(None, APP_NAME, "There is no active restart notification.")
            return

        self.persisted.restart_notification_cleared = True
        self.state.reboot_recommended = False
        self.state.reboot_reason = ""
        self._save_persisted_state(self.persisted)
        self._set_state_icon(self._current_icon_state())
        self._refresh_menu_labels()
        self.tray.showMessage(APP_NAME, "Restart notification cleared.", QSystemTrayIcon.MessageIcon.Information, 3000)

    def run_system_update(self) -> None:
        if self.update_watch_timer.isActive():
            QMessageBox.information(None, APP_NAME, "An update is already running in an external terminal.")
            return

        started_at = self._now()
        log_path = self._log_path_for_timestamp(started_at)
        terminal_command = self._build_terminal_command(self._build_update_script(log_path))
        if terminal_command is None:
            QMessageBox.warning(
                None,
                APP_NAME,
                "No supported terminal emulator was found.",
            )
            return

        package_summary = "\n".join(self.state.packages[:15]) if self.state.packages else "Package list is not available yet."
        if self.state.update_count > 15:
            package_summary += f"\n... and {self.state.update_count - 15} more"

        reply = QMessageBox.question(
            None,
            APP_NAME,
            "Run `sudo pacman -Syu` in a terminal?\n\n"
            f"Pending updates: {self.state.update_count}\n\n{package_summary}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.persisted.last_update_started_at = started_at
        self.persisted.last_update_finished_at = ""
        self.persisted.last_update_exit_code = None
        self.persisted.pending_update_packages = list(self.state.packages)
        self._save_persisted_state(self.persisted)
        self._write_status_file({"status": "running", "started_at": self.persisted.last_update_started_at, "log_path": str(log_path)})
        if LOG_PATH.exists():
            LOG_PATH.unlink()

        try:
            subprocess.Popen(terminal_command, cwd=BASE_DIR)
        except Exception as exc:
            QMessageBox.critical(None, APP_NAME, f"Failed to start terminal: {exc}")
            return

        self.update_watch_timer.start(2000)
        self._set_state_icon("running")
        self._refresh_menu_labels()
        self._show_log_path(log_path, f"Saved Update Log: {started_at}", live=False)

    def run_rollback(self) -> None:
        history = self._rollback_transaction_history()
        if not history:
            QMessageBox.information(None, APP_NAME, "No rollback-capable pacman transactions were found.")
            return

        selected_entry = self._select_history_entry("Select a transaction:", history)
        if selected_entry is None:
            return

        selection_dialog = RollbackSelectionDialog(selected_entry)
        if selection_dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected_events = selection_dialog.selected_events()
        action = selection_dialog.selected_action()

        cache_files, removable_packages, missing = self._resolve_rollback_transaction(selected_events, action)
        if missing:
            QMessageBox.warning(
                None,
                APP_NAME,
                "Rollback cannot proceed because these package versions are missing from the pacman cache:\n\n" + "\n".join(missing),
            )
            return

        terminal_command = self._build_terminal_command(self._build_rollback_script(cache_files, removable_packages, action))
        if terminal_command is None:
            QMessageBox.warning(None, APP_NAME, "No supported terminal emulator was found.")
            return

        summary = self._events_summary(selected_events)
        title = "Run rollback?" if action == "rollback" else "Remove selected packages?"
        body = (
            "Rollback uses the pacman cache. Installed packages are downgraded to the latest older cached version when available.\n\n"
            if action == "rollback"
            else "This will fully remove the selected packages with `sudo pacman -Rns`.\n\n"
        )
        reply = QMessageBox.question(
            None,
            APP_NAME,
            f"{title}\n\n{body}{summary}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        subprocess.Popen(terminal_command, cwd=BASE_DIR)

    def _read_package_changelog(self, package_name: str) -> str:
        pacman_path = shutil.which("pacman")
        if not pacman_path:
            return "pacman binary not found."
        try:
            result = subprocess.run([pacman_path, "-Qc", package_name], capture_output=True, text=True, check=False, timeout=20)
        except Exception as exc:
            return f"Failed to read changelog: {exc}"

        text = (result.stdout or result.stderr).strip()
        if not text:
            return "No changelog is available for this package."

        lines = text.splitlines()[:PACMAN_CHANGELOG_LINES]
        return "\n".join(lines)

    def _show_text_dialog(self, title: str, text: str) -> None:
        dialog = QDialog()
        dialog.setWindowTitle(title)
        dialog.resize(900, 620)
        layout = QVBoxLayout(dialog)
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setFont(QFont("monospace"))
        editor.setPlainText(text)
        layout.addWidget(editor)
        close_button = QPushButton("Close")
        close_button.clicked.connect(dialog.close)
        layout.addWidget(close_button)
        dialog.exec()

    def _resolve_rollback_transaction(self, selected_events: list[dict[str, object]], action_mode: str) -> tuple[list[str], list[str], list[str]]:
        cache_files: list[str] = []
        removable_packages: list[str] = []
        missing: list[str] = []
        for event in selected_events:
            action = str(event.get("action", ""))
            package_name = str(event.get("package", ""))
            old_version = str(event.get("old_version", ""))
            new_version = str(event.get("new_version", ""))

            if action_mode == "remove":
                removable_packages.append(package_name)
                continue

            if action in {"upgraded", "downgraded"}:
                target_version = old_version
                matches = find_cached_package_files(package_name, target_version)
                if not matches:
                    missing.append(f"{package_name} {target_version}")
                    continue
                cache_files.append(matches[-1])
            elif action == "installed":
                previous_matches = find_previous_cached_package_files(package_name, new_version)
                if not previous_matches:
                    missing.append(f"{package_name} previous cached version before {new_version}")
                    continue
                cache_files.append(previous_matches[-1])
            elif action == "reinstalled":
                matches = find_cached_package_files(package_name, new_version)
                if not matches:
                    missing.append(f"{package_name} {new_version}")
                    continue
                cache_files.append(matches[-1])

        return cache_files, removable_packages, missing

    def _build_update_script(self, log_path: Path) -> str:
        status_file = shlex.quote(str(STATUS_PATH))
        log_file = shlex.quote(str(log_path))
        latest_log_file = shlex.quote(str(LOG_PATH))
        return (
            f"echo '==> Running sudo pacman -Syu' | tee {log_file}; "
            f"sudo pacman -Syu 2>&1 | tee -a {log_file}; "
            "exit_code=${PIPESTATUS[0]}; "
            f"cp {log_file} {latest_log_file}; "
            f"printf '{{\"status\":\"finished\",\"exit_code\":%s,\"finished_at\":\"%s\",\"log_path\":\"%s\"}}' \"$exit_code\" \"$(date '+%Y-%m-%d %H:%M:%S')\" {log_file} > {status_file}; "
            "echo; "
            "if [ \"$exit_code\" -eq 0 ]; then echo 'Update finished.'; else echo \"Update failed with exit code $exit_code.\"; fi; "
            "read -rp 'Press Enter to close...' _"
        )

    def _build_rollback_script(self, cache_files: list[str], removable_packages: list[str], action_mode: str) -> str:
        commands: list[str] = ["echo '==> Running package management workflow'"]
        if removable_packages:
            remove_cmd = "sudo pacman -Rns" if action_mode == "remove" else "sudo pacman -R"
            commands.append(f"{remove_cmd} {' '.join(shlex.quote(pkg) for pkg in removable_packages)}")
            commands.append("remove_exit=$?")
            commands.append("if [ \"$remove_exit\" -ne 0 ]; then echo \"Package removal failed with exit code $remove_exit.\"; read -rp 'Press Enter to close...' _; exit \"$remove_exit\"; fi")
        if cache_files:
            commands.append(f"sudo pacman -U {' '.join(shlex.quote(path) for path in cache_files)}")
            commands.append("rollback_exit=$?")
        else:
            commands.append("rollback_exit=0")
        commands.append("echo")
        commands.append("if [ \"$rollback_exit\" -eq 0 ]; then echo 'Rollback completed successfully.'; else echo \"Rollback failed with exit code $rollback_exit.\"; fi")
        commands.append("read -rp 'Press Enter to close...' _")
        return "; ".join(commands)

    def _build_terminal_command(self, script: str) -> list[str] | None:
        for terminal in TERMINALS:
            path = shutil.which(terminal)
            if not path:
                continue

            if terminal == "kitty":
                return [path, "bash", "-lc", script]
            if terminal == "xfce4-terminal":
                return [path, "--command", f"bash -lc {shlex.quote(script)}"]
            if terminal in {"alacritty", "konsole", "xterm"}:
                return [path, "-e", "bash", "-lc", script]
            if terminal == "gnome-terminal":
                return [path, "--", "bash", "-lc", script]
            if terminal == "wezterm":
                return [path, "start", "--always-new-process", "--", "bash", "-lc", script]
        return None

    def _poll_update_status(self) -> None:
        if not STATUS_PATH.exists():
            return
        try:
            status_data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        if status_data.get("status") != "finished":
            return

        self.update_watch_timer.stop()
        exit_code = int(status_data.get("exit_code", 1))
        finished_at = str(status_data.get("finished_at", self._now()))
        self.persisted.last_update_finished_at = finished_at
        self.persisted.last_update_exit_code = exit_code
        log_path = str(status_data.get("log_path", self._log_path_for_timestamp(self.persisted.last_update_started_at)))
        if exit_code == 0:
            self.persisted.last_update_packages = list(self.persisted.pending_update_packages)
            self.persisted.update_history.append(
                {
                    "finished_at": finished_at,
                    "packages": list(self.persisted.pending_update_packages),
                    "package_events": self._package_lines_to_events(self.persisted.pending_update_packages),
                    "log_path": log_path,
                    "source": "app-update",
                }
            )
            self.persisted.update_history = self.persisted.update_history[-20:]
            self.tray.showMessage(APP_NAME, "Update finished. Checking again.", QSystemTrayIcon.MessageIcon.Information, 5000)
            self.check_for_updates(silent=True)
        else:
            self.persisted.update_history.append(
                {
                    "finished_at": finished_at,
                    "packages": list(self.persisted.pending_update_packages),
                    "package_events": self._package_lines_to_events(self.persisted.pending_update_packages),
                    "log_path": log_path,
                    "failed": True,
                    "source": "app-update",
                }
            )
            self.persisted.update_history = self.persisted.update_history[-20:]
            self.tray.showMessage(APP_NAME, f"Update failed with exit code {exit_code}.", QSystemTrayIcon.MessageIcon.Warning, 6000)
        self._save_persisted_state(self.persisted)
        reboot_recommended, reboot_reason = self._runtime_reboot_state()
        raw_reboot = reboot_recommended or self.persisted.reboot_recommended
        if not raw_reboot:
            self.persisted.restart_notification_cleared = False
            self._save_persisted_state(self.persisted)
        self.state.reboot_recommended = raw_reboot and not self.persisted.restart_notification_cleared
        self.state.reboot_reason = reboot_reason if self.state.reboot_recommended else ""
        self._set_state_icon(self._current_icon_state())
        self._refresh_menu_labels()

    def _write_status_file(self, payload: dict[str, object]) -> None:
        STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _package_name(self, package_line: str) -> str:
        return package_line.split()[0]

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _history_label(self, entry: dict[str, object]) -> str:
        finished_at = str(entry.get("finished_at", "unknown date"))
        failed = " failed" if entry.get("failed") else ""
        packages = entry.get("packages")
        package_events = entry.get("package_events")
        count = len(packages) if isinstance(packages, list) else len(package_events) if isinstance(package_events, list) else 0
        if entry.get("source") == "app-update":
            source = " update"
        elif entry.get("source") == "pacman-log":
            source = " pacman"
        else:
            source = ""
        return f"{finished_at} ({count} items{failed}{source})"

    def _select_history_entry(self, prompt: str, history_entries: list[dict[str, object]]) -> dict[str, object] | None:
        if not history_entries:
            return None
        history = list(reversed(history_entries))
        options = [self._history_label(entry) for entry in history]
        selection, ok = QInputDialog.getItem(None, APP_NAME, prompt, options, 0, False)
        if not ok or not selection:
            return None
        return next(entry for entry in history if self._history_label(entry) == selection)

    def _show_log_path(self, log_path: Path, title: str, live: bool) -> None:
        dialog_attr = "live_log_dialog" if live else "log_dialog"
        dialog = getattr(self, dialog_attr)
        if dialog is None:
            dialog = LogDialog()
            setattr(self, dialog_attr, dialog)
        dialog.set_log_path(log_path, title)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _log_path_for_timestamp(self, timestamp: str) -> Path:
        safe = timestamp.replace(" ", "_").replace(":", "-")
        return LOGS_DIR / f"update-{safe}.log"

    def _log_path_from_entry(self, entry: dict[str, object]) -> Path:
        log_path = entry.get("log_path")
        if isinstance(log_path, str) and log_path:
            return Path(log_path)
        return self._log_path_for_timestamp(str(entry.get("finished_at", self._now())))

    def _log_history_entries(self) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        for entry in self.persisted.update_history:
            log_path = entry.get("log_path")
            if not isinstance(log_path, str) or not log_path:
                continue
            if Path(log_path).exists():
                entries.append(entry)
        return entries

    def _cleanup_old_logs(self) -> None:
        retention_days = max(0, self.config.log_retention_days)
        if retention_days <= 0:
            self._prune_missing_log_references()
            return

        cutoff = datetime.now() - timedelta(days=retention_days)
        changed = False
        for entry in self.persisted.update_history:
            log_path = entry.get("log_path")
            if not isinstance(log_path, str) or not log_path:
                continue
            path = Path(log_path)
            if not path.exists():
                entry.pop("log_path", None)
                changed = True
                continue
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime)
            except Exception:
                continue
            if modified < cutoff:
                try:
                    path.unlink()
                except OSError:
                    continue
                entry.pop("log_path", None)
                changed = True

        if changed:
            self._save_persisted_state(self.persisted)

    def _prune_missing_log_references(self) -> None:
        changed = False
        for entry in self.persisted.update_history:
            log_path = entry.get("log_path")
            if not isinstance(log_path, str) or not log_path:
                continue
            if not Path(log_path).exists():
                entry.pop("log_path", None)
                changed = True
        if changed:
            self._save_persisted_state(self.persisted)

    def _delete_selected_logs(self, entries: list[dict[str, object]]) -> int:
        removed_count = 0
        changed = False
        for entry in entries:
            log_path = entry.get("log_path")
            if not isinstance(log_path, str) or not log_path:
                continue
            path = Path(log_path)
            if path.exists():
                try:
                    path.unlink()
                    removed_count += 1
                except OSError:
                    continue
            entry.pop("log_path", None)
            changed = True

        if changed:
            self._save_persisted_state(self.persisted)
            self._refresh_menu_labels()
        return removed_count

    def _parse_pacman_log_transactions(self) -> list[dict[str, object]]:
        if not PACMAN_LOG_PATH.exists():
            return []
        try:
            lines = PACMAN_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return []

        transactions: list[dict[str, object]] = []
        current: dict[str, object] | None = None
        for line in lines:
            if "[ALPM] transaction started" in line:
                current = {
                    "source": "pacman-log",
                    "started_at": self._line_timestamp(line),
                    "finished_at": self._line_timestamp(line),
                    "package_events": [],
                    "failed": False,
                    "reboot_recommended": False,
                }
                continue
            if current is None:
                continue
            if "[ALPM] transaction completed" in line:
                current["finished_at"] = self._line_timestamp(line)
                if current["package_events"]:
                    transactions.append(current)
                current = None
                continue
            if "Reboot is recommended" in line:
                current["reboot_recommended"] = True
                continue

            event = self._parse_pacman_event(line)
            if event is not None:
                current["package_events"].append(event)

        return transactions[-PACMAN_LOG_TRANSACTION_LIMIT:]

    def _parse_pacman_event(self, line: str) -> dict[str, object] | None:
        match = PACMAN_EVENT_RE.search(line)
        if not match:
            return None

        action = match.group("action")
        package = match.group("package")
        versions = match.group("versions")
        old_version = ""
        new_version = ""

        if action in {"upgraded", "downgraded"}:
            old_version, new_version = [part.strip() for part in versions.split("->", 1)]
        elif action in {"installed", "reinstalled"}:
            new_version = versions.strip()
        elif action == "removed":
            old_version = versions.strip()

        return {
            "action": action,
            "package": package,
            "old_version": old_version,
            "new_version": new_version,
            "display": self._format_pacman_event(action, package, old_version, new_version),
        }

    def _format_pacman_event(self, action: str, package: str, old_version: str, new_version: str) -> str:
        if action in {"upgraded", "downgraded"}:
            return f"{package} {old_version} -> {new_version}"
        if action in {"installed", "reinstalled"}:
            return f"{action} {package} {new_version}"
        return f"{action} {package} {old_version}"

    def _line_timestamp(self, line: str) -> str:
        if line.startswith("[") and "]" in line:
            return line[1 : line.index("]")]
        return self._now()

    def _rollback_transaction_history(self) -> list[dict[str, object]]:
        history: list[dict[str, object]] = []
        seen_keys: set[tuple[str, int]] = set()
        for entry in self.persisted.update_history:
            events = entry.get("package_events")
            if entry.get("failed"):
                continue
            if isinstance(events, list) and events:
                key = (str(entry.get("finished_at", "")), len(events))
                seen_keys.add(key)
                history.append(entry)

        for transaction in self._parse_pacman_log_transactions():
            events = transaction.get("package_events", [])
            if any(isinstance(event, dict) and event.get("action") in {"upgraded", "downgraded", "installed", "reinstalled"} for event in events):
                key = (str(transaction.get("finished_at", "")), len(events))
                if key not in seen_keys:
                    history.append(transaction)
        return history

    def _transaction_summary(self, transaction: dict[str, object]) -> str:
        events = transaction.get("package_events", [])
        lines = [str(event.get("display", "")) for event in events if isinstance(event, dict)]
        return "\n".join(lines[:18]) + (f"\n... and {len(lines) - 18} more" if len(lines) > 18 else "")

    def _events_summary(self, events: list[dict[str, object]]) -> str:
        lines = [str(event.get("display", "")) for event in events]
        return "\n".join(lines[:18]) + (f"\n... and {len(lines) - 18} more" if len(lines) > 18 else "")

    def _package_lines_to_events(self, package_lines: list[str]) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        for line in package_lines:
            parts = line.split()
            if len(parts) >= 3 and "->" in line:
                package = parts[0]
                old_version = parts[1]
                new_version = parts[3] if len(parts) >= 4 and parts[2] == "->" else parts[-1]
                events.append(
                    {
                        "action": "upgraded",
                        "package": package,
                        "old_version": old_version,
                        "new_version": new_version,
                        "display": f"{package} {old_version} -> {new_version}",
                    }
                )
            elif parts:
                package = parts[0]
                events.append(
                    {
                        "action": "installed",
                        "package": package,
                        "old_version": "",
                        "new_version": parts[-1] if len(parts) > 1 else "",
                        "display": line,
                    }
                )
        return events

    def run(self) -> int:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.critical(None, APP_NAME, "System tray is not available in this session.")
            return 1
        return self.app.exec()


def main() -> int:
    return TrayApp().run()


if __name__ == "__main__":
    raise SystemExit(main())
