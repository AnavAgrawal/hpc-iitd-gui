"""App-wide stylesheet + palette. Cohesive, modern dark theme."""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette


# ---------- color tokens ----------
BG = "#0f1418"
BG_ALT = "#151b22"
SURFACE = "#1a2129"
SURFACE_HI = "#222a34"
BORDER = "#2a333e"
BORDER_HI = "#3a4552"
TEXT = "#e6edf3"
TEXT_DIM = "#8b99a8"
TEXT_MUTED = "#6b7785"

ACCENT = "#4f9eff"      # primary/CTA
ACCENT_HOVER = "#6cb0ff"
ACCENT_DARK = "#2d6bbd"

OK = "#3fb950"
WARN = "#e3b341"
ERR = "#f85149"
INFO = "#58a6ff"

# Job state colors (used by Jobs tab cell highlights)
STATE_COLORS = {
    "R": OK,       # running
    "Q": WARN,     # queued
    "H": TEXT_DIM, # held
    "F": TEXT_MUTED, # finished
    "E": ERR,      # exiting
    "C": TEXT_MUTED, # completed
    "S": WARN,     # suspended
    "W": WARN,     # waiting
    "T": WARN,     # transit
}


APP_QSS = f"""
/* ---------- base ---------- */
QWidget {{
    background: {BG};
    color: {TEXT};
    font-size: 10pt;
    selection-background-color: {ACCENT_DARK};
    selection-color: {TEXT};
}}
QMainWindow, QDialog {{ background: {BG}; }}

/* ---------- tabs ---------- */
QTabWidget::pane {{
    background: {BG};
    border: none;
    border-top: 1px solid {BORDER};
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_DIM};
    padding: 8px 18px;
    border: none;
    font-weight: 500;
}}
QTabBar::tab:hover {{
    color: {TEXT};
    background: {SURFACE};
}}
QTabBar::tab:selected {{
    color: {TEXT};
    border-bottom: 2px solid {ACCENT};
}}
QTabBar::tab:disabled {{
    color: {TEXT_MUTED};
}}

/* ---------- inputs ---------- */
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QAbstractSpinBox {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 10px;
    min-height: 22px;
    selection-background-color: {ACCENT_DARK};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QComboBox:focus, QSpinBox:focus, QAbstractSpinBox:focus {{
    border: 1px solid {ACCENT};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    background: {BG_ALT};
    color: {TEXT_MUTED};
    border-color: {BORDER};
}}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right; border: none; width: 20px; }}
QComboBox::down-arrow {{
    image: none; width: 0; height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_DIM};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER_HI};
    selection-background-color: {ACCENT_DARK};
}}
QSpinBox::up-button {{
    subcontrol-origin: border; subcontrol-position: top right;
    width: 18px; border: none; background: transparent;
}}
QSpinBox::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: 18px; border: none; background: transparent;
}}
QSpinBox::up-arrow {{
    image: none; width: 0; height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid {TEXT_DIM};
}}
QSpinBox::down-arrow {{
    image: none; width: 0; height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_DIM};
}}

/* ---------- buttons ---------- */
QPushButton {{
    background: {SURFACE_HI};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 18px;
    font-weight: 500;
}}
QPushButton:hover {{ background: {BORDER_HI}; border-color: {BORDER_HI}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; background: {BG_ALT}; border-color: {BORDER}; }}
QPushButton:default {{
    background: {ACCENT};
    color: white;
    border: 1px solid {ACCENT};
}}
QPushButton:default:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton:default:disabled {{ background: {ACCENT_DARK}; color: #bcd6ff; }}

QPushButton[role="primary"] {{
    background: {ACCENT}; color: white; border: 1px solid {ACCENT};
}}
QPushButton[role="primary"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[role="danger"] {{
    background: {ERR}; color: white; border: 1px solid {ERR};
}}
QPushButton[role="danger"]:hover {{ background: #ff6e66; border-color: #ff6e66; }}
QPushButton[role="ghost"] {{
    background: transparent; border: 1px solid {BORDER};
}}
QPushButton[role="ghost"]:hover {{ background: {SURFACE}; }}

/* ---------- checkbox/radio ---------- */
QCheckBox, QRadioButton {{ spacing: 8px; color: {TEXT}; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; }}
QCheckBox::indicator:unchecked, QRadioButton::indicator:unchecked {{
    border: 1px solid {BORDER_HI}; border-radius: 3px; background: {SURFACE};
}}
QCheckBox::indicator:checked {{
    border: 1px solid {ACCENT}; border-radius: 3px; background: {ACCENT};
}}
QRadioButton::indicator:unchecked {{ border-radius: 8px; }}
QRadioButton::indicator:checked {{
    border: 1px solid {ACCENT}; border-radius: 8px; background: {ACCENT};
}}

/* ---------- tables ---------- */
QTableWidget, QTableView {{
    background: {BG_ALT};
    alternate-background-color: {SURFACE};
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    border-radius: 6px;
    selection-background-color: {ACCENT_DARK};
    selection-color: {TEXT};
}}
QHeaderView::section {{
    background: {SURFACE};
    color: {TEXT_DIM};
    padding: 6px 10px;
    border: none;
    border-bottom: 1px solid {BORDER};
    font-weight: 600;
}}
QHeaderView::section:hover {{ color: {TEXT}; }}
QTableCornerButton::section {{
    background: {SURFACE}; border: none; border-bottom: 1px solid {BORDER};
}}
QTableWidget::item, QTableView::item {{ padding: 4px 8px; border: none; }}
QTableWidget::item:selected, QTableView::item:selected {{
    background: {ACCENT_DARK}; color: {TEXT};
}}

/* ---------- group box ---------- */
QGroupBox {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 14px;
    padding: 14px 12px 10px 12px;
    font-weight: 600;
    color: {TEXT_DIM};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    top: 0px;
    padding: 2px 6px;
    background: {BG};
    color: {TEXT};
}}

/* ---------- frames / splitter ---------- */
QFrame[frameShape="4"], QFrame[frameShape="5"] {{ color: {BORDER}; }}
QSplitter::handle {{ background: {BG}; }}
QSplitter::handle:horizontal {{ width: 4px; }}
QSplitter::handle:vertical {{ height: 4px; }}
QSplitter::handle:hover {{ background: {BORDER_HI}; }}

/* ---------- scrollbars ---------- */
QScrollBar:vertical {{
    background: transparent; width: 12px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_HI}; min-height: 24px; border-radius: 6px; margin: 2px;
}}
QScrollBar::handle:vertical:hover {{ background: {TEXT_MUTED}; }}
QScrollBar:horizontal {{
    background: transparent; height: 12px; margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER_HI}; min-width: 24px; border-radius: 6px; margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{ background: {TEXT_MUTED}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- progress ---------- */
QProgressBar {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    text-align: center;
    color: {TEXT};
    height: 18px;
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 5px;
}}

/* ---------- status bar / menu ---------- */
QStatusBar {{
    background: {BG_ALT};
    border-top: 1px solid {BORDER};
    color: {TEXT_DIM};
}}
QStatusBar::item {{ border: none; }}
QMenuBar {{ background: {BG_ALT}; color: {TEXT}; border-bottom: 1px solid {BORDER}; }}
QMenuBar::item {{ padding: 6px 12px; background: transparent; }}
QMenuBar::item:selected {{ background: {SURFACE}; }}
QMenu {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER_HI}; }}
QMenu::item {{ padding: 6px 24px; }}
QMenu::item:selected {{ background: {ACCENT_DARK}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 0; }}

/* ---------- tooltips ---------- */
QToolTip {{
    background: {SURFACE_HI};
    color: {TEXT};
    border: 1px solid {BORDER_HI};
    padding: 4px 8px;
}}
"""


def apply(app) -> None:
    """Install the global stylesheet and palette on a QApplication."""
    app.setStyle("Fusion")
    app.setStyleSheet(APP_QSS)

    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(BG))
    pal.setColor(QPalette.Base, QColor(BG_ALT))
    pal.setColor(QPalette.AlternateBase, QColor(SURFACE))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor(SURFACE_HI))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.Highlight, QColor(ACCENT_DARK))
    pal.setColor(QPalette.HighlightedText, QColor(TEXT))
    pal.setColor(QPalette.ToolTipBase, QColor(SURFACE_HI))
    pal.setColor(QPalette.ToolTipText, QColor(TEXT))
    pal.setColor(QPalette.PlaceholderText, QColor(TEXT_MUTED))
    app.setPalette(pal)

    default = QFont("Segoe UI", 10)
    app.setFont(default)
