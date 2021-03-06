"""
Prototype code for analyzing connectivity and synaptic properties between cell classes.


"""

from __future__ import print_function, division

from collections import OrderedDict
import numpy as np
import pyqtgraph as pg
from multipatch_analysis.database import database as db
from multipatch_analysis.connectivity import query_pairs, measure_connectivity
from multipatch_analysis.connection_strength import ConnectionStrength, get_amps, get_baseline_amps
from multipatch_analysis.morphology import Morphology
from multipatch_analysis import constants
from multipatch_analysis.cell_class import CellClass, classify_cells, classify_pairs
from multipatch_analysis.ui.graphics import MatrixItem


class MainWindow(pg.QtGui.QWidget):
    def __init__(self):
        pg.QtGui.QWidget.__init__(self)
        self.layout = pg.QtGui.QGridLayout()
        self.setLayout(self.layout)
        self.h_splitter = pg.QtGui.QSplitter()
        self.h_splitter.setOrientation(pg.QtCore.Qt.Horizontal)
        self.layout.addWidget(self.h_splitter, 0, 0)
        self.filter_control_panel = FilterControlPanel()
        self.h_splitter.addWidget(self.filter_control_panel)
        self.matrix_widget = MatrixWidget()
        self.h_splitter.addWidget(self.matrix_widget)
        self.v_splitter = pg.QtGui.QSplitter()
        self.v_splitter.setOrientation(pg.QtCore.Qt.Vertical)
        self.h_splitter.addWidget(self.v_splitter)
        self.scatter_plot = ScatterPlot()
        self.trace_plot = TracePlot()
        self.v_splitter.addWidget(self.scatter_plot)
        self.v_splitter.addWidget(self.trace_plot)

class FilterControlPanel(pg.QtGui.QWidget):
    def __init__(self):
        pg.QtGui.QWidget.__init__(self)
        self.layout = pg.QtGui.QVBoxLayout()
        self.setLayout(self.layout)
        self.update_button = pg.QtGui.QPushButton("Update Matrix")
        self.layout.addWidget(self.update_button)
        self.project_list = pg.QtGui.QListWidget()
        self.layout.addWidget(self.project_list)
        s = db.Session()
        projects = s.query(db.Experiment.project_name).distinct().all()
        for record in projects:
            project = record[0]
            project_item = pg.QtGui.QListWidgetItem(project)
            project_item.setFlags(project_item.flags() | pg.QtCore.Qt.ItemIsUserCheckable)
            project_item.setCheckState(pg.QtCore.Qt.Unchecked)
            self.project_list.addItem(project_item)

    def selected_project_names(self):
        n_projects = self.project_list.count()
        project_names = []
        for n in range(n_projects):
            project_item = self.project_list.item(n)
            check_state = project_item.checkState()
            if check_state == pg.QtCore.Qt.Checked:
                project_names.append(str(project_item.text()))

        return project_names

class MatrixWidget(pg.GraphicsLayoutWidget):
    def __init__(self):
        pg.GraphicsLayoutWidget.__init__(self)
        self.setRenderHints(self.renderHints() | pg.QtGui.QPainter.Antialiasing)
        v = self.addViewBox()
        v.setBackgroundColor('w')
        v.setAspectLocked()
        v.invertY()
        self.view_box = v
        self.matrix = None

    def set_matrix_data(self, text, fgcolor, bgcolor, border_color, rows, cols, size=50, header_color='k'):
        if self.matrix is not None:
            self.view_box.removeItem(self.matrix)

        self.matrix = MatrixItem(text=text, fgcolor=fgcolor, bgcolor=bgcolor, border_color=border_color,
                    rows=rows, cols=rows, size=50, header_color='k')
        self.view_box.addItem(self.matrix)

class ScatterPlot(pg.GraphicsLayoutWidget):
    def __init__(self):
        pg.GraphicsLayoutWidget.__init__(self)
        self.setRenderHints(self.renderHints() | pg.QtGui.QPainter.Antialiasing)

class TracePlot(pg.GraphicsLayoutWidget):
    def __init__(self):
        pg.GraphicsLayoutWidget.__init__(self)
        self.setRenderHints(self.renderHints() | pg.QtGui.QPainter.Antialiasing)

def display_connectivity(pre_class, post_class, result, show_confidence=True):
    # Print results
    print("{pre_class:>20s} -> {post_class:20s} {connections_found:>5s} / {connections_probed}".format(
        pre_class=pre_class.name, 
        post_class=post_class.name, 
        connections_found=str(len(result['connected_pairs'])),
        connections_probed=len(result['probed_pairs']),
    ))

    # Pretty matrix results
    colormap = pg.ColorMap(
        [0, 0.01, 0.03, 0.1, 0.3, 1.0],
        [(0,0,100), (80,0,80), (140,0,0), (255,100,0), (255,255,100), (255,255,255)],
    )

    connectivity, lower_ci, upper_ci = result['connection_probability']

    if show_confidence:
        output = {'bordercolor': 0.6}
        default_bgcolor = np.array([128., 128., 128.])
    else:
        output = {'bordercolor': 0.8}
        default_bgcolor = np.array([220., 220., 220.])
    
    if np.isnan(connectivity):
        output['bgcolor'] = tuple(default_bgcolor)
        output['fgcolor'] = 0.6
        output['text'] = ''
    else:
        # get color based on connectivity
        color = colormap.map(connectivity)
        
        # desaturate low confidence cells
        if show_confidence:
            confidence = (1.0 - (upper_ci - lower_ci)) ** 2
            color = color * confidence + default_bgcolor * (1.0 - confidence)
        
        # invert text color for dark background
        output['fgcolor'] = 'w' if sum(color[:3]) < 384 else 'k'
        output['text'] = "%d/%d" % (result['n_connected'], result['n_probed'])
        output['bgcolor'] = tuple(color)

    return output


class MatrixAnalyzer(object):
    def __init__(self, cell_classes, analysis_func, display_func, title, session):
        self.session = session
        self.cell_classes = cell_classes
        self.analysis_func = analysis_func
        self.display_func = display_func
        self.session = session
        self.win = MainWindow()
        self.win.show()
        self.win.setWindowTitle(title)

        self.win.filter_control_panel.update_button.clicked.connect(self.update_clicked)

    def update_clicked(self):
        with pg.BusyCursor():
            self.update_matrix()

    def update_matrix(self):
        project_names = self.win.filter_control_panel.selected_project_names()

        # Select pairs (todo: age, acsf, internal, temp, etc.)
        self.pairs = query_pairs(project_name=project_names, session=self.session).all()

        # Group all cells by selected classes
        cell_groups = classify_cells(self.cell_classes, pairs=self.pairs)

        # Group pairs into (pre_class, post_class) groups
        pair_groups = classify_pairs(self.pairs, cell_groups)

        # analyze matrix elements
        results = self.analysis_func(pair_groups)

        shape = (len(cell_groups),) * 2
        text = np.empty(shape, dtype=object)
        fgcolor = np.empty(shape, dtype=object)
        bgcolor = np.empty(shape, dtype=object)
        bordercolor = np.empty(shape, dtype=object)

        # call display function on every matrix element
        for i,row in enumerate(cell_groups):
            for j,col in enumerate(cell_groups):
                output = self.display_func(row, col, results[(row, col)])
                text[i, j] = output['text']
                fgcolor[i, j] = output['fgcolor']
                bgcolor[i, j] = output['bgcolor']
                bordercolor[i, j] = output['bordercolor']
                
        # Force cell class descriptions down to tuples of 2 items
        # Kludgy, but works for now.
        rows = []
        for cell_class in self.cell_classes:
            tup = cell_class.as_tuple
            row = tup[:1]
            if len(tup) > 1:
                row = row + (' '.join(tup[1:]),)
            rows.append(row)

        self.win.matrix_widget.set_matrix_data(text=text, fgcolor=fgcolor, bgcolor=bgcolor, border_color=bordercolor,
                    rows=rows, cols=rows, size=50, header_color='k')


if __name__ == '__main__':

    import pyqtgraph as pg
    pg.dbg()

    session = db.Session()
    
    # Define cell classes

    mouse_cell_classes = [
        # {'cre_type': 'unknown', 'pyramidal': True, 'target_layer': '2/3'},
        # {'cre_type': 'unknown', 'target_layer': '2/3'},
        # {'pyramidal': True, 'target_layer': '2/3'},
        {'pyramidal': True, 'target_layer': '2/3'},
        {'cre_type': 'sst', 'target_layer': '2/3'},
        {'cre_type': 'pvalb', 'target_layer': '2/3'},
        {'cre_type': 'vip', 'target_layer': '2/3'},
        {'cre_type': 'rorb', 'target_layer': '4'},
        {'cre_type': 'nr5a1', 'target_layer': '4'},
        {'cre_type': 'sst', 'target_layer': '4'},
        {'cre_type': 'pvalb', 'target_layer': '4'},
        {'cre_type': 'vip', 'target_layer': '4'},
        {'cre_type': 'sim1', 'target_layer': '5'},
        {'cre_type': 'tlx3', 'target_layer': '5'},
        {'cre_type': 'sst', 'target_layer': '5'},
        {'cre_type': 'pvalb', 'target_layer': '5'},
        {'cre_type': 'vip', 'target_layer': '5'},
        {'cre_type': 'ntsr1', 'target_layer': '6'},
        {'cre_type': 'sst', 'target_layer': '6'},
        {'cre_type': 'pvalb', 'target_layer': '6'},
        {'cre_type': 'vip', 'target_layer': '6'},
    ]

    human_cell_classes = [
        {'pyramidal': True, 'target_layer': '2'},
        {'pyramidal': False, 'target_layer': '2'},
        {'pyramidal': True, 'target_layer': '3'},
        {'pyramidal': False, 'target_layer': '3'},
        {'pyramidal': True, 'target_layer': '4'},
        {'pyramidal': False, 'target_layer': '4'},
        {'pyramidal': True, 'target_layer': '5'},
        {'pyramidal': False, 'target_layer': '5'},
        {'pyramidal': True, 'target_layer': '6'},
        {'pyramidal': False, 'target_layer': '6'},
    ]

    analyzers = []
    for cell_classes, title in [(mouse_cell_classes, 'Mouse'), (human_cell_classes, 'Human')]:
        cell_classes = [CellClass(**c) for c in cell_classes]

        maz = MatrixAnalyzer(cell_classes, analysis_func=measure_connectivity, display_func=display_connectivity, title=title, session=session)
        analyzers.append(maz)