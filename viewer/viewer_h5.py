"""H5 data-source adapter for the existing FXViewer window and PlotItems.

This module never creates a second viewer, GraphicsLayout or plotting canvas.
Live receivers/history remain independent while H5 owns the display.
"""
from collections import OrderedDict
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import replace
import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets
import viewer_ui as ui

VN = timezone(timedelta(hours=7))


def replace_axis(plot, side, axis):
    old = plot.getAxis(side)
    pos = plot.axes[side]['pos']
    plot.layout.removeItem(old)
    old.unlinkFromView()
    old.setParentItem(None)
    if old.scene() is not None:
        old.scene().removeItem(old)
    axis.linkToView(plot.vb)
    plot.axes[side] = {'item': axis, 'pos': pos}
    plot.layout.addItem(axis, *pos)
    axis.setZValue(.5)
    axis.setFlag(axis.GraphicsItemFlag.ItemNegativeZStacksBehindParent)
    plot.showAxis(side, True)
    return old


def saved_time(value, fmt='%H:%M:%S'):
    try:
        return datetime.fromtimestamp(value, VN).strftime(fmt) if np.isfinite(value) else ''
    except (ValueError, OverflowError, OSError):
        return 'Invalid UTC'


class SavedTimeAxis(pg.AxisItem):
    def __init__(self, orientation='left'):
        super().__init__(orientation=orientation); self.enableAutoSIPrefix(False)

    def tickStrings(self, values, scale, spacing):
        return [saved_time(v, '%d/%m' if spacing >= 86400 else '%H:%M:%S') for v in values]


class ReaderWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(int,int)
    ready = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, function, parent):
        super().__init__(parent); self.function = function

    def report_progress(self,done,total):
        percent=int(100*done/max(total,1))
        if percent!=getattr(self,'last_percent',None):
            self.last_percent=percent;self.progress.emit(done,total)

    def run(self):
        try:
            result = self.function(self.isInterruptionRequested,self.report_progress)
            if not self.isInterruptionRequested(): self.ready.emit(result)
        except InterruptedError: pass
        except Exception as e:
            if not self.isInterruptionRequested():self.failed.emit(f'{type(e).__name__}: {e}')


class H5Source(QtCore.QObject):
    def __init__(self, viewer, toolbar, side):
        super().__init__(viewer)
        self.v = viewer; self.active = False; self.worker = None
        self.catalog = None; self.data = None; self.deep = None
        self.generation = 0; self.pending_load = False; self.error_text = ''
        self.mesh_items = []; self.mesh_key = None; self.syncing = False
        self.drift = []; self.saved = None; self.fit_next=False
        self.record_cache=None;self.pending_spectrum=False
        self.view_data=None;self.view_cache=OrderedDict();self.view_cache_bytes=0
        self.view_revision=0;self.pending_view=False
        self.view_timer=QtCore.QTimer(self);self.view_timer.setSingleShot(True)
        self.view_timer.setInterval(180);self.view_timer.timeout.connect(self.refine_view)
        self.rolling_cache=None;self.cache_hits=0;self.median_runs=0
        self.source = QtWidgets.QComboBox()
        self.source.addItem('Live UDP', 'live'); self.source.addItem('H5 files', 'h5')
        self.source.setToolTip('Select the data source for this same viewer and plots')
        toolbar.insertWidget(1, self.source)
        self.open_button = QtWidgets.QPushButton('Open H5…')
        self.folder_button = QtWidgets.QPushButton('Folder…')
        toolbar.insertWidget(2, self.open_button); toolbar.insertWidget(3, self.folder_button)
        self.panel = QtWidgets.QFrame(); box = QtWidgets.QVBoxLayout(self.panel)
        box.setContentsMargins(0,0,0,8)
        ui.sidebar_section(box, 'H5 SOURCE  /  UTC+07')
        self.start = QtWidgets.QDateTimeEdit(); self.end = QtWidgets.QDateTimeEdit()
        for w in (self.start,self.end):
            w.setTimeSpec(QtCore.Qt.UTC); w.setCalendarPopup(True)
            w.setDisplayFormat('yyyy-MM-dd HH:mm:ss.zzz')
        ui.sidebar_field(box,'From',self.start); ui.sidebar_field(box,'To',self.end)
        self.roi = QtWidgets.QCheckBox('Use frequency ROI')
        self.quantity = QtWidgets.QComboBox();self.quantity.addItems(['S12','S11','S22'])
        ui.sidebar_field(box,'Power waterfall',self.quantity)
        self.spectrum_mode=QtWidgets.QComboBox()
        self.spectrum_mode.addItems(['Mean spectrum','Single H5 record'])
        ui.sidebar_field(box,'Spectrum view',self.spectrum_mode)
        self.record_number=QtWidgets.QSpinBox();self.record_number.setRange(1,1)
        self.record_number.setKeyboardTracking(False)
        ui.sidebar_field(box,'Saved record (use arrows for previous/next)',self.record_number)
        self.spectrum_mode.currentIndexChanged.connect(self.request_spectrum)
        self.record_number.valueChanged.connect(self.request_spectrum)
        self.high = QtWidgets.QDoubleSpinBox(); self.high.setDecimals(6)
        self.high.setRange(0,100000); self.high.setValue(35.6); self.high.setSuffix(' MHz')
        box.addWidget(self.roi); ui.sidebar_field(box,'ROI high (low = Requested frequency)',self.high)
        self.load_button = QtWidgets.QPushButton('Load selected range'); self.load_button.setObjectName('pauseButton')
        self.full_button = QtWidgets.QPushButton('Full H5 time range')
        self.cancel_button = QtWidgets.QPushButton('Cancel read')
        self.meta_button = QtWidgets.QPushButton('Files / measurement metadata')
        for w in (self.load_button,self.full_button,self.cancel_button,self.meta_button):box.addWidget(w)
        note=QtWidgets.QLabel('Closed H5 files only. Time gaps / session boundaries are not joined. Zoom time axis for individual saved records; overview is averaged.')
        note.setWordWrap(True); note.setObjectName('mutedLabel'); box.addWidget(note)
        side.insertWidget(0,self.panel); self.panel.hide(); self.cancel_button.setEnabled(False)
        self.source.currentIndexChanged.connect(self.change_source)
        self.open_button.clicked.connect(self.choose_files); self.folder_button.clicked.connect(self.choose_folder)
        self.load_button.clicked.connect(self.load); self.full_button.clicked.connect(self.full_range)
        self.cancel_button.clicked.connect(self.cancel); self.meta_button.clicked.connect(self.metadata)
        self.quantity.currentIndexChanged.connect(self.load)
        for bar in (viewer.cbar_fast,viewer.cbar_phase,viewer.cbar_deep):
            bar.sigLevelsChanged.connect(self.render_waterfalls)
        self.live_controls=(viewer.time_window,viewer.follow_live,viewer.wf_window,viewer.wf_follow,
                            viewer.pause_button,viewer.link_power_scales)

    def change_source(self, *_):
        if self.source.currentData() == 'h5': self.enter()
        else: self.leave()

    def choose_files(self):
        paths,_=QtWidgets.QFileDialog.getOpenFileNames(self.v,'Open closed H5 files','','HDF5 (*.h5)')
        if paths:self.open(paths)

    def choose_folder(self):
        path=QtWidgets.QFileDialog.getExistingDirectory(self.v,'Open H5 folder (top level)')
        if path:self.open(sorted(str(p) for p in Path(path).glob('*.h5')))

    def open(self, paths):
        if not self.active:self.source.setCurrentIndex(1)
        if not self.active or self.worker is not None:return
        def task(cancel,progress):
            from offline_h5 import Catalog
            return Catalog(paths,cancel)
        self.launch(task,self.indexed)

    def launch(self, function, callback, kind=None):
        if self.worker is not None:return
        self.error_text=''; token=self.generation
        self.task_kind=kind or ("index" if callback.__name__=="indexed" else "data")
        self.worker=worker=ReaderWorker(function,self)
        worker.ready.connect(lambda value: callback(value) if self.active and token==self.generation else None)
        worker.failed.connect(lambda error:self.failed(error) if self.active and token==self.generation else None)
        worker.progress.connect(lambda done,total:self.v.rolling.set_progress(done,total) if self.active and token==self.generation else None)
        worker.finished.connect(self.finished)
        self.busy(True); self.rolling_status();self.v.rolling.set_progress(0,0);worker.start()

    def busy(self, yes):
        for w in (self.open_button,self.folder_button,self.start,self.end,self.high,self.roi,
                  self.quantity,self.spectrum_mode,self.record_number,self.load_button,self.full_button,self.v.frequency,self.v.bin_minus_button,
                  self.v.bin_plus_button,self.v.snap_peak_button):w.setEnabled(not yes)
        self.cancel_button.setEnabled(yes)
        if self.active:self.v.marker.setMovable(not yes)
        self.status()

    def finished(self):
        worker=self.worker; self.worker=None
        if worker:worker.deleteLater()
        self.v.rolling.panel.cancel.setEnabled(False)
        if self.active:
            self.busy(False);self.rolling_status()
            if self.pending_load:
                self.pending_load=False; QtCore.QTimer.singleShot(0,self.load)
            elif self.pending_spectrum:
                self.pending_spectrum=False;QtCore.QTimer.singleShot(0,self.request_spectrum)
            elif self.pending_view:
                self.pending_view=False;self.view_timer.start()
        else:
            # Do not alter live frequency/pause controls after a cancelled H5 read.
            for w in (self.open_button,self.folder_button,self.start,self.end,self.high,
                      self.roi,self.quantity,self.load_button,self.full_button):w.setEnabled(True)
            self.cancel_button.setEnabled(False)

    def cancel(self):
        self.pending_load=False;self.pending_spectrum=False
        self.reset_view()
        self.v.rolling.panel.cancel.setEnabled(False)
        self.generation += 1
        if self.worker:self.worker.requestInterruption()

    def failed(self, error):
        self.error_text=error
        if self.task_kind=='view':self.status()
        else:QtWidgets.QMessageBox.warning(self.v,'H5 source',error)

    def enter(self):
        if self.active:return
        v=self.v
        v.rolling.remember_levels()
        if v._persist_settings:v._save_ui_settings()
        self.saved={
            'frequency':(v.frequency.minimum(),v.frequency.maximum(),v.frequency.singleStep(),v.frequency.value()),
            'threshold':v.threshold.value(),'fringe_mode':v.fringe_mode.currentIndex(),
            'pause':v.pause_button.isChecked(),'follow':v.follow_live.isChecked(),'wf_follow':v.wf_follow.isChecked(),
            'link':v.link_power_scales.isChecked(),'levels':[tuple(b.values) for b in (v.cbar_fast,v.cbar_phase,v.cbar_deep)],
            'ranges':[p.viewRange() for p in v.all_plots],
            'enabled':[(w,w.isEnabled()) for w in self.live_controls],
            'axes':[(p,p.getAxis('left')) for p in (v.wf_fast_plot,v.wf_phase_plot,v.wf_deep_plot)],
            'time_axes':[(p,p.getAxis('bottom')) for p in (v.phase_plot,v.fringe_plot,v.gamma_plot,v.power_plot)],
            'titles':[(p,p.titleLabel.text) for p in v.all_plots],
            'peak':v.peak_summary_text,
            'bar_labels':[b.getAxis('left').labelText for b in (v.cbar_fast,v.cbar_deep)],
            'checks':{k:w.isChecked() for k,w in v.plot_checks.items()},'focus':v.focus_combo.currentIndex(),
            'mode':v.mode.currentIndex(),
        }
        self.saved['curves'] = [(curve, tuple(None if x is None else x.copy() for x in curve.getData()), curve.isVisible())
            for curve in (*v.curves.values(),*v.power_curves.values(),v.deep_curve,v.phase_curve,v.phase_points,v.gamma_curve,v.fringe_re_curve,v.fringe_im_curve)]
        self.saved['labels'] = [(label,label.text(),label.toolTip()) for label in
            (*v.metric_values.values(),*v.metric_details.values(),v.readout_pinned,v.readout_hover,v.bin_label)]
        self.v.rolling.invalidate()
        self.active=True; self.generation+=1
        tag=v.findChild(QtWidgets.QLabel,'protocolTag')
        if tag:tag.setText('H5 SCHEMA 1')
        self.panel.show(); v._hide_all_hover_overlays(); v.clear_drift_measurement()
        for w in self.live_controls:w.setEnabled(False)
        for w in (v.pause_button,v.follow_live,v.wf_follow,v.link_power_scales):
            w.blockSignals(True); w.setChecked(False); w.blockSignals(False)
        v.return_live_button.setEnabled(True)
        v.threshold.setEnabled(True); v.marker.setMovable(True)
        v.threshold.blockSignals(True); v.threshold.setValue(.1); v.threshold.blockSignals(False)
        v.fringe_mode.blockSignals(True); v.fringe_mode.setCurrentIndex(1); v.fringe_mode.blockSignals(False)
        v.img_fast.hide(); v.img_phase.hide(); v.img_deep.hide()
        for p,_ in self.saved['axes']:
            replace_axis(p,'left',SavedTimeAxis()); p.enableAutoRange(x=False,y=False)
            p.setLimits(yMin=None,yMax=None,minYRange=None,maxYRange=None)
            p.setLabel('left','Vietnam UTC+07')
        for p,_ in self.saved['time_axes']:replace_axis(p,'bottom',SavedTimeAxis('bottom'))
        ui.style_plots(v.all_plots)
        v.cbar_fast.setLevels((18,40)); v.cbar_deep.setLevels((18,40))
        v.display_badge.setText('H5  /  READ ONLY')
        v.return_live_button.setText('Return to live')
        self.clear_plots(); self.set_titles(); self.status()
        self.busy(self.worker is not None)
        if self.data is not None and self.data.get('rolling_config')!=v.rolling.config:
            self.data=self.deep=None
            self.pending_load=True
            if self.worker is None:
                self.pending_load=False;QtCore.QTimer.singleShot(0,self.load)
        if self.catalog:
            if self.data is not None:
                v.frequency.blockSignals(True);v.frequency.setValue(float(self.data['frequency'][0]/1e6));v.frequency.blockSignals(False)
            self.configure_frequency(); self.render(); self.fit_time()

    def leave(self):
        if not self.active:return
        v=self.v
        try:v.start_live_receivers()
        except Exception as e:
            self.source.blockSignals(True); self.source.setCurrentIndex(1); self.source.blockSignals(False)
            QtWidgets.QMessageBox.warning(v,'Live UDP could not start',str(e));return
        self.v.rolling.remember_levels()
        self.v.rolling.invalidate()
        self.active=False; self.generation+=1; self.cancel(); self.panel.hide()
        tag=v.findChild(QtWidgets.QLabel,'protocolTag')
        if tag:tag.setText('SOL2 v3')
        self.clear_meshes()
        for p,axis in self.saved['axes']:replace_axis(p,'left',axis).deleteLater()
        for p,axis in self.saved['time_axes']:replace_axis(p,'bottom',axis).deleteLater()
        for p,title in self.saved['titles']:p.setTitle(title)
        for b,label in zip((v.cbar_fast,v.cbar_deep),self.saved['bar_labels']):b.getAxis('left').setLabel(label)
        v.peak_summary_text=self.saved['peak'];v.clear_drift_measurement()
        for image in (v.img_fast,v.img_phase,v.img_deep):image.show()
        for w,en in self.saved['enabled']:w.setEnabled(en)
        lo,hi,step,freq=self.saved['frequency']
        v.frequency.blockSignals(True); v.frequency.setRange(lo,hi); v.frequency.setSingleStep(step); v.frequency.setValue(freq); v.frequency.blockSignals(False)
        for widget,value in ((v.threshold,self.saved['threshold']),(v.fringe_mode,self.saved['fringe_mode'])):
            widget.blockSignals(True)
            if widget is v.fringe_mode:widget.setCurrentIndex(value)
            else:widget.setValue(value)
            widget.blockSignals(False)
        for widget,key in ((v.pause_button,'pause'),(v.follow_live,'follow'),(v.wf_follow,'wf_follow'),(v.link_power_scales,'link')):
            widget.blockSignals(True);widget.setChecked(self.saved[key]);widget.blockSignals(False)
        for bar,levels in zip((v.cbar_fast,v.cbar_phase,v.cbar_deep),self.saved['levels']):
            bar.blockSignals(True);bar.setLevels(levels);bar.blockSignals(False)
        for k,on in self.saved['checks'].items():
            v.plot_checks[k].blockSignals(True);v.plot_checks[k].setChecked(on);v.plot_checks[k].blockSignals(False)
        v.mode.blockSignals(True);v.mode.setCurrentIndex(self.saved['mode']);v.mode.blockSignals(False)
        v.focus_combo.blockSignals(True);v.focus_combo.setCurrentIndex(self.saved['focus']);v.focus_combo.blockSignals(False)
        v._focus_key=v.focus_combo.currentData()
        v.workspace_title.setText(v.mode.currentText().upper()+' OBSERVATION')
        v.display_badge.setText('DISPLAY PAUSED' if v.pause_button.isChecked() else 'DISPLAY LIVE')
        self.clear_plots()
        v.on_pause_toggled(v.pause_button.isChecked())
        v._update_bin_buttons();v._update_return_live_button()
        v.select_frequency(freq);v.update_layout_geometry()
        if self.saved['pause']:
            for curve, data, visible in self.saved['curves']:
                curve.setData(*tuple([] if x is None else x for x in data));curve.setVisible(visible)
            for label, text, tooltip in self.saved['labels']:
                label.setText(text);label.setToolTip(tooltip)
            v.marker.setValue(v.model.actual)
        else:
            v.sync_displayed_curves();v.update_curve_visibility();v.render()
        for p,ranges in zip(v.all_plots,self.saved['ranges']):p.setRange(xRange=ranges[0],yRange=ranges[1],padding=0)
        # Reapply live slot and follow coordinates after actual H5 UTC axes.
        v._update_wf_view();v._render_wf_window();v.update_time_view()

    def indexed(self, catalog):
        self.rolling_cache=None;self.record_cache=None
        self.reset_view()
        self.catalog=catalog;self.data=None;self.deep=None;self.clear_plots()
        self.configure_frequency();self.set_dates();self.pending_load=True;self.fit_next=True

    def configure_frequency(self):
        if self.catalog is None:return
        w=self.v.frequency;freq=self.catalog.freq/1e6
        w.blockSignals(True);w.setRange(float(freq[0]),float(freq[-1]));w.setSingleStep(self.catalog.bandwidth/1e6)
        w.setValue(float(freq[np.argmin(abs(freq-w.value()))]));w.blockSignals(False)

    def set_dates(self):
        if self.catalog:
            for widget,t in ((self.start,self.catalog.times[0]),(self.end,self.catalog.times[-1])):
                widget.setDateTime(QtCore.QDateTime.fromMSecsSinceEpoch(round((t+25200)*1000),QtCore.Qt.UTC))

    def full_range(self):
        self.fit_next=True;self.set_dates();self.load()

    def load(self, *_, use_loaded_range=False):
        if not self.active or self.catalog is None or self.worker is not None:return
        c=self.catalog
        if use_loaded_range and self.data is not None:
            # A bin change acts on the displayed observation, not uncommitted
            # edits in From/To. Keep the exact successful request for caching.
            start,end=self.data.get('_time_range',
                (float(self.data['times'][0])-.0005,float(self.data['times'][-1])+.0005))
        else:
            # Interpret displayed calendar fields as UTC+07, independently of
            # Qt/OS timezone settings; the editor stores a wall-clock display.
            def utc_seconds(widget):
                dt=widget.dateTime()
                wall=QtCore.QDateTime(dt.date(),dt.time(),QtCore.Qt.UTC)
                return wall.toMSecsSinceEpoch()/1000-25200
            start=utc_seconds(self.start)-.0005
            end=utc_seconds(self.end)+.0005
        if start>end or not np.any((c.times>=start)&(c.times<=end)):
            self.error_text=('No H5 records in From/To. Choose a range containing saved records '
                             'or click Full H5 time range. Previous display is retained.')
            self.status()
            return
        low=self.v.frequency.value()*1e6;high=self.high.value()*1e6 if self.roi.isChecked() else low
        group=self.v.deep_frames
        quantity=self.quantity.currentText()
        config=self.v.rolling.config
        # One complete signed result only: bounded by existing display mesh limits.
        key=(id(c),start,end,quantity,group,config.window_s,config.alignment,config.min_samples)
        cached=self.rolling_cache
        previous,previous_deep=self.data,self.deep
        selection_only=(use_loaded_range and previous is not None and previous_deep is not None
                        and previous['quantity']==quantity
                        and previous['rolling_config']==config
                        and previous_deep['group']==max(1,int(group),int(np.ceil(len(previous['times'])/1200))))
        reuse_raw=(previous is not None and previous_deep is not None
                   and previous.get('_time_range')==(start,end)
                   and previous['quantity']==quantity
                   and previous_deep['group']==max(1,int(group),int(np.ceil(len(previous['times'])/1200))))
        def task(cancel,progress):
            if selection_only:
                fast=c.select_traces(previous,low,high,cancel)
                deep=dict(previous_deep);deep['traces']=fast['traces'];deep['frequency']=fast['frequency'];deep['power_traces']=fast['power_traces']
                return fast,deep
            if reuse_raw:
                # Validate sources even when no spectral block needs reading.
                from h5_cache import fingerprint
                fingerprint(c,('reuse-raw',))
                fast=dict(previous);deep=dict(previous_deep)
                for item in (fast,deep):
                    item['meshes']=item.get('_raw_meshes',item['meshes'])
                    item.pop('_rolling_deferred',None)
                if not (len(fast['frequency'])==1 and low==high==fast['frequency'][0]):
                    fast=c.select_traces(fast,low,high,cancel)
                    for name in ('traces','frequency','power_traces'):deep[name]=fast[name]
            else:
                fast=c.load(start,end,low,high,quantity=quantity,cancelled=cancel,
                    progress=lambda done,total:progress(int(40*done/max(total,1)),100))
                deep=(dict(fast) if max(1,int(group),int(np.ceil(len(fast['times'])/1200)))==fast['group']
                      else c.load(start,end,low,high,quantity=quantity,cancelled=cancel,display_group=group))
            progress(2,4)
            fast['_time_range']=deep['_time_range']=(start,end)
            fast['rolling_config']=deep['rolling_config']=config
            if config.enabled:
                # Compute residuals only after the loaded viewport is known.
                for item in (fast,deep):
                    item['_raw_meshes']=item['meshes']
                    item['meshes']=[(edges,np.full_like(values,np.nan),gamma)
                                    for edges,values,gamma in item['meshes']]
                    item['_rolling_deferred']=True
            progress(100,100)
            return fast,deep
        self.launch(task,self.loaded)

    def reclip(self,config):
        if self.data is None or self.worker is not None:return False
        old=self.data['rolling_config']
        if not old.enabled or replace(old,clip=False)!=replace(config,clip=False):return False
        if self.rolling_cache is None:return False
        pair=[]
        for source,meshes in zip((self.data,self.deep),self.rolling_cache[1]):
            item=dict(source);item['rolling_config']=config
            item['meshes']=[(edges,np.maximum(values,0) if config.clip else values,gamma) for edges,values,gamma in meshes]
            pair.append(item)
        self.loaded(pair);return True

    def loaded(self, pair):
        if pair[0]['rolling_config']!=self.v.rolling.config:
            self.pending_load=True;return
        if pair[0].get('_rolling_deferred'):self.rolling_cache=None
        cache=pair[0].pop('_rolling_cache',None)
        if cache is not None:self.rolling_cache=cache
        old=self.data
        fit=self.fit_next or old is None or not (np.array_equal(old['starts'],pair[0]['starts']) and np.array_equal(old['ends'],pair[0]['ends']))
        self.fit_next=False
        reuse_display=(old is not None and old['meshes'] is pair[0]['meshes']
                       and old['phase_meshes'] is pair[0]['phase_meshes'])
        if not reuse_display:self.reset_view()
        self.data,self.deep=pair
        self.record_number.blockSignals(True)
        self.record_number.setRange(1,len(self.data['times']))
        self.record_number.blockSignals(False)
        if reuse_display:
            self.v.marker.setValue(float(self.data['frequency'][0]/1e6))
            self.render_traces();self.rolling_status()
        else:
            self.mesh_key=None;self.render()
        if fit:self.fit_time()
        if self.data.get('_rolling_deferred') and len(self.data['times'])>1200 and (fit or old is None or not old['rolling_config'].enabled):
            self.v.wf_fast_plot.setYRange(float(self.data['starts'][-1200]),float(self.data['ends'][-1]),padding=0)
        self.v.update_layout_geometry();self.status()
        if not reuse_display:self.view_timer.start()

    def select(self, frequency):
        if self.catalog is None:return
        i=int(np.argmin(abs(self.catalog.freq-frequency*1e6)))
        self.v.marker.setValue(float(self.catalog.freq[i]/1e6))
        self.v.bin_label.setText(f'H5 bin {i} → {self.catalog.freq[i]/1e6:.6f} MHz')
        self.load(use_loaded_range=True)

    def step(self, delta):
        if self.catalog is None or self.worker is not None:return
        i=int(np.argmin(abs(self.catalog.freq-self.v.frequency.value()*1e6)))
        self.v.frequency.setValue(float(self.catalog.freq[np.clip(i+delta,0,len(self.catalog.freq)-1)]/1e6))

    def snap(self):
        if self.data is None:return
        arr=self.data['mean_abs']
        if np.any(np.isfinite(arr)):
            i=int(np.nanargmax(arr));self.v.frequency.setValue(float(self.catalog.freq[i]/1e6))

    def fit_time(self):
        if self.data is None:return
        v=self.v;d=self.data
        for p in (v.phase_plot,v.fringe_plot,v.gamma_plot,v.power_plot):p.setXRange(float(d['starts'][0]),float(d['ends'][-1]),padding=.01)
        self.syncing=True
        try:
            for p in (v.wf_fast_plot,v.wf_phase_plot,v.wf_deep_plot):
                p.setYRange(float(d['starts'][0]),float(d['ends'][-1]),padding=0)
                p.setXRange(float((self.catalog.freq[0]-self.catalog.bandwidth/2)/1e6),float((self.catalog.freq[-1]+self.catalog.bandwidth/2)/1e6),padding=0)
        finally:self.syncing=False
        v.phase_plot.setYRange(-180,180,padding=0);v.gamma_plot.setYRange(0,1.05,padding=0)
        self.view_revision+=1
        if self.worker is not None and self.task_kind=='view':self.worker.requestInterruption()
        self.view_timer.start()

    def sync_y(self, source, ranges):
        if self.syncing:return
        self.syncing=True
        try:
            plots={'wf_fast':self.v.wf_fast_plot,'wf_phase':self.v.wf_phase_plot,'wf_deep':self.v.wf_deep_plot}
            for name,plot in plots.items():
                if name!=source:plot.setYRange(*ranges,padding=0)
        finally:self.syncing=False
        self.view_revision+=1
        if self.worker is not None and self.task_kind=='view':
            self.worker.requestInterruption()
        self.view_timer.start()

    def reset_view(self):
        self.view_timer.stop();self.view_revision+=1;self.pending_view=False
        self.view_data=None;self.view_cache.clear();self.view_cache_bytes=0

    def refine_view(self):
        """Read only visible time supports; never replace full-range science data."""
        if not self.active or self.data is None:return
        if self.worker is not None:
            self.pending_view=True;return
        d=self.data;c=self.catalog
        lo,hi=self.v.wf_fast_plot.viewRange()[1]
        # Include supports crossing the view edge, not only timestamp centers.
        first=int(np.searchsorted(d['ends'],lo,side='right'))
        stop=int(np.searchsorted(d['starts'],hi,side='left'))
        if stop<=first:
            self.view_data=None;self.mesh_key=None;self.render_waterfalls();return
        group=max(1,int(np.ceil((stop-first)/1200)))
        if first==0 and stop==len(d['times']) and not d['rolling_config'].enabled:
            self.view_data=None;self.mesh_key=None;self.render_waterfalls();return
        # Prefetch nearby records in fixed blocks, so small pans reuse the mesh.
        first=max(0,(first//64)*64)
        stop=min(len(d['times']),int(np.ceil(stop/64))*64)
        cfg=d['rolling_config']
        key=(first,stop,group,d['quantity'],cfg)
        if self.view_data is not None and self.view_data[0]==key:return
        # Validate cached meshes just as raw block cache entries are validated.
        try:
            for entry in c.entries:
                stat=Path(entry.path).stat()
                if (stat.st_size,stat.st_mtime_ns)!=entry.stat:
                    raise ValueError('An H5 file changed. Reopen only closed files.')
        except (OSError,ValueError) as error:
            self.error_text=str(error);self.status();return
        if key in self.view_cache:
            self.view_cache.move_to_end(key);self.view_data=(key,self.view_cache[key])
            self.mesh_key=None;self.render_waterfalls();return
        revision=self.view_revision
        def task(cancel,progress):
            result=c.load(float(d['times'][first]),float(d['times'][stop-1]),
                float(d['frequency'][0]),float(d['frequency'][0]),quantity=d['quantity'],
                cancelled=cancel,display_group=group,waterfall_only=True)
            # Padding must not coarsen the resolution chosen for the visible area.
            result['rolling_config']=cfg
            if cfg.enabled:
                from rolling_median import h5_residuals
                result=h5_residuals(c,result,result,cfg,cancel,progress,context=d)[0]
            return result
        def ready(result):
            if revision!=self.view_revision:return
            arrays={id(a):a for name in ('meshes','phase_meshes')
                    for mesh in result[name] for a in mesh}
            size=sum(a.nbytes for a in arrays.values())
            # Cache only meshes; spectra and full-range traces stay in self.data.
            result={k:result[k] for k in ('meshes','phase_meshes','group','quantity')}
            if size<=96*1024**2:
                self.view_cache[key]=result;self.view_cache_bytes+=size
                while len(self.view_cache)>4 or self.view_cache_bytes>96*1024**2:
                    _,old=self.view_cache.popitem(last=False)
                    unique={id(a):a for name in ('meshes','phase_meshes') for mesh in old[name] for a in mesh}
                    self.view_cache_bytes-=sum(a.nbytes for a in unique.values())
            self.view_data=(key,result);self.mesh_key=None;self.render_waterfalls()
        self.launch(task,ready,kind='view')

    def clear_meshes(self):
        for plot,item in self.mesh_items:plot.removeItem(item)
        self.mesh_items=[];self.mesh_key=None

    def clear_plots(self):
        self.clear_meshes();v=self.v
        for curve in v.power_curves.values():curve.setData([],[])
        for curve in (*v.curves.values(),*v.power_curves.values(),v.deep_curve,v.phase_curve,v.phase_points,v.gamma_curve,v.fringe_re_curve,v.fringe_im_curve):curve.setData([],[])
        v._hide_all_hover_overlays()
        v.readout_hover.setText('H5: no hover sample' if self.active else 'Live: hover a sample')
        v.readout_hover.setToolTip('')
        for label in (*v.metric_values.values(),*v.metric_details.values(),v.readout_pinned):label.setToolTip('')
        for key in v.metric_values:v.metric_values[key].setText('—');v.metric_details[key].setText('H5: no selected data')
        if self.active:
            v.readout_pinned.setText('H5: open files and load a selected range')
            v.readout_hover.setText('H5: hover a saved sample or waterfall cell')

    def set_titles(self):
        v=self.v
        v.workspace_title.setText(v.mode.currentText().upper()+' / H5 OBSERVATION')
        v.spectrum_plot.setTitle('H5: FFT-weighted time mean S11 / S22 / mean |S12| — dB re 1 ADC-count²')
        v.wf_fast_plot.setTitle('H5 saved S12 waterfall')
        v.wf_phase_plot.setTitle('H5 saved phase ° — coherence display mask')
        v.wf_deep_plot.setTitle('H5 coherent time-averaged S12')

    def spectrum_data(self):
        if self.spectrum_mode.currentIndex()==0:return self.data
        i=int(np.searchsorted(self.catalog.times,self.data['times'][self.record_number.value()-1]))
        key=(id(self.catalog),i)
        if self.record_cache is not None and self.record_cache[0]==key:return self.record_cache[1]
        return None

    def request_spectrum(self, *_):
        if not self.active or self.data is None:return
        if self.worker is not None:
            self.pending_spectrum=True;return
        if self.spectrum_mode.currentIndex()==0:
            self.render();return
        index=int(np.searchsorted(self.catalog.times,self.data['times'][self.record_number.value()-1]))
        key=(id(self.catalog),index)
        if self.spectrum_data() is not None:
            self.render();return
        catalog=self.catalog
        def task(cancel,progress):return key,catalog.read_spectrum(index,cancel)
        def ready(value):
            self.record_cache=value;self.render()
        self.launch(task,ready)

    def render(self):
        if not self.active:return
        self.set_titles()
        if self.data is None:return
        from offline_h5 import db
        v=self.v;d=self.spectrum_data();freq=self.catalog.freq/1e6
        if d is None:
            self.pending_spectrum=True
            if self.worker is None:QtCore.QTimer.singleShot(0,self.request_spectrum)
            d={'mean':np.full((4,len(freq)),np.nan),'mean_abs':np.full(len(freq),np.nan)}
            v.spectrum_plot.setTitle('H5 spectrum — reading selected record…')
        elif self.spectrum_mode.currentIndex()==1:
            v.spectrum_plot.setTitle(f'H5 record {self.record_number.value()}/{len(self.data["times"])} — '
                f'{saved_time(d["timestamp"],"%Y-%m-%d %H:%M:%S.%f")} UTC+07 — row {d["row"]} — raw saved spectrum')
        # Existing live curve objects, not a second graphics canvas.
        # Stream identifiers belong to the live protocol module.
        from telemetry_protocol import S12,S11,S22
        for kind,y in ((S12,db(d['mean_abs'])),(S11,db(d['mean'][0])),(S22,db(d['mean'][1]))):
            v.curves[kind].setData(freq,y,connect='finite');v.curves[kind].setVisible(v.curve_checks[kind].isChecked())
        v.deep_curve.setData([],[])  # Only one time-mean spectrum; no duplicate 'Deep' spectrum.
        v.deep_curve.setVisible(False)
        if v.autoscale_y_check.isChecked():self.fit_spectrum()
        v.marker.setValue(float(self.data['frequency'][0]/1e6))
        if np.any(np.isfinite(d['mean_abs'])):
            i=int(np.nanargmax(d['mean_abs']))
            v.peak_summary_text=f'H5 spectrum |S12| peak: {freq[i]:.6f} MHz, {db(d["mean_abs"][i]):.2f} dB'
            v.update_metrics_readout()
        self.v.rolling.style(self.data["rolling_config"])
        self.render_traces();self.render_waterfalls();self.status();self.rolling_status()

    def fit_spectrum(self):
        if self.data is None:return
        from offline_h5 import db
        from telemetry_protocol import S12,S11,S22
        d=self.spectrum_data()
        if d is None:return
        values=[db(a) for key,a in ((S12,d['mean_abs']),(S11,d['mean'][0]),(S22,d['mean'][1])) if self.v.curve_checks[key].isChecked()]
        vals=np.concatenate([a[np.isfinite(a)] for a in values]) if values else np.array([])
        if len(vals):self.v.spectrum_plot.setYRange(float(vals.min())-3,float(vals.max())+3,padding=0)

    def render_power(self):
        if self.data is None:return
        from offline_h5 import broken_series,db
        from telemetry_protocol import S11,S22,S12
        d=self.data
        for key,values in zip((S11,S22,S12),d['power_traces']):
            x,y=broken_series(d['times'],db(values),d['breaks'])
            self.v.power_curves[key].setData(x,y,connect='finite')
            self.v.power_curves[key].setVisible(self.v.power_checks[key].isChecked())
        self.v.power_plot.setTitle(f'H5 power vs time — {d["frequency"][0]/1e6:.6f} MHz (single saved bin; no coherence mask)')

    def render_traces(self):
        if not self.active or self.data is None:return
        from offline_h5 import derived,broken_series
        self.render_power()
        v=self.v;d=self.data;a,b,re,im=d['traces'];mag,phase,gamma=derived(a,b,re,im)
        good=np.isfinite(phase)&np.isfinite(gamma)&(gamma>0)&(gamma>=v.threshold.value())
        x,y=broken_series(d['times'],np.where(good,np.rad2deg(phase),np.nan),d['breaks'])
        v.phase_curve.setData([],[]);finite=np.isfinite(x)&np.isfinite(y);v.phase_points.setData(x[finite],y[finite])
        x,y=broken_series(d['times'],gamma,d['breaks']);v.gamma_curve.setData(x,y,connect='finite')
        normalized=v.fringe_mode.currentData()=='normalized'
        if normalized:
            denom=np.sqrt(a*b)
            with np.errstate(divide='ignore',invalid='ignore'):rr,ii=re/denom,im/denom
        else:rr,ii=re,im
        for curve,vals in ((v.fringe_re_curve,rr),(v.fringe_im_curve,ii)):
            x,y=broken_series(d['times'],np.where(good,vals,np.nan),d['breaks']);curve.setData(x,y,connect='finite')
        v.fringe_re_curve.setVisible(v.fringe_re_check.isChecked());v.fringe_im_curve.setVisible(v.fringe_im_check.isChecked())
        v.fringe_plot.setLabel('left','Normalized R / I' if normalized else 'ADC-count² / input FFT bin')
        v.fringe_re_check.setText('Real: γ cosφ' if normalized else 'Re(S12) / real')
        v.fringe_im_check.setText('Imaginary: γ sinφ' if normalized else 'Im(S12) / imaginary')
        if v.fringe_auto_y.isChecked():
            vals=np.concatenate([a[good] for a,on in ((rr,v.fringe_re_check.isChecked()),(ii,v.fringe_im_check.isChecked())) if on]) if np.any(good) and (v.fringe_re_check.isChecked() or v.fringe_im_check.isChecked()) else np.array([])
            if normalized:lo,hi=-1.05,1.05
            elif len(vals):lo,hi=min(0.,float(vals.min())),max(0.,float(vals.max()));pad=max((hi-lo)*.06,1e-20);lo-=pad;hi+=pad
            else:lo,hi=-1,1
            v.fringe_plot.setYRange(lo,hi,padding=0)
        v.mask_line.setValue(v.threshold.value())
        fq=d['frequency']/1e6;selection=f'{fq[0]:.6f} MHz' if len(fq)==1 else f'{fq[0]:.6f}–{fq[-1]:.6f} MHz'
        v.fringe_plot.setTitle('H5 '+selection+' — '+('normalized correlation' if normalized else 'saved Re / Im'))
        v.phase_plot.setTitle('H5 '+selection+' — stored phase, no additional calibration / fringe stopping')
        v.metric_values['frequency'].setText(f'{fq[0]:.6f}');v.metric_details['frequency'].setText(f'MHz · {len(fq)} saved bin(s)')
        v.metric_values['phase'].setText(f'{np.rad2deg(phase[-1]):+.2f}°' if good[-1] else 'MASKED / INVALID')
        v.metric_details['phase'].setText('Last selected H5 record')
        v.metric_values['gamma'].setText(f'{gamma[-1]:.4f}' if np.isfinite(gamma[-1]) else 'INVALID')
        v.metric_details['gamma'].setText('Magnitude coherence · not γ²')
        v.metric_values['time'].setText(datetime.fromtimestamp(d['times'][-1],VN).strftime('%H:%M:%S'))
        v.metric_details['time'].setText('H5 · UTC estimated' if d['flags'][-1]&1 else 'H5 · saved UTC')
        v.readout_pinned.setText(f'H5 | {selection} | {len(d["times"])} records | {len(d["meshes"])} continuous segments | threshold γ ≥ {v.threshold.value():.2f}')
        v.bin_label.setText('H5 selection: '+selection)
        for key,label in v.metric_values.items():
            label.setToolTip(label.text()+'\n'+v.metric_details[key].text())
            v.metric_details[key].setToolTip(v.metric_details[key].text())
        v.readout_pinned.setToolTip(v.readout_pinned.text())

    def render_waterfalls(self, *_):
        if not self.active or self.data is None:return
        v=self.v
        fast=self.view_data[1] if self.view_data is not None else self.data
        deep=fast if self.view_data is not None else self.deep
        key=(id(fast),id(deep),v.threshold.value(),tuple(v.cbar_fast.values),tuple(v.cbar_phase.values),tuple(v.cbar_deep.values))
        if key==self.mesh_key:return
        self.clear_meshes()
        fe=np.r_[self.catalog.freq-self.catalog.bandwidth/2,self.catalog.freq[-1]+self.catalog.bandwidth/2]/1e6
        for plot,bar,meshes,phase in ((v.wf_fast_plot,v.cbar_fast,fast['meshes'],False),
                                     (v.wf_phase_plot,v.cbar_phase,fast['phase_meshes'],True),
                                     (v.wf_deep_plot,v.cbar_deep,deep['meshes'],False)):
            for edges,values,gamma in meshes:
                values=values.copy()
                if phase:values=np.where(np.isfinite(gamma)&(gamma>0)&(gamma>=v.threshold.value()),np.rad2deg(values),np.nan)
                from h5_raster import TimeRasterItem
                item=TimeRasterItem(fe,edges,values,tuple(bar.values),bar.colorMap())
                plot.addItem(item);self.mesh_items.append((plot,item))
        self.mesh_key=key
        quantity=self.data['quantity']
        def detail(source):
            return ('1 saved record/row' if source['group']==1 else
                    f'FFT-weighted display mean ≤{source["group"]} saved records/row')
        pending=' — rolling background pending…' if self.view_data is None and self.data.get('_rolling_deferred') else ''
        v.wf_fast_plot.setTitle(f'H5 {quantity} — {detail(fast)}{pending}')
        v.wf_deep_plot.setTitle(f'H5 deep {quantity} — {detail(deep)}')
        v.wf_phase_plot.setTitle(f'H5 phase ° — {detail(fast)} — coherence display mask')
        for bar in (v.cbar_fast,v.cbar_deep):bar.getAxis('left').setLabel(quantity+' [dB]' )
        v.rolling.style(self.data['rolling_config'])
        v.wf_history_label.setText(f'H5: {len(self.data["times"])} rows · {len(self.catalog.freq)} saved bins · actual UTC axis')

    def status(self):
        if not self.active:return
        v=self.v
        text='H5 READING…' if self.worker is not None else f'H5 / {len(self.catalog.entries)} FILES' if self.catalog else 'H5 / OPEN FILES'
        if self.error_text:text='H5 READ FAILED'
        v.connection_badge.setText(text)
        v.data_age_label.setText('Saved observations · read only')
        v.display_badge.setText('H5 / PREVIOUS CONFIGURATION' if self.data is not None and self.data['rolling_config']!=v.rolling.config else 'H5 / READ ONLY');v.return_live_button.setEnabled(True)
        v.statusBar().showMessage(self.error_text or (f'H5: {len(self.catalog.records)} indexed records; background Live UDP history remains separate' if self.catalog else 'Open one or more closed H5 files'))

    def metadata(self):
        if not self.catalog:return
        lines=['Saved S12 is used as stored; calibration_phi_rad_unshifted is NOT applied again.',
               'Time gaps and session/config/calibration boundaries are split. UTC may be estimated.']
        for e in self.catalog.entries:
            lines.append('\n'+Path(e.path).name)
            for k in ('session_id','calibration_id','phase_calibrated','timestamp_semantics','utc_uncertainty','units'):
                value=e.meta.get(k,'missing');value=value.decode() if isinstance(value,bytes) else str(value)
                lines.append(k+': '+value)
        dialog=QtWidgets.QDialog(self.v);dialog.setWindowTitle('H5 measurement metadata');dialog.resize(800,550)
        layout=QtWidgets.QVBoxLayout(dialog);text=QtWidgets.QPlainTextEdit();text.setReadOnly(True);text.setPlainText('\n'.join(lines));layout.addWidget(text)
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close);buttons.rejected.connect(dialog.reject);layout.addWidget(buttons);dialog.exec_()

    def point(self, key, x, y):
        if self.data is None:return None
        b=int(np.argmin(abs(self.catalog.freq-x*1e6)))
        if not self.catalog.freq[0]-self.catalog.bandwidth/2<=x*1e6<=self.catalog.freq[-1]+self.catalog.bandwidth/2:return None
        source=self.view_data[1] if self.view_data is not None else (self.deep if key=='wf_deep' else self.data)
        meshes=source['phase_meshes'] if key=='wf_phase' else source['meshes']
        for si,(edges,values,gamma) in enumerate(meshes):
            ri=int(np.searchsorted(edges,y,side='right')-1)
            if 0<=ri<len(values) and edges[ri]<=y<edges[ri+1]:
                val=values[ri,b];g=gamma[ri,b]
                if key=='wf_phase':
                    if not np.isfinite(g) or g<=0 or g<self.v.threshold.value():return None
                    val=np.rad2deg(val)
                if np.isfinite(val):return b,float(val),(edges[ri]+edges[ri+1])/2,si
        return None

    def mouse_move(self, pos):
        if self.data is None:return
        v=self.v;v._hide_all_hover_overlays()
        plots={'spectrum':v.spectrum_plot,'phase':v.phase_plot,'gamma':v.gamma_plot,'fringe':v.fringe_plot,
               'wf_fast':v.wf_fast_plot,'wf_phase':v.wf_phase_plot,'wf_deep':v.wf_deep_plot}
        for key,p in plots.items():
            if not p.isVisible() or not p.vb.sceneBoundingRect().contains(pos):continue
            point=p.vb.mapSceneToView(pos);x,y=point.x(),point.y()
            if key.startswith('wf_'):
                hit=self.point(key,x,y)
                if hit is None:message='H5: gap, masked or invalid cell'
                else:
                    b,val,t,_=hit;message=f'H5 {key} | {self.catalog.freq[b]/1e6:.6f} MHz | {datetime.fromtimestamp(t,VN):%d/%m %H:%M:%S} | {val:+.3f} '+('°' if key=='wf_phase' else 'dB')
            elif key=='spectrum':
                i=int(np.argmin(abs(self.catalog.freq-x*1e6)))
                from offline_h5 import db
                d=self.spectrum_data()
                if d is None:return
                label='single record' if self.spectrum_mode.currentIndex() else 'time mean'
                message=f'H5 {label} | {self.catalog.freq[i]/1e6:.6f} MHz | S11 {db(d["mean"][0,i]):.2f} dB | S22 {db(d["mean"][1,i]):.2f} dB | |S12| {db(d["mean_abs"][i]):.2f} dB'
            else:
                i=int(np.argmin(abs(self.data['times']-x)))
                if not self.data['starts'][i]<=x<=self.data['ends'][i]:message='H5: no record at this time'
                else:
                    a,b,re,im=self.data['traces'][:,i]
                    from offline_h5 import derived
                    _,phase,gamma=derived(a,b,re,im)
                    quality='VALID' if np.isfinite(gamma) and gamma>0 and gamma>=v.threshold.value() else 'MASKED / INVALID'
                    message=f'H5 raw [{quality}] | {datetime.fromtimestamp(self.data["times"][i],VN):%d/%m %H:%M:%S} | Re {re:.5g} Im {im:.5g} ADC-count²/bin | phase {np.rad2deg(phase):+.2f}° | γ {gamma:.4f} | flags {self.data["flags"][i]}'
            if key in ('wf_fast','wf_deep') and self.data['rolling_config'].enabled:
                message+=' | dB relative to rolling median'+(' (display clipped)' if self.data['rolling_config'].clip else ' (signed)')
            v.readout_hover.setText(message);v.readout_hover.setToolTip(message)
            v.crosshair_v[key].setValue(x);v.crosshair_h[key].setValue(y)
            v.crosshair_v[key].show();v.crosshair_h[key].show()
            return

    def mouse_click(self, event):
        if event.button()!=QtCore.Qt.LeftButton or self.data is None or self.worker is not None:return
        pos=event.scenePos();v=self.v
        if self.spectrum_mode.currentIndex()==1 and not event.modifiers()&QtCore.Qt.ShiftModifier:
            for p,axis in ((v.power_plot,'x'),(v.phase_plot,'x'),(v.fringe_plot,'x'),(v.gamma_plot,'x'),
                           (v.wf_fast_plot,'y'),(v.wf_phase_plot,'y'),(v.wf_deep_plot,'y')):
                if not p.isVisible() or not p.vb.sceneBoundingRect().contains(pos):continue
                point=p.vb.mapSceneToView(pos);t=point.x() if axis=='x' else point.y()
                i=int(np.argmin(abs(self.data['times']-t)))
                if not self.data['starts'][i]<=t<=self.data['ends'][i]:return
                self.record_number.setValue(i+1);return
        for key,p in (('spectrum',v.spectrum_plot),('wf_fast',v.wf_fast_plot),('wf_phase',v.wf_phase_plot),('wf_deep',v.wf_deep_plot)):
            if not p.isVisible() or not p.vb.sceneBoundingRect().contains(pos):continue
            pt=p.vb.mapSceneToView(pos)
            if key.startswith('wf_') and event.modifiers()&QtCore.Qt.ShiftModifier:self.measure(key,pt.x(),pt.y())
            else:v.frequency.setValue(float(np.clip(pt.x(),v.frequency.minimum(),v.frequency.maximum())))
            return

    def measure(self, key, x, y):
        hit=self.point(key,x,y)
        if hit is None:self.v.drift_summary_text='H5 drift: choose a valid cell, not a gap or masked phase';self.v.update_metrics_readout();return
        b,_,t,segment=hit
        marker=(key,b,t,segment)
        if not self.drift or len(self.drift)==2 or self.drift[0][0]!=key:self.drift=[marker]
        else:self.drift.append(marker)
        if len(self.drift)==1:self.v.drift_summary_text='H5 drift: choose point B on the same waterfall / continuous segment'
        elif self.drift[0][3]!=segment:self.v.drift_summary_text='H5 drift: endpoints cross a session / missing-data boundary; measurement rejected';self.drift=[]
        else:
            a,z=self.drift;dt=z[2]-a[2];df=(self.catalog.freq[z[1]]-self.catalog.freq[a[1]])/1e6
            rate=f'{df/dt:+.6f} MHz/s' if abs(dt)>1e-9 else 'undefined (same display row)'
            self.v.drift_summary_text=f'H5 drift: Δf={df:+.6f} MHz; Δt={dt:+.6f} s; {rate}; saved UTC row centers'
        self.v.update_metrics_readout()

    def rolling_status(self):
        if not self.active:return
        v=self.v
        if self.data is None:
            v.rolling.panel.status.setText('H5: reading input…' if self.worker is not None else 'H5: load a time range')
            return
        cfg=self.data['rolling_config']
        if cfg!=v.rolling.config:
            state='Recomputing' if self.worker is not None or self.pending_load else 'Not applied / cancelled; Load to retry'
            v.display_badge.setText('H5 / PREVIOUS CONFIGURATION')
            v.rolling.panel.status.setText(state+' — showing previous complete H5 result')
        elif cfg.enabled:
            from rolling_background import incremental
            engine=('Numba incremental' if incremental is not None else
                    'SLOW NumPy fallback — install viewer/requirements.txt')
            v.rolling.panel.status.setText(f'H5 rolling {cfg.window_s:g} s {cfg.alignment.lower()} · {len(self.data["times"])} input records\n'
                f'{engine} · visible range only; zooming out computes more records.\n'
                f'Window clipped at selected range/segment edges. Bins with fewer than {cfg.min_samples} finite records are gaps.\n'
                'Fast/Deep power only; spectra, phase, coherence and fringe use original statistics.')
        else:v.rolling.panel.status.setText('H5 rolling off — original data')
