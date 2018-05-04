from __future__ import print_function
import os, sys, datetime, re, glob, traceback, time, atexit, threading
try:
    import queue
except ImportError:
    import Queue as queue
from pprint import pprint
from collections import OrderedDict
import numpy as np
from multipatch_analysis import config, lims
from multipatch_analysis.database import database
from multipatch_analysis.genotypes import Genotype
from acq4.util.DataManager import getDirHandle
import pyqtgraph as pg
from pyqtgraph.Qt import QtGui, QtCore


fail_color = (255, 200, 200)
pass_color = (200, 255, 200)


class Dashboard(QtGui.QWidget):
    def __init__(self, limit=0, no_thread=False):
        QtGui.QWidget.__init__(self)

        # fields displayed in ui
        self.visible_fields = [
            ('timestamp', float), 
            ('path', 'S100'), 
            ('rig', 'S100'), 
            ('description', 'S100'), 
            ('primary', 'S100'), 
            ('archive', 'S100'), 
            ('backup', 'S100'), 
            ('NAS', 'S100'), 
            ('pipettes.yml', 'S100'), 
            ('site.mosaic', 'S100'), 
            ('DB', 'S100'), 
            ('LIMS', 'S100'), 
            ('20x', 'S100'), 
            ('cell map', 'S100'), 
            ('63x', 'S100'), 
            ('morphology', 'S100')
        ]

        # data tracked but not displayed
        self.hidden_fields = [
            ('experiment', object),
            ('item', object),
            ('error', object),
        ]

        # maps field name : index (column number)
        self.field_indices = {self.visible_fields[i][0]:i for i in range(len(self.visible_fields))}

        self.records = GrowingArray(dtype=self.visible_fields + self.hidden_fields)
        self.records_by_expt = {}  # maps expt:index

        self.selected = None

        # set up UI
        self.layout = QtGui.QGridLayout()
        self.setLayout(self.layout)
        self.layout.setContentsMargins(0, 0, 0, 0)

        self.splitter = QtGui.QSplitter(QtCore.Qt.Horizontal)
        self.layout.addWidget(self.splitter, 0, 0)

        self.filter = pg.DataFilterWidget()
        self.filter_fields = OrderedDict([(field[0], {'mode': 'enum', 'values': []}) for field in self.visible_fields])
        self.filter_fields['timestamp'] = {'mode': 'range'}
        self.filter_fields.pop('path')

        self.filter.setFields(self.filter_fields)
        self.splitter.addWidget(self.filter)
        self.filter.sigFilterChanged.connect(self.filter_changed)

        self.expt_tree = pg.TreeWidget()
        self.expt_tree.setSortingEnabled(True)
        self.expt_tree.setColumnCount(len(self.visible_fields))
        self.expt_tree.setHeaderLabels([f[0] for f in self.visible_fields])
        self.splitter.addWidget(self.expt_tree)
        self.expt_tree.itemSelectionChanged.connect(self.tree_selection_changed)

        self.status = QtGui.QStatusBar()
        self.layout.addWidget(self.status, 1, 0)

        self.resize(1000, 900)
        self.splitter.setSizes([200, 800])
        
        # Queue of experiments to be checked
        self.expt_queue = queue.PriorityQueue()

        # collect a list of all data sources to search
        search_paths = [config.synphys_data]
        for rig_name, rig_path_sets in config.rig_data_paths.items():
            for path_set in rig_path_sets:
                search_paths.extend(list(path_set.values()))

        # Each poller searches a single data source for new experiments, adding them to the queue
        self.pollers = []
        self.poller_status = {}
        for search_path in search_paths:
            if not os.path.exists(search_path):
                print("Ignoring search path:", search_path)
                continue
            poll_thread = PollThread(self.expt_queue, search_path, limit=limit)
            poll_thread.update.connect(self.poller_update)
            if no_thread:
                poll_thread.poll()  # for local debugging
            else:
                poll_thread.start()
            self.pollers.append(poll_thread)

        # Checkers pull experiments off of the queue and check their status
        if no_thread:
            self.checker = ExptCheckerThread(self.expt_queue)
            self.checker.update.connect(self.checker_update)
            self.checker.run(block=False)            
        else:
            self.checkers = []
            for i in range(3):
                self.checkers.append(ExptCheckerThread(self.expt_queue))
                self.checkers[-1].update.connect(self.checker_update)
                self.checkers[-1].start()

        # shut down threads nicely on exit
        atexit.register(self.quit)

    def tree_selection_changed(self):
        sel = self.expt_tree.selectedItems()[0]
        rec = self.records[sel.index]
        self.selected = rec
        expt = rec['experiment']
        print("===================", expt.timestamp)
        print(" description:", rec['description'])
        print("    NAS path:", expt.nas_path)
        print("primary path:", expt.nas_path)
        print("archive path:", expt.nas_path)
        print(" backup path:", expt.nas_path)
        err = rec['error']
        if err is not None:
            print("Error checking experiment:")
            traceback.print_exception(*err)

    def poller_update(self, path, status):
        """Received an update on the status of a poller
        """
        self.poller_status[path] = status
        paths = sorted(self.poller_status.keys())
        msg = '    '.join(['%s: %s' % (p,self.poller_status[p]) for p in paths])
        self.status.showMessage(msg)

    def checker_update(self, rec):
        """Received an update from a worker thread describing information about an experiment
        """
        expt = rec['experiment']
        if expt in self.records_by_expt:
            # use old record / item
            index = self.records_by_expt[expt]
            item = self.records[index]['item']
        else:
            # add new record / item
            item = pg.TreeWidgetItem()
            self.expt_tree.addTopLevelItem(item)
            rec['item'] = item
            index = self.records.add_record({})
            item.index = index

        record = self.records[index]

        # update item/record fields
        for field, val in rec.items():
            if field in self.field_indices and isinstance(val, tuple):
                # if a tuple was given, interpret it as (text, color)
                val, color = val
            else:
                # otherwise make a guess on a good color
                color = None
                if val is True:
                    color = pass_color
                elif val in (False, 'ERROR', 'MISSING'):
                    color = fail_color

            # update this field in the record
            record[field] = val
            
            # update this field in the tree item
            try:
                i = self.field_indices[field]
            except KeyError:
                continue
            item.setText(i, str(val))
            if color is not None:
                item.setBackgroundColor(i, pg.mkColor(color))

            # update filter fields
            filter_field = self.filter_fields.get(field)
            if filter_field is not None and filter_field['mode'] == 'enum' and val not in filter_field['values']:
                filter_field['values'].append(val)
                self.filter.setFields(self.filter_fields)

    def filter_changed(self):
        mask = self.filter.generateMask(self.records)
        for i,item in enumerate(self.records['item']):
            item.setHidden(not mask[i])

    def quit(self):
        for t in self.pollers:
            t.stop()
            t.wait()

        for t in self.checkers:
            t.stop()
            t.wait()


class GrowingArray(object):
    def __init__(self, dtype, init_size=1000):
        self.size = 0
        self._data = np.empty(init_size, dtype=dtype)
        self._view = self._data[:0]

    def __len__(self):
        return self.size

    @property
    def shape(self):
        return (self.size,)

    def __getitem__(self, i):
        return self._view[i]

    def add_record(self, rec):
        index = self.size
        self._grow(self.size+1)
        self.update_record(index, rec)
        return index

    def update_record(self, index, rec):
        for k,v in rec.items():
            print(k, v)
            self._data[index][k] = v

    def _grow(self, size):
        if size > len(self._data):
            self._data = np.resize(self._data, len(self._data)*2)
        self._view = self._data[:size]
        self.size = size


class PollThread(QtCore.QThread):
    """Used to check in the background for changes to experiment status.
    """
    update = QtCore.Signal(object, object)  # search_path, status_message
    
    def __init__(self, expt_queue, search_path, limit=0):
        QtCore.QThread.__init__(self)
        self.expt_queue = expt_queue
        self.search_path = search_path
        self.limit = limit
        self._stop = False
        self.waker = threading.Event()
        
    def stop(self):
        self._stop = True
        self.waker.set()

    def run(self):
        while True:
            try:
                # check for new experiments hourly
                self.poll()
                self.waker.wait(3600)
                if self._stop:
                    return
            except Exception:
                sys.excepthook(*sys.exc_info())
            break
                
    def poll(self):
        self.session = database.Session()
        expts = {}

        # Find all available site paths across all data sources
        count = 0
        path = self.search_path

        self.update.emit(path, "Updating...")
        root_dh = getDirHandle(path)

        # iterate over all expt sites in this path
        for expt_path in glob.iglob(os.path.join(root_dh.name(), '*', 'slice_*', 'site_*')):
            if self._stop:
                return

            expt = ExperimentMetadata(path=expt_path)
            ts = expt.timestamp

            # Couldn't get timestamp; show an error message
            if ts is None:
                print("Error getting timestamp for %s" % expt)
                continue

            # We've already seen this expt elsewhere; skip
            if ts in expts:
                continue
            
            # Add this expt to the queue
            expts[ts] = expt
            self.expt_queue.put((-ts, expt))

            count += 1
            if self.limit > 0 and count >= self.limit:
                print("Hit limit; exiting poller")
                return
        self.update.emit(path, "Finished")


class ExptCheckerThread(QtCore.QThread):
    update = QtCore.Signal(object)

    def __init__(self, expt_queue):
        QtCore.QThread.__init__(self)
        self.expt_queue = expt_queue
        self._stop = False

    def stop(self):
        self._stop = True
        self.expt_queue.put(('stop', None))

    def run(self, block=True):
        while True:
            ts, expt = self.expt_queue.get(block=block)
            if self._stop or ts == 'stop':
                return
            try:
                rec = self.check(expt)
                self.update.emit(rec)
            except Exception as exc:
                rec = {
                    'experiment': expt,
                    'path': expt.site_dh.name(),
                    'timestamp': expt.timestamp or 'ERROR',
                    'rig': 'ERROR',
                    'error': sys.exc_info(),
                }
                self.update.emit(rec)

    def check(self, expt):
        org = expt.organism
        if org is None:
            description = ("no LIMS spec info", fail_color)
        elif org == 'human':
            description = org
        else:
            gtyp = expt.genotype
            if gtyp is None:
                description = (org + ' (no genotype)', fail_color)
            else:
                try:
                    description = ','.join(Genotype(gtyp).drivers())
                except Exception:
                    description = (org + ' (unknown: %s)'%gtyp, fail_color)

        subs = expt.lims_submissions
        if subs is None:
            lims = "ERROR"
        else:
            lims = len(subs) == 1

        rec = {
            'experiment': expt,
            'path': expt.site_dh.name(), 
            'timestamp': expt.timestamp, 
            'rig': expt.rig_name, 
            'primary': False if expt.primary_path is None else (True if os.path.exists(expt.primary_path) else "-"),
            'archive': False if expt.archive_path is None else (True if os.path.exists(expt.archive_path) else "MISSING"),
            'backup': False if expt.backup_path is None else (True if os.path.exists(expt.backup_path) else "MISSING"),
            'description': description,
            'pipettes.yml': expt.pipette_file is not None,
            'site.mosaic': expt.mosaic_file is not None,
            'DB': expt.in_database,
            'NAS': expt.nas_path is not None,
            'LIMS': lims,
        }
        return rec


class ExperimentMetadata(object):
    """Handles reading experiment metadata from several possible locations.
    """
    def __init__(self, path=None):
        self.site_dh = getDirHandle(path)
        self.site_info = self.site_dh.info()
        self._slice_info = None
        self._expt_info = None
        self._specimen_info = None
        self._rig_name = None
        self._primary_path = None
        self._archive_path = None
        self._backup_path = None

    def _get_raw_paths(self):
        path = self.site_dh.name()

        # get raw data subpath  (eg: 2018.01.20_000/slice_000/site_000)
        if os.path.abspath(path).startswith(os.path.abspath(config.synphys_data).rstrip(os.path.sep) + os.path.sep):
            # this is a server path; need to back-convert to rig path
            source_path = open(os.path.join(path, 'sync_source')).read()
            expt_subpath = os.path.join(*source_path.split('/')[-3:])
            assert os.path.abspath(path) == os.path.abspath(self.nas_path), "Expected equal paths:\n  %s\n  %s" % (os.path.abspath(path), os.path.abspath(self.nas_path))
        else:
            expt_subpath = os.path.join(*path.split(os.path.sep)[-3:])
        
        # find the local primary/archive paths that contain this experiment
        found_paths = False
        rig_data_paths = config.rig_data_paths.get(self.rig_name, [])
        for path_set in rig_data_paths:
            for root in path_set.values():
                test_path = os.path.join(root, expt_subpath)
                if not os.path.isdir(test_path):
                    continue
                dh = getDirHandle(test_path)
                if dh.info()['__timestamp__'] == self.site_info['__timestamp__']:
                    found_paths = True
                    # set self._primary_path, self._archive_path, etc.
                    for k,v in path_set.items():
                        setattr(self, '_'+k+'_path', os.path.join(v, expt_subpath))
                    break
            if found_paths:
                break

    @property
    def primary_path(self):
        if self._primary_path is None:
            self._get_raw_paths()
        return self._primary_path

    @property
    def archive_path(self):
        if self._archive_path is None:
            self._get_raw_paths()
        return self._archive_path

    @property
    def backup_path(self):
        if self._backup_path is None:
            self._get_raw_paths()
        return self._backup_path

    @property
    def slice_dh(self):
        return self.site_dh.parent()

    @property
    def expt_dh(self):
        return self.slice_dh.parent()

    @property
    def slice_info(self):
        if self._slice_info is None:
            self._slice_info = self.slice_dh.info()
        return self._slice_info

    @property
    def expt_info(self):
        if self._expt_info is None:
            self._expt_info = self.expt_dh.info()
        return self._expt_info

    @property
    def specimen_info(self):
        if self._specimen_info is None:
            spec_name = self.slice_info['specimen_ID'].strip()
            try:
                self._specimen_info = lims.specimen_info(spec_name)
            except Exception as exc:
                if 'returned 0 results' in exc.args[0]:
                    pass
                else:
                    raise
        return self._specimen_info

    @property
    def nas_path(self):
        expt_dir = '%0.3f' % self.expt_info['__timestamp__']
        subpath = self.site_dh.name(relativeTo=self.expt_dh)
        return os.path.abspath(os.path.join(config.synphys_data, expt_dir, subpath))

    @property
    def organism(self):
        org = self.expt_info.get('organism')
        if org is not None:
            return org
        spec_info = self.specimen_info
        if spec_info is None:
            return None
        return spec_info['organism']

    @property
    def genotype(self):
        gtyp = self.expt_info.get('genotype')
        if gtyp is not None:
            return gtyp
        spec_info = self.specimen_info
        if spec_info is None:
            return None
        return spec_info['genotype']

    @property
    def rig_name(self):
        if self._rig_name is None:
            name = self.expt_info.get('rig_name')
            if name is not None:
                self._rig_name = name.lower()
            else:
                # infer rig name from paths
                if 'sync_source' in self.site_dh.ls():
                    path = self.site_dh['sync_source'].read()
                else:
                    path = self.site_dh.name()
                m = re.search(r'(/|\\)(mp\d)(/|\\)', path)
                if m is None:
                    raise Exception("Can't determine rig name for %s" % path)
                self._rig_name = m.groups()[1].lower()
        return self._rig_name

    @property
    def timestamp(self):
        return self.site_info.get('__timestamp__')

    @property
    def datetime(self):
        return datetime.datetime.fromtimestamp(self.timestamp)

    @property
    def pipette_file(self):
        if 'pipettes.yml' in self.site_dh.ls():
            return self.site_dh['pipettes.yml'].name()
        return None

    @property
    def mosaic_file(self):
        if 'site.mosaic' in self.site_dh.ls():
            return self.site_dh['site.mosaic'].name()
        return None

    @property
    def in_database(self):
        session = database.Session()
        expts = session.query(database.Experiment).filter(database.Experiment.acq_timestamp==self.datetime).all()
        return len(expts) == 1

    @property
    def lims_submissions(self):
        if self.specimen_info is None:
            return None
        spec_id = self.specimen_info['specimen_id']
        if spec_id is None:
            return None
        return lims.expt_submissions(spec_id, self.timestamp)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--no-thread', action='store_true', default=False, dest='no_thread',
                    help='Do all polling in main thread (to make debugging easier).')
    parser.add_argument('--limit', type=int, dest='limit', default=0, help="Limit the number of experiments to poll (to make testing easier).")
    args = parser.parse_args(sys.argv[1:])

    app = pg.mkQApp()
    # console = pg.dbg()
    db = Dashboard(limit=args.limit, no_thread=args.no_thread)
    db.show()

    if sys.flags.interactive == 0:
        app.exec_()

