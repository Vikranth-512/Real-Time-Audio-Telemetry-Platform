"""
export_worker.py
----------------
QObject worker for running raw CSV export safely off the GUI thread.
"""

import logging
from pathlib import Path
from PySide6.QtCore import QObject, Signal, Slot

from app.storage.csv_exporter import export_raw_session_csv

logger = logging.getLogger(__name__)

class ExportWorker(QObject):
    """
    Worker for exporting Parquet to CSV.
    Designed to be moved to a QThread via moveToThread().
    """
    progress = Signal(int)
    finished = Signal(Path)
    error = Signal(str)

    def __init__(self, parquet_path: Path, output_path: Path, parent=None):
        super().__init__(parent)
        self._parquet_path = Path(parquet_path)
        self._output_path = Path(output_path)

    @Slot()
    def export(self) -> None:
        """Run the export process. Should be invoked via Qt signals or QTimer."""
        try:
            logger.info(f"Starting background export to {self._output_path.name}")
            out_path = export_raw_session_csv(
                self._parquet_path, 
                self._output_path, 
                progress_callback=self.progress.emit
            )
            self.finished.emit(out_path)
        except Exception as exc:
            logger.error(f"Export failed: {exc}", exc_info=True)
            self.error.emit(str(exc))
