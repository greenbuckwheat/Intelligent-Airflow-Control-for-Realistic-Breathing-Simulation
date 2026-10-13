"""Capture deterministic User and Developer mode screenshots for the medical deck."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from single_lung_pid_gui import FormulaDialog, MainWindow, STATE_INSPIRATION


OUT = Path(__file__).with_name("assets")


def save_window(window: MainWindow, name: str) -> None:
    QtWidgets.QApplication.processEvents()
    pixmap = window.grab()
    target = OUT / name
    if not pixmap.save(str(target), "PNG"):
        raise RuntimeError(f"Could not save {target}")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.resize(1500, 900)
    window.show()
    app.processEvents()

    # Keep the capture focused on controls and feedback. The full model caveat
    # is presented on its own slide, so the long in-app paragraph can be hidden
    # here to give every User Mode input enough vertical room.
    window.user_model_assumption.hide()
    window.user_flow_note.hide()
    window.user_accuracy.hide()
    for label in window.findChildren(QtWidgets.QLabel):
        if label.text().startswith("All values are simulated"):
            label.hide()
    for group in window.findChildren(QtWidgets.QGroupBox):
        if group.title() == "Breathing Settings":
            group.setMinimumHeight(285)
    app.processEvents()

    inspiration = [
        i for i, state in enumerate(window.result.state)
        if int(state) == STATE_INSPIRATION
    ]
    frame = inspiration[len(inspiration) // 2]
    window._show_index(frame)
    save_window(window, "ui_user_mode.png")

    window.mode_combo.setCurrentIndex(1)
    window.live_monitor.hide()
    window.tabs.setCurrentIndex(1)
    window._show_index(frame)
    save_window(window, "ui_developer_mode.png")

    guide = FormulaDialog(window)
    guide.resize(1200, 820)
    guide.show()
    app.processEvents()
    guide_pixmap = guide.grab()
    guide_target = OUT / "ui_modeling_guide.png"
    if not guide_pixmap.save(str(guide_target), "PNG"):
        raise RuntimeError(f"Could not save {guide_target}")
    guide.close()

    window.close()
    app.quit()


if __name__ == "__main__":
    main()
