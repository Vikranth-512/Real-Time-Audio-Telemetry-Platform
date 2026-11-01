"""
session_history.py
------------------
UI for viewing, filtering, and exporting historical sessions.
"""

import os
import platform
import subprocess
import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Slot, QThread
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableView, QHeaderView,
    QPushButton, QLabel, QLineEdit, QSplitter, QFrame, QMessageBox,
    QFileDialog, QProgressDialog, QMenu
)

from app.storage.session_manager import SessionManager
from app.storage.export_worker import ExportWorker


class SessionTableModel(QAbstractTableModel):
    def __init__(self, sessions=None):
        super().__init__()
        self._data = sessions or []
        self._headers = ["Date / Time", "Duration", "Source", "Device", "RMS", "Peak", "Freq", "Status"]

    def set_data(self, sessions: list[dict]):
        self.beginResetModel()
        self._data = sessions
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return len(self._data)

    def columnCount(self, parent=QModelIndex()):
        return len(self._headers)

    def data(self, index, role):
        if not index.isValid():
            return None
            
        row = self._data[index.row()]
        col = index.column()
        
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                dt = row.get("start_time", "")
                try:
                    return datetime.datetime.fromisoformat(dt).strftime("%b %d, %H:%M:%S")
                except: 
                    return dt
            elif col == 1:
                dur = row.get("duration_seconds", 0)
                m, s = divmod(int(dur), 60)
                return f"{m:02d}:{s:02d}"
            elif col == 2: return row.get("source", "")
            elif col == 3: return row.get("device", "")
            
            summary = row.get("summary", {})
            if col == 4: return f"{summary.get('rms_mean', 0):.3f}" if summary else "—"
            elif col == 5: return f"{summary.get('peak_max', 0):.3f}" if summary else "—"
            elif col == 6: return f"{summary.get('dominant_frequency_mean', 0):.1f}" if summary else "—"
            elif col == 7: return row.get("status", "").upper()
            
        elif role == Qt.ItemDataRole.UserRole:
            return row
            
        return None

    def headerData(self, section, orientation, role):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self._headers[section]
        return None


class SessionHistoryWindow(QWidget):
    def __init__(self, session_manager: SessionManager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Session History")
        self.resize(1000, 600)
        
        self._session_manager = session_manager
        
        self._export_worker = None
        self._export_thread = None
        self._replay_windows = set()
        
        self._build_ui()
        self._apply_stylesheet()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        # ── Toolbar ──────────────────────────────────────────────────────────
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Search:"))
        
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Filter by date, device, status...")
        self._search_input.setFixedWidth(250)
        self._search_input.textChanged.connect(self._on_search)
        toolbar.addWidget(self._search_input)
        
        toolbar.addStretch()
        
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        toolbar.addWidget(self._refresh_btn)
        
        root.addLayout(toolbar)

        # ── Splitter ──────────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Table
        self._model = SessionTableModel()
        self._proxy = QSortFilterProxyModel()
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterKeyColumn(-1) # Filter all columns
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        
        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setAlternatingRowColors(True)
        self._table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.doubleClicked.connect(self._on_table_double_clicked)
        
        splitter.addWidget(self._table)
        
        # Details Panel
        details_panel = QFrame()
        details_panel.setObjectName("details-panel")
        details_layout = QVBoxLayout(details_panel)
        
        self._details_text = QLabel("Select a session to view details.")
        self._details_text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._details_text.setWordWrap(True)
        details_layout.addWidget(self._details_text, stretch=1)
        
        # Actions
        actions_layout = QHBoxLayout()
        self._replay_btn = QPushButton("Replay")
        self._replay_btn.setObjectName("replay-btn")
        self._replay_btn.setEnabled(False)
        self._replay_btn.clicked.connect(self._on_replay)

        self._export_btn = QPushButton("Export CSV")
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._on_export)
        
        self._open_folder_btn = QPushButton("Open Folder")
        self._open_folder_btn.setEnabled(False)
        self._open_folder_btn.clicked.connect(self._on_open_folder)
        
        self._delete_btn = QPushButton("Delete")
        self._delete_btn.setEnabled(False)
        self._delete_btn.setObjectName("delete-btn")
        self._delete_btn.clicked.connect(self._on_delete)
        
        actions_layout.addWidget(self._replay_btn)
        actions_layout.addWidget(self._export_btn)
        actions_layout.addWidget(self._open_folder_btn)
        actions_layout.addWidget(self._delete_btn)
        
        details_layout.addLayout(actions_layout)
        
        splitter.addWidget(details_panel)
        splitter.setSizes([700, 300])
        
        root.addWidget(splitter)

    @Slot()
    def refresh(self):
        sessions = self._session_manager.list_sessions()
        self._model.set_data(sessions)
        
        if len(sessions) == 0:
            self._details_text.setText("No recorded sessions yet.\n\nStart an acquisition session to begin building your session history.")
        
        # Sort by Date/Time (Column 0) descending by default
        self._table.sortByColumn(0, Qt.SortOrder.DescendingOrder)

    @Slot(str)
    def _on_search(self, text: str):
        self._proxy.setFilterWildcard(f"*{text}*")

    @Slot()
    def _on_selection_changed(self):
        indexes = self._table.selectionModel().selectedRows()
        
        has_selection = len(indexes) > 0
        self._export_btn.setEnabled(has_selection)
        self._delete_btn.setEnabled(has_selection)
        
        # Check if single selection is replayable
        can_replay = False
        if len(indexes) == 1:
            source_idx = self._proxy.mapToSource(indexes[0])
            row_data = self._model.data(source_idx, Qt.ItemDataRole.UserRole)
            if row_data:
                folder = row_data.get("_directory", "")
                parquet_path = Path(folder) / "data.parquet"
                status = str(row_data.get("status", "")).lower()
                if parquet_path.exists() and parquet_path.stat().st_size > 0 and status != "active":
                    can_replay = True

        self._replay_btn.setEnabled(can_replay)
        self._open_folder_btn.setEnabled(len(indexes) == 1)
        
        if not has_selection:
            self._details_text.setText("Select a session to view details.")
            return
            
        if len(indexes) > 1:
            self._details_text.setText(f"{len(indexes)} sessions selected.")
            return

        # Single selection details
        source_index = self._proxy.mapToSource(indexes[0])
        row_data = self._model.data(source_index, Qt.ItemDataRole.UserRole)
        
        if not row_data:
            return
            
        summary = row_data.get("summary", {})
        
        details = f"""
<b>SESSION DETAILS</b><br><br>
<b>Session:</b><br>{row_data.get("session_id", "Unknown")}<br><br>
<b>Start:</b> {row_data.get("start_time", "")}<br>
<b>Duration:</b> {row_data.get("duration_seconds", 0)} seconds<br>
<b>Status:</b> {row_data.get("status", "Unknown").upper()}<br><br>
<b>Source:</b> {row_data.get("source", "")}<br>
<b>Device:</b> {row_data.get("device", "")}<br>
<b>Configuration:</b><br>
{row_data.get("sample_rate", 0)/1000:.1f} kHz | {row_data.get("channels", 1)} ch | {row_data.get("frames_per_buffer", "")} samples<br><br>
"""
        if summary:
            details += f"""
<b>SESSION SUMMARY</b><br>
<table width="100%">
<tr><td>RMS:</td><td>{summary.get("rms_mean", 0):.3f} (mean) / {summary.get("rms_max", 0):.3f} (max)</td></tr>
<tr><td>Peak:</td><td>{summary.get("peak_max", 0):.3f} (max)</td></tr>
<tr><td>Dom Freq:</td><td>{summary.get("dominant_frequency_mean", 0):.1f} Hz</td></tr>
<tr><td>Centroid:</td><td>{summary.get("centroid_mean", 0):.1f} Hz</td></tr>
<tr><td>Rolloff:</td><td>{summary.get("rolloff_mean", 0):.1f} Hz</td></tr>
<tr><td>Flatness:</td><td>{summary.get("flatness_mean", 0):.4f}</td></tr>
<tr><td>ZCR:</td><td>{summary.get("zcr_mean", 0):.4f}</td></tr>
<tr><td>BPM:</td><td>{summary.get("bpm_mean", 0):.1f}</td></tr>
<tr><td>Data Size:</td><td>{summary.get("data_size_mb", 0):.2f} MB</td></tr>
</table>
"""
        elif "error" in row_data:
            details += f"<br><b>Error:</b> {row_data['error']}"
            
        self._details_text.setText(details)

    @Slot()
    def _on_export(self):
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return
            
        selected_sessions = []
        for idx in indexes:
            source_idx = self._proxy.mapToSource(idx)
            row_data = self._model.data(source_idx, Qt.ItemDataRole.UserRole)
            if row_data:
                selected_sessions.append(row_data)
                
        if len(selected_sessions) == 1:
            session = selected_sessions[0]
            parquet_path = Path(session.get("_directory", "")) / "data.parquet"
            default = str(parquet_path.with_suffix(".csv"))
            dest, _ = QFileDialog.getSaveFileName(self, "Export Session CSV", default, "CSV Files (*.csv)")
            if not dest:
                return
                
            self._run_export_worker(parquet_path, Path(dest))
        else:
            # Multiple select export
            dest_dir = QFileDialog.getExistingDirectory(self, "Select Export Directory")
            if not dest_dir:
                return
            
            # Simple synchronous loop for MVP, but a real app might chain workers
            # We will use the worker sequentially
            self._export_queue = [(Path(s.get("_directory", "")) / "data.parquet", Path(dest_dir) / f"session_{s.get('session_id')}.csv") for s in selected_sessions]
            self._process_export_queue()

    def _run_export_worker(self, parquet_path: Path, dest: Path):
        self._progress_dialog = QProgressDialog("Exporting CSV...", "Cancel", 0, 100, self)
        self._progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress_dialog.setAutoClose(True)
        self._progress_dialog.setAutoReset(True)
        self._progress_dialog.show()
        
        self._export_thread = QThread()
        self._export_worker = ExportWorker(parquet_path, dest)
        self._export_worker.moveToThread(self._export_thread)
        
        self._export_thread.started.connect(self._export_worker.export)
        self._export_worker.progress.connect(self._progress_dialog.setValue)
        self._export_worker.finished.connect(self._on_export_finished)
        self._export_worker.error.connect(self._on_export_error)
        
        self._export_thread.start()

    def _process_export_queue(self):
        if not hasattr(self, '_export_queue') or not self._export_queue:
            QMessageBox.information(self, "Export Complete", "Multiple sessions exported successfully.")
            return
            
        parquet_path, dest = self._export_queue.pop(0)
        self._run_export_worker(parquet_path, dest)

    @Slot(Path)
    def _on_export_finished(self, out_path: Path):
        if self._export_thread:
            self._export_thread.quit()
            self._export_thread.wait()
            self._export_thread = None
            
        if hasattr(self, '_export_queue') and self._export_queue:
            self._process_export_queue()
        else:
            QMessageBox.information(self, "Export Success", f"Session exported to:\n{out_path.name}")

    @Slot(str)
    def _on_export_error(self, msg: str):
        if self._export_thread:
            self._export_thread.quit()
            self._export_thread.wait()
            self._export_thread = None
        QMessageBox.critical(self, "Export Error", f"Export failed:\n{msg}")

    @Slot()
    def _on_open_folder(self):
        indexes = self._table.selectionModel().selectedRows()
        if len(indexes) != 1:
            return
            
        source_idx = self._proxy.mapToSource(indexes[0])
        row_data = self._model.data(source_idx, Qt.ItemDataRole.UserRole)
        folder = row_data.get("_directory")
        if not folder or not Path(folder).exists():
            QMessageBox.warning(self, "Error", "Directory not found.")
            return
            
        # Cross-platform open folder
        if platform.system() == "Windows":
            os.startfile(folder)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])

    @Slot()
    def _on_delete(self):
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return
            
        reply = QMessageBox.question(
            self, "Confirm Delete", 
            f"Are you sure you want to permanently delete {len(indexes)} session(s)?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            for idx in indexes:
                source_idx = self._proxy.mapToSource(idx)
                row_data = self._model.data(source_idx, Qt.ItemDataRole.UserRole)
                session_id = row_data.get("session_id")
                if session_id:
                    self._session_manager.delete_session(session_id)
            self.refresh()

    @Slot()
    def _on_replay(self):
        indexes = self._table.selectionModel().selectedRows()
        if len(indexes) != 1:
            return
        source_idx = self._proxy.mapToSource(indexes[0])
        row_data = self._model.data(source_idx, Qt.ItemDataRole.UserRole)
        if row_data:
            self._open_replay_for_session(row_data)

    @Slot(QModelIndex)
    def _on_table_double_clicked(self, index: QModelIndex):
        if not index.isValid():
            return
        source_idx = self._proxy.mapToSource(index)
        row_data = self._model.data(source_idx, Qt.ItemDataRole.UserRole)
        if row_data:
            folder = row_data.get("_directory", "")
            parquet_path = Path(folder) / "data.parquet"
            status = str(row_data.get("status", "")).lower()
            if parquet_path.exists() and parquet_path.stat().st_size > 0 and status != "active":
                self._open_replay_for_session(row_data)

    @Slot(object)
    def _on_context_menu(self, pos):
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return

        menu = QMenu(self)
        
        single_select = len(indexes) == 1
        can_replay = False
        if single_select:
            source_idx = self._proxy.mapToSource(indexes[0])
            row_data = self._model.data(source_idx, Qt.ItemDataRole.UserRole)
            if row_data:
                folder = row_data.get("_directory", "")
                parquet_path = Path(folder) / "data.parquet"
                status = str(row_data.get("status", "")).lower()
                if parquet_path.exists() and parquet_path.stat().st_size > 0 and status != "active":
                    can_replay = True

        action_replay = menu.addAction("Replay Session")
        action_replay.setEnabled(can_replay)
        action_replay.triggered.connect(self._on_replay)

        menu.addSeparator()

        action_export = menu.addAction("Export CSV…")
        action_export.triggered.connect(self._on_export)

        action_open_folder = menu.addAction("Open Folder")
        action_open_folder.setEnabled(single_select)
        action_open_folder.triggered.connect(self._on_open_folder)

        menu.addSeparator()

        action_delete = menu.addAction("Delete")
        action_delete.triggered.connect(self._on_delete)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _open_replay_for_session(self, row_data: dict) -> None:
        from app.ui.session_replay import SessionReplayWindow
        folder = row_data.get("_directory", "")
        dir_path = Path(folder)
        parquet_path = dir_path / "data.parquet"
        if not parquet_path.exists() or parquet_path.stat().st_size == 0:
            QMessageBox.warning(self, "Cannot Replay Session", "Historical measurement data is unavailable.")
            return

        try:
            replay_win = SessionReplayWindow(dir_path)
            # Retain reference until window is closed/destroyed
            replay_win.destroyed.connect(lambda obj=None, w=replay_win: self._replay_windows.discard(w))
            self._replay_windows.add(replay_win)
            replay_win.show()
            replay_win.raise_()
            replay_win.activateWindow()
        except Exception as exc:
            QMessageBox.critical(self, "Replay Error", f"Unable to read session data:\n{exc}")

    def _apply_stylesheet(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #0d1117;
                color: #c9d8e8;
                font-family: 'Segoe UI', sans-serif;
                font-size: 11px;
            }
            QTableView {
                background-color: #111820;
                alternate-background-color: #151d27;
                border: 1px solid #1a2535;
                gridline-color: #1a2535;
            }
            QHeaderView::section {
                background-color: #151d27;
                color: #5e7790;
                padding: 4px;
                border: 1px solid #1a2535;
                font-weight: bold;
            }
            QFrame#details-panel {
                background-color: #111820;
                border: 1px solid #1a2535;
                border-radius: 4px;
            }
            QPushButton {
                background-color: #151d27;
                color: #c9d8e8;
                border: 1px solid #2d4560;
                border-radius: 3px;
                padding: 4px 12px;
            }
            QPushButton:hover { background-color: #1f2b3b; }
            QPushButton#replay-btn {
                background-color: #142838;
                border-color: #2b577a;
                color: #00b4d8;
                font-weight: bold;
            }
            QPushButton#replay-btn:hover {
                background-color: #1d3a54;
                color: #38d6f5;
            }
            QPushButton#replay-btn:disabled {
                color: #5e7790;
                border-color: #1a2535;
                background-color: #151d27;
            }
            QPushButton#delete-btn:hover { background-color: #2b1111; color: #ff6b6b; border-color: #aa2020; }
            QPushButton:disabled { color: #5e7790; border-color: #1a2535; }
            QLineEdit {
                background-color: #151d27;
                border: 1px solid #2d4560;
                padding: 4px;
                border-radius: 3px;
            }
            QSplitter::handle { background-color: #1a2535; }
        """)
