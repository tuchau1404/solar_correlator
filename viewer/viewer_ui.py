"""Shared presentation components extracted from the live FXViewer.

Both live UDP and offline H5 import these widgets and styles. No data-source
or DSP imports belong in this module.
"""
from PyQt5 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg


def apply_window_theme(window):
    pg.setConfigOption('foreground', '#90a7be')
    window.setFont(QtGui.QFont('Segoe UI', 10))
    window.setStyleSheet('''
        QMainWindow, QWidget#appRoot { background: #0a1018; color: #cbd9e8; }
        QWidget { color: #cbd9e8; font-size: 12px; }
        QWidget#workspace, QWidget#sideContents { background: transparent; }
        QLabel { background: transparent; }
        QLabel#brandMark { background: #213d3e; color: #7be0cd;
            border: 1px solid #39635f; border-radius: 10px; font-size: 22px; font-weight: 700; }
        QLabel#brandTitle { font-size: 16px; font-weight: 700; color: #eef4fc; }
        QLabel#protocolTag { color: #8ca2b9; padding: 5px 8px; background: #152131; border-radius: 4px; }
        QLabel#connectionBadge { color: #91a4b9; font-size: 10px; padding: 5px 9px;
            border: 1px solid #2b3d51; border-radius: 5px; }
        QLabel#displayBadge { color: #7ad4c1; font-size: 10px; }
        QFrame#toolbar, QScrollArea#sidebar, QFrame#metricCard, QFrame#readoutPanel {
            background: #101a27; border: 1px solid #213247; border-radius: 8px; }
        QLabel#sectionHeading { color: #81acbc; font-size: 10px; font-weight: 700;
            padding-top: 14px; padding-bottom: 3px; }
        QLabel#fieldLabel { color: #bac9d9; }
        QLabel#mutedLabel { color: #778ea5; font-size: 11px; }
        QLabel#metricCaption { color: #829bb2; font-size: 10px; font-weight: 600; }
        QLabel#metricValue { color: #ebf4ff; font-family: monospace; font-size: 20px; font-weight: 600; }
        QLabel#metricDetail { color: #88a3b6; font-size: 10px; }
        QLabel#workspaceTitle { color: #8ca6be; font-size: 10px; font-weight: 700; }
        QLabel#emptyLabel { background: #101a27; color: #91a8be; padding: 14px; border-radius: 6px; }
        QPushButton { background: #18283a; border: 1px solid #2d435a; border-radius: 5px;
            color: #cbd9e8; padding: 7px 10px; min-height: 16px; }
        QPushButton:hover { background: #223950; border-color: #52728b; }
        QPushButton:pressed { background: #2b485e; }
        QPushButton:checked { background: #203f43; border-color: #467e7b; color: #91e6d5; }
        QPushButton#pauseButton { background: #204540; border-color: #3b7970; color: #9be6d6; }
        QComboBox, QSpinBox, QDoubleSpinBox { background: #0c1521; border: 1px solid #2a4057;
            border-radius: 5px; padding: 6px 8px; min-height: 18px; selection-background-color: #285b62; }
        QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover { border-color: #557d96; }
        QComboBox QAbstractItemView { background: #152335; selection-background-color: #28495b;
            color: #dfebf7; border: 1px solid #3a556d; }
        QCheckBox { spacing: 8px; padding: 3px 0; }
        QCheckBox::indicator { width: 14px; height: 14px; }
        QCheckBox::indicator:unchecked { background: #0c1521; border: 1px solid #3a526a; border-radius: 3px; }
        QCheckBox::indicator:checked { background: #5bbfae; border: 1px solid #83ddca; border-radius: 3px; }
        QCheckBox:disabled, QPushButton:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { color: #526577; }
        QSplitter::handle { background: #0a1018; }
        QSplitter::handle:hover { background: #24384b; }
        QScrollArea { border: none; background: transparent; }
        QScrollBar:vertical { background: #0d1723; width: 7px; margin: 0; }
        QScrollBar::handle:vertical { background: #304b60; border-radius: 3px; min-height: 28px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        QStatusBar { background: #0a1018; color: #718da5; font-size: 10px; }
        QStatusBar::item { border: none; }
        QToolTip { background: #182a3a; color: #dfebf7; border: 1px solid #426078; padding: 6px; }
        QMessageBox { background: #101a27; }
    ''')
    window.setStyleSheet(window.styleSheet() + """
        QDateTimeEdit, QPlainTextEdit { background: #0c1521; color: #cbd9e8;
            border: 1px solid #2a4057; border-radius: 5px; padding: 6px 8px;
            selection-background-color: #285b62; }
        QDateTimeEdit:hover { border-color: #557d96; }
        QDateTimeEdit:disabled { color: #526577; }
        QTabWidget::pane { background: #0b121c; border: 1px solid #213247; border-radius: 6px; }
        QTabBar::tab { background: #101a27; color: #829bb2; padding: 10px 15px;
            border: 1px solid #213247; border-bottom: none; }
        QTabBar::tab:selected { background: #1b333b; color: #91e6d5; border-top: 2px solid #5bbfae; }
        QTabBar::tab:hover { color: #eef4fc; }
        QMenuBar, QMenu { background: #101a27; color: #cbd9e8; }
        QMenu::item:selected, QMenuBar::item:selected { background: #28495b; }
        QCalendarWidget QWidget { background: #101a27; color: #cbd9e8; }
    """)


def style_plots(plots, left_width=92):
    font = QtGui.QFont('Segoe UI', 9)
    for plot in plots:
        plot.vb.setBackgroundColor('#0b121c')
        plot.showGrid(x=True, y=True, alpha=.10)
        plot.setMenuEnabled(True)
        plot.hideButtons()
        for name in ('left', 'bottom'):
            axis = plot.getAxis(name)
            axis.setPen(pg.mkPen('#2c4054'))
            axis.setTextPen(pg.mkPen('#8ca5bd'))
            axis.setTickFont(font)
        plot.getAxis('left').setWidth(left_width)


def style_colorbars(bars):
    for bar in bars:
        bar.setMinimumWidth(68)
        bar.setPreferredWidth(78)
        bar.setMaximumWidth(92)


def create_root(window):
    root = QtWidgets.QWidget()
    root.setObjectName('appRoot')
    layout = QtWidgets.QVBoxLayout(root)
    layout.setContentsMargins(16, 12, 16, 8)
    layout.setSpacing(12)
    scroll = QtWidgets.QScrollArea()
    scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setWidget(root)
    window.setCentralWidget(scroll)
    return root, layout, scroll


def brand_header(layout, protocol='SOL2 v3', status='WAITING FOR FRAMES'):
    header = QtWidgets.QHBoxLayout()
    header.setSpacing(10)
    mark = QtWidgets.QLabel('S')
    mark.setObjectName('brandMark')
    mark.setAlignment(QtCore.Qt.AlignCenter)
    mark.setFixedSize(34, 34)
    header.addWidget(mark)
    title = QtWidgets.QLabel('SOLAR  /  CORRELATOR')
    title.setObjectName('brandTitle')
    header.addWidget(title)
    tag = QtWidgets.QLabel(protocol)
    tag.setObjectName('protocolTag')
    header.addWidget(tag)
    header.addStretch()
    badge = QtWidgets.QLabel(status)
    badge.setObjectName('connectionBadge')
    header.addWidget(badge)
    layout.addLayout(header)
    return badge


def toolbar_panel(layout):
    frame = QtWidgets.QFrame()
    frame.setObjectName('toolbar')
    row = QtWidgets.QHBoxLayout(frame)
    row.setContentsMargins(10, 8, 10, 8)
    row.setSpacing(10)
    layout.addWidget(frame)
    return frame, row


def create_splitter(layout):
    splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
    splitter.setChildrenCollapsible(False)
    splitter.setHandleWidth(10)
    layout.addWidget(splitter, 1)
    return splitter


def create_sidebar(splitter):
    sidebar = QtWidgets.QScrollArea()
    sidebar.setObjectName('sidebar')
    sidebar.setWidgetResizable(True)
    sidebar.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
    sidebar.setMinimumWidth(250)
    sidebar.setMaximumWidth(330)
    contents = QtWidgets.QWidget()
    contents.setObjectName('sideContents')
    side = QtWidgets.QVBoxLayout(contents)
    side.setContentsMargins(14, 14, 14, 14)
    side.setSpacing(10)
    sidebar.setWidget(contents)
    splitter.addWidget(sidebar)
    return sidebar, side


def sidebar_section(side, title):
    label = QtWidgets.QLabel(title)
    label.setObjectName('sectionHeading')
    side.addWidget(label)
    return label


def sidebar_field(side, title, widget):
    label = QtWidgets.QLabel(title)
    label.setObjectName('fieldLabel')
    label.setWordWrap(True)
    side.addWidget(label)
    side.addWidget(widget)
    return label


def create_workspace(splitter):
    workspace = QtWidgets.QWidget()
    workspace.setObjectName('workspace')
    right = QtWidgets.QVBoxLayout(workspace)
    right.setContentsMargins(0, 0, 0, 0)
    right.setSpacing(10)
    splitter.addWidget(workspace)
    splitter.setStretchFactor(0, 0)
    splitter.setStretchFactor(1, 1)
    splitter.setSizes([270, 1250])
    return workspace, right


def metric_cards(right, fields):
    cards = QtWidgets.QHBoxLayout()
    cards.setSpacing(10)
    values, details = {}, {}
    for key, caption in fields:
        card = QtWidgets.QFrame()
        card.setObjectName('metricCard')
        card.setMinimumWidth(0)
        card.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        box = QtWidgets.QVBoxLayout(card)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(4)
        label = QtWidgets.QLabel(caption)
        label.setObjectName('metricCaption')
        value = QtWidgets.QLabel('—')
        value.setObjectName('metricValue')
        detail = QtWidgets.QLabel('Waiting for data')
        detail.setObjectName('metricDetail')
        for item in (label, value, detail):
            item.setMinimumWidth(0)
            item.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            box.addWidget(item)
        values[key] = value
        details[key] = detail
        cards.addWidget(card, 1)
    right.addLayout(cards)
    return values, details
