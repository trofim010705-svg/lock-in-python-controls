"""Shared widgets for both standalone and embedded instrument windows."""
from PyQt5.QtCore import QSize, Qt
from PyQt5.QtWidgets import QTabBar


class InstrumentTabBar(QTabBar):
    """Size tabs from their actual font metrics, including on Windows/high DPI."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setExpanding(False)
        self.setElideMode(Qt.ElideNone)
        self.setUsesScrollButtons(True)

    def tabSizeHint(self, index):
        native = super().tabSizeHint(index)
        metrics = self.fontMetrics()
        width = metrics.horizontalAdvance(self.tabText(index)) + 48
        return QSize(max(native.width(), width), max(native.height(), metrics.height()+24, 44))
