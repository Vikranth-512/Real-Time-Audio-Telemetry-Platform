import sys
import logging
from enum import auto, Enum
from PySide6.QtWidgets import QApplication
from app.ui.main_window import MainWindow

# Configure minimal logging for the shell
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

class AppState(Enum):
    IDLE = auto()
    INITIALIZING = auto()
    RUNNING = auto()
    STOPPING = auto()
    SAVED = auto()
    ERROR = auto()

def main():
    app = QApplication(sys.argv)
    
    # Initialize the main window
    window = MainWindow()
    window.show()
    
    # Application State Initialization
    app_state = AppState.INITIALIZING
    logger.info(f"Application state: {app_state.name}")
    
    # Transition to IDLE after startup
    app_state = AppState.IDLE
    logger.info(f"Application state: {app_state.name}")
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
