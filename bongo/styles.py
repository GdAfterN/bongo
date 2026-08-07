APP_STYLE = """
QWidget {
    color: #202429;
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-size: 14px;
}
QMainWindow, QWidget#appRoot { background: #f5f6f7; }
QFrame#sidebar { background: #202429; border: none; }
QLabel#brand { color: white; font-size: 22px; font-weight: 700; padding: 8px; }
QLabel#brandSub { color: #aeb6bf; font-size: 12px; padding: 0 8px 14px 8px; }
QPushButton#navButton {
    color: #cbd2d9; background: transparent; border: none;
    text-align: left; padding: 11px 14px; border-radius: 5px;
}
QPushButton#navButton:hover { background: #30363d; color: white; }
QPushButton#navButton:checked { background: #dcefe8; color: #135c45; font-weight: 600; }
QLabel#pageTitle { font-size: 22px; font-weight: 700; color: #171a1d; }
QLabel#muted { color: #687078; }
QFrame#panel { background: white; border: 1px solid #dde1e4; border-radius: 7px; }
QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QTableWidget, QListWidget {
    background: white; border: 1px solid #cfd5da; border-radius: 5px; padding: 7px;
    selection-background-color: #b8ded0;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border: 1px solid #268060; }
QPushButton {
    background: #268060; color: white; border: none; border-radius: 5px;
    padding: 8px 14px; font-weight: 600;
}
QPushButton:hover { background: #1f6c51; }
QPushButton:disabled { background: #b5c1bc; }
QPushButton[secondary="true"] { background: #e9edef; color: #30363d; }
QPushButton[secondary="true"]:hover { background: #dce2e5; }
QPushButton[danger="true"] { background: #b94a48; }
QHeaderView::section {
    background: #eef1f2; color: #4a5259; border: none; border-bottom: 1px solid #d5dade;
    padding: 8px; font-weight: 600;
}
QTableWidget { gridline-color: #e5e8ea; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #c0c7cc; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""
