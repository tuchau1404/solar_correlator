import datetime,io,math
from zoneinfo import ZoneInfo
import numpy as np
import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt,dates as mdates
from .reader import ARRAYS,snapshot,text
from .science import derived,db
class BoundedBuffer(io.BytesIO):
    def __init__(self,cap):super().__init__();self.cap=cap
    def write(self,b):
        if self.tell()+len(b)>self.cap:raise ValueError('PNG byte cap')
        return super().write(b)
def render(f):
    c=snapshot(f);q=c['quicklook'];n=len(f['data/timestamps_us']);freq=f['frequency_hz'][:];bins=len(freq)
    maxrows=max(1,q['max_pixels']//bins);indices=np.unique(np.linspace(0,n-1,min(n,maxrows)).astype(int))
    v=[f['data/'+k][indices,:].astype(np.float64) for k in ARRAYS]
    mag,phase,gamma,gamma2,violations=derived(*v)
    coh=gamma if q['coherence_estimator']=='magnitude' else gamma2
    phase=np.where(coh>=q['coherence_threshold'],phase,np.nan)
    # Full-file weighted spectra streamed by native HDF5 chunk size.
    sums=[np.zeros(bins) for _ in range(3)];weights=np.zeros(bins)
    chunk=f['data/s11_linear'].chunks[0]
    for start in range(0,n,chunk):
        block=[f['data/'+k][start:start+chunk,:].astype(np.float64) for k in ARRAYS]
        w=f['data/valid_fft_frames'][start:start+chunk][:,None].astype(float)
        valid=np.logical_and.reduce([np.isfinite(x) for x in block]);ww=np.where(valid,w,0);weights+=ww.sum(axis=0)
        for dest,x in zip(sums,[block[0],block[1],np.hypot(block[2],block[3])]):dest+=np.where(valid,x*ww,0).sum(axis=0)
    with np.errstate(divide='ignore',invalid='ignore'):means=[np.where(weights>0,x/weights,np.nan) for x in sums]
    starts=f['data/start_utc_us'][indices]/1e6;ends=f['data/end_utc_us'][indices]/1e6
    stamps=f['data/timestamps_us'][indices]/1e6
    # Use actual interval edges; explicit NaN rows expose missing time. Shared
    # Welch support at consecutive boundaries is shown only once on the plot.
    edges=[];rows=[];phase_rows=[]
    second=phase if q['second_panel']=='phase' else coh
    for i in range(len(indices)):
        if edges and starts[i]>edges[-1]:rows.append(np.full(bins,np.nan));phase_rows.append(np.full(bins,np.nan));edges.append(starts[i])
        if not edges:edges.append(starts[i])
        end=min(ends[i],starts[i+1]) if i+1<len(indices) else ends[i]
        if end<=edges[-1]:raise ValueError('nonmonotonic observation time')
        rows.append(db(mag[i]));phase_rows.append(second[i]);edges.append(end)
    tz=ZoneInfo(c['time']['display_timezone'])
    times=mdates.date2num([datetime.datetime.fromtimestamp(x,datetime.timezone.utc) for x in stamps])
    time_edges=mdates.date2num([datetime.datetime.fromtimestamp(x,datetime.timezone.utc) for x in edges])
    bandwidth=float(f['metadata'].attrs['frequency_bin_bandwidth_hz']);fe=np.r_[freq-bandwidth/2,freq[-1]+bandwidth/2]/1e6
    plot=np.asarray(rows);valid=plot[np.isfinite(plot)]
    if not valid.size:raise ValueError('no valid power for quicklook')
    vmin,vmax=np.percentile(valid,q['percentiles']) if q['color_mode']=='percentile' else (q['vmin_db'],q['vmax_db'])
    if vmax<=vmin:vmax=vmin+1
    fig,axes=plt.subplots(2,2,figsize=q['figure_inches'],layout='constrained');ax=axes.ravel()
    m=ax[0].pcolormesh(fe,time_edges,plot,cmap=q['colormap'],vmin=vmin,vmax=vmax,shading='flat',rasterized=True)
    fig.colorbar(m,ax=ax[0],label='10 log10 |S12| / 1 ADC-count²')
    m=ax[1].pcolormesh(fe,time_edges,np.asarray(phase_rows),cmap=q['phase_colormap'] if q['second_panel']=='phase' else q['colormap'],vmin=-np.pi if q['second_panel']=='phase' else 0,vmax=np.pi if q['second_panel']=='phase' else 1,shading='flat',rasterized=True)
    fig.colorbar(m,ax=ax[1],label='Phase [rad], coherence masked' if q['second_panel']=='phase' else 'Coherence '+q['coherence_estimator'])
    for a in ax[:2]:a.set_xlabel('RF frequency [MHz]');a.yaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S',tz=tz));a.set_ylabel(c['time']['display_timezone'])
    for name,y in zip(('S11','S22','mean(|S12| saved visibility)'),means):ax[2].plot(freq/1e6,db(y),label=name)
    ax[2].set(xlabel='RF frequency [MHz]',ylabel='Power [dB re 1 ADC-count²]',title='FFT-weighted time mean');ax[2].legend(fontsize=8)
    if q['selection_mode']=='frequency':
        if not fe[0]*1e6<=q['frequency_hz']<=fe[-1]*1e6:raise ValueError('quicklook frequency outside band')
        mask=np.zeros(bins,dtype=bool);mask[np.argmin(abs(freq-q['frequency_hz']))]=True
    else:mask=(freq>=q['roi_hz'][0])&(freq<=q['roi_hz'][1])
    if not mask.any():raise ValueError('empty quicklook ROI')
    # A strict common ROI mask: a missing constituent invalidates that point.
    roi=[np.mean(x[:,mask],axis=1) for x in v]
    cut=np.r_[False,(indices[1:]!=indices[:-1]+1)|(starts[1:]>ends[:-1])]
    tx=[];ys=[[],[],[]]
    band=np.mean(v[0],axis=1)
    for i,t in enumerate(times):
        if cut[i]:tx.append(np.nan);[y.append(np.nan) for y in ys]
        tx.append(t)
        for y,value in zip(ys,(roi[2][i],roi[3][i],band[i])):y.append(value)
    ax[3].plot(tx,ys[0],label='Re(S12)');ax[3].plot(tx,ys[1],label='Im(S12)')
    power_ax=ax[3].twinx();power_ax.plot(tx,ys[2],color='gray',alpha=.4,label='Mean S11 across saved bins');power_ax.set_ylabel('Mean S11 [ADC-count² / input bin]')
    ax[3].set_ylabel('Visibility [ADC-count² / input bin]');ax[3].set_title(f'Fixed bin/ROI: {freq[mask][0]/1e6:.6f}–{freq[mask][-1]/1e6:.6f} MHz');ax[3].legend(fontsize=8);ax[3].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S',tz=tz))
    meta=f['metadata'].attrs
    fig.suptitle(f"Solar visibility — {text(meta['observation_day'])}\nschema 1 | calibration {text(meta['calibration_id'])} | estimated UTC | {text(meta['artifact_id'])}",fontsize=10)
    dpi=min(q['dpi'],math.sqrt(q['max_pixels']/(q['figure_inches'][0]*q['figure_inches'][1])))
    try:
        while dpi>=q['min_dpi']:
            buffer=BoundedBuffer(q['max_png_bytes'])
            try:fig.savefig(buffer,format='png',dpi=dpi,metadata={'Provenance':text(meta['config_hash'])+'; '+text(meta['grouping_policy'])});return buffer.getvalue(),{'vmin_db':float(vmin),'vmax_db':float(vmax),'render_rows':len(indices),'source_rows':n,'coherence_violations':int(violations.sum())}
            except ValueError as e:
                if str(e)!='PNG byte cap':raise
                dpi*=.8
        raise ValueError('PNG cannot fit configured byte/pixel caps')
    finally:plt.close(fig)
