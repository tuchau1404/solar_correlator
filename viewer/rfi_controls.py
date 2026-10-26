"""RFI widgets, bounded background live mask cache, and plotting adapters."""
from dataclasses import replace
import json
import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets
from rfi_mask import RfiConfig, make_mask, weighted_mean, VersionGate, frequency_edges
from telemetry_protocol import S11,S22,S12,PHASE,COHERENCE
from science_model import PERIOD


def row_key(row): return tuple(row[:4])
def group_key(group): return tuple(group[:6])


class LiveMaskWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, rows, groups, frequency, config, previous, parent, selection=None):
        super().__init__(parent)
        self.rows,self.groups,self.frequency,self.config,self.previous = rows,groups,frequency,config,previous
        self.selection=selection

    def run(self):
        try:
            masks,deep = {},{}
            old_masks,old_deep = self.previous
            by_frame = {}
            widths=np.diff(frequency_edges(self.frequency))
            if self.selection is None:roi=np.ones(len(widths),bool);selected=None
            else:roi,selected=self.selection
            summary=dict(total=0.,bad=0.,valid=0.,bandwidth=0.,channels=0,unrecomputable_groups=0)
            for row in self.rows:
                if self.isInterruptionRequested(): return
                if row is None: continue
                key=row_key(row); streams=row[4]
                by_frame[(row[0],row[1])] = row
                if key in old_masks: masks[key]=old_masks[key]
                available=np.isfinite(streams[S12])
                if key not in masks:
                    masks[key]=make_mask(self.frequency,self.config,streams.get(S11),streams.get(S22),
                                         domain='db',available=available)
                m=masks[key];good=~m.missing&(~m.bad_mask if self.config.enabled else True)
                summary['bandwidth']=widths[roi&good].sum();summary['channels']=m.detector_channels
                if selected is None or key in selected:
                    w=row[3];summary['total']+=widths[roi].sum()*w
                    summary['bad']+=widths[roi&m.bad_mask].sum()*w
                    summary['valid']+=widths[roi&good].sum()*w
            for g in self.groups:
                if self.isInterruptionRequested(): return
                key=group_key(g)
                if key in old_deep: deep[key]=old_deep[key];continue
                members=[by_frame.get((g[0],i)) for i in range(g[2],g[3]+1)]
                if not members or any(r is None for r in members):
                    summary['unrecomputable_groups']+=1;continue
                db=np.array([r[4][S12] for r in members],float)
                bad=np.array([masks[row_key(r)].bad_mask for r in members])
                weights=np.array([r[3] for r in members],float)[:,None]
                with np.errstate(over='ignore',invalid='ignore'):
                    linear=np.power(10.,db/10.)
                good=np.isfinite(linear)
                if self.config.enabled:good &= (db > -120.) & ~bad
                avg,denom=weighted_mean(linear,weights,good)
                result=np.full(avg.shape,np.nan)
                positive=np.isfinite(avg)&(avg>0)
                result[positive]=10*np.log10(avg[positive])
                coverage=denom/weights.sum()
                result[coverage < self.config.min_valid_fraction]=np.nan
                fraction=(bad*weights).sum(axis=0)/weights.sum()
                deep[key]=(result.astype(np.float32),fraction,coverage)
            if not self.isInterruptionRequested(): self.ready.emit((masks,deep,summary))
        except Exception as exc: self.failed.emit(f'{type(exc).__name__}: {exc}')


class RfiControls(QtWidgets.QGroupBox):
    changed = QtCore.pyqtSignal(object)
    def __init__(self, parent=None):
        super().__init__('RFI mask',parent)
        box=QtWidgets.QVBoxLayout(self)
        self.applied=RfiConfig()
        self.enable=QtWidgets.QCheckBox('Enable RFI mask')
        self.mode=QtWidgets.QComboBox();self.mode.addItems(['Manual','Auto','Combined']);self.mode.setCurrentText('Combined')
        self.overlay=QtWidgets.QCheckBox('Show mask overlay')
        self.overlay.setToolTip('Preview works while Enable is off. Magenta alpha = candidate fraction; amber = partial valid support. Missing cells are not RFI.')
        for w in (self.enable,self.mode,self.overlay):box.addWidget(w)
        self.intervals=QtWidgets.QListWidget();self.intervals.setMaximumHeight(95)
        self.intervals.setStyleSheet('QListWidget { background: #0c1521; color: #cbd9e8; border: 1px solid #2a4057; }')
        box.addWidget(self.intervals)
        self.ranges=[]
        row=QtWidgets.QHBoxLayout()
        for title,slot in [('Add',self.add),('Edit',self.edit),('Delete',self.remove)]:
            b=QtWidgets.QPushButton(title);b.clicked.connect(slot);row.addWidget(b)
        box.addLayout(row)
        self.window=self.spin(box,'Local background window',1,100000,250,' kHz')
        self.sigma=self.spin(box,'Robust threshold (MAD scale)',1,100,6,' σ')
        self.prominence=self.spin(box,'Minimum prominence',.1,100,6,' dB')
        self.width=self.spin(box,'Maximum narrow-line width',.001,100000,50,' kHz')
        self.expand=self.spin(box,'Expansion beyond measured line',0,10000,5,' kHz')
        self.minimum=self.spin(box,'Minimum valid fraction',0,1,.5,'')
        self.apply=QtWidgets.QPushButton('Apply RFI configuration');box.addWidget(self.apply)
        self.apply.clicked.connect(self.commit)
        self.enable.toggled.connect(self.toggle_enabled);self.mode.currentTextChanged.connect(self.commit);self.overlay.toggled.connect(self.commit)
        self.status=QtWidgets.QLabel('RFI off');self.status.setWordWrap(True);box.addWidget(self.status)
        note=QtWidgets.QLabel('Candidates only; no signal recovery. Auto uses S11 OR S22. Broadband bursts can be misclassified. Changing bandwidth can change fringe amplitude and phase.')
        note.setWordWrap(True);box.addWidget(note)

    @staticmethod
    def spin(box,label,lo,hi,value,suffix):
        box.addWidget(QtWidgets.QLabel(label));w=QtWidgets.QDoubleSpinBox()
        w.setDecimals(3);w.setRange(lo,hi);w.setValue(value);w.setSuffix(suffix);box.addWidget(w);return w

    def choose_interval(self, initial=None):
        dialog=QtWidgets.QDialog(self);dialog.setWindowTitle('Manual frequency interval')
        box=QtWidgets.QVBoxLayout(dialog)
        a=self.spin(box,'Low MHz',0,100000,initial[0] if initial else 35.,' MHz')
        b=self.spin(box,'High MHz',0,100000,initial[1] if initial else 35.01,' MHz')
        a.setDecimals(6);b.setDecimals(6)
        if initial:a.setValue(initial[0]);b.setValue(initial[1])
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok|QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);box.addWidget(buttons)
        if dialog.exec_()!=QtWidgets.QDialog.Accepted:return
        if a.value()>b.value():QtWidgets.QMessageBox.warning(self,'RFI','Low must be ≤ high');return
        return a.value(),b.value()

    def update_ranges(self):
        self.intervals.clear();self.intervals.addItems([f'{a:.6f} – {b:.6f} MHz' for a,b in self.ranges])
        self.commit()

    def add(self):
        value=self.choose_interval()
        if value is not None:self.ranges.append(value);self.update_ranges()
    def edit(self):
        i=self.intervals.currentRow()
        if i<0:return
        value=self.choose_interval(self.ranges[i])
        if value is not None:self.ranges[i]=value;self.update_ranges()
    def remove(self):
        i=self.intervals.currentRow()
        if i>=0:self.ranges.pop(i);self.update_ranges()

    def config(self):
        return RfiConfig(self.enable.isChecked(),self.mode.currentText(),self.overlay.isChecked(),tuple(self.ranges),
                         self.window.value()*1000,self.sigma.value(),self.prominence.value(),
                         self.width.value()*1000,self.expand.value()*1000,self.minimum.value())
    def toggle_enabled(self,enabled):
        if enabled:self.commit()
        else:
            self.applied=replace(self.applied,enabled=False)
            self.changed.emit(self.applied)

    def commit(self,*_):
        try:cfg=self.config()
        except ValueError as exc:
            for widget,value in ((self.enable,self.applied.enabled),(self.overlay,self.applied.overlay)):
                widget.blockSignals(True);widget.setChecked(value);widget.blockSignals(False)
            self.mode.blockSignals(True);self.mode.setCurrentText(self.applied.mode);self.mode.blockSignals(False)
            self.status.setText(str(exc));return
        self.applied=cfg
        self.changed.emit(cfg)

    def restore(self,cfg):
        self.applied=cfg
        for w in (self.enable,self.mode,self.overlay):w.blockSignals(True)
        self.enable.setChecked(cfg.enabled);self.mode.setCurrentText(cfg.mode);self.overlay.setChecked(cfg.overlay)
        for w in (self.enable,self.mode,self.overlay):w.blockSignals(False)
        self.ranges=list(cfg.ranges_mhz);self.intervals.clear()
        self.intervals.addItems([f'{a:.6f} – {b:.6f} MHz' for a,b in self.ranges])
        for w,x in ((self.window,cfg.window_hz/1000),(self.sigma,cfg.sigma),(self.prominence,cfg.prominence_db),
                    (self.width,cfg.max_width_hz/1000),(self.expand,cfg.expand_hz/1000),(self.minimum,cfg.min_valid_fraction)):w.setValue(x)


def overlay_rgba(rfi_fraction, valid_fraction):
    bad=np.nan_to_num(np.asarray(rfi_fraction,float),nan=0.)
    valid=np.asarray(valid_fraction,float)
    rgba=np.zeros(bad.shape+(4,),np.uint8)
    partial=(valid>0)&(valid<1-1e-6)
    rgba[partial]=[255,175,0,65]
    hit=bad>0
    rgba[hit,:3]=[255,30,190]
    rgba[hit,3]=np.asarray(np.clip(bad[hit],0,1)*180,np.uint8)
    return rgba


class RfiController(QtCore.QObject):
    def __init__(self,viewer,side):
        super().__init__(viewer);self.v=viewer;self.config=RfiConfig();self.gate=VersionGate()
        self.masks={};self.deep={};self.summary={};self.worker=None;self.signature=None;self.source_identity=None
        self.closed=False;self.error='';self.overlays={}
        self.panel=RfiControls(viewer);side.insertWidget(0,self.panel)
        # Always starts disabled, even if last session was enabled.
        try:
            cfg=RfiConfig.from_dict(json.loads(str(viewer.settings.value('rfi/state_v1','{}'))))
            self.config=replace(cfg,enabled=False)
        except (ValueError,TypeError,KeyError):pass
        self.panel.restore(self.config);self.panel.changed.connect(self.change)
        self.bandwidth_curve=pg.PlotDataItem(pen=pg.mkPen('#f4be50',width=1))
        self.bandwidth_view=pg.ViewBox();self.bandwidth_view.setMouseEnabled(x=False,y=False)
        viewer.graphics.scene().addItem(self.bandwidth_view)
        viewer.fringe_plot.showAxis('right');viewer.fringe_plot.getAxis('right').linkToView(self.bandwidth_view)
        viewer.fringe_plot.getAxis('right').setLabel('Valid bandwidth',units='Hz',color='#f4be50')
        self.bandwidth_view.setXLink(viewer.fringe_plot.vb);self.bandwidth_view.addItem(self.bandwidth_curve)
        viewer.fringe_plot.vb.sigResized.connect(self.resize_bandwidth)
        viewer.fringe_mode.currentIndexChanged.connect(self.refresh_live)
        self.resize_bandwidth()
        self.timer=QtCore.QTimer(self);self.timer.timeout.connect(self.pulse);self.timer.start(150)

    def resize_bandwidth(self):
        plot=self.v.fringe_plot
        self.bandwidth_view.setVisible(plot.scene() is not None and plot.isVisible())
        self.bandwidth_view.setGeometry(self.v.fringe_plot.vb.sceneBoundingRect())
        self.bandwidth_view.linkedViewChanged(self.v.fringe_plot.vb,self.bandwidth_view.XAxis)

    def redraw(self):
        self.repaint_paused=True
        try:
            self.v.sync_displayed_curves();self.v.render()
        finally:self.repaint_paused=False

    def save(self):
        if self.v._persist_settings:
            self.v.settings.setValue('rfi/state_v1',json.dumps(self.config.to_dict(),allow_nan=False));self.v.settings.sync()

    def invalidate(self):
        self.gate.invalidate();self.signature=None;self.masks={};self.deep={};self.summary={};self.error=''
        if self.worker:self.worker.requestInterruption()
        self.clear_overlays();self.bandwidth_curve.setData([],[])

    def change(self,cfg):
        if cfg==self.config:return
        self.config=cfg;self.invalidate();self.save()
        v=self.v
        v.clear_drift_measurement()
        if v._is_h5():
            h=v.h5;h.drift=[]
            # Catalog indexing has no detector configuration; let it finish.
            if h.worker is None or h.task_kind != 'index':h.cancel()
            # Keep the completed display until the new pair is ready. Its own
            # configuration remains attached; prominently label it as previous.
            h.pending_load=True;h.rfi_status()
            if h.worker is None:h.pending_load=False;h.load()
        else:
            v._wf_image_signature=None;self.redraw()
        self.pulse()

    def pulse(self):
        if self.closed:return
        self.resize_bandwidth()
        v=self.v
        identity=('h5',id(v.h5.catalog)) if v._is_h5() else ('live',v.model.session)
        identity_changed=identity!=self.source_identity
        if identity_changed:
            self.source_identity=identity;self.invalidate()
        if v._is_h5():
            if v.h5.data is not None and identity_changed:v.h5.render_traces()
            return
        if not (self.config.enabled or self.config.overlay):
            self.panel.status.setText('RFI off · input data shown');return
        rows=tuple(v._display_rows) if v.pause_button.isChecked() else tuple(v.model.rows)
        # Pause keeps a separate raw snapshot; live acquisition can continue.
        if v.pause_button.isChecked():rows=getattr(self,'paused_raw',tuple(v.model.rows))
        groups=self.paused_groups if v.pause_button.isChecked() else tuple(v.deep_history)
        lo,hi=v.spectrum_plot.vb.viewRange()[0];low,high=v._wf_view_range
        base=v._wf_display_end-len(rows)
        selected=frozenset(row_key(r) for i,r in enumerate(rows,base) if r is not None and low<=i<high)
        roi=(v.freq>=lo)&(v.freq<=hi)
        sig=(self.gate.version,tuple(row_key(r) if r else None for r in rows),tuple(group_key(g) for g in groups),lo,hi,low,high)
        if sig==self.signature or self.worker is not None:return
        self.signature=sig;token=self.gate.version
        self.panel.status.setText('Calculating RFI candidates… unavailable results shown as gaps')
        self.worker=worker=LiveMaskWorker(rows,groups,v.freq*1e6,self.config,(self.masks,self.deep),self,selection=(roi,selected))
        worker.ready.connect(lambda result:self.accept(token,result))
        worker.failed.connect(lambda error:self.fail(token,error))
        worker.finished.connect(self.finished);worker.start()

    def accept(self,token,result):
        if self.closed or not self.gate.accepts(token):return
        current=('h5',id(self.v.h5.catalog)) if self.v._is_h5() else ('live',self.v.model.session)
        if current != self.source_identity:
            self.invalidate();return
        self.masks,self.deep,self.summary=result
        v=self.v;v._wf_image_signature=None
        if not v._is_h5():
            v.sync_displayed_curves()
            if v.pause_button.isChecked():
                self.redraw()
            else:v.render()
    def fail(self,token,error):
        if self.gate.accepts(token):self.error=error;self.panel.status.setText('RFI failed: '+error)
    def finished(self):
        worker=self.worker;self.worker=None
        if worker:worker.deleteLater()
        if not self.closed:self.pulse()

    def values(self,row,kind):
        values=row[4].get(kind)
        if values is None:return np.full(len(self.v.freq),np.nan)
        if not self.config.enabled:return values
        mask=self.masks.get(row_key(row))
        return np.where(mask.bad_mask,np.nan,values) if mask is not None else np.full_like(values,np.nan)

    def streams(self,row):
        if not self.config.enabled:return row[4]
        mask=self.masks.get(row_key(row))
        return {k:np.where(mask.bad_mask,np.nan,a) if mask is not None else np.full_like(a,np.nan)
                for k,a in row[4].items()}
    def trace(self,trace):
        if not self.config.enabled:return tuple(trace)
        out=[];b=self.v.model.bin
        for r in trace:
            if r is None:out.append(None);continue
            mask=self.masks.get(row_key(r))
            out.append(tuple(r[:4])+(np.nan,np.nan) if mask is None or mask.bad_mask[b] else r)
        return tuple(out)
    def deep_values(self,g):
        if not self.config.enabled:return g[6]
        item=self.deep.get(group_key(g))
        return item[0] if item else np.full_like(g[6],np.nan)

    def clear_overlays(self):
        for key,item in self.overlays.items():
            getattr(self.v,key+'_plot').removeItem(item)
        self.overlays={}
    def draw_overlay(self,key,bad,valid,rect):
        if not self.config.overlay:return
        if key not in self.overlays:
            item=pg.ImageItem(axisOrder='row-major');item.setZValue(20);item.setAcceptedMouseButtons(QtCore.Qt.NoButton)
            getattr(self.v,key+'_plot').addItem(item);self.overlays[key]=item
        item=self.overlays[key];item.setImage(overlay_rgba(bad,valid),autoLevels=False);item.setRect(rect)

    def live_overlay(self,subset,groups,rect):
        if not self.config.overlay:self.clear_overlays();return
        n=len(self.v.freq);bad=np.zeros((max(1,len(subset)),n));valid=np.zeros_like(bad)
        deepbad=np.zeros_like(bad);deepvalid=np.zeros_like(bad)
        by_fid={}
        for g in groups:
            entry=self.deep.get(group_key(g))
            if entry:
                for fid in range(g[2],g[3]+1):by_fid[(g[0],fid)]=entry
        for i,row in enumerate(subset):
            if row is None:continue
            m=self.masks.get(row_key(row))
            if m is not None:bad[i]=m.bad_mask;valid[i]=~m.missing&(~m.bad_mask if self.config.enabled else True)
            entry=by_fid.get((row[0],row[1]))
            if entry:deepbad[i]=entry[1];deepvalid[i]=entry[2]
        for key in ('wf_fast','wf_phase'):self.draw_overlay(key,bad,valid,rect)
        self.draw_overlay('wf_deep',deepbad,deepvalid,rect)

    def refresh_live(self,*_):
        v=self.v
        if v._is_h5():return
        trace=v._display_trace;rows=v._display_rows
        raw=tuple(v.model.rows) if not v.pause_button.isChecked() else getattr(self,'paused_raw',())
        bykey={row_key(r):r for r in raw if r}
        widths=np.diff(frequency_edges(v.freq*1e6));lo,hi=v.spectrum_plot.vb.viewRange()[0]
        x=[];band=[];previous=None
        b=v.model.bin
        for r in trace:
            if r is None:
                x.append(np.nan);band.append(np.nan);previous=None;continue
            if previous is not None:
                if (r[0]!=previous[0] or r[1]!=previous[1]+1 or r[2]<=previous[2]
                        or r[2]-previous[2]>PERIOD*1.5e6):
                    x.append(np.nan);band.append(np.nan)
            previous=r
            m=self.masks.get(row_key(r));source=bykey.get(row_key(r))
            x.append(r[2]/1e6)
            # Live scientific fringe uses one selected bin, not the visible band.
            available=(source is not None and all(k in source[4] and np.isfinite(source[4][k][b])
                         for k in (S12,PHASE,COHERENCE)))
            if available:
                phase,gamma=source[4][PHASE][b],source[4][COHERENCE][b]
                available=abs(phase)<=np.pi+1e-6 and 0<gamma<=1
                if v.fringe_mode.currentData()!='normalized':available &= source[4][S12][b]>-120.
            rejected=m is None or m.bad_mask[b]
            band.append(widths[b] if available and (not self.config.enabled or not rejected) else 0.)
        self.bandwidth_curve.setData(x,band,connect='finite')
        self.bandwidth_view.setYRange(0,float(widths[b])*1.05,padding=0)
        total=self.summary.get('total',0.)
        rb=self.summary.get('bad',0.);vv=self.summary.get('valid',0.)
        if total:
            latest=next((r for r in reversed(raw) if r),None)
            m=self.masks.get(row_key(latest)) if latest else None
            bw=self.summary.get('bandwidth',0.)
            self.panel.status.setText(f'Received samples in visible ROI: RFI candidates {rb/total:.1%} · valid {vv/total:.1%}\nLatest usable band {bw/1e6:.4f} MHz; fringe = selected bin only.\n'+('Mask applied' if self.config.enabled else 'Preview only; input data shown')
                + (f'\nHistory limit: {self.summary["unrecomputable_groups"]} deep groups lack original rows.' if self.summary.get('unrecomputable_groups',0) else '')
                + ('\nAuto unavailable: no S11/S22 in latest observation.' if m is not None and m.detector_channels==0 and self.config.mode!='Manual' else '')
                + ('\nAuto width below live bin width; increase width or use Manual.' if np.min(widths)>self.config.max_width_hz and self.config.mode!='Manual' else ''))
        elif self.config.enabled or self.config.overlay:self.panel.status.setText(self.error or 'No valid data / calculating candidates')
        if self.config.enabled:
            latest=next((r for r in reversed(raw) if r),None)
            m=self.masks.get(row_key(latest)) if latest else None
            if m is not None and m.bad_mask[b]:
                for key in ('phase','gamma'):v.metric_values[key].setText('Masked')
                v.readout_pinned.setText(v.readout_pinned.text()+' | Masked')
            elif m is None:
                v.readout_pinned.setText('No valid data · RFI recalculation pending')

    def close(self):
        self.closed=True;self.timer.stop();self.save();self.gate.invalidate()
        if self.worker:self.worker.requestInterruption()
        return self.worker is None or not self.worker.isRunning()

    def hover(self,pos):
        if not self.config.enabled:return False
        v=self.v
        for key,plot in v.freq_plots.items():
            if not plot.isVisible() or not plot.vb.sceneBoundingRect().contains(pos):continue
            pt=plot.vb.mapSceneToView(pos)
            b=int(np.argmin(abs(v.freq-pt.x())))
            if key=='spectrum':row=next((r for r in reversed(v._display_rows) if r),None)
            elif key=='wf_deep':
                group=v._deep_group_at(pt.y())
                entry=self.deep.get(group_key(group)) if group else None
                if entry is not None:
                    bad,valid=entry[1][b],entry[2][b]
                    message=('Masked' if bad>=1 else 'No valid data' if valid==0 else f'Partial support: valid {valid:.1%}, RFI {bad:.1%}')
                    if bad==0 and valid>=1:return False
                    v.readout_hover.setText(f'{v.freq[b]:.6f} MHz | '+message);return True
                row=None
            else:row=v._wf_row_at(pt.y())
            m=self.masks.get(row_key(row)) if row is not None else None
            message='No valid data' if m is None or m.missing[b] else 'Masked' if m.bad_mask[b] else None
            if message:
                v.readout_hover.setText(f'{v.freq[b]:.6f} MHz | '+message);return True
            return False
        for key,plot in v.time_plots.items():
            if not plot.isVisible() or not plot.vb.sceneBoundingRect().contains(pos):continue
            pt=plot.vb.mapSceneToView(pos)
            raw=getattr(self,'paused_trace',()) if v.pause_button.isChecked() else v.model.trace
            samples=[r for r in raw if r is not None]
            if not samples:return False
            row=min(samples,key=lambda r:abs(r[2]/1e6-pt.x()))
            m=self.masks.get(row_key(row));b=v.model.bin
            if m is None or m.missing[b] or m.bad_mask[b]:
                v.readout_hover.setText('Masked' if m is not None and m.bad_mask[b] else 'No valid data')
                return True
        return False
