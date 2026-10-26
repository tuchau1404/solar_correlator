"""Rolling-median controls and live worker; scientific curves remain raw."""
from dataclasses import replace
import json
import numpy as np
from PyQt5 import QtCore, QtWidgets
from rolling_median import RollingConfig,live_residuals,row_key,group_key
from telemetry_protocol import S12


class RollingWorker(QtCore.QThread):
    progress=QtCore.pyqtSignal(int,int)
    ready=QtCore.pyqtSignal(object)
    failed=QtCore.pyqtSignal(str)
    def __init__(self,rows,groups,config,cache,deep,parent):
        super().__init__(parent);self.rows=rows;self.groups=groups;self.config=config;self.cache=cache;self.deep=deep
    def report_progress(self,done,total):
        percent=int(100*done/max(total,1))
        if percent!=getattr(self,'last_percent',None):
            self.last_percent=percent;self.progress.emit(done,total)

    def run(self):
        try:
            result=live_residuals(self.rows,self.groups,self.config,self.cache,self.isInterruptionRequested,self.deep,self.report_progress)
            if not self.isInterruptionRequested():self.ready.emit(result)
        except InterruptedError:pass
        except Exception as exc:self.failed.emit(f'{type(exc).__name__}: {exc}')


class RollingPanel(QtWidgets.QGroupBox):
    changed=QtCore.pyqtSignal(object)
    def __init__(self,parent):
        super().__init__('Rolling median background',parent)
        box=QtWidgets.QVBoxLayout(self)
        self.enable=QtWidgets.QCheckBox('Enable rolling median');box.addWidget(self.enable)
        box.addWidget(QtWidgets.QLabel('Window (actual time)'))
        self.window=QtWidgets.QDoubleSpinBox();self.window.setRange(.1,600);self.window.setDecimals(1);self.window.setSuffix(' s');box.addWidget(self.window)
        box.addWidget(QtWidgets.QLabel('Window alignment'))
        self.alignment=QtWidgets.QComboBox();self.alignment.addItems(['Trailing','Centered']);box.addWidget(self.alignment)
        box.addWidget(QtWidgets.QLabel('Minimum valid records per bin'))
        self.minimum=QtWidgets.QSpinBox();self.minimum.setRange(1,10000);box.addWidget(self.minimum)
        self.clip=QtWidgets.QCheckBox('Clip below 0 dB for display');box.addWidget(self.clip)
        self.apply=QtWidgets.QPushButton('Apply rolling settings');box.addWidget(self.apply)
        self.progress=QtWidgets.QProgressBar();self.progress.setRange(0,100);self.progress.hide();box.addWidget(self.progress)
        self.cancel=QtWidgets.QPushButton('Cancel rolling');self.cancel.setEnabled(False);box.addWidget(self.cancel)
        self.status=QtWidgets.QLabel('Rolling off — original data');self.status.setWordWrap(True);box.addWidget(self.status)
        note=QtWidgets.QLabel('Fast/Deep power waterfalls only. Median is per input frequency bin in dB. Slow/long astronomical signals may be subtracted too. Centered live edges are provisional until future data arrive.')
        note.setWordWrap(True);box.addWidget(note)
        self.enable.toggled.connect(self.commit);self.clip.toggled.connect(self.commit)
        self.apply.clicked.connect(self.commit)

    def restore(self,cfg):
        for w in (self.enable,self.clip):w.blockSignals(True)
        self.enable.setChecked(cfg.enabled);self.clip.setChecked(cfg.clip)
        for w in (self.enable,self.clip):w.blockSignals(False)
        self.window.setValue(cfg.window_s);self.minimum.setValue(cfg.min_samples);self.alignment.setCurrentText(cfg.alignment)

    def commit(self,*_):
        try:cfg=RollingConfig(self.enable.isChecked(),self.window.value(),self.alignment.currentText(),self.clip.isChecked(),self.minimum.value())
        except ValueError as exc:self.status.setText(str(exc));return
        self.changed.emit(cfg)


class RollingController(QtCore.QObject):
    def __init__(self,viewer,side):
        super().__init__(viewer);self.v=viewer;self.config=RollingConfig()
        try:self.config=replace(RollingConfig.from_dict(json.loads(str(viewer.settings.value('rolling/state_v1','{}')))),enabled=False)
        except (ValueError,TypeError,KeyError):pass
        self.panel=RollingPanel(viewer);side.insertWidget(0,self.panel);self.panel.restore(self.config);self.panel.changed.connect(self.resume)
        self.worker=None;self.version=0;self.closed=False;self.cache={};self.deep={}
        self.signature=None;self.source_identity=None;self.error='';self.level_key=('live',False,False);self.original_levels={};self.levels={self.level_key:tuple(tuple(b.values) for b in (viewer.cbar_fast,viewer.cbar_deep))};self.suspended=False
        self.panel.cancel.clicked.connect(self.cancel)
        for bar in (viewer.cbar_fast,viewer.cbar_deep):bar.sigLevelsChanged.connect(self.remember_levels)
        self.original_titles=(viewer.wf_fast_plot.titleLabel.text,viewer.wf_deep_plot.titleLabel.text)
        self.original_labels=(viewer.cbar_fast.getAxis('left').labelText,viewer.cbar_deep.getAxis('left').labelText)
        self.timer=QtCore.QTimer(self);self.timer.timeout.connect(self.pulse);self.timer.start(600)
        viewer.pause_button.toggled.connect(self.invalidate)

    def save(self):
        if self.v._persist_settings:
            self.v.settings.setValue('rolling/state_v1',json.dumps(self.config.to_dict(),allow_nan=False));self.v.settings.sync()

    def invalidate(self,*_):
        self.version+=1;self.signature=None;self.cache={};self.deep={};self.error=''
        if self.worker:self.worker.requestInterruption()
        self.v._wf_image_signature=None

    def identity(self):
        v=self.v
        if v._is_h5():return ('h5',id(v.h5.catalog))
        rows=v._display_rows if v.pause_button.isChecked() else v.model.rows
        last=next((r for r in reversed(rows) if r is not None),None)
        return ('live',last[0] if last else None,v.pause_button.isChecked())

    def remember_levels(self,*_):
        source='h5' if self.v._is_h5() else 'live'
        if self.level_key is not None and self.level_key[0]==source:
            self.levels[self.level_key]=tuple(tuple(b.values) for b in (self.v.cbar_fast,self.v.cbar_deep))

    def set_progress(self,done,total):
        self.panel.progress.show();self.panel.progress.setRange(0,100 if total else 0)
        if total:self.panel.progress.setValue(int(100*done/total))
        self.panel.cancel.setEnabled(True)

    def cancel(self):
        if self.v._is_h5():
            self.v.h5.cancel();return
        self.suspended=True;self.version+=1
        if self.worker:self.worker.requestInterruption()
        self.panel.cancel.setEnabled(False)
        self.panel.status.setText('Rolling cancelled — Apply settings to resume.')

    def resume(self,cfg):
        if self.suspended:self.signature=None
        self.suspended=False
        if cfg==self.config and self.v._is_h5():self.v.h5.load()
        self.change(cfg);self.pulse()

    def change(self,cfg):
        if cfg==self.config:return
        self.remember_levels()
        display_only=replace(cfg,clip=False)==replace(self.config,clip=False)
        self.config=cfg
        if not display_only:self.invalidate()
        self.save();v=self.v;v.clear_drift_measurement()
        if v._is_h5():
            h=v.h5
            if display_only and h.reclip(cfg):return
            if h.worker is None or h.task_kind!='index':h.cancel()
            h.pending_load=True;h.rolling_status()
            if h.worker is None:h.pending_load=False;h.load()
        else:
            self.style();v._render_wf_window()
        self.pulse()

    def pulse(self):
        if self.closed or self.suspended:return
        identity=self.identity()
        if identity!=self.source_identity:
            self.source_identity=identity;self.invalidate()
        v=self.v
        if v._is_h5():return
        self.style()
        if not self.config.enabled:
            self.panel.status.setText('Rolling off — original data');return
        rows=tuple(v._display_rows) if v.pause_button.isChecked() else tuple(v.model.rows)
        groups=tuple(v._display_deep_rows) if v.pause_button.isChecked() else tuple(v.deep_history)
        signature=(self.version,tuple(row_key(r) if r is not None else None for r in rows),tuple(group_key(g) for g in groups))
        if signature==self.signature or self.worker is not None:return
        self.signature=signature;token=self.version;source=self.source_identity
        self.panel.status.setText('Updating rolling background… incomplete results are gaps')
        self.worker=worker=RollingWorker(rows,groups,self.config,self.cache,self.deep,self)
        worker.progress.connect(lambda done,total:self.set_progress(done,total) if token==self.version and source==self.identity() else None)
        self.set_progress(0,0)
        worker.ready.connect(lambda result:self.accept(token,source,result))
        worker.failed.connect(lambda error:self.fail(token,source,error))
        worker.finished.connect(self.finished);worker.start()

    def accept(self,token,source,result):
        if self.closed or token!=self.version or source!=self.identity():return
        self.cache,self.deep=result;self.error=''
        self.v._wf_image_signature=None
        self.v._render_wf_window();self.style()
        missing=sum(group_key(g) not in self.deep for g in self.v._display_deep_rows)
        edge=(f'Centered live edges use truncated support; latest {self.config.window_s/2:g} s provisional.'
              if self.config.alignment=='Centered' else 'Trailing: past/current samples only; no future look-ahead.')
        self.panel.status.setText(f'Rolling {self.config.window_s:g} s · {len(self.cache)} retained input rows\n{edge}\n'
            f'Bins with fewer than {self.config.min_samples} finite records are gaps.'
            +(f'\n{missing} Deep groups lack retained input rows/results.' if missing else ''))

    def fail(self,token,source,error):
        if not self.closed and token==self.version and source==self.identity():
            self.error=error;self.panel.status.setText('Rolling failed: '+error)

    def finished(self):
        worker=self.worker;self.worker=None
        if worker:worker.deleteLater()
        if not self.v._is_h5():self.panel.cancel.setEnabled(False)
        if not self.closed:self.pulse()

    def fast_values(self,row):
        if not self.config.enabled:return row[4].get(S12,np.nan)
        item=self.cache.get(row_key(row));values=item[1] if item else np.full(len(self.v.freq),np.nan)
        return np.maximum(values,0) if self.config.clip else values

    def deep_values(self,group):
        if not self.config.enabled:return group[6]
        values=self.deep.get(group_key(group),np.full(len(self.v.freq),np.nan))
        return np.maximum(values,0) if self.config.clip else values

    def style(self,config=None):
        cfg=config or self.config;v=self.v;source='h5' if v._is_h5() else 'live'
        key=(source,cfg.enabled,cfg.clip if cfg.enabled else False)
        bars=(v.cbar_fast,v.cbar_deep)
        if key!=self.level_key:
            if self.level_key is not None and self.level_key[0]==source:self.remember_levels()
            default=((0,12),(0,12)) if cfg.clip else ((-12,12),(-12,12))
            if not cfg.enabled:default=((18,40),(18,40)) if source=='h5' else tuple(tuple(b.values) for b in bars)
            levels=self.levels.get(key,default)
            for bar,value in zip(bars,levels):
                bar.blockSignals(True);bar.setLevels(value);bar.blockSignals(False)
            self.level_key=key;self.levels[key]=levels
        caption=(source,cfg)
        if source=='live' and getattr(self,'caption_key',None)==caption:return
        self.caption_key=caption
        if cfg.enabled:
            for bar in bars:bar.getAxis('left').setLabel('Relative to rolling median [dB]')
            quantity=v.h5.data['quantity'] if v._is_h5() and v.h5.data else '|S12|'
            if quantity=='S12':quantity='|S12| per input record'
            for name,plot in (('Fast',v.wf_fast_plot),('Deep',v.wf_deep_plot)):
                plot.setTitle(f'{name} {quantity} — rolling {cfg.window_s:g} s {cfg.alignment.lower()} · dB relative to median'
                              + (' · display clipped below 0' if cfg.clip else ' · signed')
                              + (' · FFT-weighted ratio mean' if name=='Deep' else ''))
        elif not v._is_h5():
            for plot,title in zip((v.wf_fast_plot,v.wf_deep_plot),self.original_titles):plot.setTitle(title)
            for bar,label in zip(bars,self.original_labels):bar.getAxis('left').setLabel(label)

    def hover(self,pos):
        v=self.v
        if not self.config.enabled:return False
        for key in ('wf_fast','wf_deep'):
            plot=v.freq_plots[key]
            if not plot.isVisible() or not plot.vb.sceneBoundingRect().contains(pos):continue
            point=plot.vb.mapSceneToView(pos);b=int(np.argmin(abs(v.freq-point.x())))
            row=v._wf_row_at(point.y()) if key=='wf_fast' else v._deep_group_at(point.y())
            values=(self.fast_values(row) if key=='wf_fast' else self.deep_values(row)) if row is not None else None
            val=values[b] if values is not None else np.nan
            for k,p in v.freq_plots.items():
                v.crosshair_v[k].setPos(float(v.freq[b]));v.crosshair_v[k].setVisible(p.isVisible())
                v.crosshair_h[k].setPos(float(point.y()));v.crosshair_h[k].setVisible(k==key)
            for k in v.time_plots:
                v.crosshair_v[k].hide();v.crosshair_h[k].hide()
            text=f'{v.freq[b]:.6f} MHz | '+(f'{val:+.3f} dB relative to rolling median' if np.isfinite(val) else 'No valid data / rolling pending')
            v.readout_hover.setText(text);v._place_tooltip(key,plot,float(v.freq[b]),float(point.y()),text)
            return True
        return False

    def close(self):
        self.closed=True;self.timer.stop();self.save();self.version+=1
        if self.worker:self.worker.requestInterruption()
        return self.worker is None or not self.worker.isRunning()
